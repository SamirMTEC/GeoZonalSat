from __future__ import annotations

from pathlib import Path
from typing import Dict, Mapping, Optional, Tuple

import numpy as np
from osgeo import gdal, ogr, osr
from qgis.PyQt.QtCore import QPoint, QRect, Qt
from qgis.PyQt.QtGui import QColor, QFont, QImage, QPainter, QPainterPath, QPen, QPolygon

from .raster_manager import RasterManager
from .style_manager import StyleManager


gdal.UseExceptions()


class MapExporter:
    """Export organized PNG maps without invoking the full QGIS map renderer.

    The previous implementation rendered every full raster through
    QgsMapRendererParallelJob. That is precise, but it can be extremely slow for
    large Sentinel-2 rasters. This exporter asks GDAL for a downsized view that
    already matches the map frame, then draws the title, legend and AOI outline.
    """

    MAP_WIDTH = 1230
    MAP_HEIGHT = 955

    def __init__(self, style_manager: Optional[StyleManager] = None) -> None:
        self.style_manager = style_manager or StyleManager()

    def export_index_map(
        self,
        raster_info: Mapping[str, object],
        aoi_path: str,
        output_folder: str,
        margin_percent: int = 80,
    ) -> Optional[Dict[str, object]]:
        raster_path = str(raster_info.get("map_path") or raster_info.get("path", ""))
        index_name = str(raster_info.get("indice", "")).upper()
        date_text = str(raster_info.get("data", ""))
        area_label = str(raster_info.get("area", "AOI"))
        scl_mask_applied = bool(raster_info.get("scl_mask_applied", False))
        cloud_percent = _optional_float(raster_info.get("cloud_percent"))
        cloud_status = str(raster_info.get("cloud_status", "")).strip()
        if not raster_path or not Path(raster_path).exists():
            return None
        map_run_id = _safe_name(str(raster_info.get("map_run_id", "")))
        if not map_run_id:
            map_run_id = f"r{Path(raster_path).stat().st_mtime_ns:x}"

        output_path = self._output_path(output_folder, index_name, date_text, area_label, map_run_id)
        output_path.parent.mkdir(parents=True, exist_ok=True)

        image = self._render_map(
            raster_path,
            aoi_path,
            index_name,
            date_text,
            area_label,
            int(margin_percent),
            scl_mask_applied,
            cloud_percent,
            cloud_status,
        )
        if not image.save(str(output_path), "PNG") or not output_path.exists():
            raise RuntimeError(f"Nao foi possivel gravar o mapa PNG: {output_path}")
        result: Dict[str, object] = {
            "path": str(output_path),
            "indice": index_name,
            "data": date_text,
            "area": area_label,
        }
        if cloud_percent is not None:
            result["cloud_percent"] = cloud_percent
            result["cloud_status"] = cloud_status or _cloud_status(cloud_percent)
        return result

    def _render_map(
        self,
        raster_path: str,
        aoi_path: str,
        index_name: str,
        date_text: str,
        area_label: str,
        margin_percent: int,
        scl_mask_applied: bool,
        cloud_percent: Optional[float],
        cloud_status: str,
    ) -> QImage:
        width, height = 1800, 1200
        map_rect = QRect(70, 145, self.MAP_WIDTH, self.MAP_HEIGHT)
        panel_rect = QRect(1330, 145, 400, 955)

        dataset = RasterManager.open_dataset(raster_path)
        extent = self._centered_extent(aoi_path, dataset, margin_percent)
        map_image = self._render_fast_raster(dataset, extent, index_name, self.MAP_WIDTH, self.MAP_HEIGHT)
        self._draw_aoi_outline(map_image, aoi_path, dataset, extent)

        final = QImage(width, height, QImage.Format_ARGB32_Premultiplied)
        final.fill(QColor("#ffffff"))
        painter = QPainter(final)
        painter.setRenderHint(QPainter.Antialiasing)

        self._draw_header(painter, index_name, date_text, area_label, width)
        painter.fillRect(map_rect, QColor("#f8fafc"))
        painter.drawImage(map_rect, map_image)
        painter.setPen(QPen(QColor("#111827"), 3))
        painter.drawRect(map_rect)
        self._draw_side_panel(
            painter,
            panel_rect,
            index_name,
            date_text,
            area_label,
            margin_percent,
            scl_mask_applied,
            cloud_percent,
            cloud_status,
        )
        painter.end()
        return final

    def _render_fast_raster(
        self,
        dataset,
        extent: Tuple[float, float, float, float],
        index_name: str,
        width: int,
        height: int,
    ) -> QImage:
        band = dataset.GetRasterBand(1)
        source_nodata = band.GetNoDataValue()
        options_kwargs = {
            "format": "MEM",
            "outputBounds": extent,
            "width": int(width),
            "height": int(height),
            "resampleAlg": "bilinear",
            "dstNodata": -9999.0,
        }
        projection = dataset.GetProjection()
        if projection:
            options_kwargs["dstSRS"] = projection
        if source_nodata is not None:
            options_kwargs["srcNodata"] = source_nodata

        preview = gdal.Warp("", dataset, options=gdal.WarpOptions(**options_kwargs))
        if preview is None:
            raise RuntimeError("Nao foi possivel criar a visualizacao rapida do raster.")
        preview_band = preview.GetRasterBand(1)
        nodata = preview_band.GetNoDataValue()
        array = preview_band.ReadAsArray().astype(np.float32)

        invalid = ~np.isfinite(array)
        if nodata is not None:
            invalid |= np.isclose(array, float(nodata))
        if source_nodata is not None:
            invalid |= np.isclose(array, float(source_nodata))
        if not np.any(~invalid):
            raise RuntimeError("O raster nao possui pixels validos na extensao do mapa.")
        return self._colorize(array, invalid, index_name)

    @staticmethod
    def _colorize(array: np.ndarray, invalid: np.ndarray, index_name: str) -> QImage:
        stops = _color_stops(index_name)
        values = np.array([item[0] for item in stops], dtype=np.float32)
        colors = np.array([_rgb(item[1]) for item in stops], dtype=np.float32)

        safe_array = np.where(invalid, float(values[0]), array)
        clipped = np.clip(safe_array, float(values[0]), float(values[-1]))
        red = np.interp(clipped, values, colors[:, 0])
        green = np.interp(clipped, values, colors[:, 1])
        blue = np.interp(clipped, values, colors[:, 2])

        rgba = np.zeros((array.shape[0], array.shape[1], 4), dtype=np.uint8)
        rgba[..., 0] = red.astype(np.uint8)
        rgba[..., 1] = green.astype(np.uint8)
        rgba[..., 2] = blue.astype(np.uint8)
        rgba[..., 3] = 255
        rgba[invalid, 0] = 226
        rgba[invalid, 1] = 232
        rgba[invalid, 2] = 240
        return QImage(rgba.data, rgba.shape[1], rgba.shape[0], rgba.strides[0], QImage.Format_RGBA8888).copy()

    @staticmethod
    def _centered_extent(aoi_path: str, raster_dataset, margin_percent: int) -> Tuple[float, float, float, float]:
        raster_extent = RasterManager.bounds(raster_dataset)
        aoi_extent = _aoi_extent_in_raster_crs(aoi_path, raster_dataset)
        if aoi_extent is None:
            aoi_extent = raster_extent

        min_x, min_y, max_x, max_y = aoi_extent
        gt = raster_dataset.GetGeoTransform()
        pixel_width = abs(float(gt[1])) if gt else 10.0
        pixel_height = abs(float(gt[5])) if gt else pixel_width
        width = max(max_x - min_x, pixel_width * 40.0)
        height = max(max_y - min_y, pixel_height * 40.0)
        factor = 1.0 + max(0, margin_percent) / 100.0
        width *= factor
        height *= factor

        aspect = MapExporter.MAP_WIDTH / MapExporter.MAP_HEIGHT
        if width / max(height, 1e-12) < aspect:
            width = height * aspect
        else:
            height = width / aspect

        center_x = (min_x + max_x) / 2.0
        center_y = (min_y + max_y) / 2.0
        return (
            center_x - width / 2.0,
            center_y - height / 2.0,
            center_x + width / 2.0,
            center_y + height / 2.0,
        )

    def _draw_aoi_outline(
        self,
        image: QImage,
        aoi_path: str,
        raster_dataset,
        extent: Tuple[float, float, float, float],
    ) -> None:
        geometries = _aoi_geometries_in_raster_crs(aoi_path, raster_dataset)
        if not geometries:
            return
        painter = QPainter(image)
        painter.setRenderHint(QPainter.Antialiasing)
        painter.setPen(QPen(QColor("#111827"), 3))
        painter.setBrush(Qt.NoBrush)
        for geometry in geometries:
            self._draw_geometry(painter, geometry, extent, image.width(), image.height())
        painter.end()

    def _draw_geometry(
        self,
        painter: QPainter,
        geometry,
        extent: Tuple[float, float, float, float],
        width: int,
        height: int,
    ) -> None:
        name = geometry.GetGeometryName().upper()
        if "MULTI" in name or "COLLECTION" in name:
            for index in range(geometry.GetGeometryCount()):
                child = geometry.GetGeometryRef(index)
                if child is not None:
                    self._draw_geometry(painter, child, extent, width, height)
        elif "POLYGON" in name and geometry.GetGeometryCount() > 0:
            for ring_index in range(geometry.GetGeometryCount()):
                ring = geometry.GetGeometryRef(ring_index)
                path = self._ring_to_path(ring, extent, width, height)
                painter.drawPath(path)

    @staticmethod
    def _ring_to_path(ring, extent: Tuple[float, float, float, float], width: int, height: int) -> QPainterPath:
        min_x, min_y, max_x, max_y = extent
        extent_width = max(max_x - min_x, 1e-12)
        extent_height = max(max_y - min_y, 1e-12)
        path = QPainterPath()
        point_count = ring.GetPointCount()
        for index in range(point_count):
            x, y, _ = ring.GetPoint(index)
            px = (x - min_x) / extent_width * width
            py = (max_y - y) / extent_height * height
            if index == 0:
                path.moveTo(px, py)
            else:
                path.lineTo(px, py)
        path.closeSubpath()
        return path

    @staticmethod
    def _draw_header(painter: QPainter, index_name: str, date_text: str, area_label: str, width: int) -> None:
        painter.setPen(QColor("#111827"))
        painter.setFont(QFont("Arial", 30, QFont.Bold))
        painter.drawText(70, 42, width - 140, 42, Qt.AlignLeft | Qt.AlignVCenter, f"{index_name} - {date_text}")
        painter.setFont(QFont("Arial", 16))
        painter.setPen(QColor("#475467"))
        painter.drawText(
            70,
            86,
            width - 140,
            36,
            Qt.AlignLeft | Qt.AlignVCenter,
            f"GeoZonalSat | Area centralizada: {area_label}",
        )

    def _draw_side_panel(
        self,
        painter: QPainter,
        rect: QRect,
        index_name: str,
        date_text: str,
        area_label: str,
        margin_percent: int,
        scl_mask_applied: bool,
        cloud_percent: Optional[float],
        cloud_status: str,
    ) -> None:
        painter.setPen(QPen(QColor("#d0d5dd"), 2))
        painter.setBrush(QColor("#ffffff"))
        painter.drawRect(rect)

        x = rect.x() + 28
        y = rect.y() + 36
        painter.setPen(QColor("#111827"))
        painter.setFont(QFont("Arial", 18, QFont.Bold))
        painter.drawText(x, y, rect.width() - 56, 34, Qt.AlignLeft | Qt.AlignVCenter, "Legenda")
        y += 58

        for value, color, label in _color_stops(index_name):
            painter.setPen(QPen(QColor("#475467"), 1))
            painter.setBrush(QColor(color))
            painter.drawRect(x, y, 42, 24)
            painter.setPen(QColor("#111827"))
            painter.setFont(QFont("Arial", 13))
            painter.drawText(x + 58, y - 2, rect.width() - 86, 30, Qt.AlignLeft | Qt.AlignVCenter, f"{value:g} - {label}")
            y += 38
        painter.setPen(QPen(QColor("#475467"), 1))
        painter.setBrush(QColor("#e2e8f0"))
        painter.drawRect(x, y, 42, 24)
        painter.setPen(QColor("#111827"))
        painter.setFont(QFont("Arial", 13))
        painter.drawText(x + 58, y - 2, rect.width() - 86, 30, Qt.AlignLeft | Qt.AlignVCenter, "sem dados/mascarado")
        y += 38

        y += 26
        painter.setPen(QColor("#111827"))
        painter.setFont(QFont("Arial", 18, QFont.Bold))
        painter.drawText(x, y, rect.width() - 56, 34, Qt.AlignLeft | Qt.AlignVCenter, "Mapa")
        y += 50
        painter.setFont(QFont("Arial", 13))
        painter.setPen(QColor("#475467"))
        notes = [
            f"Indice: {index_name}",
            f"Data da imagem: {date_text}",
            f"Area: {area_label}",
            f"Margem aplicada: {margin_percent}%",
            "Contorno preto: area estudada",
            "Visualizacao: indice completo",
            "Estatistica: somente dentro da area",
            f"Mascara SCL: {'aplicada' if scl_mask_applied else 'desativada'}",
            "Marrom/amarelo: solo ou baixo indice",
            "Cinza claro: sem dados ou mascarado",
            "Render rapido: raster reamostrado",
        ]
        if cloud_percent is not None:
            status = cloud_status or _cloud_status(cloud_percent)
            notes.insert(4, f"Nuvem na cena: {cloud_percent:.1f}% - {status}")
        for note in notes:
            painter.drawText(x, y, rect.width() - 56, 28, Qt.AlignLeft | Qt.AlignVCenter, note)
            y += 32

        self._draw_north_arrow(painter, rect.x() + rect.width() - 88, rect.y() + rect.height() - 140)

    @staticmethod
    def _draw_north_arrow(painter: QPainter, x: int, y: int) -> None:
        painter.setPen(QPen(QColor("#111827"), 3))
        painter.setBrush(QColor("#111827"))
        polygon = QPolygon(
            [
                QPoint(x, y),
                QPoint(x - 18, y + 58),
                QPoint(x, y + 44),
                QPoint(x + 18, y + 58),
            ]
        )
        painter.drawPolygon(polygon)
        painter.setFont(QFont("Arial", 16, QFont.Bold))
        painter.drawText(x - 18, y + 70, 36, 28, Qt.AlignCenter, "N")

    @staticmethod
    def _output_path(
        output_folder: str,
        index_name: str,
        date_text: str,
        area_label: str,
        map_run_id: str,
    ) -> Path:
        safe_area = _safe_name(area_label or "AOI")
        safe_date = _safe_name(date_text or "sem_data")
        safe_index = _safe_name(index_name or "indice")
        safe_run = _safe_name(map_run_id or "execucao")
        return Path(output_folder) / safe_index / f"mapa_{safe_index}_{safe_date}_{safe_area}_{safe_run}.png"


