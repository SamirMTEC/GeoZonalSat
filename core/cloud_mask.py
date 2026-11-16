from __future__ import annotations

from pathlib import Path
from typing import Iterable, Set

import numpy as np
from osgeo import gdal

from .raster_manager import RasterManager


gdal.UseExceptions()


SENTINEL2_SCL_MASK_CLASSES = {
    3,   # cloud shadow
    8,   # medium probability cloud
    9,   # high probability cloud
    10,  # cirrus
    11,  # snow or ice
}


def sentinel2_valid_mask(scl_array: np.ndarray, mask_water: bool = False, extra_classes: Iterable[int] = ()) -> np.ndarray:
    """Return True where Sentinel-2 SCL pixels should be kept."""

    classes: Set[int] = set(SENTINEL2_SCL_MASK_CLASSES)
    classes.update(int(value) for value in extra_classes)
    if mask_water:
        classes.add(6)
    invalid = np.isin(scl_array, list(classes))
    return ~invalid


def apply_sentinel2_scl_mask(
    raster_path: str,
    scl_path: str,
    output_path: str,
    mask_water: bool = False,
    nodata: float = -9999.0,
) -> str:
    """Apply Sentinel-2 SCL masking to an already calculated index raster."""

    output_path = RasterManager.available_path(output_path)
    reference = RasterManager.open_dataset(raster_path)
    scl_aligned = RasterManager.available_path(str(Path(output_path).with_name(Path(output_path).stem + "_scl_aligned.tif")))
    RasterManager.ensure_parent(scl_aligned)
    bounds = RasterManager.bounds(reference)
    aligned = gdal.Warp(
        scl_aligned,
        scl_path,
        options=gdal.WarpOptions(
            format="GTiff",
            dstSRS=reference.GetProjection(),
            outputBounds=bounds,
            width=reference.RasterXSize,
            height=reference.RasterYSize,
            resampleAlg="near",
            srcNodata=0,
            dstNodata=0,
            creationOptions=["COMPRESS=LZW", "TILED=YES"],
        ),
    )
    if aligned is None:
        raise RuntimeError(f"GDAL nao conseguiu alinhar a mascara SCL: {scl_aligned}.")
    aligned = None
    source_band = reference.GetRasterBand(1)
    source_nodata = source_band.GetNoDataValue()
    values = source_band.ReadAsArray().astype(np.float32)
    invalid = ~np.isfinite(values)
    if source_nodata is not None:
        invalid |= np.isclose(values, float(source_nodata))

    scl_dataset = RasterManager.open_dataset(scl_aligned)
    scl = scl_dataset.GetRasterBand(1).ReadAsArray()
    keep = sentinel2_valid_mask(scl, mask_water=mask_water)

    output_values = np.full(values.shape, nodata, dtype=np.float32)
    valid = keep & ~invalid
    output_values[valid] = values[valid]

    RasterManager.ensure_parent(output_path)
    driver = gdal.GetDriverByName("GTiff")
    width = int(reference.RasterXSize)
    height = int(reference.RasterYSize)
    try:
        output = driver.Create(
            output_path,
            width,
            height,
            1,
            gdal.GDT_Float32,
            options=["COMPRESS=LZW", "TILED=YES", "BIGTIFF=IF_SAFER"],
        )
    except RuntimeError as exc:
        raise RuntimeError(f"GDAL nao conseguiu criar a mascara de nuvem: {output_path}. Detalhe: {exc}") from exc
    if output is None:
        raise RuntimeError(f"GDAL nao conseguiu criar a mascara de nuvem: {output_path}.")
    output.SetGeoTransform(reference.GetGeoTransform())
    output.SetProjection(reference.GetProjection())
    band = output.GetRasterBand(1)
    band.SetNoDataValue(nodata)
    band.WriteArray(output_values)
    band.FlushCache()
    output.FlushCache()
    output = None
    return output_path
