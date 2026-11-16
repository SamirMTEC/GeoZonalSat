from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence

from .date_manager import DateManager


INDEX_NAMES = ("NDVI", "GNDVI", "NDRE", "NDMI", "SAVI", "EVI")
RAW_BANDS = ("B02", "B03", "B04", "B05", "B08", "B8A", "B11")
RASTER_SUFFIXES = {".tif", ".tiff", ".vrt", ".img", ".jp2"}


@dataclass
class LocalRasterItem:
    path: str
    date: Optional[date]
    index_name: Optional[str] = None
    input_type: str = "index"
    band_name: Optional[str] = None
    selected: bool = True

    @property
    def label(self) -> str:
        return Path(self.path).name


class LocalRasterLoader:
    """Find local rasters and infer date/index/band from common file names."""

    @staticmethod
    def scan_folder(folder: str, recursive: bool = False) -> List[LocalRasterItem]:
        root = Path(folder)
        if not root.exists():
            raise FileNotFoundError(f"Pasta nao encontrada: {folder}")
        iterator = root.rglob("*") if recursive else root.glob("*")
        items: List[LocalRasterItem] = []
        for path in iterator:
            if path.is_file() and path.suffix.lower() in RASTER_SUFFIXES:
                items.append(LocalRasterLoader.from_path(str(path)))
        return sorted(items, key=lambda item: item.path.lower())

    @staticmethod
    def from_path(path: str) -> LocalRasterItem:
        name = Path(path).stem.upper()
        raster_date = DateManager.detect_in_text(path)
        index_name = LocalRasterLoader.detect_index(name)
        band_name = LocalRasterLoader.detect_band(name)
        if index_name:
            return LocalRasterItem(path=path, date=raster_date, index_name=index_name, input_type="index")
        if band_name:
            return LocalRasterItem(path=path, date=raster_date, band_name=band_name, input_type="band")
        return LocalRasterItem(path=path, date=raster_date, input_type="index")

    @staticmethod
    def detect_index(text: str) -> Optional[str]:
        upper = text.upper()
        for index_name in INDEX_NAMES:
            if re.search(rf"(^|[^A-Z0-9]){index_name}([^A-Z0-9]|$)", upper):
                return index_name
        return None

    @staticmethod
    def detect_band(text: str) -> Optional[str]:
        upper = text.upper()
        for band_name in sorted(RAW_BANDS, key=len, reverse=True):
            if re.search(rf"(^|[^A-Z0-9]){band_name}([^A-Z0-9]|$)", upper):
                return band_name
        return None

    @staticmethod
    def group_bands(items: Sequence[LocalRasterItem]) -> Dict[str, Dict[str, str]]:
        grouped: Dict[str, Dict[str, str]] = {}
        for item in items:
            if not item.selected or item.input_type != "band" or not item.band_name:
                continue
            key = item.date.isoformat() if item.date else "sem_data"
            grouped.setdefault(key, {})[item.band_name.upper()] = item.path
        return grouped

    @staticmethod
    def filter_ready_index_items(items: Iterable[LocalRasterItem]) -> List[LocalRasterItem]:
        ready: List[LocalRasterItem] = []
        for item in items:
            if item.selected and item.input_type == "index" and item.index_name and item.date:
                ready.append(item)
        return ready

