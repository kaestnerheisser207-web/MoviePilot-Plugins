"""ParserConfig excerpt from cmmchina/chdbits-hnr-monitor, MIT licensed.

Pinned revision: 627e67098fc6aa97912897ba95e13f53281a0b82.
Only parser configuration is included; see LICENSE and THIRD_PARTY_NOTICES.md.
"""
from dataclasses import dataclass, field


@dataclass(frozen=True)
class ParserConfig:
    progress_columns: list[str] = field(default_factory=list)
    name_columns: list[str] = field(default_factory=list)
    status_columns: list[str] = field(default_factory=list)
    name_column_index: int = -1
    progress_column_index: int = -1
    status_column_index: int = -1
    torrent_id_patterns: list[str] = field(default_factory=list)
    login_failed_markers: list[str] = field(default_factory=list)
