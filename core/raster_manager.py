from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Tuple

from osgeo import gdal


gdal.UseExceptions()


@dataclass(frozen=True)
class RasterInfo:
    path: str
    width: int
    height: int
    projection: str
    geotransform: Tuple[float, float, float, float, float, float]
    nodata: Optional[float]
    pixel_width: float
    pixel_height: float


class RasterManager:
    """GDAL helpers shared by index calculation and zonal statistics."""

    @staticmethod
    def open_dataset(path: str):
        dataset = gdal.Open(path)
        if dataset is None:
            raise ValueError(f"Nao foi possivel abrir o raster: {path}")
        return dataset

    @staticmethod
    def info(path: str) -> RasterInfo:
        dataset = RasterManager.open_dataset(path)
        band = dataset.GetRasterBand(1)
        geotransform = dataset.GetGeoTransform()
        return RasterInfo(
            path=path,
            width=dataset.RasterXSize,
            height=dataset.RasterYSize,
            projection=dataset.GetProjection(),
            geotransform=geotransform,
            nodata=band.GetNoDataValue(),
            pixel_width=abs(float(geotransform[1])),
            pixel_height=abs(float(geotransform[5])),
        )

    @staticmethod
    def bounds(dataset) -> Tuple[float, float, float, float]:
        gt = dataset.GetGeoTransform()
        width = dataset.RasterXSize
        height = dataset.RasterYSize
        xs = [gt[0], gt[0] + gt[1] * width, gt[0] + gt[2] * height, gt[0] + gt[1] * width + gt[2] * height]
        ys = [gt[3], gt[3] + gt[4] * width, gt[3] + gt[5] * height, gt[3] + gt[4] * width + gt[5] * height]
        return min(xs), min(ys), max(xs), max(ys)

    @staticmethod
    def ensure_parent(path: str) -> None:
        Path(path).parent.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def available_path(output_path: str) -> str:
        path = Path(output_path)
        if not path.exists():
            return str(path)
        stem = path.stem
        suffix = path.suffix or ".tif"
        for index in range(2, 1000):
            candidate = path.with_name(f"{stem}_v{index}{suffix}")
            if not candidate.exists():
                return str(candidate)
        return str(path.with_name(f"{stem}_{int(time.time())}{suffix}"))

    @staticmethod
    def same_grid(path_a: str, path_b: str) -> bool:
        a = RasterManager.info(path_a)
        b = RasterManager.info(path_b)
        return (
            a.width == b.width
            and a.height == b.height
            and a.projection == b.projection
            and all(abs(x - y) <= 1e-9 for x, y in zip(a.geotransform, b.geotransform))
        )
