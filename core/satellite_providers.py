from __future__ import annotations

import json
import socket
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Dict, List, Mapping, Optional, Sequence, Tuple

from osgeo import gdal, ogr, osr

from .date_manager import DateManager
from .raster_manager import RasterManager


gdal.UseExceptions()


@dataclass
class Scene:
    scene_id: str
    date: str
    requested_date: str
    source: str
    sensor: str
    cloud_percent: float
    available_bands: Sequence[str]
    assets: Mapping[str, str] = field(default_factory=dict)
    asset_metadata: Mapping[str, Mapping[str, object]] = field(default_factory=dict)
    properties: Mapping[str, object] = field(default_factory=dict)
    status: str = "disponivel"


class SatelliteProvider:
    """Base class for online providers.

    Implementations query real catalogs and do not fabricate scenes.
    """

    name = "Base"

    def test_connection(self) -> bool:
        raise NotImplementedError

    def search_images(
        self,
        area_path: str,
        dates: Sequence[date],
        cloud_limit: float,
        tolerance_days: int = 5,
        limit_per_date: int = 10,
    ) -> List[Scene]:
        raise NotImplementedError

    def download_bands(
        self,
        scene: Scene,
        bands: Sequence[str],
        output_folder: str,
        area_path: Optional[str] = None,
        context_margin_percent: int = 80,
    ) -> Mapping[str, str]:
        raise NotImplementedError

    def get_metadata(self, scene: Scene) -> Mapping[str, object]:
        raise NotImplementedError


