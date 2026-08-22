"""
sar_indices -- Sentinel-1 (SAR/radar) derived indices. Unlike Sentinel-2, S1
sees through cloud cover entirely -- this is what covers your farms during
monsoon season when optical imagery is unusable for weeks at a time.

Inputs are calibrated backscatter (sigma naught, linear power) for VV and VH
polarizations. Production note: raw GRD digital numbers require radiometric
calibration + terrain correction before these formulas are meaningful --
see the caveat in processing/pipeline_s1.py.
"""
import numpy as np


def compute_rvi(vv: np.ndarray, vh: np.ndarray) -> np.ndarray:
    """
    Radar Vegetation Index (dual-pol form, Kim & van Zyl / Trudel et al.):
        RVI = 4 * VH / (VV + VH)

    Ranges roughly 0-1. Higher values indicate more volume scattering, which
    correlates with denser vegetation canopy structure -- useful as a
    cloud-independent stand-in for NDVI-style vegetation density during
    monsoon when Sentinel-2 can't get a clear look at the field.
    """
    vv_f = vv.astype("float32")
    vh_f = vh.astype("float32")
    denom = vv_f + vh_f
    with np.errstate(divide="ignore", invalid="ignore"):
        return np.where(denom != 0, 4 * vh_f / denom, np.nan)


def compute_vv_vh_ratio(vv: np.ndarray, vh: np.ndarray) -> np.ndarray:
    """
    Simple VV/VH backscatter ratio (linear power). A coarser, more direct
    signal than RVI: cross-pol (VH) backscatter is more sensitive to canopy
    volume scattering than co-pol (VV), so a falling VV/VH ratio over time on
    the same field is a reasonable early signal of canopy development, and
    a sharp change can flag flooding (open water gives very low backscatter
    in both polarizations) or harvest (sudden structural loss).
    """
    vv_f = vv.astype("float32")
    vh_f = vh.astype("float32")
    with np.errstate(divide="ignore", invalid="ignore"):
        return np.where(vh_f != 0, vv_f / vh_f, np.nan)
