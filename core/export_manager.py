from __future__ import annotations

import csv
from collections import defaultdict
from pathlib import Path
from typing import Dict, Iterable, List, Mapping, Sequence

from .zonal_statistics import RESULT_FIELDS


class ExportManager:
    """Export statistics tables to common tabular formats."""

    def save_csv(self, rows: Sequence[Mapping[str, object]], output_path: str, fields: Sequence[str] = RESULT_FIELDS) -> str:
        Path(output_path).parent.mkdir(parents=True, exist_ok=True)
        with open(output_path, "w", newline="", encoding="utf-8-sig") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(fields), extrasaction="ignore")
            writer.writeheader()
            for row in rows:
                writer.writerow({field: _format_value(row.get(field)) for field in fields})
        return output_path

    def save_xlsx(self, rows: Sequence[Mapping[str, object]], output_path: str) -> str:
        try:
            import pandas as pd
        except Exception as exc:
            raise RuntimeError("Pandas/openpyxl nao estao disponiveis para exportar XLSX.") from exc
        Path(output_path).parent.mkdir(parents=True, exist_ok=True)
        frame = pd.DataFrame(list(rows))
        frame.to_excel(output_path, index=False)
        return output_path

    def temporal_table(self, rows: Sequence[Mapping[str, object]]) -> List[Dict[str, object]]:
        grouped: Dict[tuple, Dict[str, List[float]]] = defaultdict(lambda: defaultdict(list))
        labels: Dict[tuple, Dict[str, object]] = {}
        for row in rows:
            key = (
                row.get("id_area", ""),
                row.get("nome_area", ""),
                row.get("data_imagem") or row.get("data_solicitada") or "",
            )
            labels[key] = {
                "id_area": key[0],
                "nome_area": key[1],
                "data": key[2],
            }
            index_name = str(row.get("indice") or "").upper()
            value = row.get("media")
            if index_name and isinstance(value, (int, float)):
                grouped[key][index_name].append(float(value))

        output: List[Dict[str, object]] = []
        for key in sorted(labels, key=lambda item: (str(item[0]), str(item[2]))):
            row = dict(labels[key])
            for index_name, values in sorted(grouped[key].items()):
                row[f"{index_name}_medio"] = sum(values) / len(values) if values else None
            output.append(row)
        return output

    def save_temporal_csv(self, rows: Sequence[Mapping[str, object]], output_path: str) -> str:
        fields = self._fields_for_temporal(rows)
        Path(output_path).parent.mkdir(parents=True, exist_ok=True)
        with open(output_path, "w", newline="", encoding="utf-8-sig") as handle:
            writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
            writer.writeheader()
            for row in rows:
                writer.writerow({field: _format_value(row.get(field)) for field in fields})
        return output_path

    @staticmethod
    def _fields_for_temporal(rows: Sequence[Mapping[str, object]]) -> List[str]:
        fields = ["id_area", "nome_area", "data"]
        dynamic = sorted({field for row in rows for field in row if field.endswith("_medio")})
        return fields + dynamic


def _format_value(value):
    if value is None:
        return ""
    if isinstance(value, float):
        return f"{value:.10g}"
    return value

