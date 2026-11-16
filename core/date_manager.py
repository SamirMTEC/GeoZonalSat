from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Iterable, List, Optional, Sequence, Tuple


DATE_PATTERNS = (
    re.compile(r"(?P<year>20\d{2})[-_](?P<month>\d{2})[-_](?P<day>\d{2})"),
    re.compile(r"(?P<day>\d{2})[-_/](?P<month>\d{2})[-_/](?P<year>20\d{2})"),
    re.compile(r"(?P<year>20\d{2})(?P<month>\d{2})(?P<day>\d{2})"),
)


@dataclass(frozen=True)
class DateWindow:
    requested: date
    start: date
    end: date


class DateManager:
    """Parse explicit dates, interval dates, and dates embedded in file names."""

    @staticmethod
    def parse(value: str) -> date:
        value = (value or "").strip()
        if not value:
            raise ValueError("Data vazia.")

        for fmt in ("%d/%m/%Y", "%Y-%m-%d", "%d-%m-%Y", "%Y_%m_%d", "%Y%m%d"):
            try:
                return datetime.strptime(value, fmt).date()
            except ValueError:
                pass

        match = DateManager.detect_in_text(value)
        if match:
            return match
        raise ValueError(f"Data invalida: {value}")

    @staticmethod
    def detect_in_text(value: str) -> Optional[date]:
        text = Path(value).stem
        for pattern in DATE_PATTERNS:
            match = pattern.search(text)
            if not match:
                continue
            try:
                return date(
                    int(match.group("year")),
                    int(match.group("month")),
                    int(match.group("day")),
                )
            except ValueError:
                continue
        return None

    @staticmethod
    def parse_many(text: str) -> List[date]:
        if not text:
            return []
        tokens = re.split(r"[\n,;]+", text)
        dates: List[date] = []
        for token in tokens:
            token = token.strip()
            if token:
                dates.append(DateManager.parse(token))
        return dates

    @staticmethod
    def interval(start: date, end: date, step_days: int) -> List[date]:
        if step_days <= 0:
            raise ValueError("O intervalo em dias deve ser maior que zero.")
        if end < start:
            raise ValueError("A data final nao pode ser anterior a inicial.")
        values: List[date] = []
        current = start
        while current <= end:
            values.append(current)
            current += timedelta(days=step_days)
        return values

    @staticmethod
    def windows(dates: Sequence[date], tolerance_days: int) -> List[DateWindow]:
        tolerance = max(0, int(tolerance_days))
        delta = timedelta(days=tolerance)
        return [DateWindow(item, item - delta, item + delta) for item in dates]

    @staticmethod
    def closest(target: date, candidates: Iterable[date], tolerance_days: int) -> Optional[date]:
        candidates = list(candidates)
        if not candidates:
            return None
        tolerance = timedelta(days=max(0, tolerance_days))
        ranked: List[Tuple[timedelta, date]] = []
        for item in candidates:
            distance = abs(item - target)
            if distance <= tolerance:
                ranked.append((distance, item))
        if not ranked:
            return None
        ranked.sort(key=lambda pair: (pair[0], pair[1]))
        return ranked[0][1]

    @staticmethod
    def iso(value: Optional[date]) -> str:
        return value.isoformat() if value else ""

