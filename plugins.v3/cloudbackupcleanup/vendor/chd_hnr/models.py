from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class Link:
    href: str
    text: str


@dataclass(frozen=True)
class TorrentRecord:
    key: str
    name: str
    progress_value: str
    status: str
    detail_url: str
    raw_cells: list[str]


@dataclass(frozen=True)
class Alert:
    key: str
    name: str
    detail_url: str
    progress_value: str
    stalled_since: datetime
    stalled_hours: float
    last_seen_at: datetime
    status: str


@dataclass(frozen=True)
class CheckResult:
    records_seen: int
    new_records: int
    changed_records: int
    stalled_records: int
    missing_records: int
    alerts: list[Alert]
