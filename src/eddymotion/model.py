"""A factory class that adapts DIPY's dMRI models."""
import warnings
from joblib import Parallel, delayed

import numpy as np
from dipy.core.gradients import check_multi_b, gradient_table


def _exec_fit(model, data, block=None):
    retval = model.fit(data)
    return retval, block


def _exec_predict(model, gradient, block=None, **kwargs):
    """Propagate model parameters and call predict."""
    return np.squeeze(model.predict(gradient, S0=kwargs.pop("S0", None))), block


class ModelFactory:
    """A factory for instantiating diffusion models."""

    @staticmethod
    def init(gtab, model="DTI", **kwargs):
        """
        Instatiate a diffusion model.

        Parameters
        ----------
        gtab : :obj:`numpy.ndarray`
            An array representing the gradient table in RAS+B format.
        model : :obj:`str`
            Diffusion model.
            Options: ``"DTI"``, ``"DKI"``, ``"S0"``, ``"AverageDW"``

        Return
        ------
        model : :obj:`~dipy.reconst.ReconstModel`
            An model object compliant with DIPY's interface.

        """
        if model.lower() in ("s0", "b0"):
            return TrivialB0Model(gtab=gtab, S0=kwargs.pop("S0"))

        if model.lower() in ("avg", "average", "mean"):
            return AverageDWModel(gtab=gtab, **kwargs)

        # Generate a GradientTable object for DIPY
        gtab = _rasb2dipy(gtab)
        param = {}

        if model.lower().startswith("3dshore"):
            from dipy.reconst.shore import ShoreModel as Model

            param = {
                "radial_order": 6,
                "zeta": 700,
                "lambdaN": 1e-8,
                "lambdaL": 1e-8,
            }

        elif model.lower() in ("sfm", "gp"):
            Model = SparseFascicleModel
            param = {"solver": "ElasticNet"}

            if model.lower() == "gp":
                from sklearn.gaussian_process import GaussianProcessRegressor

                param = {"solver": GaussianProcessRegressor}

            multi_b = check_multi_b(gtab, 2, non_zero=False)
            if multi_b:
                from dipy.reconst.sfm import ExponentialIsotropicModel

                param.update({"isotropic": ExponentialIsotropicModel})

        elif model.lower() in ("dti", "dki"):
            Model = DTIModel if model.lower() == "dti" else DKIModel

        else:
            raise NotImplementedError(f"Unsupported model <{model}>.")

        param.update(kwargs)
        return Model(gtab, **param)


class BaseModel:
    """
    Defines the interface and default methods.

    """

    __slots__ = (
        "_model",
        "_mask",
        "_S0",
        "_models",
        "_datashape",
    )

    def __init__(self):
        self._model = None
        self._mask = None
        self._S0 = None
        self._models = None

    def fit(self, data, n_jobs=None, **kwargs):
        """Fit the model chunk-by-chunk asynchronously"""
        n_jobs = n_jobs or 1

        self._datashape = data.shape

        # Select voxels within mask or just unravel 3D if no mask
        data = (
            data[self._mask, ...]
            if self._mask is not None
            else data.reshape(-1, data.shape[-1])
        )

        # One single CPU - linear execution (full model)
        if n_jobs == 1:
            self._model, _ = _exec_fit(self._model, data)
            return

        # Split data into chunks of group of slices
        data_chunks = np.array_split(data, n_jobs)

        self._models = [None] * n_jobs

        # Parallelize process with joblib
        with Parallel(n_jobs=n_jobs) as executor:
            results = executor(
                delayed(_exec_fit)(self._model, dblock, i)
                for i, dblock in enumerate(data_chunks)
            )
        for submodel, index in results:
            self._models[index] = submodel

        self._model = None  # Preempt further actions on the model

    def predict(self, gradient, **kwargs):
        """Predict asynchronously chunk-by-chunk the diffusion signal."""
        gradient = _rasb2dipy(gradient)

        n_models = len(self._models) if self._model is None and self._models else 1

        if n_models == 1:
            predicted, _ = _exec_predict(self._model, gradient, S0=self._S0, **kwargs)
        else:
            S0 = [None] * n_models
            if self._S0 is not None:
                S0 = np.array_split(self._S0, n_models)

            predicted = [None] * n_models

            # Parallelize process with joblib
            with Parallel(n_jobs=n_models) as executor:
                results = executor(
                    delayed(_exec_predict)(model, gradient, S0=S0[i], block=i, **kwargs)
                    for i, model in enumerate(self._models)
                )
            for subprediction, index in results:
                predicted[index] = subprediction

            predicted = np.hstack(predicted)

        if self._mask is not None:
            retval = np.zeros_like(self._mask, dtype="float32")
            retval[self._mask, ...] = predicted
        else:
            retval = predicted.reshape(self._datashape[:-1])

        return retval


class TrivialB0Model:
    """
    A trivial model that returns a *b=0* map always.

    Implements the interface of :obj:`dipy.reconst.base.ReconstModel`.
    Instead of inheriting from the abstract base, this implementation
    follows type adaptation principles, as it is easier to maintain
    and to read (see https://www.youtube.com/watch?v=3MNVP9-hglc).

    """

    __slots__ = ("_S0",)

    def __init__(self, gtab, S0=None, **kwargs):
        """Implement object initialization."""
        if S0 is None:
            raise ValueError("S0 must be provided")

        self._S0 = S0

    def fit(self, *args, **kwargs):
        """Do nothing."""

    def predict(self, gradient, **kwargs):
        """Return the *b=0* map."""
        return self._S0


