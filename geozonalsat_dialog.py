from __future__ import annotations

import shutil
import traceback
from datetime import date, datetime
from pathlib import Path
from typing import Dict, List, Optional

from qgis.PyQt.QtCore import QDate, QTimer, Qt, QUrl
from qgis.PyQt.QtGui import QColor, QDesktopServices
from qgis.PyQt.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QDateEdit,
    QDialog,
    QFileDialog,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QPlainTextEdit,
    QProgressBar,
    QRadioButton,
    QSpinBox,
    QDoubleSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QTextBrowser,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)
from qgis.core import QgsApplication, QgsMapLayerProxyModel, QgsTask
from qgis.gui import QgsFieldComboBox, QgsMapLayerComboBox

from .core.area_manager import AreaManager
from .core.cloud_mask import apply_sentinel2_scl_mask
from .core.date_manager import DateManager
from .core.export_manager import ExportManager
from .core.index_calculator import INDEX_REQUIRED_BANDS, IndexCalculator
from .core.local_raster_loader import INDEX_NAMES, RAW_BANDS, LocalRasterItem, LocalRasterLoader
from .core.logger import ProcessingLogger
from .core.map_exporter import MapExporter
from .core.report_generator import ReportGenerator
from .core.satellite_providers import Scene, Sentinel2Provider
from .core.style_manager import StyleManager
from .core.zonal_statistics import RESULT_FIELDS, ZonalStatisticsEngine, output_clip_name


PLUGIN_VERSION = "0.6.0"


class LocalProcessingTask(QgsTask):
    def __init__(self, config: Dict[str, object], on_finished):
        super().__init__("GeoZonalSat - processamento local", QgsTask.CanCancel)
        self.config = config
        self.on_finished = on_finished
        self.result_data: Dict[str, object] = {}
        self.exception: Optional[Exception] = None
        self.traceback_text = ""

    def run(self):
        try:
            self.result_data = process_local_job(self.config, self)
            return True
        except Exception as exc:
            self.exception = exc
            self.traceback_text = traceback.format_exc()
            return False

    def finished(self, result):
        self.on_finished(bool(result), self.result_data, self.exception, self.traceback_text)


