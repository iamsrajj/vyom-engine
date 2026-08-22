"""
cog_writer — writes a single-band float32 array (NDVI or NDWI) out as a
Cloud-Optimized GeoTIFF: internally tiled 512x512, overviews baked in at
creation time. COG is what lets the tile-serving layer (rio-tiler) do partial,
range-request-based reads instead of pulling the whole file.
"""
import numpy as np
import rasterio
from rasterio.io import MemoryFile
from rio_cogeo.cogeo import cog_translate
from rio_cogeo.profiles import cog_profiles


def write_index_cog(
    array: np.ndarray,
    transform,
    crs,
    out_path: str,
    nodata: float = -9999.0,
) -> str:
    """
    Write `array` (2D float32, NaN for masked/invalid pixels) to `out_path` as a
    COG. Returns out_path.
    """
    filled = np.where(np.isnan(array), nodata, array).astype("float32")
    height, width = filled.shape

    src_profile = {
        "driver": "GTiff",
        "height": height,
        "width": width,
        "count": 1,
        "dtype": "float32",
        "crs": crs,
        "transform": transform,
        "nodata": nodata,
    }

    dst_profile = cog_profiles.get("deflate")
    dst_profile.update(blocksize=512)

    with MemoryFile() as memfile:
        with memfile.open(**src_profile) as mem:
            mem.write(filled, 1)
        cog_translate(
            memfile,
            out_path,
            dst_profile,
            in_memory=False,
            overview_resampling="average",
            web_optimized=False,
            quiet=True,
        )

    return out_path
