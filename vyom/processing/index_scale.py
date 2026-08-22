"""
index_scale -- discrete, labelled color bands per index (Low/Med/High tiers,
each split into sub-bands), replacing the continuous default_cmaps used before.

Bands are (upper_bound, tier, hex_color), ordered low -> high, upper_bound
being the upper edge of that band in the index's native units (the last band's
upper_bound is None, meaning "and above"). Values are matched by the *first*
band whose upper_bound the value is <= to.

RVI and VV_VH_RATIO -- RVI's hex codes are exact, taken directly from the
supplied color reference. Every other index's hex values are a close visual
approximation of that same reference (only RVI/VV_VH_RATIO's tiles came with
exact hex codes; the rest specified numeric breakpoints with a color family/
gradient, not exact hex) -- close enough for a consistent-looking map and
legend, but treat the exact shade as approximate if it's ever compared
pixel-for-pixel against the original reference.

Only indices this deployment actually computes (see indices.py/sar_indices.py)
have an entry: NDVI, NDWI, NDMI, NDRE, MSAVI2, SOC_VIS, RVI. EVI, SOC_SWIR, and
RSM are not implemented in this codebase (no formula exists yet), and
VV_VH_RATIO has no supplied scale -- none of those four are in this table, and
they fall back to a plain continuous colormap in tiles.py rather than a
fabricated discrete scale.
"""

INDEX_SCALES: dict[str, list[tuple[float | None, str, str]]] = {
    "NDVI": [
        (0.3, "Low", "#d9b98b"),
        (0.4, "Low", "#c9bc7c"),
        (0.5, "Low", "#b8bf6c"),
        (0.6, "Med", "#93b656"),
        (0.7, "Med", "#6fa646"),
        (0.8, "High", "#4d8f3a"),
        (0.9, "High", "#2f6e28"),
        (None, "High", "#12451a"),
    ],
    "NDRE": [
        (0.25, "Low", "#e8e4a8"),
        (0.375, "Low", "#c7d18a"),
        (0.5, "Med", "#9dbd6c"),
        (0.625, "Med", "#6fa64e"),
        (0.75, "High", "#457f37"),
        (None, "High", "#1f4d1e"),
    ],
    "MSAVI2": [
        (0.3, "Low", "#f3cdb0"),
        (0.4, "Low", "#dfc98b"),
        (0.5, "Low", "#c9c26e"),
        (0.6, "Med", "#a3b957"),
        (0.7, "Med", "#79a648"),
        (0.8, "High", "#4d8a3a"),
        (0.9, "High", "#2c6a28"),
        (None, "High", "#0f3d16"),
    ],
    "SOC_VIS": [
        (1.8, "Low", "#e4ec8e"),
        (2.4, "Low", "#cfd66a"),
        (3.0, "Low", "#c9b95a"),
        (3.6, "Med", "#c99a4a"),
        (4.2, "Med", "#b8793d"),
        (4.8, "High", "#a15530"),
        (5.4, "High", "#7c3823"),
        (None, "High", "#4a1f14"),
    ],
    "NDMI": [
        (-0.2, "Low", "#b5651d"),
        (-0.1, "Low", "#c99a4a"),
        (0.0, "Low", "#f0e9a8"),
        (0.1, "Low", "#b9ecd6"),
        (0.2, "Med", "#7fd6bb"),
        (0.3, "Med", "#4fb8a3"),
        (0.4, "High", "#2f8f8a"),
        (0.5, "High", "#1c6670"),
        (0.6, "High", "#0f4552"),
        (None, "High", "#062733"),
    ],
    "NDWI": [
        (-0.75, "Low", "#4d4d4d"),
        (-0.5, "Low", "#7a7a7a"),
        (-0.25, "Low", "#aaaaaa"),
        (0.0, "Med", "#cfd8dc"),
        (0.25, "Med", "#8ecae6"),
        (0.5, "High", "#4a90b8"),
        (0.75, "High", "#1c5d8a"),
        (None, "High", "#0a2f52"),
    ],
    "RVI": [
        (0.4, "Low", "#d9f5d3"),
        (0.5, "Low", "#b4e7a1"),
        (0.6, "Med", "#8ed681"),
        (0.7, "Med", "#63c466"),
        (0.8, "High", "#42af4c"),
        (0.9, "High", "#2e8b41"),
        (1.0, "High", "#1f6034"),
        (None, "High", "#0c3b1e"),
    ],
}

# Kept separate on purpose -- not wired into a colormap anywhere, since RSM
# isn't a computed index yet (see module docstring). Here only so the exact
# hex codes supplied for it aren't lost if that index gets implemented later.
_RSM_REFERENCE_ONLY = [
    (None, "Low", "#b32d24"),
    (None, "Low", "#c97342"),
    (None, "Med", "#e1b667"),
    (None, "Med", "#f9f591"),
    (None, "High", "#9af2f4"),
    (None, "High", "#5fb1c0"),
    (None, "High", "#31728c"),
    (None, "High", "#123a58"),
]


def _hex_to_rgba(hex_color: str) -> tuple[int, int, int, int]:
    hex_color = hex_color.lstrip("#")
    return (int(hex_color[0:2], 16), int(hex_color[2:4], 16), int(hex_color[4:6], 16), 255)


def rio_tiler_intervals(index_name: str):
    """Build a rio-tiler IntervalColorMapType (list of ((min,max), rgba)) from
    this index's bands, operating directly on native data values -- no need to
    rescale to 0-255 first the way a continuous/discrete-LUT colormap would.
    Returns None if this index has no defined scale (caller should fall back
    to a continuous colormap in that case)."""
    bands = INDEX_SCALES.get(index_name)
    if not bands:
        return None
    intervals = []
    lower = float("-inf")
    for upper, _tier, hex_color in bands:
        upper_bound = upper if upper is not None else float("inf")
        intervals.append(((lower, upper_bound), _hex_to_rgba(hex_color)))
        lower = upper_bound
    return intervals


def band_for_value(index_name: str, value: float | None):
    """Returns (tier, hex_color) for a value, or None if this index has no
    defined scale or the value itself is None."""
    if value is None or index_name not in INDEX_SCALES:
        return None
    for upper_bound, tier, color in INDEX_SCALES[index_name]:
        if upper_bound is None or value <= upper_bound:
            return (tier, color)
    return None


def scales_for_api() -> dict:
    """JSON-friendly shape for the /farms/index-scales endpoint: each band as
    {upper, tier, color}, in low-to-high order, ready for a frontend legend."""
    return {
        index_name: [
            {"upper": upper, "tier": tier, "color": color}
            for upper, tier, color in bands
        ]
        for index_name, bands in INDEX_SCALES.items()
    }