def process_local_job(config: Dict[str, object], task: QgsTask) -> Dict[str, object]:
    output_root = Path(str(config["output_root"]))
    folders = {
        "rasters": output_root / "rasters",
        "tabelas": output_root / "tabelas",
        "relatorios": output_root / "relatorios",
        "logs": output_root / "logs",
        "recortes": output_root / "rasters" / "recortes",
        "cache": output_root / "cache",
    }
    for folder in folders.values():
        folder.mkdir(parents=True, exist_ok=True)

    logger = ProcessingLogger(str(folders["logs"] / "processamento.log"))
    mode = str(config.get("mode", "local"))
    logger.write(f"Inicio do processamento: {mode}.")

    selected_indices = [str(value).upper() for value in config["selected_indices"]]
    aoi_path = str(config["aoi_path"])
    resampling = str(config.get("resampling", "bilinear"))
    savi_l = float(config.get("savi_l", 0.5))
    ndre_use_b8a = bool(config.get("ndre_use_b8a", False))
    map_margin_percent = int(config.get("map_margin_percent", 80))
    apply_scl_mask = bool(config.get("apply_scl_mask", False))
    map_run_id = str(config.get("map_run_id", "")).strip()

    calculator = IndexCalculator(temp_folder=str(folders["cache"]))
    engine = ZonalStatisticsEngine()
    exporter = ExportManager()
    warnings: List[str] = []
    rows: List[Dict[str, object]] = []
    rasters_to_load: List[Dict[str, object]] = []

    if mode == "online":
        scenes: List[Scene] = list(config.get("scenes", []))
        if not scenes:
            raise RuntimeError("Nenhuma cena online selecionada para processamento.")
        total_jobs = max(1, len(scenes) * max(1, len(selected_indices)))
        finished_jobs = 0
        provider = Sentinel2Provider()

        for scene in scenes:
            if task.isCanceled():
                raise RuntimeError("Processamento cancelado.")
            cloud_status, _, _ = _cloud_classification(scene.cloud_percent)
            if scene.cloud_percent > 10.0:
                warnings.append(
                    f"ATENCAO NUVEM: cena {scene.scene_id} de {scene.date} possui "
                    f"{scene.cloud_percent:.1f}% de nuvens na cena inteira ({cloud_status})."
                )
            needed_bands = _required_bands_for_indices(selected_indices, ndre_use_b8a)
            if apply_scl_mask and "SCL" in scene.available_bands:
                needed_bands.append("SCL")
            missing_scene_bands = [band for band in needed_bands if band not in scene.available_bands]
            if missing_scene_bands:
                warnings.append(
                    f"ATENCAO: cena {scene.scene_id} ignorada parcialmente: "
                    f"faltam bandas {', '.join(missing_scene_bands)}."
                )
            download_bands = [band for band in needed_bands if band in scene.available_bands]
            logger.write(f"Baixando/recortando bandas da cena {scene.scene_id}: {', '.join(download_bands)}")
            try:
                band_paths = provider.download_bands(
                    scene,
                    download_bands,
                    str(folders["cache"] / "bandas" / scene.scene_id),
                    area_path=aoi_path,
                    context_margin_percent=map_margin_percent,
                )
            except Exception as exc:
                message = f"ERRO NO DOWNLOAD: cena {scene.scene_id} ({scene.date}): {exc}"
                warnings.append(message)
                logger.write(message, level="ERROR")
                finished_jobs += len(selected_indices)
                task.setProgress(min(99.0, finished_jobs / total_jobs * 100.0))
                continue
            scl_path = band_paths.get("SCL")
            for index_name in selected_indices:
                required = list(INDEX_REQUIRED_BANDS.get(index_name, ()))
                if index_name == "NDRE" and ndre_use_b8a:
                    required = ["B8A", "B05"]
                if not required:
                    continue
                if not all(band in band_paths for band in required):
                    missing = ", ".join(band for band in required if band not in band_paths)
                    warnings.append(
                        f"ATENCAO: {index_name} na cena {scene.scene_id} ignorado: faltam bandas {missing}."
                    )
                    finished_jobs += 1
                    task.setProgress(min(99.0, finished_jobs / total_jobs * 100.0))
                    continue
                try:
                    output_index = str(folders["rasters"] / f"{index_name}_{scene.date}_{scene.scene_id}.tif")
                    logger.write(f"Calculando {index_name} para cena {scene.scene_id}.")
                    output_index = calculator.calculate(
                        index_name,
                        band_paths,
                        output_index,
                        resampling=resampling,
                        savi_l=savi_l,
                        ndre_use_b8a=ndre_use_b8a,
                    )
                    raster_for_stats = output_index
                    if apply_scl_mask and scl_path:
                        masked_path = str(
                            folders["rasters"] / f"{index_name}_{scene.date}_{scene.scene_id}_mascarado.tif"
                        )
                        raster_for_stats = apply_sentinel2_scl_mask(output_index, scl_path, masked_path)
                    scene_label = f"AOI_{scene.scene_id}"
                    clip_path = output_clip_name(str(folders["recortes"]), index_name, scene.date, scene_label)
                    metadata = dict(provider.get_metadata(scene))
                    metadata["indice"] = index_name
                    result_rows = engine.calculate_for_raster(raster_for_stats, aoi_path, metadata, clip_path)
                    if not result_rows:
                        raise RuntimeError("nenhuma area valida foi encontrada no raster")
                    rows.extend(result_rows)
                    actual_clip_path = _actual_clip_path(result_rows, clip_path)
                    rasters_to_load.append(
                        {
                            "path": actual_clip_path,
                            "map_path": output_index,
                            "indice": index_name,
                            "data": scene.date,
                            "area": scene_label,
                            "scl_mask_applied": apply_scl_mask and bool(scl_path),
                            "cloud_percent": scene.cloud_percent,
                            "cloud_status": cloud_status,
                            "map_run_id": map_run_id,
                        }
                    )
                except Exception as exc:
                    message = f"ERRO NO CALCULO: {index_name} em {scene.date}, cena {scene.scene_id}: {exc}"
                    warnings.append(message)
                    logger.write(message, level="ERROR")
                finally:
                    finished_jobs += 1
                    task.setProgress(min(99.0, finished_jobs / total_jobs * 100.0))
    else:
        items: List[LocalRasterItem] = list(config["items"])

        ready_items = [
            item
            for item in LocalRasterLoader.filter_ready_index_items(items)
            if item.index_name and item.index_name.upper() in selected_indices
        ]
        raw_plan = _build_raw_plan(items, selected_indices, ndre_use_b8a, warnings)
        total_jobs = max(1, len(ready_items) + len(raw_plan))
        finished_jobs = 0

        for item in ready_items:
            if task.isCanceled():
                raise RuntimeError("Processamento cancelado.")
            index_name = str(item.index_name).upper()
            image_date = item.date.isoformat() if item.date else ""
            clip_path = output_clip_name(str(folders["recortes"]), index_name, image_date, "AOI")
            metadata = {
                "data_solicitada": image_date,
                "data_imagem": image_date,
                "fonte": "Local",
                "sensor": "Indice pronto",
                "id_cena": Path(item.path).name,
                "indice": index_name,
                "nuvem_cena": "",
            }
            try:
                logger.write(f"Estatistica zonal para indice pronto: {Path(item.path).name}")
                result_rows = engine.calculate_for_raster(item.path, aoi_path, metadata, clip_path)
                if not result_rows:
                    raise RuntimeError("nenhuma area valida foi encontrada no raster")
                rows.extend(result_rows)
                actual_clip_path = _actual_clip_path(result_rows, clip_path)
                rasters_to_load.append(
                    {
                        "path": actual_clip_path,
                        "map_path": item.path,
                        "indice": index_name,
                        "data": image_date,
                        "area": "AOI",
                        "map_run_id": map_run_id,
                    }
                )
            except Exception as exc:
                message = f"ERRO NO CALCULO: {index_name} em {image_date or 'sem data'}, arquivo {item.path}: {exc}"
                warnings.append(message)
                logger.write(message, level="ERROR")
            finally:
                finished_jobs += 1
                task.setProgress(finished_jobs / total_jobs * 100.0)

        for image_date, index_name, bands in raw_plan:
            if task.isCanceled():
                raise RuntimeError("Processamento cancelado.")
            try:
                output_index = str(folders["rasters"] / f"{index_name}_{image_date}_calculado.tif")
                logger.write(f"Calculando {index_name} para {image_date}.")
                output_index = calculator.calculate(
                    index_name,
                    bands,
                    output_index,
                    resampling=resampling,
                    savi_l=savi_l,
                    ndre_use_b8a=ndre_use_b8a,
                )
                clip_path = output_clip_name(str(folders["recortes"]), index_name, image_date, "AOI")
                metadata = {
                    "data_solicitada": image_date,
                    "data_imagem": image_date,
                    "fonte": "Local",
                    "sensor": "Bandas locais",
                    "id_cena": f"bandas_{image_date}",
                    "indice": index_name,
                    "nuvem_cena": "",
                }
                result_rows = engine.calculate_for_raster(output_index, aoi_path, metadata, clip_path)
                if not result_rows:
                    raise RuntimeError("nenhuma area valida foi encontrada no raster")
                rows.extend(result_rows)
                actual_clip_path = _actual_clip_path(result_rows, clip_path)
                rasters_to_load.append(
                    {
                        "path": actual_clip_path,
                        "map_path": output_index,
                        "indice": index_name,
                        "data": image_date,
                        "area": "AOI",
                        "map_run_id": map_run_id,
                    }
                )
            except Exception as exc:
                message = f"ERRO NO CALCULO: {index_name} em {image_date}: {exc}"
                warnings.append(message)
                logger.write(message, level="ERROR")
            finally:
                finished_jobs += 1
                task.setProgress(finished_jobs / total_jobs * 100.0)

    if not rows:
        details = " ".join(warnings[-3:])
        raise RuntimeError(
            "Nenhuma estatistica foi gerada. Verifique datas, indices e rasters selecionados. " + details
        )

    for row in rows:
        valid_percent = row.get("percentual_valido")
        if isinstance(valid_percent, (int, float)) and valid_percent < 50:
            warnings.append(
                f"ATENCAO: {row.get('indice')} em {row.get('data_imagem')} para {row.get('nome_area')} "
                f"teve apenas {valid_percent:.1f}% de pixels validos."
            )

    csv_path = str(folders["tabelas"] / "estatisticas_completas.csv")
    exporter.save_csv(rows, csv_path)
    temporal_rows = exporter.temporal_table(rows)
    temporal_csv_path = str(folders["tabelas"] / "tabela_temporal.csv")
    exporter.save_temporal_csv(temporal_rows, temporal_csv_path)
    report_path = str(folders["relatorios"] / "relatorio_geozonalsat.html")
    logger.write("Processamento concluido com sucesso.")

    return {
        "rows": rows,
        "temporal_rows": temporal_rows,
        "csv_path": csv_path,
        "temporal_csv_path": temporal_csv_path,
        "report_path": report_path,
        "rasters_to_load": rasters_to_load,
        "warnings": warnings,
        "output_root": str(output_root),
        "aoi_path": aoi_path,
    }


def _build_raw_plan(items: List[LocalRasterItem], selected_indices: List[str], ndre_use_b8a: bool, warnings: List[str]):
    grouped = LocalRasterLoader.group_bands(items)
    plan = []
    for image_date, bands in sorted(grouped.items()):
        if image_date == "sem_data":
            warnings.append("ATENCAO: bandas brutas sem data foram ignoradas. Informe a data na tabela.")
            continue
        band_names = {name.upper() for name in bands}
        for index_name in selected_indices:
            required = list(INDEX_REQUIRED_BANDS.get(index_name, ()))
            if index_name == "NDRE" and ndre_use_b8a:
                required = ["B8A", "B05"]
            if required and all(band in band_names for band in required):
                plan.append((image_date, index_name, bands))
            elif required:
                missing = ", ".join(band for band in required if band not in band_names)
                warnings.append(f"ATENCAO: {index_name} em {image_date} ignorado: faltam bandas {missing}.")
    return plan


def _required_bands_for_indices(selected_indices: List[str], ndre_use_b8a: bool) -> List[str]:
    bands: List[str] = []
    for index_name in selected_indices:
        required = list(INDEX_REQUIRED_BANDS.get(index_name, ()))
        if index_name == "NDRE" and ndre_use_b8a:
            required = ["B8A", "B05"]
        for band in required:
            if band not in bands:
                bands.append(band)
    return bands


def _actual_clip_path(result_rows: List[Dict[str, object]], fallback_path: str) -> str:
    for row in result_rows:
        path = row.get("caminho_raster_saida")
        if path:
            return str(path)
    return fallback_path