class Sentinel2Provider(SatelliteProvider):
    """Sentinel-2 L2A provider backed by Element 84 Earth Search STAC v1."""

    name = "Sentinel-2 L2A - Earth Search"
    endpoint = "https://earth-search.aws.element84.com/v1"
    collection = "sentinel-2-l2a"
    band_assets = {
        "B02": "blue",
        "B03": "green",
        "B04": "red",
        "B05": "rededge1",
        "B08": "nir",
        "B8A": "nir08",
        "B11": "swir16",
        "SCL": "scl",
    }

    def test_connection(self) -> bool:
        response = self._request_json(self.endpoint, method="GET")
        return response.get("type") == "Catalog" or "links" in response

    def search_images(
        self,
        area_path: str,
        dates: Sequence[date],
        cloud_limit: float,
        tolerance_days: int = 5,
        limit_per_date: int = 10,
    ) -> List[Scene]:
        if not dates:
            raise ValueError("Informe pelo menos uma data para buscar imagens online.")
        bbox, intersects = self._aoi_wgs84(area_path)
        scenes: Dict[str, Scene] = {}
        for window in DateManager.windows(dates, tolerance_days):
            body = {
                "collections": [self.collection],
                "datetime": f"{window.start.isoformat()}T00:00:00Z/{window.end.isoformat()}T23:59:59Z",
                "limit": int(limit_per_date),
                "query": {"eo:cloud_cover": {"lte": float(cloud_limit)}},
            }
            if intersects:
                body["intersects"] = intersects
            else:
                body["bbox"] = list(bbox)
            response = self._request_json(f"{self.endpoint}/search", method="POST", body=body)
            for feature in response.get("features", []):
                scene = self._feature_to_scene(feature, window.requested.isoformat())
                existing = scenes.get(scene.scene_id)
                if existing is None or scene.requested_date < existing.requested_date:
                    scenes[scene.scene_id] = scene
        return sorted(scenes.values(), key=lambda item: (item.date, item.cloud_percent, item.scene_id))

    def download_bands(
        self,
        scene: Scene,
        bands: Sequence[str],
        output_folder: str,
        area_path: Optional[str] = None,
        context_margin_percent: int = 80,
    ) -> Mapping[str, str]:
        Path(output_folder).mkdir(parents=True, exist_ok=True)
        outputs: Dict[str, str] = {}
        gdal.SetConfigOption("GDAL_DISABLE_READDIR_ON_OPEN", "EMPTY_DIR")
        gdal.SetConfigOption("CPL_VSIL_CURL_ALLOWED_EXTENSIONS", ".tif,.TIF")

        for band_name in bands:
            band_name = band_name.upper()
            href = scene.assets.get(band_name)
            if not href:
                continue
            output_path = RasterManager.available_path(str(Path(output_folder) / f"{scene.scene_id}_{band_name}.tif"))
            source_path = f"/vsicurl/{href}" if href.startswith("http") else href
            source = gdal.Open(source_path)
            if source is None:
                raise RuntimeError(f"Nao foi possivel abrir a banda online {band_name} da cena {scene.scene_id}.")
            options_kwargs = {
                "format": "GTiff",
                "dstSRS": source.GetProjection(),
                "dstNodata": -9999.0,
                "multithread": True,
                "creationOptions": ["COMPRESS=LZW", "TILED=YES", "BIGTIFF=IF_SAFER"],
            }
            if area_path:
                options_kwargs["outputBounds"] = self._aoi_bounds_for_dataset(
                    area_path,
                    source,
                    context_margin_percent,
                )
            if band_name == "SCL":
                options_kwargs["resampleAlg"] = "near"
                options_kwargs["srcNodata"] = 0
                options_kwargs["dstNodata"] = 0
            else:
                options_kwargs["resampleAlg"] = "bilinear"
                options_kwargs["srcNodata"] = 0
            options = gdal.WarpOptions(**options_kwargs)
            result = gdal.Warp(output_path, source, options=options)
            if result is None:
                raise RuntimeError(f"Nao foi possivel baixar/recortar a banda {band_name} da cena {scene.scene_id}.")
            result = None
            source = None
            self._write_asset_metadata(output_path, scene.asset_metadata.get(band_name, {}))
            outputs[band_name] = output_path
        return outputs

    def get_metadata(self, scene: Scene) -> Mapping[str, object]:
        return {
            "data_solicitada": scene.requested_date,
            "data_imagem": scene.date,
            "fonte": self.name,
            "sensor": scene.sensor,
            "id_cena": scene.scene_id,
            "nuvem_cena": scene.cloud_percent,
        }

    def _feature_to_scene(self, feature: Mapping[str, object], requested_date: str) -> Scene:
        properties = feature.get("properties", {}) or {}
        assets = feature.get("assets", {}) or {}
        mapped_assets: Dict[str, str] = {}
        mapped_metadata: Dict[str, Mapping[str, object]] = {}
        available: List[str] = []
        for band_name, asset_key in self.band_assets.items():
            asset = assets.get(asset_key)
            if asset and asset.get("href"):
                mapped_assets[band_name] = asset["href"]
                mapped_metadata[band_name] = self._asset_raster_metadata(asset)
                available.append(band_name)
        platform = str(properties.get("platform") or "sentinel-2")
        sensor = f"{platform.upper()} MSI"
        scene_date = str(properties.get("datetime") or "")[:10]
        cloud = float(properties.get("eo:cloud_cover") or 0.0)
        return Scene(
            scene_id=str(feature.get("id")),
            date=scene_date,
            requested_date=requested_date,
            source=self.name,
            sensor=sensor,
            cloud_percent=cloud,
            available_bands=available,
            assets=mapped_assets,
            asset_metadata=mapped_metadata,
            properties=properties,
        )

    @staticmethod
    def _asset_raster_metadata(asset: Mapping[str, object]) -> Mapping[str, object]:
        bands = asset.get("raster:bands") or []
        if not bands:
            return {}
        first = bands[0] or {}
        return {
            "scale": first.get("scale"),
            "offset": first.get("offset"),
            "nodata": first.get("nodata"),
        }

    @staticmethod
    def _write_asset_metadata(path: str, metadata: Mapping[str, object]) -> None:
        if not metadata:
            return
        dataset = gdal.Open(path, gdal.GA_Update)
        if dataset is None:
            return
        band = dataset.GetRasterBand(1)
        scale = metadata.get("scale")
        offset = metadata.get("offset")
        if scale not in (None, ""):
            band.SetScale(float(scale))
        if offset not in (None, ""):
            band.SetOffset(float(offset))
        band.FlushCache()
        dataset.FlushCache()

    @staticmethod
    def _aoi_bounds_for_dataset(area_path: str, dataset, margin_percent: int) -> Tuple[float, float, float, float]:
        vector_dataset = ogr.Open(area_path)
        if vector_dataset is None:
            raise ValueError(f"Nao foi possivel abrir a area para recorte online: {area_path}")
        layer = vector_dataset.GetLayer(0)
        if layer is None:
            raise ValueError("A area de interesse nao possui camada vetorial.")

        raster_srs = osr.SpatialReference()
        raster_srs.ImportFromWkt(dataset.GetProjection())
        raster_srs.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
        vector_srs = layer.GetSpatialRef()
        if vector_srs:
            vector_srs = vector_srs.Clone()
            vector_srs.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)

        transform = None
        if vector_srs and not vector_srs.IsSame(raster_srs):
            transform = osr.CoordinateTransformation(vector_srs, raster_srs)

        union = None
        layer.ResetReading()
        for feature in layer:
            geometry = feature.GetGeometryRef()
            if geometry is None or geometry.IsEmpty():
                continue
            geometry = geometry.Clone()
            if transform is not None:
                geometry.Transform(transform)
            union = geometry if union is None else union.Union(geometry)
        if union is None or union.IsEmpty():
            raise ValueError("A area de interesse nao possui geometria valida para recorte online.")

        min_x, max_x, min_y, max_y = union.GetEnvelope()
        gt = dataset.GetGeoTransform()
        pixel_width = abs(float(gt[1])) if gt else 10.0
        pixel_height = abs(float(gt[5])) if gt else pixel_width
        width = max(max_x - min_x, pixel_width * 20.0)
        height = max(max_y - min_y, pixel_height * 20.0)
        factor = 1.0 + max(0, int(margin_percent)) / 100.0
        width *= factor
        height *= factor
        center_x = (min_x + max_x) / 2.0
        center_y = (min_y + max_y) / 2.0
        return (
            center_x - width / 2.0,
            center_y - height / 2.0,
            center_x + width / 2.0,
            center_y + height / 2.0,
        )

    @staticmethod
    def _request_json(url: str, method: str = "GET", body: Optional[Mapping[str, object]] = None) -> Mapping[str, object]:
        data = None
        headers = {"User-Agent": "GeoZonalSat/0.2", "Accept": "application/json"}
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="ignore")
            raise RuntimeError(f"Erro HTTP no catalogo STAC ({exc.code}): {detail}") from exc
        except (urllib.error.URLError, socket.timeout) as exc:
            raise RuntimeError(f"Nao foi possivel acessar o catalogo STAC: {exc}") from exc

    @staticmethod
    def _aoi_wgs84(area_path: str) -> Tuple[Tuple[float, float, float, float], Optional[Mapping[str, object]]]:
        dataset = ogr.Open(area_path)
        if dataset is None:
            raise ValueError(f"Nao foi possivel abrir a area para busca online: {area_path}")
        layer = dataset.GetLayer(0)
        if layer is None:
            raise ValueError("A area de interesse nao possui camada vetorial.")

        source_srs = layer.GetSpatialRef()
        if source_srs:
            source_srs = source_srs.Clone()
            source_srs.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
        target_srs = osr.SpatialReference()
        target_srs.ImportFromEPSG(4326)
        target_srs.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
        transform = None
        if source_srs and not source_srs.IsSame(target_srs):
            transform = osr.CoordinateTransformation(source_srs, target_srs)

        union = None
        layer.ResetReading()
        for feature in layer:
            geometry = feature.GetGeometryRef()
            if geometry is None or geometry.IsEmpty():
                continue
            geometry = geometry.Clone()
            if transform is not None:
                geometry.Transform(transform)
            union = geometry if union is None else union.Union(geometry)
        if union is None or union.IsEmpty():
            raise ValueError("A area de interesse nao possui geometria valida para busca online.")

        envelope = union.GetEnvelope()
        bbox = (float(envelope[0]), float(envelope[2]), float(envelope[1]), float(envelope[3]))
        intersects = json.loads(union.ExportToJson())
        return bbox, intersects
