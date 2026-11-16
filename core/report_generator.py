from __future__ import annotations

import html
import os
from pathlib import Path
from typing import Dict, Iterable, List, Mapping, Sequence, Tuple


class ReportGenerator:
    """Generate a simple self-contained HTML report."""

    def generate_html(
        self,
        rows: Sequence[Mapping[str, object]],
        temporal_rows: Sequence[Mapping[str, object]],
        output_path: str,
        title: str = "GeoZonalSat - Relatório",
        notes: str = "",
        map_paths: Sequence[Mapping[str, object]] = (),
    ) -> str:
        Path(output_path).parent.mkdir(parents=True, exist_ok=True)
        html_text = "\n".join(
            [
                "<!doctype html>",
                "<html lang=\"pt-BR\">",
                "<head>",
                "<meta charset=\"utf-8\">",
                f"<title>{html.escape(title)}</title>",
                "<style>",
                "body{font-family:Arial,sans-serif;margin:32px;color:#1f2933;max-width:100%;overflow-x:hidden;}",
                "h1,h2{margin:0 0 12px 0;} section{margin:24px 0;}",
                ".table-wrap{width:100%;max-width:100%;overflow-x:auto;}",
                "table{border-collapse:collapse;width:100%;table-layout:fixed;font-size:11px;}",
                "th,td{border:1px solid #d8dee9;padding:5px;text-align:left;vertical-align:top;word-break:break-word;overflow-wrap:anywhere;white-space:normal;}",
                "th{background:#eef2f7;} .warn{color:#9a3412;font-weight:bold;}",
                ".muted{color:#667085;} svg{max-width:100%;height:auto;border:1px solid #d8dee9;}",
                ".quality{border:1px solid #d0d5dd;padding:14px;margin:20px 0;background:#f8fafc;}",
                ".quality h2{font-size:18px;margin-bottom:8px;}.quality ul{margin:8px 0 0 20px;padding:0;}",
                ".quality-ok{border-color:#86efac;background:#ecfdf3;color:#166534;}",
                ".quality-warning{border-color:#facc15;background:#fefce8;color:#854d0e;}",
                ".quality-error{border-color:#fca5a5;background:#fef2f2;color:#991b1b;}",
                ".chart-card{border:1px solid #d8dee9;padding:14px;margin:14px 0;background:#fff;}",
                ".chart-title{font-weight:bold;margin-bottom:8px;}",
                ".chart-note{font-size:12px;color:#9a3412;margin:8px 0 0 0;}",
                ".legend{font-size:12px;margin:0 0 8px 0;}",
                ".chart-scroll{max-width:100%;overflow-x:auto;overflow-y:hidden;}",
                ".chart-scroll svg{max-width:none;display:block;}",
                ".map-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(360px,1fr));gap:18px;}",
                ".map-card{margin:0;border:1px solid #d8dee9;padding:10px;background:#fff;}",
                ".map-error{border-color:#fca5a5;background:#fef2f2;color:#991b1b;min-height:160px;}",
                ".map-card img{width:100%;height:auto;display:block;}",
                ".map-card figcaption{font-size:12px;color:#475467;margin-top:8px;}",
                ".map-actions{display:flex;gap:8px;align-items:center;margin-top:10px;}",
                ".download-button{display:inline-block;padding:8px 12px;border:1px solid #2563eb;"
                "background:#2563eb;color:#fff;text-decoration:none;border-radius:4px;font-weight:bold;}",
                ".cloud-badge{display:inline-block;padding:5px 8px;font-size:12px;border-radius:4px;font-weight:bold;}",
                ".cloud-low{border:1px solid #22c55e;background:#f0fdf4;color:#166534;}",
                ".cloud-moderate{border:1px solid #eab308;background:#fefce8;color:#854d0e;}",
                ".cloud-high{border:1px solid #f97316;background:#fff7ed;color:#9a3412;}",
                ".cloud-very-high{border:1px solid #ef4444;background:#fef2f2;color:#991b1b;}",
                ".map-cloud-risk{border-color:#f97316;}",
                "</style>",
                "</head>",
                "<body>",
                f"<h1>{html.escape(title)}</h1>",
                "<p class=\"muted\">Relatório gerado a partir de estatísticas zonais reais dos pixels válidos dos rasters processados.</p>",
                self._quality_alerts(notes),
                self._map_gallery(map_paths, output_path),
                "<section><h2>Resumo temporal</h2>",
                self._temporal_chart(temporal_rows),
                self._table(temporal_rows),
                "</section>",
                "<section><h2>Estatísticas completas</h2>",
                self._table(rows),
                "</section>",
                "<section><h2>Observações</h2>",
                "<ul>",
                "<li>Linhas com baixo percentual válido devem ser interpretadas com cautela.</li>",
                "<li>Datas sem raster local válido não entram nas estatísticas.</li>",
                "<li>No modo local, a qualidade depende dos rasters fornecidos pelo usuário.</li>",
                "</ul>",
                "</section>",
                "</body></html>",
            ]
        )
        with open(output_path, "w", encoding="utf-8") as handle:
            handle.write(html_text)
        return output_path

    def generate_pdf_from_html(self, html_path: str, output_path: str) -> str:
        """Render the HTML report to a basic PDF through Qt, when available."""

        from qgis.PyQt.QtGui import QTextDocument
        from qgis.PyQt.QtPrintSupport import QPrinter

        html_text = Path(html_path).read_text(encoding="utf-8")
        Path(output_path).parent.mkdir(parents=True, exist_ok=True)
        document = QTextDocument()
        document.setHtml(html_text)
        printer = QPrinter(QPrinter.HighResolution)
        printer.setOutputFormat(QPrinter.PdfFormat)
        printer.setOutputFileName(output_path)
        document.print_(printer)
        return output_path

    @staticmethod
    def _quality_alerts(notes: str) -> str:
        messages = [line.strip() for line in str(notes or "").splitlines() if line.strip()]
        if not messages:
            return (
                '<section class="quality quality-ok"><h2>Validação do processamento</h2>'
                '<p>Nenhum erro ou alerta de qualidade foi detectado.</p></section>'
            )
        has_error = any(message.upper().startswith("ERRO") for message in messages)
        css_class = "quality-error" if has_error else "quality-warning"
        heading = "Erros e alertas encontrados" if has_error else "Alertas de qualidade"
        items = "".join(f"<li>{html.escape(message)}</li>" for message in messages)
        return f'<section class="quality {css_class}"><h2>{heading}</h2><ul>{items}</ul></section>'

    @staticmethod
    def _table(rows: Sequence[Mapping[str, object]]) -> str:
        if not rows:
            return "<p>Nenhum resultado.</p>"
        fields = _ordered_fields(rows)
        head = "".join(f"<th>{html.escape(str(field))}</th>" for field in fields)
        body_rows = []
        for row in rows:
            cells = "".join(f"<td>{html.escape(_format(row.get(field)))}</td>" for field in fields)
            body_rows.append(f"<tr>{cells}</tr>")
        return (
            '<div class="table-wrap">'
            f"<table><thead><tr>{head}</tr></thead><tbody>{''.join(body_rows)}</tbody></table>"
            "</div>"
        )

    @staticmethod
    def _map_gallery(map_paths: Sequence[Mapping[str, object]], report_path: str) -> str:
        if not map_paths:
            return ""
        base = Path(report_path).parent
        cards = []
        for item in map_paths:
            title = f"{item.get('indice', '')} - {item.get('data', '')}"
            subtitle = str(item.get("area", ""))
            error = str(item.get("error", "")).strip()
            path = Path(str(item.get("path", "")))
            if error or not path.exists():
                message = error or "O arquivo PNG não foi encontrado depois da exportação."
                cards.append(
                    '<figure class="map-card map-error">'
                    f"<figcaption><strong>{html.escape(title)}</strong><br>{html.escape(subtitle)}</figcaption>"
                    f"<p><strong>Erro ao gerar este mapa:</strong><br>{html.escape(message)}</p>"
                    "</figure>"
                )
                continue
            src = os.path.relpath(path, base).replace("\\", "/")
            cloud_html = ""
            card_class = "map-card"
            cloud_percent = item.get("cloud_percent")
            if isinstance(cloud_percent, (int, float)):
                cloud_status = str(item.get("cloud_status", "")).strip().upper()
                cloud_class = _cloud_css_class(float(cloud_percent), cloud_status)
                if float(cloud_percent) > 10.0:
                    card_class += " map-cloud-risk"
                cloud_html = (
                    f'<span class="cloud-badge {cloud_class}">'
                    f"Nuvem na cena: {float(cloud_percent):.1f}% - {html.escape(cloud_status)}"
                    "</span>"
                )
            cards.append(
                f'<figure class="{card_class}">'
                f"<img src=\"{html.escape(src)}\" alt=\"{html.escape(title)}\" width=\"900\">"
                f"<figcaption><strong>{html.escape(title)}</strong><br>{html.escape(subtitle)}</figcaption>"
                '<div class="map-actions">'
                f'<a class="download-button" href="{html.escape(src)}" download="{html.escape(path.name)}">Baixar PNG</a>'
                f"{cloud_html}"
                "</div>"
                "</figure>"
            )
        if not cards:
            return ""
        return (
            "<section><h2>Mapas gerados</h2>"
            "<div class=\"map-grid\">"
            + "".join(cards)
            + "</div></section>"
        )

    @staticmethod
    def _temporal_chart(rows: Sequence[Mapping[str, object]]) -> str:
        if not rows:
            return "<p>Nenhum dado temporal para gráfico.</p>"
        metric_fields = sorted({field for row in rows for field in row if field.endswith("_medio")})
        if not metric_fields:
            return "<p>Nenhuma média temporal disponível.</p>"

        grouped = _group_temporal_rows(rows)
        charts = []
        for (area_id, area_name), area_rows in grouped:
            if area_id and area_name and area_id != area_name:
                title = f"{area_name} (ID {area_id})"
            else:
                title = area_name or area_id or "sem identificador"
            charts.append(ReportGenerator._temporal_chart_for_area(title, area_rows, metric_fields))
        return "".join(charts)

    @staticmethod
    def _temporal_chart_for_area(area_name: str, rows: Sequence[Mapping[str, object]], metric_fields: Sequence[str]) -> str:
        rows = sorted(rows, key=lambda row: str(row.get("data", "")))
        values = [
            float(row.get(field))
            for row in rows
            for field in metric_fields
            if isinstance(row.get(field), (int, float))
        ]
        title = f"Área: {area_name or 'sem identificador'}"
        if not values:
            return (
                "<div class=\"chart-card\">"
                f"<div class=\"chart-title\">{html.escape(title)}</div>"
                "<p class=\"chart-note\">Nenhum valor médio válido para esta área.</p>"
                "</div>"
            )

        # Reserve enough horizontal space for every full ISO date. The chart
        # scrolls horizontally when there are more dates than fit on screen.
        width, height = max(1100, 76 + 50 + max(0, len(rows) - 1) * 90), 430
        left, right, top, bottom = 76, 50, 28, 86
        plot_width = width - left - right
        plot_height = height - top - bottom
        vmin, vmax = _nice_range(min(values), max(values))
        x_step = plot_width / max(1, len(rows) - 1)
        colors = _metric_colors(metric_fields)

        legend = "".join(
            f"<span style=\"color:{html.escape(colors[field])};margin-right:16px\">{html.escape(field)}</span>"
            for field in metric_fields
        )
        grid = []
        for tick in _ticks(vmin, vmax, 5):
            y = top + (vmax - tick) / (vmax - vmin) * plot_height
            grid.append(f"<line x1=\"{left}\" y1=\"{y:.1f}\" x2=\"{width-right}\" y2=\"{y:.1f}\" stroke=\"#e5e7eb\"/>")
            grid.append(
                f"<text x=\"{left-10}\" y=\"{y+4:.1f}\" text-anchor=\"end\" font-size=\"12\" fill=\"#475467\">{tick:.2f}</text>"
            )

        x_labels = []
        for row_index, row in enumerate(rows):
            x = left + row_index * x_step
            label = html.escape(str(row.get("data", "")))
            x_labels.append(f"<line x1=\"{x:.1f}\" y1=\"{top}\" x2=\"{x:.1f}\" y2=\"{height-bottom}\" stroke=\"#f1f5f9\"/>")
            x_labels.append(
                f"<text x=\"{x:.1f}\" y=\"{height-bottom+24}\" text-anchor=\"middle\" font-size=\"12\" fill=\"#475467\">{label}</text>"
            )

        series = []
        for field in metric_fields:
            color = colors[field]
            segments: List[List[str]] = []
            current: List[str] = []
            points = []
            for row_index, row in enumerate(rows):
                value = row.get(field)
                if not isinstance(value, (int, float)):
                    if current:
                        segments.append(current)
                        current = []
                    continue
                x = left + row_index * x_step
                y = top + (vmax - float(value)) / (vmax - vmin) * plot_height
                current.append(f"{x:.1f},{y:.1f}")
                point_title = html.escape(
                    f"{row.get('data', '')} — {field}: {float(value):.6g}"
                )
                points.append(
                    f"<circle cx=\"{x:.1f}\" cy=\"{y:.1f}\" r=\"4\" fill=\"{color}\">"
                    f"<title>{point_title}</title></circle>"
                )
            if current:
                segments.append(current)
            for segment in segments:
                if len(segment) == 1:
                    continue
                series.append(
                    f"<polyline fill=\"none\" stroke=\"{color}\" stroke-width=\"3\" points=\"{' '.join(segment)}\"/>"
                )
            series.extend(points)

        missing_notes = _missing_notes(rows, metric_fields)
        notes_html = ""
        if missing_notes:
            notes_html = "<p class=\"chart-note\">" + "<br>".join(html.escape(note) for note in missing_notes) + "</p>"

        svg = (
            f"<svg width=\"{width}\" height=\"{height}\" viewBox=\"0 0 {width} {height}\" role=\"img\">"
            f"<rect x=\"0\" y=\"0\" width=\"{width}\" height=\"{height}\" fill=\"#ffffff\"/>"
            f"{''.join(grid)}"
            f"{''.join(x_labels)}"
            f"<line x1=\"{left}\" y1=\"{height-bottom}\" x2=\"{width-right}\" y2=\"{height-bottom}\" stroke=\"#94a3b8\"/>"
            f"<line x1=\"{left}\" y1=\"{top}\" x2=\"{left}\" y2=\"{height-bottom}\" stroke=\"#94a3b8\"/>"
            f"<text x=\"{left}\" y=\"18\" font-size=\"12\" fill=\"#475467\">Média do índice</text>"
            f"<text x=\"{width-right}\" y=\"{height-14}\" text-anchor=\"end\" font-size=\"12\" fill=\"#475467\">Data da imagem</text>"
            f"{''.join(series)}</svg>"
        )

        return (
            "<div class=\"chart-card\">"
            f"<div class=\"chart-title\">{html.escape(title)}</div>"
            f"<div class=\"legend\">{legend}</div>"
            f"<div class=\"chart-scroll\">{svg}</div>"
            f"{notes_html}"
            "</div>"
        )