def _cloud_classification(cloud_percent: float):
    value = max(0.0, float(cloud_percent))
    if value <= 2.0:
        return "BAIXA", "#dcfce7", "#166534"
    if value <= 10.0:
        return "MODERADA", "#fef9c3", "#854d0e"
    if value <= 20.0:
        return "ALTA", "#ffedd5", "#9a3412"
    return "MUITO ALTA", "#fee2e2", "#991b1b"


class GeoZonalSatDialog(QDialog):
    def __init__(self, iface, parent=None):
        super().__init__(parent or iface.mainWindow())
        self.iface = iface
        self.area_manager = AreaManager(iface)
        self.export_manager = ExportManager()
        self.style_manager = StyleManager()
        self.map_exporter = MapExporter(self.style_manager)
        self.online_provider = Sentinel2Provider()
        self.online_scenes: List[Scene] = []
        self.results: List[Dict[str, object]] = []
        self.result_paths: Dict[str, str] = {}
        self._task: Optional[LocalProcessingTask] = None
        self.index_checks: Dict[str, QCheckBox] = {}
        self._build_ui()
        self._connect_signals()
        self._update_source_mode()
        self._on_layer_changed(self.layer_combo.currentLayer())
        self._restore_existing_report()

    def _build_ui(self):
        self.setWindowTitle(f"GeoZonalSat {PLUGIN_VERSION}")
        self.resize(1180, 760)
        root = QVBoxLayout(self)

        self.tabs = QTabWidget()
        self.tabs.addTab(self._area_tab(), "1. Area")
        self.tabs.addTab(self._date_index_tab(), "2. Datas e indices")
        self.tabs.addTab(self._source_tab(), "3. Imagens")
        self.tabs.addTab(self._results_tab(), "4. Processar e resultados")
        self.tabs.addTab(self._report_tab(), "5. Relatorio")
        root.addWidget(self.tabs)

        bottom = QHBoxLayout()
        self.status_label = QLabel(f"Pronto. Versao {PLUGIN_VERSION}")
        self.progress = QProgressBar()
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        self.progress.setMaximumWidth(240)
        bottom.addWidget(self.status_label, 1)
        bottom.addWidget(self.progress)
        root.addLayout(bottom)

    def _area_tab(self):
        widget = QWidget()
        layout = QGridLayout(widget)

        self.layer_combo = QgsMapLayerComboBox()
        self.layer_combo.setFilters(QgsMapLayerProxyModel.PolygonLayer)
        self.field_combo = QgsFieldComboBox()
        self.selected_only_check = QCheckBox("Usar apenas feicoes selecionadas")
        self.dissolve_check = QCheckBox("Dissolver todas as feicoes em uma unica area")
        self.area_status = QLabel("Selecione uma camada poligonal.")
        self.load_area_button = QPushButton("Carregar arquivo vetorial")
        self.validate_area_button = QPushButton("Validar e continuar")

        layout.addWidget(QLabel("Camada vetorial no projeto"), 0, 0)
        layout.addWidget(self.layer_combo, 0, 1, 1, 2)
        layout.addWidget(self.load_area_button, 0, 3)
        layout.addWidget(QLabel("Campo identificador da area"), 1, 0)
        layout.addWidget(self.field_combo, 1, 1, 1, 2)
        layout.addWidget(self.selected_only_check, 2, 1)
        layout.addWidget(self.dissolve_check, 3, 1)
        layout.addWidget(self.validate_area_button, 4, 1)
        layout.addWidget(self.area_status, 5, 0, 1, 4)
        layout.setColumnStretch(2, 1)
        layout.setRowStretch(6, 1)
        return widget

    def _source_tab(self):
        widget = QWidget()
        layout = QVBoxLayout(widget)

        mode_group = QGroupBox("Modo de funcionamento")
        mode_layout = QGridLayout(mode_group)
        self.mode_combo = QComboBox()
        self.mode_combo.addItems(["Local - rasters do usuario", "Online - Sentinel-2 L2A"])
        self.cloud_spin = QSpinBox()
        self.cloud_spin.setRange(0, 100)
        self.cloud_spin.setValue(30)
        self.scl_mask_check = QCheckBox("Aplicar mascara automatica de nuvem (SCL) nas estatisticas")
        self.scl_mask_check.setChecked(False)
        self.test_online_button = QPushButton("Testar conexao")
        self.search_online_button = QPushButton("Buscar imagens disponiveis")
        mode_layout.addWidget(QLabel("Modo"), 0, 0)
        mode_layout.addWidget(self.mode_combo, 0, 1)
        mode_layout.addWidget(QLabel("Nuvem maxima (%)"), 1, 0)
        mode_layout.addWidget(self.cloud_spin, 1, 1)
        mode_layout.addWidget(self.scl_mask_check, 2, 0, 1, 2)
        mode_layout.addWidget(self.test_online_button, 3, 0)
        mode_layout.addWidget(self.search_online_button, 3, 1)
        layout.addWidget(mode_group)

        self.online_group = QGroupBox("Cenas online encontradas")
        online_layout = QVBoxLayout(self.online_group)
        self.cloud_help_label = QLabel(
            "A cobertura de nuvens informada pelo catalogo representa a cena inteira. "
            "Cenas com cobertura alta exigem mais cautela na interpretacao."
        )
        self.cloud_help_label.setWordWrap(True)
        self.cloud_help_label.setStyleSheet("color:#475467;padding:4px 0;")
        self.cloud_summary_label = QLabel("Busque imagens para avaliar a cobertura de nuvens.")
        self.cloud_summary_label.setWordWrap(True)
        self.cloud_summary_label.setStyleSheet("font-weight:bold;color:#344054;padding:4px 0;")
        online_layout.addWidget(self.cloud_help_label)
        online_layout.addWidget(self.cloud_summary_label)
        self.online_table = QTableWidget(0, 8)
        self.online_table.setHorizontalHeaderLabels(
            [
                "Usar",
                "Data da imagem",
                "Data solicitada",
                "Sensor",
                "Nuvem na cena (%)",
                "Situacao",
                "ID da cena",
                "Bandas",
            ]
        )
        for column in (0, 1, 2, 3, 4, 5, 7):
            self.online_table.horizontalHeader().setSectionResizeMode(column, QHeaderView.ResizeToContents)
        self.online_table.horizontalHeader().setSectionResizeMode(6, QHeaderView.Stretch)
        self.online_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.online_table.setAlternatingRowColors(True)
        online_layout.addWidget(self.online_table)
        layout.addWidget(self.online_group)

        self.local_group = QGroupBox("Rasters locais")
        local_layout = QVBoxLayout(self.local_group)
        folder_row = QHBoxLayout()
        self.folder_line = QLineEdit()
        self.folder_button = QPushButton("Escolher pasta")
        self.scan_button = QPushButton("Detectar rasters")
        self.add_files_button = QPushButton("Adicionar arquivos")
        self.recursive_check = QCheckBox("Buscar subpastas")
        folder_row.addWidget(self.folder_line, 1)
        folder_row.addWidget(self.folder_button)
        folder_row.addWidget(self.scan_button)
        folder_row.addWidget(self.add_files_button)
        folder_row.addWidget(self.recursive_check)
        local_layout.addLayout(folder_row)

        self.local_table = QTableWidget(0, 6)
        self.local_table.setHorizontalHeaderLabels(["Usar", "Arquivo", "Data", "Tipo", "Indice", "Banda"])
        self.local_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
        self.local_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.local_table.setAlternatingRowColors(True)
        local_layout.addWidget(self.local_table, 1)
        layout.addWidget(self.local_group, 1)
        source_actions = QHBoxLayout()
        source_actions.addStretch(1)
        self.continue_to_results_button = QPushButton("Continuar para processamento")
        source_actions.addWidget(self.continue_to_results_button)
        layout.addLayout(source_actions)
        return widget

    def _date_index_tab(self):
        widget = QWidget()
        layout = QVBoxLayout(widget)

        dates_group = QGroupBox("Datas solicitadas")
        dates_layout = QGridLayout(dates_group)
        self.specific_dates_radio = QRadioButton("Datas especificas")
        self.specific_dates_radio.setChecked(True)
        self.specific_dates_edit = QTextEdit()
        self.specific_dates_edit.setPlaceholderText("10/12/2023\n14/01/2024\n13/02/2024")
        self.interval_radio = QRadioButton("Intervalo de datas")
        self.start_date_edit = QDateEdit(QDate.currentDate().addMonths(-1))
        self.end_date_edit = QDateEdit(QDate.currentDate())
        self.start_date_edit.setCalendarPopup(True)
        self.end_date_edit.setCalendarPopup(True)
        self.interval_days_spin = QSpinBox()
        self.interval_days_spin.setRange(1, 365)
        self.interval_days_spin.setValue(10)
        self.tolerance_spin = QSpinBox()
        self.tolerance_spin.setRange(0, 60)
        self.tolerance_spin.setValue(5)
        dates_layout.addWidget(self.specific_dates_radio, 0, 0)
        dates_layout.addWidget(self.specific_dates_edit, 1, 0, 4, 2)
        dates_layout.addWidget(self.interval_radio, 0, 2)
        dates_layout.addWidget(QLabel("Inicial"), 1, 2)
        dates_layout.addWidget(self.start_date_edit, 1, 3)
        dates_layout.addWidget(QLabel("Final"), 2, 2)
        dates_layout.addWidget(self.end_date_edit, 2, 3)
        dates_layout.addWidget(QLabel("Intervalo (dias)"), 3, 2)
        dates_layout.addWidget(self.interval_days_spin, 3, 3)
        dates_layout.addWidget(QLabel("Tolerancia (dias)"), 4, 2)
        dates_layout.addWidget(self.tolerance_spin, 4, 3)
        layout.addWidget(dates_group)

        index_group = QGroupBox("Indices")
        index_layout = QGridLayout(index_group)
        for column, index_name in enumerate(INDEX_NAMES):
            check = QCheckBox(index_name)
            check.setChecked(True)
            self.index_checks[index_name] = check
            index_layout.addWidget(check, 0, column)
        self.ndre_b8a_check = QCheckBox("NDRE usando B8A + B05")
        self.savi_l_spin = QDoubleSpinBox()
        self.savi_l_spin.setDecimals(2)
        self.savi_l_spin.setRange(0.0, 2.0)
        self.savi_l_spin.setSingleStep(0.1)
        self.savi_l_spin.setValue(0.5)
        index_layout.addWidget(self.ndre_b8a_check, 1, 0, 1, 2)
        index_layout.addWidget(QLabel("L do SAVI"), 1, 2)
        index_layout.addWidget(self.savi_l_spin, 1, 3)
        layout.addWidget(index_group)
        layout.addStretch(1)
        date_actions = QHBoxLayout()
        date_actions.addStretch(1)
        self.continue_to_images_button = QPushButton("Continuar para imagens")
        date_actions.addWidget(self.continue_to_images_button)
        layout.addLayout(date_actions)
        return widget

    def _results_tab(self):
        widget = QWidget()
        layout = QVBoxLayout(widget)

        config_group = QGroupBox("Saida e processamento")
        config_layout = QGridLayout(config_group)
        self.output_line = QLineEdit(str(Path.home() / "GeoZonalSat_Projeto"))
        self.output_button = QPushButton("Escolher pasta")
        self.resampling_combo = QComboBox()
        self.resampling_combo.addItems(["bilinear", "nearest", "cubic"])
        self.load_rasters_check = QCheckBox("Carregar rasters no QGIS ao concluir")
        self.load_rasters_check.setChecked(True)
        self.export_maps_check = QCheckBox("Gerar mapas PNG rapidos por data e indice")
        self.export_maps_check.setChecked(True)
        self.map_margin_spin = QSpinBox()
        self.map_margin_spin.setRange(10, 300)
        self.map_margin_spin.setValue(80)
        self.map_margin_spin.setSuffix("%")
        self.process_button = QPushButton("Iniciar processamento")
        self.export_csv_button = QPushButton("Exportar CSV")
        self.export_csv_button.setEnabled(False)
        self.export_xlsx_button = QPushButton("Exportar XLSX")
        self.export_xlsx_button.setEnabled(False)
        self.export_pdf_button = QPushButton("Exportar PDF")
        self.export_pdf_button.setEnabled(False)
        self.open_report_button = QPushButton("Ver relatorio no QGIS")
        self.open_report_button.setEnabled(False)
        config_layout.addWidget(QLabel("Pasta de saida"), 0, 0)
        config_layout.addWidget(self.output_line, 0, 1, 1, 3)
        config_layout.addWidget(self.output_button, 0, 4)
        config_layout.addWidget(QLabel("Reamostragem"), 1, 0)
        config_layout.addWidget(self.resampling_combo, 1, 1)
        config_layout.addWidget(self.load_rasters_check, 1, 2)
        config_layout.addWidget(self.process_button, 1, 3)
        config_layout.addWidget(self.export_csv_button, 1, 4)
        config_layout.addWidget(self.open_report_button, 1, 5)
        config_layout.addWidget(self.export_maps_check, 2, 0, 1, 2)
        config_layout.addWidget(QLabel("Margem do mapa"), 2, 2)
        config_layout.addWidget(self.map_margin_spin, 2, 3)
        config_layout.addWidget(self.export_xlsx_button, 2, 4)
        config_layout.addWidget(self.export_pdf_button, 2, 5)
        layout.addWidget(config_group)

        self.quality_status_label = QLabel("Nenhum erro detectado. Execute o processamento para validar os resultados.")
        self.quality_status_label.setWordWrap(True)
        self.quality_status_label.setStyleSheet(
            "background:#ecfdf3;color:#166534;border:1px solid #86efac;padding:8px;"
        )
        layout.addWidget(self.quality_status_label)

        self.results_table = QTableWidget(0, len(RESULT_FIELDS))
        self.results_table.setHorizontalHeaderLabels(RESULT_FIELDS)
        self.results_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeToContents)
        self.results_table.horizontalHeader().setSectionResizeMode(RESULT_FIELDS.index("caminho_raster_saida"), QHeaderView.Stretch)
        self.results_table.setAlternatingRowColors(True)
        self.results_table.setSortingEnabled(True)
        layout.addWidget(self.results_table, 1)

        self.log_box = QPlainTextEdit()
        self.log_box.setReadOnly(True)
        self.log_box.setMaximumHeight(120)
        layout.addWidget(self.log_box)
        return widget

    def _report_tab(self):
        widget = QWidget()
        self.report_tab_widget = widget
        layout = QVBoxLayout(widget)

        top_row = QHBoxLayout()
        self.report_status_label = QLabel("Nenhum relatorio gerado ainda.")
        self.refresh_report_button = QPushButton("Atualizar relatorio")
        self.refresh_report_button.setEnabled(False)
        self.open_external_report_button = QPushButton("Abrir fora do QGIS")
        self.open_external_report_button.setEnabled(False)
        top_row.addWidget(self.report_status_label, 1)
        top_row.addWidget(self.refresh_report_button)
        top_row.addWidget(self.open_external_report_button)
        layout.addLayout(top_row)

        self.report_view = QTextBrowser()
        self.report_view.setOpenLinks(False)
        self.report_view.setOpenExternalLinks(False)
        self.report_view.setHtml(
            "<h2>Relatorio GeoZonalSat</h2>"
            "<p>Depois do processamento, o relatorio aparecera aqui dentro do QGIS.</p>"
        )
        layout.addWidget(self.report_view, 1)
        return widget

    def _connect_signals(self):
        self.layer_combo.layerChanged.connect(self._on_layer_changed)
        self.load_area_button.clicked.connect(self._load_external_area)
        self.validate_area_button.clicked.connect(self._validate_area)
        self.folder_button.clicked.connect(self._choose_folder)
        self.scan_button.clicked.connect(self._scan_folder)
        self.add_files_button.clicked.connect(self._add_raster_files)
        self.test_online_button.clicked.connect(self._test_online_connection)
        self.search_online_button.clicked.connect(self._search_online_scenes)
        self.mode_combo.currentIndexChanged.connect(self._update_source_mode)
        self.continue_to_images_button.clicked.connect(self._continue_to_images)
        self.continue_to_results_button.clicked.connect(self._continue_to_results)
        self.output_button.clicked.connect(self._choose_output_folder)
        self.process_button.clicked.connect(self._start_processing)
        self.export_csv_button.clicked.connect(self._export_csv_as)
        self.export_xlsx_button.clicked.connect(self._export_xlsx_as)
        self.export_pdf_button.clicked.connect(self._export_pdf_report)
        self.open_report_button.clicked.connect(self._open_report)
        self.refresh_report_button.clicked.connect(self._open_report)
        self.open_external_report_button.clicked.connect(self._open_report_external)
        self.report_view.anchorClicked.connect(self._handle_report_link)

    def _on_layer_changed(self, layer):
        self.field_combo.setLayer(layer)

    def _update_source_mode(self):
        online = self.mode_combo.currentIndex() == 1
        self.online_group.setVisible(online)
        self.local_group.setVisible(not online)
        self.cloud_spin.setEnabled(online)
        self.scl_mask_check.setEnabled(online)
        self.test_online_button.setEnabled(online)
        self.search_online_button.setEnabled(online)

    def _load_external_area(self):
        path, _ = QFileDialog.getOpenFileName(
            self,
            "Carregar area",
            "",
            "Vetores (*.shp *.gpkg *.kml *.geojson);;Todos os arquivos (*.*)",
        )
        if not path:
            return
        try:
            layer = self.area_manager.load_external_layer(path)
            self.layer_combo.setLayer(layer)
            self._add_message(f"Area carregada: {Path(path).name}")
        except Exception as exc:
            QMessageBox.warning(self, "GeoZonalSat", str(exc))

    def _validate_area(self):
        try:
            layer = self.layer_combo.currentLayer()
            summary = self.area_manager.validate_layer(layer, self.selected_only_check.isChecked())
            self.area_status.setText(summary.message)
            self._add_message(summary.message)
            self.tabs.setCurrentIndex(1)
        except Exception as exc:
            self.area_status.setText(str(exc))
            QMessageBox.warning(self, "GeoZonalSat", str(exc))

    def _continue_to_images(self):
        if not self._selected_indices():
            QMessageBox.warning(self, "GeoZonalSat", "Selecione pelo menos um indice.")
            return
        try:
            dates = self._requested_dates()
        except Exception as exc:
            QMessageBox.warning(self, "GeoZonalSat", str(exc))
            return
        if not dates:
            QMessageBox.warning(self, "GeoZonalSat", "Informe pelo menos uma data antes de continuar.")
            return
        self.tabs.setCurrentIndex(2)

    def _continue_to_results(self):
        if self.mode_combo.currentIndex() == 1:
            if not self._selected_online_scenes():
                QMessageBox.warning(
                    self,
                    "GeoZonalSat",
                    "Busque as imagens e selecione pelo menos uma cena antes de continuar.",
                )
                return
        elif self.local_table.rowCount() == 0:
            QMessageBox.warning(self, "GeoZonalSat", "Adicione ou detecte pelo menos um raster local.")
            return
        self.tabs.setCurrentIndex(3)

    def _choose_folder(self):
        folder = QFileDialog.getExistingDirectory(self, "Escolher pasta com rasters", self.folder_line.text())
        if folder:
            self.folder_line.setText(folder)

    def _choose_output_folder(self):
        folder = QFileDialog.getExistingDirectory(self, "Escolher pasta de saida", self.output_line.text())
        if folder:
            self.output_line.setText(folder)

    def _scan_folder(self):
        folder = self.folder_line.text().strip()
        if not folder:
            self._choose_folder()
            folder = self.folder_line.text().strip()
        if not folder:
            return
        try:
            items = LocalRasterLoader.scan_folder(folder, self.recursive_check.isChecked())
            for item in items:
                self._append_local_item(item)
            self._add_message(f"{len(items)} raster(s) detectado(s).")
        except Exception as exc:
            QMessageBox.warning(self, "GeoZonalSat", str(exc))

    def _add_raster_files(self):
        paths, _ = QFileDialog.getOpenFileNames(
            self,
            "Adicionar rasters",
            "",
            "Rasters (*.tif *.tiff *.vrt *.img *.jp2);;Todos os arquivos (*.*)",
        )
        for path in paths:
            self._append_local_item(LocalRasterLoader.from_path(path))
        if paths:
            self._add_message(f"{len(paths)} arquivo(s) adicionado(s).")

    def _append_local_item(self, item: LocalRasterItem):
        row = self.local_table.rowCount()
        self.local_table.insertRow(row)

        use_item = QTableWidgetItem("")
        use_item.setFlags(Qt.ItemIsEnabled | Qt.ItemIsUserCheckable | Qt.ItemIsSelectable)
        use_item.setCheckState(Qt.Checked if item.selected else Qt.Unchecked)
        self.local_table.setItem(row, 0, use_item)

        path_item = QTableWidgetItem(item.path)
        path_item.setFlags(path_item.flags() & ~Qt.ItemIsEditable)
        self.local_table.setItem(row, 1, path_item)

        date_item = QTableWidgetItem(item.date.isoformat() if item.date else "")
        self.local_table.setItem(row, 2, date_item)

        type_combo = QComboBox()
        type_combo.addItems(["Indice pronto", "Banda bruta"])
        type_combo.setCurrentIndex(0 if item.input_type == "index" else 1)
        self.local_table.setCellWidget(row, 3, type_combo)

        index_combo = QComboBox()
        index_combo.addItems([""] + list(INDEX_NAMES))
        if item.index_name:
            index_combo.setCurrentText(item.index_name.upper())
        self.local_table.setCellWidget(row, 4, index_combo)

        band_combo = QComboBox()
        band_combo.addItems([""] + list(RAW_BANDS))
        if item.band_name:
            band_combo.setCurrentText(item.band_name.upper())
        self.local_table.setCellWidget(row, 5, band_combo)

    def _test_online_connection(self):
        try:
            if self.online_provider.test_connection():
                QMessageBox.information(self, "GeoZonalSat", "Conexao com Earth Search STAC funcionando.")
                self._add_message("Conexao online validada: Earth Search STAC.")
            else:
                QMessageBox.warning(self, "GeoZonalSat", "O catalogo respondeu, mas nao retornou uma resposta esperada.")
        except Exception as exc:
            QMessageBox.warning(self, "GeoZonalSat", str(exc))

    def _search_online_scenes(self):
        try:
            dates = self._requested_dates()
            if not dates:
                raise ValueError("Informe datas especificas ou um intervalo de datas.")
            aoi_path = self._export_aoi_to_output("aoi_busca_online.gpkg")
            self.status_label.setText("Buscando cenas online...")
            self._add_message("Buscando cenas Sentinel-2 L2A no Earth Search STAC.")
            scenes = self.online_provider.search_images(
                aoi_path,
                dates,
                cloud_limit=self.cloud_spin.value(),
                tolerance_days=self.tolerance_spin.value(),
            )
            self.online_scenes = scenes
            self._populate_online_scenes(scenes)
            self.status_label.setText("Busca online concluida.")
            if scenes:
                self._add_message(f"{len(scenes)} cena(s) real(is) encontrada(s).")
                QMessageBox.information(self, "GeoZonalSat", f"{len(scenes)} cena(s) encontrada(s).")
            else:
                QMessageBox.information(self, "GeoZonalSat", "Nenhuma imagem encontrada para as datas e nuvem informadas.")
        except Exception as exc:
            self.status_label.setText("Erro na busca online.")
            QMessageBox.warning(self, "GeoZonalSat", str(exc))

    def _populate_online_scenes(self, scenes: List[Scene]):
        self.online_table.setRowCount(0)
        clouded = []
        for scene in scenes:
            row = self.online_table.rowCount()
            self.online_table.insertRow(row)
            use_item = QTableWidgetItem("")
            use_item.setFlags(Qt.ItemIsEnabled | Qt.ItemIsUserCheckable | Qt.ItemIsSelectable)
            use_item.setCheckState(Qt.Checked)
            self.online_table.setItem(row, 0, use_item)
            status, background, foreground = _cloud_classification(scene.cloud_percent)
            values = [scene.date, scene.requested_date, scene.sensor, f"{scene.cloud_percent:.2f}"]
            for offset, value in enumerate(values, start=1):
                item = QTableWidgetItem(str(value))
                item.setFlags(item.flags() & ~Qt.ItemIsEditable)
                self.online_table.setItem(row, offset, item)

            cloud_item = self.online_table.item(row, 4)
            cloud_item.setText(f"{scene.cloud_percent:.2f}%")
            cloud_item.setBackground(QColor(background))
            cloud_item.setForeground(QColor(foreground))
            cloud_item.setToolTip("Percentual de nuvens informado pelo catalogo para a cena inteira.")

            status_item = QTableWidgetItem(f"{status} COBERTURA")
            status_item.setFlags(status_item.flags() & ~Qt.ItemIsEditable)
            status_item.setBackground(QColor(background))
            status_item.setForeground(QColor(foreground))
            status_item.setToolTip("Classificacao da cobertura de nuvens da cena inteira.")
            self.online_table.setItem(row, 5, status_item)

            for column, value in ((6, scene.scene_id), (7, ", ".join(scene.available_bands))):
                item = QTableWidgetItem(str(value))
                item.setFlags(item.flags() & ~Qt.ItemIsEditable)
                self.online_table.setItem(row, column, item)
            if scene.cloud_percent > 10.0:
                clouded.append(f"{scene.date} ({scene.cloud_percent:.1f}%)")

        if not scenes:
            self.cloud_summary_label.setText("Nenhuma cena encontrada para os filtros informados.")
            self.cloud_summary_label.setStyleSheet("font-weight:bold;color:#9a3412;padding:4px 0;")
        elif clouded:
            self.cloud_summary_label.setText(
                "Cenas com cobertura alta de nuvens: " + ", ".join(clouded) + "."
            )
            self.cloud_summary_label.setStyleSheet("font-weight:bold;color:#b42318;padding:4px 0;")
        else:
            self.cloud_summary_label.setText("Todas as cenas encontradas apresentam cobertura baixa ou moderada.")
            self.cloud_summary_label.setStyleSheet("font-weight:bold;color:#166534;padding:4px 0;")

    def _selected_online_scenes(self) -> List[Scene]:
        scenes: List[Scene] = []
        for row, scene in enumerate(self.online_scenes):
            item = self.online_table.item(row, 0)
            if item and item.checkState() == Qt.Checked:
                scenes.append(scene)
        return scenes

    def _export_aoi_to_output(self, filename: str) -> str:
        layer = self.layer_combo.currentLayer()
        self.area_manager.validate_layer(layer, self.selected_only_check.isChecked())
        output_root = Path(self.output_line.text().strip() or str(Path.home() / "GeoZonalSat_Projeto"))
        output_root.mkdir(parents=True, exist_ok=True)
        output_path = str(output_root / "logs" / filename)
        field_name = self.field_combo.currentField() or None
        self.area_manager.export_aoi(
            layer,
            output_path,
            id_field=field_name,
            selected_only=self.selected_only_check.isChecked(),
            dissolve=self.dissolve_check.isChecked(),
        )
        return output_path

    def _start_processing(self):
        if self._task is not None:
            QMessageBox.information(self, "GeoZonalSat", "Ja existe um processamento em andamento.")
            return

        try:
            output_root = Path(self.output_line.text().strip() or str(Path.home() / "GeoZonalSat_Projeto"))
            output_root.mkdir(parents=True, exist_ok=True)
            aoi_path = self._export_aoi_to_output("aoi.gpkg")
            selected_indices = self._selected_indices()
            if not selected_indices:
                raise ValueError("Selecione pelo menos um indice.")

            mode = "online" if self.mode_combo.currentIndex() == 1 else "local"
            config = {
                "mode": mode,
                "output_root": str(output_root),
                "aoi_path": aoi_path,
                "selected_indices": selected_indices,
                "resampling": self.resampling_combo.currentText(),
                "savi_l": self.savi_l_spin.value(),
                "ndre_use_b8a": self.ndre_b8a_check.isChecked(),
                "requested_dates": self._requested_dates(),
                "tolerance_days": self.tolerance_spin.value(),
                "map_margin_percent": self.map_margin_spin.value(),
                "apply_scl_mask": self.scl_mask_check.isChecked(),
                "map_run_id": datetime.now().strftime("%Y%m%d_%H%M%S_%f"),
            }
            if mode == "online":
                scenes = self._selected_online_scenes()
                if not scenes:
                    raise ValueError("Nenhuma cena online selecionada. Clique em Buscar imagens disponiveis primeiro.")
                config["scenes"] = scenes
            else:
                items = self._items_from_table()
                if not any(item.selected for item in items):
                    raise ValueError("Nenhum raster selecionado para processamento.")
                config["items"] = items
        except Exception as exc:
            QMessageBox.warning(self, "GeoZonalSat", str(exc))
            return

        self.results = []
        self.result_paths = {}
        self.results_table.setRowCount(0)
        self.progress.setValue(0)
        self.process_button.setEnabled(False)
        self.export_csv_button.setEnabled(False)
        self.export_xlsx_button.setEnabled(False)
        self.export_pdf_button.setEnabled(False)
        self.open_report_button.setEnabled(False)
        self.refresh_report_button.setEnabled(False)
        self.open_external_report_button.setEnabled(False)
        self.report_status_label.setText("Processando. O relatorio sera exibido aqui ao final.")
        self.report_view.setHtml("<h2>Processando</h2><p>Aguarde a conclusao para visualizar o relatorio.</p>")
        self.status_label.setText("Processando...")
        self._add_message("Processamento iniciado.")

        self._task = LocalProcessingTask(config, self._processing_finished)
        self._task.progressChanged.connect(lambda value: self.progress.setValue(int(value)))
        QgsApplication.taskManager().addTask(self._task)

    def _items_from_table(self) -> List[LocalRasterItem]:
        items: List[LocalRasterItem] = []
        for row in range(self.local_table.rowCount()):
            selected = self.local_table.item(row, 0).checkState() == Qt.Checked
            path = self.local_table.item(row, 1).text()
            date_text = self.local_table.item(row, 2).text().strip()
            raster_date = DateManager.parse(date_text) if date_text else None
            input_type = "index" if self._combo_text(row, 3) == "Indice pronto" else "band"
            index_name = self._combo_text(row, 4) or None
            band_name = self._combo_text(row, 5) or None
            items.append(
                LocalRasterItem(
                    path=path,
                    date=raster_date,
                    index_name=index_name,
                    input_type=input_type,
                    band_name=band_name,
                    selected=selected,
                )
            )
        return items

    def _selected_indices(self) -> List[str]:
        return [name for name, check in self.index_checks.items() if check.isChecked()]

    def _requested_dates(self) -> List[date]:
        if self.specific_dates_radio.isChecked():
            return DateManager.parse_many(self.specific_dates_edit.toPlainText())
        start = self.start_date_edit.date().toPyDate()
        end = self.end_date_edit.date().toPyDate()
        return DateManager.interval(start, end, self.interval_days_spin.value())

    def _processing_finished(self, success: bool, result: Dict[str, object], exception: Optional[Exception], traceback_text: str):
        self._task = None
        self.process_button.setEnabled(True)
        if not success:
            self.status_label.setText("Erro no processamento.")
            self.quality_status_label.setText(
                "ERRO: o processamento foi interrompido. Consulte a mensagem exibida e o log abaixo."
            )
            self.quality_status_label.setStyleSheet(
                "background:#fef2f2;color:#991b1b;border:1px solid #fca5a5;padding:8px;"
            )
            self._add_message(traceback_text or str(exception))
            QMessageBox.critical(self, "GeoZonalSat", str(exception) if exception else "Erro no processamento.")
            return

        self.results = list(result.get("rows", []))
        self.result_paths = {
            "csv_path": str(result.get("csv_path", "")),
            "temporal_csv_path": str(result.get("temporal_csv_path", "")),
            "report_path": str(result.get("report_path", "")),
        }
        self._populate_results(self.results)
        self.export_csv_button.setEnabled(True)
        self.export_xlsx_button.setEnabled(True)
        self.export_pdf_button.setEnabled(True)
        self.open_report_button.setEnabled(False)
        self.refresh_report_button.setEnabled(False)
        self.open_external_report_button.setEnabled(False)

        map_paths: List[Dict[str, object]] = []
        if self.export_maps_check.isChecked():
            self.status_label.setText("Gerando mapas...")
            map_paths = self._export_result_maps(result)

        if self.load_rasters_check.isChecked():
            for raster in result.get("rasters_to_load", []):
                try:
                    layer = self.style_manager.load_index_raster(
                        raster["path"],
                        raster.get("indice", ""),
                        raster.get("data", ""),
                        raster.get("area", ""),
                    )
                    if layer is None:
                        message = f"ERRO AO CARREGAR RASTER: {raster.get('path')}"
                        result.setdefault("warnings", []).append(message)
                        self._add_message(message)
                except Exception as exc:
                    message = (
                        f"ERRO AO CARREGAR RASTER: {raster.get('indice')} em {raster.get('data')}: {exc}"
                    )
                    result.setdefault("warnings", []).append(message)
                    self._add_message(message)

        report_ready = False
        report_path = self.result_paths.get("report_path", "")
        if report_path:
            try:
                ReportGenerator().generate_html(
                    self.results,
                    list(result.get("temporal_rows", [])),
                    report_path,
                    notes="\n".join(str(item) for item in result.get("warnings", [])),
                    map_paths=map_paths,
                )
                report_ready = Path(report_path).exists()
            except Exception as exc:
                message = f"ERRO NO RELATORIO: nao foi possivel gerar o HTML: {exc}"
                result.setdefault("warnings", []).append(message)
                self._add_message(message)
                self.result_paths["report_path"] = ""

        self.open_report_button.setEnabled(report_ready)
        self.refresh_report_button.setEnabled(report_ready)
        self.open_external_report_button.setEnabled(report_ready)

        for warning in result.get("warnings", []):
            self._add_message(warning)
        self._update_quality_status(list(result.get("warnings", [])), report_ready)
        self.progress.setValue(100)
        self.status_label.setText("Processamento concluido.")
        self._load_report_inside_qgis(self.result_paths.get("report_path", ""))
        if report_ready:
            self.tabs.setCurrentWidget(self.report_tab_widget)
        else:
            self.tabs.setCurrentIndex(3)
        self._add_message(f"CSV: {self.result_paths.get('csv_path')}")
        self._add_message(f"Relatorio: {self.result_paths.get('report_path')}")
        valid_maps = [item for item in map_paths if item.get("path") and Path(str(item["path"])).exists()]
        if valid_maps:
            self._add_message(f"Mapas: {Path(str(valid_maps[0]['path'])).parent.parent}")
        issue_count = len(list(result.get("warnings", [])))
        if issue_count:
            destination = "Consulte a validacao no relatorio." if report_ready else "Consulte os avisos na aba de resultados."
            QMessageBox.warning(
                self,
                "GeoZonalSat",
                f"Processamento concluido com {issue_count} alerta(s). {destination}",
            )
        else:
            QMessageBox.information(self, "GeoZonalSat", "Processamento concluido e validado sem erros.")

    def _export_result_maps(self, result: Dict[str, object]) -> List[Dict[str, object]]:
        output_root = Path(str(result.get("output_root") or self.output_line.text() or Path.home()))
        aoi_path = str(result.get("aoi_path") or "")
        if not aoi_path or not Path(aoi_path).exists():
            message = "ERRO NO MAPA: area de interesse nao encontrada."
            result.setdefault("warnings", []).append(message)
            self._add_message(message)
            return []

        map_folder = output_root / "relatorios" / "mapas"
        map_paths: List[Dict[str, object]] = []
        rasters = list(result.get("rasters_to_load", []))
        total = max(1, len(rasters))
        for index, raster in enumerate(rasters, start=1):
            try:
                self.status_label.setText(f"Gerando mapa {index}/{total}...")
                QgsApplication.processEvents()
                exported = self.map_exporter.export_index_map(
                    raster,
                    aoi_path,
                    str(map_folder),
                    margin_percent=self.map_margin_spin.value(),
                )
                if exported:
                    map_paths.append(exported)
                    self._add_message(f"Mapa gerado: {exported['path']}")
                    QgsApplication.processEvents()
                else:
                    message = (
                        f"ERRO NO MAPA: {raster.get('indice')} em {raster.get('data')}: "
                        "raster de origem nao encontrado."
                    )
                    result.setdefault("warnings", []).append(message)
                    map_paths.append(
                        {
                            "indice": raster.get("indice", ""),
                            "data": raster.get("data", ""),
                            "area": raster.get("area", ""),
                            "error": message,
                        }
                    )
            except Exception as exc:
                message = f"ERRO NO MAPA: {raster.get('indice')} em {raster.get('data')}: {exc}"
                result.setdefault("warnings", []).append(message)
                self._add_message(message)
                map_paths.append(
                    {
                        "indice": raster.get("indice", ""),
                        "data": raster.get("data", ""),
                        "area": raster.get("area", ""),
                        "error": str(exc),
                    }
                )
        return map_paths

    def _update_quality_status(self, messages: List[object], report_available: bool = True):
        issues = [str(message) for message in messages if str(message).strip()]
        errors = [message for message in issues if message.upper().startswith("ERRO")]
        destination = "Abra o relatorio para ver os itens afetados." if report_available else "Consulte o log abaixo."
        if errors:
            self.quality_status_label.setText(
                f"Foram detectados {len(errors)} erro(s) e {len(issues) - len(errors)} alerta(s). "
                f"{destination}"
            )
            self.quality_status_label.setStyleSheet(
                "background:#fef2f2;color:#991b1b;border:1px solid #fca5a5;padding:8px;"
            )
        elif issues:
            self.quality_status_label.setText(
                f"Processamento concluido com {len(issues)} alerta(s) de qualidade. {destination}"
            )
            self.quality_status_label.setStyleSheet(
                "background:#fefce8;color:#854d0e;border:1px solid #facc15;padding:8px;"
            )
        else:
            self.quality_status_label.setText("Processamento concluido sem erros ou alertas detectados.")
            self.quality_status_label.setStyleSheet(
                "background:#ecfdf3;color:#166534;border:1px solid #86efac;padding:8px;"
            )

    def _populate_results(self, rows: List[Dict[str, object]]):
        self.results_table.setSortingEnabled(False)
        self.results_table.setRowCount(0)
        for row_data in rows:
            row = self.results_table.rowCount()
            self.results_table.insertRow(row)
            for column, field in enumerate(RESULT_FIELDS):
                item = QTableWidgetItem(self._format_value(row_data.get(field)))
                self.results_table.setItem(row, column, item)
        self.results_table.setSortingEnabled(True)

    def _export_csv_as(self):
        if not self.results:
            return
        path, _ = QFileDialog.getSaveFileName(
            self,
            "Exportar CSV",
            self.result_paths.get("csv_path") or str(Path.home() / "estatisticas_geozonalsat.csv"),
            "CSV (*.csv)",
        )
        if not path:
            return
        self.export_manager.save_csv(self.results, path)
        self._add_message(f"CSV exportado: {path}")

    def _export_xlsx_as(self):
        if not self.results:
            return
        path, _ = QFileDialog.getSaveFileName(
            self,
            "Exportar XLSX",
            str(Path(self.result_paths.get("csv_path") or str(Path.home())).with_suffix(".xlsx")),
            "Excel (*.xlsx)",
        )
        if not path:
            return
        try:
            self.export_manager.save_xlsx(self.results, path)
            self._add_message(f"XLSX exportado: {path}")
        except Exception as exc:
            QMessageBox.warning(self, "GeoZonalSat", str(exc))

    def _export_pdf_report(self):
        html_path = self.result_paths.get("report_path")
        if not html_path or not Path(html_path).exists():
            QMessageBox.information(self, "GeoZonalSat", "Gere o relatorio HTML antes de exportar PDF.")
            return
        default_path = str(Path(html_path).with_suffix(".pdf"))
        path, _ = QFileDialog.getSaveFileName(self, "Exportar PDF", default_path, "PDF (*.pdf)")
        if not path:
            return
        try:
            ReportGenerator().generate_pdf_from_html(html_path, path)
            self._add_message(f"PDF exportado: {path}")
        except Exception as exc:
            QMessageBox.warning(self, "GeoZonalSat", f"Nao foi possivel exportar PDF: {exc}")

    def _open_report(self):
        path = self.result_paths.get("report_path")
        if path and Path(path).exists():
            self._load_report_inside_qgis(path)
            self.tabs.setCurrentWidget(self.report_tab_widget)
        else:
            QMessageBox.information(self, "GeoZonalSat", "Nenhum relatorio HTML foi gerado ainda.")

    def _open_report_external(self):
        path = self.result_paths.get("report_path")
        if path and Path(path).exists():
            QDesktopServices.openUrl(QUrl.fromLocalFile(path))
        else:
            QMessageBox.information(self, "GeoZonalSat", "Nenhum relatorio HTML foi gerado ainda.")

    def _handle_report_link(self, url: QUrl):
        if url.isRelative():
            url = self.report_view.document().baseUrl().resolved(url)
        if not url.isLocalFile():
            QDesktopServices.openUrl(url)
            return
        source = Path(url.toLocalFile())
        if source.suffix.lower() != ".png":
            QDesktopServices.openUrl(url)
            return
        if not source.exists():
            QMessageBox.warning(self, "GeoZonalSat", f"O mapa nao foi encontrado:\n{source}")
            return

        downloads = Path.home() / "Downloads"
        initial = downloads / source.name if downloads.exists() else Path.home() / source.name
        destination, _ = QFileDialog.getSaveFileName(
            self,
            "Baixar mapa PNG",
            str(initial),
            "Imagem PNG (*.png)",
        )
        if not destination:
            return
        destination_path = Path(destination)
        if destination_path.suffix.lower() != ".png":
            destination_path = destination_path.with_suffix(".png")
        try:
            if source.resolve() != destination_path.resolve():
                destination_path.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, destination_path)
            self._add_message(f"Mapa baixado: {destination_path}")
            QMessageBox.information(self, "GeoZonalSat", f"Mapa salvo em:\n{destination_path}")
        except Exception as exc:
            QMessageBox.warning(self, "GeoZonalSat", f"Nao foi possivel baixar o mapa: {exc}")

    def _load_report_inside_qgis(self, path: str):
        if not path or not Path(path).exists():
            self.report_status_label.setText("Nenhum relatorio gerado ainda.")
            self.report_view.setHtml(
                "<h2>Relatorio GeoZonalSat</h2>"
                "<p>Depois do processamento, o relatorio aparecera aqui dentro do QGIS.</p>"
            )
            return
        report_path = Path(path)
        document = self.report_view.document()
        document.clear()
        document.setBaseUrl(QUrl.fromLocalFile(str(report_path.parent.resolve()) + "/"))
        document.setHtml(report_path.read_text(encoding="utf-8"))
        self.report_view.verticalScrollBar().setValue(0)
        self.report_view.horizontalScrollBar().setValue(0)
        QTimer.singleShot(0, lambda: self.report_view.verticalScrollBar().setValue(0))
        self.report_status_label.setText(f"Relatorio carregado: {report_path}")

    def _restore_existing_report(self):
        output_root = Path(self.output_line.text().strip() or str(Path.home() / "GeoZonalSat_Projeto"))
        report_path = output_root / "relatorios" / "relatorio_geozonalsat.html"
        if not report_path.exists():
            return
        self.result_paths.update(
            {
                "csv_path": str(output_root / "tabelas" / "estatisticas_completas.csv"),
                "temporal_csv_path": str(output_root / "tabelas" / "tabela_temporal.csv"),
                "report_path": str(report_path),
            }
        )
        self.open_report_button.setEnabled(True)
        self.refresh_report_button.setEnabled(True)
        self.open_external_report_button.setEnabled(True)
        self._load_report_inside_qgis(str(report_path))

    def _combo_text(self, row: int, column: int) -> str:
        widget = self.local_table.cellWidget(row, column)
        if isinstance(widget, QComboBox):
            return widget.currentText().strip()
        return ""

    def _add_message(self, message: str):
        self.log_box.appendPlainText(message)

    @staticmethod
    def _format_value(value) -> str:
        if value is None:
            return ""
        if isinstance(value, float):
            return f"{value:.6g}"
        return str(value)
