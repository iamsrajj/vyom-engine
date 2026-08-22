"""
indices -- Sentinel-2 spectral index formulas. All are standards-based public
remote-sensing formulas except SOC_VIS, which is explicitly marked experimental
(see its docstring).

Sentinel-2 L2A band reference used below:
  B02 Blue (10m), B03 Green (10m), B04 Red (10m), B05 Red-edge 1 (20m),
  B08 NIR (10m), B11 SWIR1 (20m)
"""
import numpy as np


def _safe_normalized_difference(band_a: np.ndarray, band_b: np.ndarray) -> np.ndarray:
    """(a - b) / (a + b), divide-by-zero pixels become NaN rather than inf/error."""
    a = band_a.astype("float32")
    b = band_b.astype("float32")
    denom = a + b
    with np.errstate(divide="ignore", invalid="ignore"):
        return np.where(denom != 0, (a - b) / denom, np.nan)


def compute_ndvi(nir: np.ndarray, red: np.ndarray) -> np.ndarray:
    """Normalized Difference Vegetation Index: (NIR-Red)/(NIR+Red). B08, B04.
    Standard vegetation vigor/density indicator. -1..1, healthy crop ~0.6-0.9."""
    return _safe_normalized_difference(nir, red)


def compute_ndwi(green: np.ndarray, nir: np.ndarray) -> np.ndarray:
    """Normalized Difference Water Index (McFeeters 1996): (Green-NIR)/(Green+NIR).
    B03, B08. Surface water / waterlogging detection."""
    return _safe_normalized_difference(green, nir)


def compute_ndmi(nir: np.ndarray, swir1: np.ndarray) -> np.ndarray:
    """Normalized Difference Moisture Index: (NIR-SWIR1)/(NIR+SWIR1). B08, B11.
    Vegetation/canopy water content -- distinct from NDWI (surface water) in
    that NDMI reflects moisture held in leaf tissue, useful for irrigation
    stress detection before visible wilting."""
    return _safe_normalized_difference(nir, swir1)


def compute_ndre(nir: np.ndarray, red_edge: np.ndarray) -> np.ndarray:
    """Normalized Difference Red Edge: (NIR-RedEdge)/(NIR+RedEdge). B08, B05.
    More sensitive than NDVI to chlorophyll content in mid-to-late season dense
    canopy, where NDVI saturates (stops changing even as biomass keeps growing).
    Standard for late-stage crop health / nitrogen status monitoring."""
    return _safe_normalized_difference(nir, red_edge)


def compute_msavi2(nir: np.ndarray, red: np.ndarray) -> np.ndarray:
    """Modified Soil-Adjusted Vegetation Index 2 (Qi et al. 1994):
    (2*NIR+1 - sqrt((2*NIR+1)^2 - 8*(NIR-Red))) / 2
    Reduces soil brightness influence on the vegetation signal compared to NDVI,
    without needing a separate soil-line calibration factor (unlike SAVI/MSAVI).
    Most useful early in the season on sparse canopy, where bare soil between
    rows would otherwise skew NDVI."""
    nir_f = nir.astype("float32")
    red_f = red.astype("float32")
    term = (2 * nir_f + 1) ** 2 - 8 * (nir_f - red_f)
    with np.errstate(invalid="ignore"):
        term = np.where(term >= 0, term, np.nan)
        return (2 * nir_f + 1 - np.sqrt(term)) / 2


def compute_soc_vis(blue: np.ndarray, green: np.ndarray, red: np.ndarray) -> np.ndarray:
    """
    EXPERIMENTAL soil organic carbon proxy from visible bands only:
        SOC_VIS = 1 - (Red / (Blue + Green + Red))

    This is NOT a calibrated, lab-validated SOC measurement. Visible reflectance
    alone is a weak proxy for organic carbon -- it is heavily confounded by soil
    moisture, texture, mineralogy, and surface roughness, none of which this
    formula corrects for. Two soils with identical carbon content but different
    moisture will produce different SOC_VIS values.

    Treat this strictly as a relative brightness/darkness indicator (darker soil
    trending toward more organic matter, all else equal) -- useful for flagging
    within-field variability worth ground-truthing, not for absolute carbon
    percentage. For production-grade SOC estimates, this needs to be calibrated
    against local soil sample lab results (regression against measured SOC%)
    before the output is presented to a farmer as a number they'd act on.
    """
    b = blue.astype("float32")
    g = green.astype("float32")
    r = red.astype("float32")
    denom = b + g + r
    with np.errstate(divide="ignore", invalid="ignore"):
        return np.where(denom != 0, 1 - (r / denom), np.nan)
