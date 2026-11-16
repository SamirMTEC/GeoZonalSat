from __future__ import annotations

import math
import tempfile
from pathlib import Path
from typing import Dict, Iterable, List, Mapping, Optional

import numpy as np
from osgeo import gdal

from .raster_manager import RasterManager


gdal.UseExceptions()


INDEX_REQUIRED_BANDS = {
    "NDVI": ("B08", "B04"),
    "GNDVI": ("B08", "B03"),
    "NDRE": ("B08", "B05"),
    "NDMI": ("B8A", "B11"),
    "SAVI": ("B08", "B04"),
    "EVI": ("B08", "B04", "B02"),
}


class IndexCalculator:
    """Calculate spectral indices from local bands using GDAL and NumPy."""

    def __init__(self, temp_folder: Optional[str] = None) -> None:
        self.temp_folder = temp_folder or tempfile.mkdtemp(prefix="geozonalsat_")
        Path(self.temp_folder).mkdir(parents=True, exist_ok=True)

    @staticmethod
    def available_indices_for_bands(bands: Mapping[str, str]) -> List[str]:
        names = {name.upper() for name in bands}
        available: List[str] = []
        for index_name, required in INDEX_REQUIRED_BANDS.items():
            if all(band in names for band in required):
                available.append(index_name)
        return available

    def calculate(
        self,
        index_name: str,
        bands: Mapping[str, str],
        output_path: str,
        resampling: str = "bilinear",
        savi_l: float = 0.5,
        ndre_use_b8a: bool = False,
    ) -> str:
        index_name = index_name.upper()
        normalized_bands = {key.upper(): value for key, value in bands.items()}
        required = list(INDEX_REQUIRED_BANDS.get(index_name, ()))
        if index_name == "NDRE" and ndre_use_b8a:
            required = ["B8A", "B05"]
        missing = [band for band in required if band not in normalized_bands]
        if missing:
            raise ValueError(f"Indice {index_name} exige bandas: {', '.join(missing)}")

        reference_path = normalized_bands[required[0]]
        aligned_paths = self._align_to_reference(reference_path, {band: normalized_bands[band] for band in required}, resampling)
        arrays, nodata_mask = self._read_arrays(aligned_paths)
        scaled = self._scale_reflectance(arrays)

        with np.errstate(divide="ignore", invalid="ignore"):
            if index_name == "NDVI":
                result = self._ratio(scaled["B08"], scaled["B04"])
            elif index_name == "GNDVI":
                result = self._ratio(scaled["B08"], scaled["B03"])
            elif index_name == "NDRE":
                nir_band = "B8A" if ndre_use_b8a else "B08"
                result = self._ratio(scaled[nir_band], scaled["B05"])
            elif index_name == "NDMI":
                result = self._ratio(scaled["B8A"], scaled["B11"])
            elif index_name == "SAVI":
                nir = scaled["B08"]
                red = scaled["B04"]
                denominator = nir + red + float(savi_l)
                result = ((nir - red) / denominator) * (1.0 + float(savi_l))
            elif index_name == "EVI":
                nir = scaled["B08"]
                red = scaled["B04"]
                blue = scaled["B02"]
                result = 2.5 * ((nir - red) / (nir + 6.0 * red - 7.5 * blue + 1.0))
            else:
                raise ValueError(f"Indice nao suportado: {index_name}")

        result = result.astype(np.float32)
        invalid = nodata_mask | ~np.isfinite(result)
        result[invalid] = -9999.0
        if index_name in {"NDVI", "GNDVI", "NDRE", "NDMI"}:
            result[(result < -1.0) & ~invalid] = -1.0
            result[(result > 1.0) & ~invalid] = 1.0
        return self._write_like(reference_path, output_path, result, -9999.0)

    @staticmethod
    def _ratio(a: np.ndarray, b: np.ndarray) -> np.ndarray:
        denominator = a + b
        result = np.full(a.shape, np.nan, dtype=np.float32)
        valid = np.abs(denominator) > 1e-12
        result[valid] = (a[valid] - b[valid]) / denominator[valid]
        return result

    def _align_to_reference(self, reference_path: str, paths: Mapping[str, str], resampling: str) -> Dict[str, str]:
        aligned: Dict[str, str] = {}
        reference = RasterManager.open_dataset(reference_path)
        bounds = RasterManager.bounds(reference)
        width = reference.RasterXSize
        height = reference.RasterYSize
        projection = reference.GetProjection()
        for band, path in paths.items():
            if RasterManager.same_grid(reference_path, path):
                aligned[band] = path
                continue
            output = RasterManager.available_path(str(Path(self.temp_folder) / f"aligned_{band}_{Path(path).stem}.tif"))
            source = RasterManager.open_dataset(path)
            src_nodata = source.GetRasterBand(1).GetNoDataValue()
            options = gdal.WarpOptions(
                format="GTiff",
                dstSRS=projection,
                outputBounds=bounds,
                width=width,
                height=height,
                resampleAlg=self._resampling_name(resampling),
                srcNodata=src_nodata,
                dstNodata=-9999.0,
                multithread=True,
                creationOptions=["COMPRESS=LZW", "TILED=YES"],
            )
            aligned_dataset = gdal.Warp(output, source, options=options)
            if aligned_dataset is None:
                raise RuntimeError(f"GDAL nao conseguiu alinhar a banda {band}: {output}.")
            aligned_dataset = None
            aligned[band] = output
        return aligned

    @staticmethod
    def _resampling_name(value: str) -> str:
        value = (value or "bilinear").lower()
        if value in {"nearest", "near"}:
            return "near"
        if value in {"cubic", "cubicspline"}:
            return "cubic"
        return "bilinear"

    @staticmethod
    def _read_arrays(paths: Mapping[str, str]):
        arrays: Dict[str, np.ndarray] = {}
        nodata_mask: Optional[np.ndarray] = None
        for band_name, path in paths.items():
            dataset = RasterManager.open_dataset(path)
            band = dataset.GetRasterBand(1)
            nodata = band.GetNoDataValue()
            array = band.ReadAsArray().astype(np.float32)
            mask = ~np.isfinite(array)
            if nodata is not None and not (isinstance(nodata, float) and math.isnan(nodata)):
                mask |= np.isclose(array, float(nodata))
            array[mask] = np.nan
            scale = band.GetScale()
            offset = band.GetOffset()
            if scale not in (None, 0, 1) or offset not in (None, 0):
                array = array * float(scale if scale not in (None, 0) else 1.0) + float(offset or 0.0)
            nodata_mask = mask if nodata_mask is None else (nodata_mask | mask)
            arrays[band_name] = array
        return arrays, nodata_mask if nodata_mask is not None else np.zeros((0, 0), dtype=bool)

    @staticmethod
    def _scale_reflectance(arrays: Mapping[str, np.ndarray]) -> Dict[str, np.ndarray]:
        scaled: Dict[str, np.ndarray] = {}
        for band, array in arrays.items():
            finite = array[np.isfinite(array)]
            if finite.size and np.nanpercentile(np.abs(finite), 95) > 2.0:
                scaled[band] = array / 10000.0
            else:
                scaled[band] = array
        return scaled

    @staticmethod
    def _write_like(reference_path: str, output_path: str, array: np.ndarray, nodata: float) -> str:
        output_path = RasterManager.available_path(output_path)
        RasterManager.ensure_parent(output_path)
        reference = RasterManager.open_dataset(reference_path)
        driver = gdal.GetDriverByName("GTiff")
        width = int(reference.RasterXSize)
        height = int(reference.RasterYSize)
        try:
            dataset = driver.Create(
                output_path,
                width,
                height,
                1,
                gdal.GDT_Float32,
                options=["COMPRESS=LZW", "TILED=YES", "BIGTIFF=IF_SAFER"],
            )
        except RuntimeError as exc:
            raise RuntimeError(f"GDAL nao conseguiu criar o indice calculado: {output_path}. Detalhe: {exc}") from exc
        if dataset is None:
            raise RuntimeError(f"GDAL nao conseguiu criar o indice calculado: {output_path}.")
        dataset.SetGeoTransform(reference.GetGeoTransform())
        dataset.SetProjection(reference.GetProjection())
        band = dataset.GetRasterBand(1)
        band.SetNoDataValue(nodata)
        band.WriteArray(array)
        band.FlushCache()
        dataset.FlushCache()
        dataset = None
        return output_path
