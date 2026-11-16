from __future__ import annotations

import math
import time
from pathlib import Path
from typing import Dict, Iterable, List, Mapping, Optional, Tuple

import numpy as np
from osgeo import gdal, ogr, osr

from .raster_manager import RasterManager


gdal.UseExceptions()


RESULT_FIELDS = [
    "id_area",
    "nome_area",
    "data_solicitada",
    "data_imagem",
    "fonte",
    "sensor",
    "id_cena",
    "indice",
    "media",
    "mediana",
    "minimo",
    "maximo",
    "desvio_padrao",
    "variancia",
    "soma",
    "percentil_5",
    "percentil_25",
    "percentil_75",
    "percentil_95",
    "amplitude",
    "pixels_validos",
    "pixels_nodata",
    "pixels_totais_area",
    "percentual_valido",
    "percentual_mascarado",
    "nuvem_cena",
    "area_ha",
    "caminho_raster_saida",
]


class ZonalStatisticsEngine:
    """Calculate real zonal statistics from raster pixels under vector areas."""

    def calculate_for_raster(
        self,
        raster_path: str,
        aoi_path: str,
        metadata: Mapping[str, object],
        output_clip_path: Optional[str] = None,
    ) -> List[Dict[str, object]]:
        dataset = RasterManager.open_dataset(raster_path)
        array, data_invalid = self._read_band(dataset)
        vector_dataset, layer, transform = self._open_vector_for_raster(aoi_path, dataset)
        results: List[Dict[str, object]] = []
        combined_mask: Optional[np.ndarray] = None

        try:
            layer.ResetReading()
            for feature in layer:
                geometry = feature.GetGeometryRef()
                if geometry is None or geometry.IsEmpty():
                    continue
                raster_geometry = geometry.Clone()
                if transform is not None:
                    raster_geometry.Transform(transform)
                mask = self._geometry_mask(dataset, raster_geometry)
                if combined_mask is None:
                    combined_mask = mask.copy()
                else:
                    combined_mask |= mask

                row = self._stats_for_mask(array, data_invalid, mask)
                row.update(self._feature_metadata(feature))
                row.update(self._scene_metadata(metadata))
                row["caminho_raster_saida"] = output_clip_path or raster_path
                results.append(row)
        finally:
            vector_dataset = None

        if output_clip_path:
            if combined_mask is None:
                combined_mask = np.zeros(array.shape, dtype=bool)
            actual_output_path = self.write_masked_clip(dataset, array, data_invalid, combined_mask, output_clip_path)
            for row in results:
                row["caminho_raster_saida"] = actual_output_path

        return results

    def write_masked_clip(
        self,
        dataset,
        array: np.ndarray,
        data_invalid: np.ndarray,
        mask: np.ndarray,
        output_path: str,
        nodata: float = -9999.0,
    ) -> str:
        output_path = _available_raster_path(output_path)
        RasterManager.ensure_parent(output_path)
        width = int(dataset.RasterXSize)
        height = int(dataset.RasterYSize)
        if width <= 0 or height <= 0:
            raise ValueError(
                f"Raster sem dimensoes validas para gravar recorte: {dataset.GetDescription()} ({width}x{height})."
            )
        if array.shape[0] != height or array.shape[1] != width:
            raise ValueError(
                f"Dimensoes inconsistentes no recorte: raster {width}x{height}, matriz {array.shape[1]}x{array.shape[0]}."
            )
        if mask.shape != array.shape:
            raise ValueError(
                f"Dimensoes inconsistentes na mascara: mascara {mask.shape[1]}x{mask.shape[0]}, matriz {array.shape[1]}x{array.shape[0]}."
            )
        out = np.full(array.shape, nodata, dtype=np.float32)
        valid = mask & ~data_invalid
        out[valid] = array[valid]
        driver = gdal.GetDriverByName("GTiff")
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
            raise RuntimeError(f"GDAL nao conseguiu criar o recorte: {output_path}. Detalhe: {exc}") from exc
        if output is None:
            raise RuntimeError(f"GDAL nao conseguiu criar o recorte: {output_path}.")
        output.SetGeoTransform(dataset.GetGeoTransform())
        output.SetProjection(dataset.GetProjection())
        band = output.GetRasterBand(1)
        band.SetNoDataValue(nodata)
        band.WriteArray(out)
        band.FlushCache()
        output.FlushCache()
        output = None
        return output_path

    @staticmethod
    def _read_band(dataset) -> Tuple[np.ndarray, np.ndarray]:
        band = dataset.GetRasterBand(1)
        nodata = band.GetNoDataValue()
        array = band.ReadAsArray().astype(np.float32)
        invalid = ~np.isfinite(array)
        if nodata is not None:
            if isinstance(nodata, float) and math.isnan(nodata):
                invalid |= np.isnan(array)
            else:
                invalid |= np.isclose(array, float(nodata))
        return array, invalid

    @staticmethod
    def _open_vector_for_raster(aoi_path: str, raster_dataset):
        vector_dataset = ogr.Open(aoi_path)
        if vector_dataset is None:
            raise ValueError(f"Nao foi possivel abrir a area: {aoi_path}")
        layer = vector_dataset.GetLayer(0)
        if layer is None:
            raise ValueError(f"Arquivo sem camada vetorial: {aoi_path}")

        raster_srs = osr.SpatialReference()
        raster_wkt = raster_dataset.GetProjection()
        if raster_wkt:
            raster_srs.ImportFromWkt(raster_wkt)
            raster_srs.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
        else:
            raster_srs = None

        vector_srs = layer.GetSpatialRef()
        if vector_srs:
            vector_srs = vector_srs.Clone()
            vector_srs.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)

        transform = None
        if raster_srs is not None and vector_srs is not None and not vector_srs.IsSame(raster_srs):
            transform = osr.CoordinateTransformation(vector_srs, raster_srs)
        return vector_dataset, layer, transform

    @staticmethod
    def _geometry_mask(dataset, geometry) -> np.ndarray:
        mask_dataset = gdal.GetDriverByName("MEM").Create(
            "",
            dataset.RasterXSize,
            dataset.RasterYSize,
            1,
            gdal.GDT_Byte,
        )
        mask_dataset.SetGeoTransform(dataset.GetGeoTransform())
        mask_dataset.SetProjection(dataset.GetProjection())

        memory_driver = ogr.GetDriverByName("Memory")
        vector_dataset = memory_driver.CreateDataSource("")
        raster_srs = osr.SpatialReference()
        raster_wkt = dataset.GetProjection()
        if raster_wkt:
            raster_srs.ImportFromWkt(raster_wkt)
            raster_srs.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
        else:
            raster_srs = None
        layer = vector_dataset.CreateLayer("mask", raster_srs, ogr.wkbUnknown)
        feature = ogr.Feature(layer.GetLayerDefn())
        feature.SetGeometry(geometry)
        layer.CreateFeature(feature)
        gdal.RasterizeLayer(mask_dataset, [1], layer, burn_values=[1], options=["ALL_TOUCHED=TRUE"])
        mask = mask_dataset.GetRasterBand(1).ReadAsArray().astype(bool)
        feature = None
        vector_dataset = None
        mask_dataset = None
        return mask

    @staticmethod
    def _stats_for_mask(array: np.ndarray, data_invalid: np.ndarray, mask: np.ndarray) -> Dict[str, object]:
        total_pixels = int(np.count_nonzero(mask))
        valid_mask = mask & ~data_invalid
        valid_pixels = int(np.count_nonzero(valid_mask))
        nodata_pixels = max(0, total_pixels - valid_pixels)
        values = array[valid_mask]
        if valid_pixels == 0:
            stats = {
                "media": None,
                "mediana": None,
                "minimo": None,
                "maximo": None,
                "desvio_padrao": None,
                "variancia": None,
                "soma": None,
                "percentil_5": None,
                "percentil_25": None,
                "percentil_75": None,
                "percentil_95": None,
                "amplitude": None,
            }
        else:
            minimum = float(np.nanmin(values))
            maximum = float(np.nanmax(values))
            stats = {
                "media": float(np.nanmean(values)),
                "mediana": float(np.nanmedian(values)),
                "minimo": minimum,
                "maximo": maximum,
                "desvio_padrao": float(np.nanstd(values)),
                "variancia": float(np.nanvar(values)),
                "soma": float(np.nansum(values)),
                "percentil_5": float(np.nanpercentile(values, 5)),
                "percentil_25": float(np.nanpercentile(values, 25)),
                "percentil_75": float(np.nanpercentile(values, 75)),
                "percentil_95": float(np.nanpercentile(values, 95)),
                "amplitude": float(maximum - minimum),
            }
        valid_percent = (valid_pixels / total_pixels * 100.0) if total_pixels else 0.0
        masked_percent = 100.0 - valid_percent if total_pixels else 0.0
        stats.update(
            {
                "pixels_validos": valid_pixels,
                "pixels_nodata": nodata_pixels,
                "pixels_totais_area": total_pixels,
                "percentual_valido": float(valid_percent),
                "percentual_mascarado": float(masked_percent),
            }
        )
        return stats

    @staticmethod
    def _feature_metadata(feature) -> Dict[str, object]:
        return {
            "id_area": _field(feature, "GZS_ID", str(feature.GetFID())),
            "nome_area": _field(feature, "GZS_NAME", _field(feature, "GZS_ID", str(feature.GetFID()))),
            "area_ha": _field(feature, "GZS_AREA_HA", None),
        }

    @staticmethod
    def _scene_metadata(metadata: Mapping[str, object]) -> Dict[str, object]:
        return {
            "data_solicitada": metadata.get("data_solicitada", ""),
            "data_imagem": metadata.get("data_imagem", ""),
            "fonte": metadata.get("fonte", "Local"),
            "sensor": metadata.get("sensor", ""),
            "id_cena": metadata.get("id_cena", ""),
            "indice": metadata.get("indice", ""),
            "nuvem_cena": metadata.get("nuvem_cena", ""),
        }


def _field(feature, field_name: str, default=None):
    definition = feature.GetDefnRef()
    index = definition.GetFieldIndex(field_name)
    if index < 0:
        return default
    value = feature.GetField(index)
    return default if value is None else value


def output_clip_name(output_folder: str, index_name: str, image_date: str, area_label: str) -> str:
    safe_area = "".join(ch if ch.isalnum() or ch in ("-", "_") else "_" for ch in (area_label or "area"))
    filename = f"{index_name}_{image_date}_{safe_area}.tif"
    return str(Path(output_folder) / filename)


def _available_raster_path(output_path: str) -> str:
    """Return a free GeoTIFF path, avoiding files that may be open in QGIS."""

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
