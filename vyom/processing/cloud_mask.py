"""
cloud_mask — Scene Classification Layer (SCL) based cloud masking for Sentinel-2 L2A.

SCL class codes (ESA standard, public/standards-based — see Sen2Cor documentation):
  0 No data, 1 Saturated/defective, 2 Dark area, 3 Cloud shadow, 4 Vegetation,
  5 Bare soil, 6 Water, 7 Unclassified, 8 Cloud medium probability,
  9 Cloud high probability, 10 Thin cirrus, 11 Snow/ice

For agricultural NDVI/NDWI use, we mask out anything that isn't a "good pixel":
no-data, saturated, cloud shadow, both cloud classes, and cirrus.
"""
import numpy as np

# SCL classes considered unusable for vegetation-index computation
_BAD_SCL_CLASSES = {0, 1, 3, 8, 9, 10}

# SCL classes explicitly considered good ground observations
_GOOD_SCL_CLASSES = {2, 4, 5, 6, 7, 11}


def build_valid_pixel_mask(scl_band: np.ndarray) -> np.ndarray:
    """
    Given an SCL band (2D array of class codes), return a boolean mask where
    True = usable pixel, False = should be excluded from index/stat computation.
    """
    mask = np.isin(scl_band, list(_GOOD_SCL_CLASSES))
    return mask


def cloud_fraction(scl_band: np.ndarray) -> float:
    """Fraction of pixels classified as cloud (medium/high prob) or cirrus — used
    to record cloud_pct alongside zonal stats even after masking."""
    total = scl_band.size
    if total == 0:
        return 0.0
    cloud_pixels = np.isin(scl_band, [8, 9, 10]).sum()
    return float(cloud_pixels) / float(total)