class AverageDWModel:
    """A trivial model that returns an average map."""

    __slots__ = ("_data", "_th_low", "_th_high", "_bias", "_stat")

    def __init__(self, gtab, **kwargs):
        r"""
        Implement object initialization.

        Parameters
        ----------
        gtab : :obj:`~numpy.ndarray`
            An :math:`N \times 4` table, where rows (*N*) are diffusion gradients and
            columns are b-vector components and corresponding b-value, respectively.
        th_low : :obj:`~numbers.Number`
            A lower bound for the b-value corresponding to the diffusion weighted images
            that will be averaged.
        th_high : :obj:`~numbers.Number`
            An upper bound for the b-value corresponding to the diffusion weighted images
            that will be averaged.
        bias : :obj:`bool`
            Whether the overall distribution of each diffusion weighted image will be
            standardized and centered around the global 75th percentile.
        stat : :obj:`str`
            Whether the summary statistic to apply is ``"mean"`` or ``"median"``.

        """
        self._th_low = kwargs.get("th_low", 50)
        self._th_high = kwargs.get("th_high", 10000)
        self._bias = kwargs.get("bias", True)
        self._stat = kwargs.get("stat", "median")

    def fit(self, data, **kwargs):
        """Calculate the average."""
        gtab = kwargs.pop("gtab", None)
        # Select the interval of b-values for which DWIs will be averaged
        b_mask = (
            ((gtab[3] >= self._th_low) & (gtab[3] <= self._th_high))
            if gtab is not None
            else np.ones((data.shape[-1],), dtype=bool)
        )
        shells = data[..., b_mask]

        # Regress out global signal differences
        if self._bias:
            centers = np.median(shells, axis=(0, 1, 2))
            reference = np.percentile(centers[centers >= 1.0], 75)
            centers[centers < 1.0] = reference
            drift = reference / centers
            shells = shells * drift

        # Select the summary statistic
        avg_func = np.median if self._stat == "median" else np.mean
        # Calculate the average
        self._data = avg_func(shells, axis=-1)

    def predict(self, gradient, **kwargs):
        """Return the average map."""
        return self._data


class DTIModel(BaseModel):
    """A wrapper of :obj:`dipy.reconst.dti.TensorModel`."""

    def __init__(self, gtab, S0=None, mask=None, **kwargs):
        """Instantiate the wrapped tensor model."""
        from dipy.reconst.dti import TensorModel as DipyTensorModel

        super().__init__()

        self._S0 = None

        if S0 is not None:
            self._S0 = np.clip(S0.astype("float32") / S0.max(), a_min=1e-5, a_max=1.0,)

        self._mask = mask > 0 if mask is not None else None
        if self._mask is None and self._S0 is not None:
            self._mask = self._S0 > np.percentile(self._S0, 35)

        if self._S0 is not None:
            self._S0 = self._S0[self._mask]

        kwargs = {
            k: v
            for k, v in kwargs.items()
            if k
            in (
                "min_signal",
                "return_S0_hat",
                "fit_method",
                "weighting",
                "sigma",
                "jac",
            )
        }

        self._model = DipyTensorModel(gtab, **kwargs)

    def predict(self, gradient, **kwargs):
        """Ensure no unsupported kwargs are passed."""
        return super().predict(gradient)


class DKIModel(BaseModel):
    """A wrapper of :obj:`dipy.reconst.dki.DiffusionKurtosisModel`."""

    def __init__(self, gtab, S0=None, mask=None, **kwargs):
        """Instantiate the wrapped tensor model."""
        from dipy.reconst.dki import DiffusionKurtosisModel

        super().__init__()

        self._S0 = None
        if S0 is not None:
            self._S0 = np.clip(S0.astype("float32") / S0.max(), a_min=1e-5, a_max=1.0,)
        self._mask = mask
        if mask is None and S0 is not None:
            self._mask = self._S0 > np.percentile(self._S0, 35)

        if self._mask is not None:
            self._S0 = self._S0[self._mask.astype(bool)]

        kwargs = {
            k: v
            for k, v in kwargs.items()
            if k
            in (
                "min_signal",
                "return_S0_hat",
                "fit_method",
                "weighting",
                "sigma",
                "jac",
            )
        }

        self._model = DiffusionKurtosisModel(gtab, **kwargs)


class SparseFascicleModel(BaseModel):
    """
    A wrapper of :obj:`dipy.reconst.sfm.SparseFascicleModel`.
    """

    __slots__ = ("_solver", )

    def __init__(self, gtab, S0=None, mask=None, solver=None, **kwargs):
        """Instantiate the wrapped model."""
        from dipy.reconst.sfm import SparseFascicleModel

        super().__init__()

        self._S0 = None
        if S0 is not None:
            self._S0 = np.clip(S0.astype("float32") / S0.max(), a_min=1e-5, a_max=1.0,)

        self._mask = mask
        if mask is None and S0 is not None:
            self._mask = self._S0 > np.percentile(self._S0, 35)

        if self._mask is not None:
            self._S0 = self._S0[self._mask.astype(bool)]

        self._solver = solver
        if solver is None:
            self._solver = "ElasticNet"

        kwargs = {k: v for k, v in kwargs.items() if k in ("solver",)}

        self._model = SparseFascicleModel(gtab, **kwargs)


def _rasb2dipy(gradient):
    gradient = np.asanyarray(gradient)
    if gradient.ndim == 1:
        if gradient.size != 4:
            raise ValueError("Missing gradient information.")
        gradient = gradient[..., np.newaxis]

    if gradient.shape[0] != 4:
        gradient = gradient.T
    elif gradient.shape == (4, 4):
        print("Warning: make sure gradient information is not transposed!")

    with warnings.catch_warnings():
        warnings.filterwarnings("ignore", category=UserWarning)
        retval = gradient_table(gradient[3, :], gradient[:3, :].T)
    return retval


def _model_fit(model, data):
    return model.fit(data)