def _aoi_extent_in_raster_crs(aoi_path: str, raster_dataset) -> Optional[Tuple[float, float, float, float]]:
    geometries = _aoi_geometries_in_raster_crs(aoi_path, raster_dataset)
    if not geometries:
        return None
    envelopes = [geometry.GetEnvelope() for geometry in geometries if geometry and not geometry.IsEmpty()]
    if not envelopes:
        return None
    min_x = min(item[0] for item in envelopes)
    max_x = max(item[1] for item in envelopes)
    min_y = min(item[2] for item in envelopes)
    max_y = max(item[3] for item in envelopes)
    return min_x, min_y, max_x, max_y


def _aoi_geometries_in_raster_crs(aoi_path: str, raster_dataset):
    vector_dataset = ogr.Open(aoi_path)
    if vector_dataset is None:
        return []
    layer = vector_dataset.GetLayer(0)
    if layer is None:
        return []

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

    geometries = []
    layer.ResetReading()
    for feature in layer:
        geometry = feature.GetGeometryRef()
        if geometry is None or geometry.IsEmpty():
            continue
        geometry = geometry.Clone()
        if transform is not None:
            geometry.Transform(transform)
        geometries.append(geometry)
    return geometries


def _color_stops(index_name: str):
    name = (index_name or "").upper()
    if name == "NDMI":
        return [
            (-1.0, "#8c510a", "muito seco"),
            (0.0, "#dfc27d", "seco/neutro"),
            (0.3, "#80cdc1", "umido"),
            (1.0, "#01665e", "muito umido"),
        ]
    if name == "EVI":
        return [
            (-0.2, "#a6611a", "solo/baixo"),
            (0.2, "#dfc27d", "baixo"),
            (0.5, "#74c476", "medio"),
            (1.0, "#00441b", "alto"),
        ]
    return [
        (-1.0, "#7f1d1d", "muito baixo"),
        (0.0, "#c08a37", "solo/baixo"),
        (0.2, "#f6e58d", "baixo-medio"),
        (0.5, "#74c476", "medio"),
        (1.0, "#006837", "alto"),
    ]


def _rgb(hex_color: str):
    color = QColor(hex_color)
    return color.red(), color.green(), color.blue()


def _safe_name(value: str) -> str:
    return "".join(ch if ch.isalnum() or ch in ("-", "_") else "_" for ch in str(value))


def _optional_float(value) -> Optional[float]:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _cloud_status(cloud_percent: float) -> str:
    if cloud_percent <= 2.0:
        return "BAIXA"
    if cloud_percent <= 10.0:
        return "MODERADA"
    if cloud_percent <= 20.0:
        return "ALTA"
    return "MUITO ALTA"
