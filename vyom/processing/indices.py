"""
indices -- Sentinel-2 spectral index formulas. All are standards-based public
remote-sensing formulas except SOC_VIS and LAI_PROXY, which are explicitly
marked experimental (see their docstrings).

Sentinel-2 L2A band reference used below:
  B02 Blue (10m), B03 Green (10m), B04 Red (10m),
  B05 Red-edge 1 (20m), B06 Red-edge 2 (20m), B07 Red-edge 3 (20m),
  B08 NIR wide (10m), B8A NIR narrow/Red-edge 4 (20m),
  B11 SWIR1 (20m), B12 SWIR2 (20m)

STILL NOT IMPLEMENTED:
  - CCC (Canopy Chlorophyll Content): confirmed to need a model-inversion
    approach (SNAP's PROSAIL/ANN Biophysical Processor) rather than a pixel
    formula -- scoped as its own integration task, not done here.
  - RSM (Sentinel-1 soil moisture): needs a change-detection algorithm
    against a rolling dry/wet reference baseline per pixel -- new
    infrastructure (historical composite storage/maintenance), scoped as
    its own task, not a simple formula addition. See pipeline_s1.py.
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


def compute_evi(nir: np.ndarray, red: np.ndarray, blue: np.ndarray) -> np.ndarray:
    """Enhanced Vegetation Index (Huete et al. 2002):
        2.5 * (NIR-Red) / (NIR + 6*Red - 7.5*Blue + 1)
    B08, B04, B02. Corrects for atmospheric/aerosol scattering and canopy
    background noise that NDVI doesn't -- more sensitive than NDVI in dense
    canopy where NDVI saturates. Range -1..1, healthy vegetation ~0.2-0.8."""
    nir_f = nir.astype("float32")
    red_f = red.astype("float32")
    blue_f = blue.astype("float32")
    denom = nir_f + 6 * red_f - 7.5 * blue_f + 1
    with np.errstate(divide="ignore", invalid="ignore"):
        return np.where(denom != 0, 2.5 * (nir_f - red_f) / denom, np.nan)


def compute_ari1(green: np.ndarray, red_edge: np.ndarray) -> np.ndarray:
    """Anthocyanin Reflectance Index (Gitelson, Merzlyak & Chivkunova 2001):
        (1/Green) - (1/RedEdge)
    B03, B05. Anthocyanins are stress-response pigments (not chlorophyll) --
    this flags physiological stress and senescence, distinct from what the
    chlorophyll-focused indices (NDVI/NDRE/EVI) capture. Theoretically
    unbounded range; most vegetation values fall roughly 0-0.2."""
    g = green.astype("float32")
    re = red_edge.astype("float32")
    with np.errstate(divide="ignore", invalid="ignore"):
        g_term = np.where(g != 0, 1.0 / g, np.nan)
        re_term = np.where(re != 0, 1.0 / re, np.nan)
        return g_term - re_term


def compute_lai_proxy(nir: np.ndarray, red: np.ndarray, blue: np.ndarray) -> np.ndarray:
    """
    EXPERIMENTAL Leaf Area Index proxy, empirically derived from EVI:
        LAI_proxy = 3.618 * EVI - 0.118

    This is NOT a physically retrieved LAI. True LAI retrieval needs either a
    radiative-transfer model inversion (e.g. the PROSAIL-based ANN used by
    SNAP's Biophysical Processor) or a regression trained against local
    ground-truth LAI measurements for the specific crop/region -- neither of
    which this pipeline has. This formula is a generic empirical regression
    from published literature, not calibrated to Indian cropping systems.
    Treat it the same way as SOC_VIS above: directional/relative signal for
    flagging canopy density changes worth ground-truthing, not an absolute
    LAI value (m^2/m^2) to present to a farmer as-is.
    """
    evi = compute_evi(nir, red, blue)
    return 3.618 * evi - 0.118


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


def compute_cari(green: np.ndarray, red: np.ndarray, red_edge: np.ndarray) -> np.ndarray:
    """Chlorophyll Absorption Ratio Index (Kim et al. 1994). Uses a baseline
    line drawn between Green (B03, ~550nm) and Red-edge1 (B05, ~705nm) to
    correct Red reflectance for background/baseline effects before forming
    the RedEdge/Red ratio -- this is the original CARI, distinct from the
    simpler two-band MCARI/TCARI variants.
        a = (RedEdge - Green) / 150
        b = Green - a*550
        CARI = (RedEdge/Red) * sqrt((a*Red + Red + b)^2 / (a^2 + 1))
    B03, B04, B05."""
    g = green.astype("float32")
    r = red.astype("float32")
    re = red_edge.astype("float32")
    a = (re - g) / 150.0
    b = g - a * 550.0
    with np.errstate(divide="ignore", invalid="ignore"):
        ratio = np.where(r != 0, re / r, np.nan)
        numerator = (a * r + r + b) ** 2
        denom = a ** 2 + 1
        return ratio * np.sqrt(numerator / denom)


def compute_ndre_b6(nir_narrow: np.ndarray, red_edge2: np.ndarray) -> np.ndarray:
    """NDRE variant on Red-edge2 (B8A, B06) instead of the default B08/B05
    pair -- probes slightly deeper into canopy structure than the standard
    NDRE. (NIRnarrow-RedEdge2)/(NIRnarrow+RedEdge2). B8A, B06."""
    return _safe_normalized_difference(nir_narrow, red_edge2)


def compute_ndre_b7(nir_narrow: np.ndarray, red_edge3: np.ndarray) -> np.ndarray:
    """NDRE variant on Red-edge3 (B8A, B07) -- dense-canopy discrimination.
    (NIRnarrow-RedEdge3)/(NIRnarrow+RedEdge3). B8A, B07."""
    return _safe_normalized_difference(nir_narrow, red_edge3)


def compute_evi2(nir: np.ndarray, red: np.ndarray) -> np.ndarray:
    """Two-band EVI (Jiang et al. 2008): 2.5*(NIR-Red)/(NIR+2.4*Red+1).
    B08, B04. NDVI alternative that stays responsive in denser canopy and
    bright soils without needing the Blue band EVI relies on (so it's not
    affected by Blue-band atmospheric noise the way full EVI can be)."""
    nir_f = nir.astype("float32")
    red_f = red.astype("float32")
    denom = nir_f + 2.4 * red_f + 1
    with np.errstate(divide="ignore", invalid="ignore"):
        return np.where(denom != 0, 2.5 * (nir_f - red_f) / denom, np.nan)


def compute_nirv(nir: np.ndarray, red: np.ndarray) -> np.ndarray:
    """Near-Infrared Reflectance of Vegetation: NIR * NDVI. B08, B04.
    Used as a productivity/photosynthetic-capacity proxy feature -- distinct
    from NDVI in that it scales the greenness signal by absolute NIR
    brightness, reducing some soil/background confusion."""
    ndvi = compute_ndvi(nir, red)
    return nir.astype("float32") * ndvi


def compute_osavi(nir: np.ndarray, red: np.ndarray) -> np.ndarray:
    """Optimized Soil-Adjusted Vegetation Index: 1.16*(NIR-Red)/(NIR+Red+0.16).
    B08, B04. Soil-adjusted vigor signal for sparse canopy / bright soils,
    with a fixed adjustment constant (no separate soil-line calibration
    needed, unlike SAVI's tunable L factor)."""
    nir_f = nir.astype("float32")
    red_f = red.astype("float32")
    denom = nir_f + red_f + 0.16
    with np.errstate(divide="ignore", invalid="ignore"):
        return np.where(denom != 0, 1.16 * (nir_f - red_f) / denom, np.nan)


def compute_vari(green: np.ndarray, red: np.ndarray, blue: np.ndarray) -> np.ndarray:
    """Visible Atmospherically Resistant Index: (Green-Red)/(Green+Red-Blue).
    B03, B04, B02. Visible-only greenness signal for when NIR is unreliable
    or unavailable. Considerably more sensitive to haze and illumination
    changes than NIR-based indices -- use with caution, prefer NDVI/EVI
    whenever NIR is available and reliable."""
    g = green.astype("float32")
    r = red.astype("float32")
    b = blue.astype("float32")
    denom = g + r - b
    with np.errstate(divide="ignore", invalid="ignore"):
        return np.where(denom != 0, (g - r) / denom, np.nan)


def compute_savi(nir: np.ndarray, red: np.ndarray, L: float = 0.5) -> np.ndarray:
    """Soil-Adjusted Vegetation Index (Huete 1988): (1+L)*(NIR-Red)/(NIR+Red+L).
    B08, B04. L=0.5 (the conventional default) assumes moderate vegetation
    density -- unlike MSAVI2 (already implemented above), this needs that L
    factor picked rather than self-adjusting."""
    nir_f = nir.astype("float32")
    red_f = red.astype("float32")
    denom = nir_f + red_f + L
    with np.errstate(divide="ignore", invalid="ignore"):
        return np.where(denom != 0, (1 + L) * (nir_f - red_f) / denom, np.nan)


def compute_msi(nir: np.ndarray, swir1: np.ndarray) -> np.ndarray:
    """Moisture Stress Index: SWIR1/NIR. B11, B08. Simple ratio moisture
    stress signal -- higher generally means drier. Sensitive to small
    denominators near shadow/water edges; NDMI (already implemented) is
    generally the more stable moisture signal to lead with."""
    nir_f = nir.astype("float32")
    swir_f = swir1.astype("float32")
    with np.errstate(divide="ignore", invalid="ignore"):
        return np.where(nir_f != 0, swir_f / nir_f, np.nan)


def compute_ndbi(swir1: np.ndarray, nir: np.ndarray) -> np.ndarray:
    """Normalized Difference Built-up Index: (SWIR1-NIR)/(SWIR1+NIR).
    B11, B08. Flags built-up/impervious surfaces -- useful for catching
    non-farm structures (sheds, paths, encroachment) inside a mapped farm
    polygon. Also responds to bare soil/dry residue, so pair with NDVI
    context rather than reading in isolation."""
    return _safe_normalized_difference(swir1, nir)


def compute_ibi(swir1: np.ndarray, nir: np.ndarray, red: np.ndarray) -> np.ndarray:
    """Index-based Built-up Index: (NDBI-NDVI)/(NDBI+NDVI). Combines NDBI and
    NDVI to sharpen built-up detection by damping vegetation influence.
    Inherits NDBI's bare-soil confusion, compounded by whatever noise is in
    the NDVI term."""
    ndbi = compute_ndbi(swir1, nir)
    ndvi = compute_ndvi(nir, red)
    denom = ndbi + ndvi
    with np.errstate(divide="ignore", invalid="ignore"):
        return np.where(denom != 0, (ndbi - ndvi) / denom, np.nan)


def compute_bsi(swir1: np.ndarray, red: np.ndarray, nir: np.ndarray, blue: np.ndarray) -> np.ndarray:
    """Bare Soil Index: ((SWIR1+Red)-(NIR+Blue)) / ((SWIR1+Red)+(NIR+Blue)).
    B11, B04, B08, B02. Highlights bright bare soil, separating it from
    vegetation and water -- useful for flagging fallow/uncultivated portions
    of a mapped farm. Cloud shadow can flip the sign and produce false
    "soil" patches -- mask cloud/shadow before trusting this."""
    swir_f = swir1.astype("float32")
    red_f = red.astype("float32")
    nir_f = nir.astype("float32")
    blue_f = blue.astype("float32")
    numerator = (swir_f + red_f) - (nir_f + blue_f)
    denom = (swir_f + red_f) + (nir_f + blue_f)
    with np.errstate(divide="ignore", invalid="ignore"):
        return np.where(denom != 0, numerator / denom, np.nan)


def compute_nbr(nir: np.ndarray, swir2: np.ndarray) -> np.ndarray:
    """Normalized Burn Ratio: (NIR-SWIR2)/(NIR+SWIR2). B08, B12. Burn
    severity/char sensitivity -- relevant here for detecting crop residue
    (stubble) burning, common in parts of India post-harvest. Works best
    comparing a pre- and post-event date in the same season/phenology stage,
    not as a single-date absolute reading."""
    return _safe_normalized_difference(nir, swir2)


def compute_nbr2(swir1: np.ndarray, swir2: np.ndarray) -> np.ndarray:
    """Normalized Burn Ratio 2: (SWIR1-SWIR2)/(SWIR1+SWIR2). B11, B12.
    Burn/recovery context using both SWIR bands -- very sensitive to
    residual cloud contamination and wet soil after rain, more so than NBR."""
    return _safe_normalized_difference(swir1, swir2)


def compute_bai(red: np.ndarray, nir: np.ndarray) -> np.ndarray:
    """Burn Area Index: 1 / ((0.1-Red)^2 + (0.06-NIR)^2). B04, B08. Burn
    scar highlight; noise-sensitive and not validated as stable across
    different processing chains -- treat as a rough flag to investigate
    further, not a quantitative burn severity measure."""
    red_f = red.astype("float32")
    nir_f = nir.astype("float32")
    denom = (0.1 - red_f) ** 2 + (0.06 - nir_f) ** 2
    with np.errstate(divide="ignore", invalid="ignore"):
        return np.where(denom != 0, 1.0 / denom, np.nan)


def compute_mndwi(green: np.ndarray, swir1: np.ndarray) -> np.ndarray:
    """Modified NDWI (Xu 2006): (Green-SWIR1)/(Green+SWIR1). B03, B11.
    Improves on standard NDWI by suppressing built-up-area false positives
    (SWIR responds very differently than NIR does to built-up surfaces).
    Can over-pick wet soil or shadowed dark roofs without a shadow mask."""
    return _safe_normalized_difference(green, swir1)


def compute_awei_sh(blue: np.ndarray, green: np.ndarray, nir: np.ndarray,
                    swir1: np.ndarray, swir2: np.ndarray) -> np.ndarray:
    """Automated Water Extraction Index, shadow-robust variant:
        Blue + 2.5*Green - 1.5*(SWIR1+SWIR2) - 0.25*NIR
    B02, B03, B08, B11, B12. Designed to stay reliable under shadow, where
    plain NDWI/MNDWI can misfire."""
    b, g = blue.astype("float32"), green.astype("float32")
    n, s1, s2 = nir.astype("float32"), swir1.astype(
        "float32"), swir2.astype("float32")
    return b + 2.5 * g - 1.5 * (s1 + s2) - 0.25 * n


def compute_awei_nsh(green: np.ndarray, swir1: np.ndarray,
                     nir: np.ndarray, swir2: np.ndarray) -> np.ndarray:
    """Automated Water Extraction Index, non-shadowed variant:
        4*(Green-SWIR1) - (0.25*NIR + 2.75*SWIR2)
    B03, B11, B08, B12. Misbehaves if SWIR is noisy or poorly cloud-masked."""
    g, s1 = green.astype("float32"), swir1.astype("float32")
    n, s2 = nir.astype("float32"), swir2.astype("float32")
    return 4 * (g - s1) - (0.25 * n + 2.75 * s2)


def compute_wi2015(blue: np.ndarray, green: np.ndarray, red: np.ndarray,
                   nir: np.ndarray, swir1: np.ndarray) -> np.ndarray:
    """WI2015 empirical water index (complex multi-band regression):
        1.7204 + 171*(Blue+Green+Red) - 3*(Blue*Green) - 1.8*(Blue*Red)
        - 48*(Green*Red) - 0.8*(NIR*SWIR1)
    B02, B03, B04, B08, B11. Designed for water separation in complex scenes
    (urban/shadow-heavy). Extremely sensitive to reflectance scaling --
    inputs MUST be 0..1 surface reflectance, never raw digital numbers."""
    b, g, r = blue.astype("float32"), green.astype(
        "float32"), red.astype("float32")
    n, s1 = nir.astype("float32"), swir1.astype("float32")
    return (1.7204 + 171 * (b + g + r) - 3 * (b * g) - 1.8 * (b * r)
            - 48 * (g * r) - 0.8 * (n * s1))


def compute_ndsi(green: np.ndarray, swir1: np.ndarray) -> np.ndarray:
    """Normalized Difference Snow Index: (Green-SWIR1)/(Green+SWIR1). B03,
    B11. Numerically IDENTICAL to MNDWI above -- same bands, same formula.
    Kept as a separate named index only because the interpretation differs
    (snow vs. water); mostly irrelevant for Indian farmland outside
    Himalayan/hill-state agriculture. Cloud and snow are easily confused
    without conservative masking."""
    return compute_mndwi(green, swir1)


def compute_snow_brightness(blue: np.ndarray, green: np.ndarray) -> np.ndarray:
    """Snow brightness cue: (Green+Blue)/2. B03, B02. Simple brightness
    screen supporting snow detection -- bright sand and cloud edges can look
    like snow, use alongside NDSI, not alone."""
    return (blue.astype("float32") + green.astype("float32")) / 2


def compute_green_blue_ratio(green: np.ndarray, blue: np.ndarray) -> np.ndarray:
    """Green/Blue ratio: qualitative turbidity proxy for water bodies in
    clear-sky conditions. B03, B02. Not generally useful over farmland --
    included for completeness (e.g. farm-adjacent ponds/tanks); illumination
    and atmosphere dominate the signal more than turbidity does."""
    b = blue.astype("float32")
    with np.errstate(divide="ignore", invalid="ignore"):
        return np.where(b != 0, green.astype("float32") / b, np.nan)
