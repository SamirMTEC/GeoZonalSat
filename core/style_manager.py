from __future__ import annotations

from pathlib import Path
from typing import Optional

from qgis.PyQt.QtGui import QColor
from qgis.core import (
    QgsColorRampShader,
    QgsProject,
    QgsRasterLayer,
    QgsRasterShader,
    QgsSingleBandPseudoColorRenderer,
)


class StyleManager:
    """Load and style result rasters in the QGIS layer tree."""

    ROOT_GROUP = "GeoZonalSat_Resultados"

    def load_index_raster(self, path: str, index_name: str, date_text: str = "", area_label: str = "") -> Optional[QgsRasterLayer]:
        layer_name = self._layer_name(index_name, date_text, area_label)
        layer = QgsRasterLayer(path, layer_name)
        if not layer.isValid():
            return None
        self.apply_index_style(layer, index_name)
        project = QgsProject.instance()
        project.addMapLayer(layer, False)
        group = self._group_for_index(index_name)
        group.addLayer(layer)
        return layer

    def apply_index_style(self, layer: QgsRasterLayer, index_name: str) -> None:
        ramp = QgsColorRampShader()
        ramp.setColorRampType(QgsColorRampShader.Interpolated)
        ramp.setColorRampItemList(self._items(index_name))
        shader = QgsRasterShader()
        shader.setRasterShaderFunction(ramp)
        renderer = QgsSingleBandPseudoColorRenderer(layer.dataProvider(), 1, shader)
        layer.setRenderer(renderer)
        layer.triggerRepaint()

    def _group_for_index(self, index_name: str):
        root = QgsProject.instance().layerTreeRoot()
        group = root.findGroup(self.ROOT_GROUP)
        if group is None:
            group = root.addGroup(self.ROOT_GROUP)
            group.addGroup("Tabelas")
            group.addGroup("Recortes")
        index_group = group.findGroup(index_name.upper())
        if index_group is None:
            index_group = group.addGroup(index_name.upper())
        return index_group

    @staticmethod
    def _layer_name(index_name: str, date_text: str = "", area_label: str = "") -> str:
        parts = [index_name.upper()]
        if date_text:
            parts.append(date_text)
        if area_label:
            safe_area = "".join(ch if ch.isalnum() or ch in ("-", "_") else "_" for ch in area_label)
            parts.append(safe_area)
        return "_".join(parts)

    @staticmethod
    def _items(index_name: str):
        name = (index_name or "").upper()
        if name == "NDMI":
            colors = [
                (-1.0, "#8c510a", "muito seco"),
                (0.0, "#dfc27d", "seco/neutro"),
                (0.3, "#80cdc1", "umido"),
                (1.0, "#01665e", "muito umido"),
            ]
        elif name == "EVI":
            colors = [
                (-0.2, "#a6611a", "solo/baixo"),
                (0.2, "#dfc27d", "baixo"),
                (0.5, "#74c476", "medio"),
                (1.0, "#00441b", "alto"),
            ]
        else:
            colors = [
                (-1.0, "#7f1d1d", "muito baixo"),
                (0.0, "#c08a37", "solo/baixo"),
                (0.2, "#f6e58d", "baixo-medio"),
                (0.5, "#74c476", "medio"),
                (1.0, "#006837", "alto"),
            ]
        return [QgsColorRampShader.ColorRampItem(value, QColor(color), label) for value, color, label in colors]
