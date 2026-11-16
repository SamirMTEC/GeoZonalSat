from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, List, Optional, Tuple

from qgis.PyQt.QtCore import QVariant
from qgis.core import (
    QgsCoordinateReferenceSystem,
    QgsDistanceArea,
    QgsFeature,
    QgsField,
    QgsGeometry,
    QgsProject,
    QgsVectorFileWriter,
    QgsVectorLayer,
    QgsWkbTypes,
)


@dataclass
class AreaSummary:
    feature_count: int
    invalid_fixed_count: int
    area_ha: float
    message: str


class AreaManager:
    """Validate, clean, and export the area of interest for background processing."""

    def __init__(self, iface=None) -> None:
        self.iface = iface

    def load_external_layer(self, path: str) -> QgsVectorLayer:
        layer = QgsVectorLayer(path, Path(path).stem, "ogr")
        if not layer.isValid():
            raise ValueError(f"Nao foi possivel carregar o arquivo vetorial: {path}")
        if not self.is_polygon_layer(layer):
            raise ValueError("A camada precisa ser do tipo poligono.")
        QgsProject.instance().addMapLayer(layer)
        return layer

    @staticmethod
    def is_polygon_layer(layer: QgsVectorLayer) -> bool:
        return bool(layer) and QgsWkbTypes.geometryType(layer.wkbType()) == QgsWkbTypes.PolygonGeometry

    def validate_layer(self, layer: QgsVectorLayer, selected_only: bool = False) -> AreaSummary:
        if not layer or not layer.isValid():
            raise ValueError("Area invalida ou nao selecionada.")
        if not self.is_polygon_layer(layer):
            raise ValueError("A camada de area precisa ser poligonal.")
        features = self._features(layer, selected_only)
        if selected_only and not features:
            raise ValueError("Nenhuma feicao selecionada na camada de area.")
        if not features:
            raise ValueError("A camada de area nao possui feicoes.")

        invalid_fixed_count = 0
        total_area = 0.0
        for feature in features:
            geometry, was_fixed = self._clean_geometry(feature.geometry())
            if was_fixed:
                invalid_fixed_count += 1
            if geometry and not geometry.isEmpty():
                total_area += self._area_ha(layer, geometry)

        message = f"{len(features)} area(s), {total_area:.4f} ha"
        if invalid_fixed_count:
            message += f" | {invalid_fixed_count} geometria(s) corrigida(s)"
        return AreaSummary(
            feature_count=len(features),
            invalid_fixed_count=invalid_fixed_count,
            area_ha=total_area,
            message=message,
        )

    def export_aoi(
        self,
        layer: QgsVectorLayer,
        output_path: str,
        id_field: Optional[str] = None,
        selected_only: bool = False,
        dissolve: bool = False,
    ) -> str:
        if not layer or not layer.isValid():
            raise ValueError("Area invalida ou nao selecionada.")
        features = self._features(layer, selected_only)
        if selected_only and not features:
            raise ValueError("Nenhuma feicao selecionada na camada de area.")
        if not features:
            raise ValueError("A camada de area nao possui feicoes.")

        memory = QgsVectorLayer("MultiPolygon", "geozonalsat_aoi", "memory")
        memory.setCrs(layer.crs() if layer.crs().isValid() else QgsCoordinateReferenceSystem("EPSG:4326"))
        provider = memory.dataProvider()
        provider.addAttributes(
            [
                QgsField("GZS_ID", QVariant.String),
                QgsField("GZS_NAME", QVariant.String),
                QgsField("GZS_AREA_HA", QVariant.Double),
            ]
        )
        memory.updateFields()

        if dissolve:
            geometries = []
            invalid_fixed = 0
            for feature in features:
                geometry, was_fixed = self._clean_geometry(feature.geometry())
                if was_fixed:
                    invalid_fixed += 1
                if geometry and not geometry.isEmpty():
                    geometries.append(geometry)
            if not geometries:
                raise ValueError("Nao ha geometria valida para dissolver.")
            dissolved = QgsGeometry.unaryUnion(geometries)
            dissolved, _ = self._clean_geometry(dissolved)
            area_ha = self._area_ha(layer, dissolved)
            out_feature = QgsFeature(memory.fields())
            out_feature.setGeometry(dissolved)
            out_feature.setAttributes(["dissolved", "Area dissolvida", area_ha])
            provider.addFeature(out_feature)
        else:
            for feature in features:
                geometry, _ = self._clean_geometry(feature.geometry())
                if not geometry or geometry.isEmpty():
                    continue
                out_feature = QgsFeature(memory.fields())
                out_feature.setGeometry(geometry)
                label = self._label_for_feature(feature, id_field)
                area_ha = self._area_ha(layer, geometry)
                out_feature.setAttributes([label, label, area_ha])
                provider.addFeature(out_feature)

        memory.updateExtents()
        Path(output_path).parent.mkdir(parents=True, exist_ok=True)
        self._write_vector(memory, output_path)
        return output_path

    @staticmethod
    def field_names(layer: QgsVectorLayer) -> List[str]:
        if not layer or not layer.isValid():
            return []
        return [field.name() for field in layer.fields()]

    @staticmethod
    def _features(layer: QgsVectorLayer, selected_only: bool) -> List[QgsFeature]:
        if selected_only:
            return list(layer.selectedFeatures())
        return list(layer.getFeatures())

    @staticmethod
    def _label_for_feature(feature: QgsFeature, id_field: Optional[str]) -> str:
        if id_field and id_field in feature.fields().names():
            value = feature[id_field]
            if value not in (None, ""):
                return str(value)
        return str(feature.id())

    @staticmethod
    def _clean_geometry(geometry: QgsGeometry) -> Tuple[Optional[QgsGeometry], bool]:
        if geometry is None or geometry.isNull() or geometry.isEmpty():
            return None, False
        clean = QgsGeometry(geometry)
        if clean.isGeosValid():
            return clean, False
        fixed = clean.makeValid()
        if (
            fixed
            and not fixed.isEmpty()
            and QgsWkbTypes.geometryType(fixed.wkbType()) == QgsWkbTypes.PolygonGeometry
        ):
            return fixed, True
        buffered = clean.buffer(0, 8)
        if (
            buffered
            and not buffered.isEmpty()
            and QgsWkbTypes.geometryType(buffered.wkbType()) == QgsWkbTypes.PolygonGeometry
        ):
            return buffered, True
        return clean, False

    @staticmethod
    def _area_ha(layer: QgsVectorLayer, geometry: QgsGeometry) -> float:
        try:
            distance = QgsDistanceArea()
            distance.setSourceCrs(layer.crs(), QgsProject.instance().transformContext())
            ellipsoid = QgsProject.instance().ellipsoid() or "WGS84"
            distance.setEllipsoid(ellipsoid)
            return float(distance.measureArea(geometry)) / 10000.0
        except Exception:
            return float(geometry.area()) / 10000.0

    @staticmethod
    def _write_vector(layer: QgsVectorLayer, output_path: str) -> None:
        options = QgsVectorFileWriter.SaveVectorOptions()
        options.driverName = "GPKG"
        options.layerName = "aoi"
        if hasattr(QgsVectorFileWriter, "CreateOrOverwriteFile"):
            options.actionOnExistingFile = QgsVectorFileWriter.CreateOrOverwriteFile
        result = QgsVectorFileWriter.writeAsVectorFormatV3(
            layer,
            output_path,
            QgsProject.instance().transformContext(),
            options,
        )
        error_code = result[0] if isinstance(result, tuple) else result
        if error_code != QgsVectorFileWriter.NoError:
            error_message = result[1] if isinstance(result, tuple) and len(result) > 1 else "Erro ao gravar GeoPackage."
            raise ValueError(str(error_message))