def _format(value) -> str:
    if value is None:
        return "sem dados"
    if isinstance(value, float):
        return f"{value:.6g}"
    return str(value)


def _cloud_css_class(percent: float, status: str) -> str:
    normalized = status.upper()
    if "MUITO ALTA" in normalized or percent > 20.0:
        return "cloud-very-high"
    if "ALTA" in normalized or percent > 10.0:
        return "cloud-high"
    if "MODERADA" in normalized or percent > 2.0:
        return "cloud-moderate"
    return "cloud-low"


def _ordered_fields(rows: Sequence[Mapping[str, object]]) -> List[str]:
    preferred = ["id_area", "nome_area", "data"]
    fields: List[str] = []
    for field in preferred:
        if any(field in row for row in rows):
            fields.append(field)
    dynamic = sorted({field for row in rows for field in row if field.endswith("_medio")})
    fields.extend(field for field in dynamic if field not in fields)
    for row in rows:
        for field in row:
            if field not in fields:
                fields.append(field)
    return fields


def _group_temporal_rows(rows: Sequence[Mapping[str, object]]) -> List[Tuple[Tuple[str, str], List[Mapping[str, object]]]]:
    grouped: Dict[Tuple[str, str], List[Mapping[str, object]]] = {}
    for row in rows:
        key = (str(row.get("id_area", "")), str(row.get("nome_area", "")))
        grouped.setdefault(key, []).append(row)
    return sorted(grouped.items(), key=lambda item: (item[0][0], item[0][1]))


def _nice_range(vmin: float, vmax: float) -> Tuple[float, float]:
    if vmin == vmax:
        delta = max(abs(vmin) * 0.1, 0.1)
        return vmin - delta, vmax + delta
    span = vmax - vmin
    padding = span * 0.08
    lower = vmin - padding
    upper = vmax + padding
    if lower > 0:
        lower = 0.0
    if upper < 0:
        upper = 0.0
    return lower, upper


def _ticks(vmin: float, vmax: float, count: int) -> List[float]:
    if count <= 1:
        return [vmin]
    step = (vmax - vmin) / (count - 1)
    return [vmin + step * index for index in range(count)]


def _metric_colors(metric_fields: Sequence[str]) -> Dict[str, str]:
    palette = {
        "NDVI_medio": "#dc2626",
        "GNDVI_medio": "#2563eb",
        "NDMI_medio": "#ea580c",
        "NDRE_medio": "#7c3aed",
        "SAVI_medio": "#0891b2",
        "EVI_medio": "#2f855a",
    }
    fallback = ["#2f855a", "#2563eb", "#d97706", "#7c3aed", "#dc2626", "#0891b2"]
    colors: Dict[str, str] = {}
    for index, field in enumerate(metric_fields):
        colors[field] = palette.get(field, fallback[index % len(fallback)])
    return colors


def _missing_notes(rows: Sequence[Mapping[str, object]], metric_fields: Sequence[str]) -> List[str]:
    notes = []
    for row in rows:
        date = str(row.get("data", "sem data"))
        missing = [field.replace("_medio", "") for field in metric_fields if not isinstance(row.get(field), (int, float))]
        if len(missing) == len(metric_fields):
            notes.append(f"{date}: sem valores médios válidos.")
        elif missing:
            notes.append(f"{date}: sem dados para {', '.join(missing)}.")
    return notes
