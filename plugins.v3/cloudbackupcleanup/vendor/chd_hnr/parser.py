from __future__ import annotations

from dataclasses import dataclass, field
from hashlib import sha1
from html.parser import HTMLParser
from re import Pattern
from urllib.parse import urljoin
import re

from .config import ParserConfig
from .models import Link, TorrentRecord


class ParseError(RuntimeError):
    pass


@dataclass
class Cell:
    text_parts: list[str] = field(default_factory=list)
    links: list[Link] = field(default_factory=list)
    is_header: bool = False

    @property
    def text(self) -> str:
        return normalize_text(" ".join(self.text_parts))


@dataclass
class Row:
    cells: list[Cell] = field(default_factory=list)

    @property
    def texts(self) -> list[str]:
        return [cell.text for cell in self.cells]

    @property
    def links(self) -> list[Link]:
        links: list[Link] = []
        for cell in self.cells:
            links.extend(cell.links)
        return links


@dataclass
class Table:
    rows: list[Row] = field(default_factory=list)


class TableHTMLParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.tables: list[Table] = []
        self._table_stack: list[Table] = []
        self._current_row: Row | None = None
        self._current_cell: Cell | None = None
        self._current_link_href: str | None = None
        self._current_link_text: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attrs_dict = {key.lower(): value or "" for key, value in attrs}
        tag = tag.lower()
        if tag == "table":
            table = Table()
            self.tables.append(table)
            self._table_stack.append(table)
        elif tag == "tr" and self._table_stack:
            self._current_row = Row()
        elif tag in {"td", "th"} and self._current_row is not None:
            self._current_cell = Cell(is_header=tag == "th")
        elif tag == "a" and self._current_cell is not None:
            self._current_link_href = attrs_dict.get("href", "")
            self._current_link_text = []

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag == "a" and self._current_cell is not None and self._current_link_href is not None:
            link_text = normalize_text(" ".join(self._current_link_text))
            self._current_cell.links.append(Link(href=self._current_link_href, text=link_text))
            self._current_link_href = None
            self._current_link_text = []
        elif tag in {"td", "th"} and self._current_row is not None and self._current_cell is not None:
            self._current_row.cells.append(self._current_cell)
            self._current_cell = None
        elif tag == "tr" and self._table_stack and self._current_row is not None:
            if self._current_row.cells:
                self._table_stack[-1].rows.append(self._current_row)
            self._current_row = None
        elif tag == "table" and self._table_stack:
            self._table_stack.pop()

    def handle_data(self, data: str) -> None:
        if self._current_cell is None:
            return
        self._current_cell.text_parts.append(data)
        if self._current_link_href is not None:
            self._current_link_text.append(data)


def parse_hnr_records(html: str, parser_config: ParserConfig, base_url: str) -> list[TorrentRecord]:
    if _looks_like_login_page(html, parser_config.login_failed_markers):
        raise ParseError("抓取到的页面像是登录页，Cookie 可能已经失效。")

    parser = TableHTMLParser()
    parser.feed(html)
    id_patterns = [re.compile(pattern) for pattern in parser_config.torrent_id_patterns]

    records: list[TorrentRecord] = []
    errors: list[str] = []
    for table_index, table in enumerate(parser.tables):
        try:
            records.extend(_records_from_table(table, parser_config, base_url, id_patterns))
        except ParseError as exc:
            errors.append(f"table {table_index}: {exc}")

    deduped = _dedupe(records)
    if deduped:
        return deduped

    summary = summarize_tables(html, limit=5)
    detail = "; ".join(errors[:3])
    raise ParseError(
        "没有解析到 H&R 记录。请用 parse-fixture 检查页面表格和列名配置。"
        f"{detail}\nTable summary:\n{summary}"
    )


def summarize_tables(html: str, limit: int = 10) -> str:
    parser = TableHTMLParser()
    parser.feed(html)
    lines: list[str] = []
    for index, table in enumerate(parser.tables[:limit]):
        first_rows = [" | ".join(row.texts[:8]) for row in table.rows[:3]]
        lines.append(f"table {index}: rows={len(table.rows)}; sample={first_rows}")
    if not lines:
        return "No tables found."
    return "\n".join(lines)


def normalize_text(value: str) -> str:
    return re.sub(r"\s+", " ", value.replace("\xa0", " ")).strip()


def _records_from_table(
    table: Table,
    parser_config: ParserConfig,
    base_url: str,
    id_patterns: list[Pattern[str]],
) -> list[TorrentRecord]:
    if len(table.rows) < 2:
        return []

    header_index, headers = _find_header(table, parser_config)
    if header_index is None:
        return []

    name_idx = _configured_or_detected_index(
        parser_config.name_column_index,
        headers,
        parser_config.name_columns,
        required=False,
    )
    progress_idx = _configured_or_detected_index(
        parser_config.progress_column_index,
        headers,
        parser_config.progress_columns,
        required=True,
    )
    status_idx = _configured_or_detected_index(
        parser_config.status_column_index,
        headers,
        parser_config.status_columns,
        required=False,
    )
    if progress_idx is None:
        raise ParseError(f"无法从表头中识别完成时间列：{headers}")

    records: list[TorrentRecord] = []
    for row in table.rows[header_index + 1 :]:
        if len(row.cells) <= progress_idx:
            continue
        raw_cells = row.texts
        progress_value = raw_cells[progress_idx]
        if not progress_value:
            continue

        key, detail_url = _extract_key(row.links, base_url, id_patterns)
        name = _extract_name(row, name_idx)
        if not key:
            key = _fallback_key(name, raw_cells)
        if not name:
            name = key
        status = raw_cells[status_idx] if status_idx is not None and len(raw_cells) > status_idx else ""
        records.append(
            TorrentRecord(
                key=key,
                name=name,
                progress_value=progress_value,
                status=status,
                detail_url=detail_url,
                raw_cells=raw_cells,
            )
        )
    return records


def _find_header(table: Table, parser_config: ParserConfig) -> tuple[int | None, list[str]]:
    for index, row in enumerate(table.rows):
        headers = row.texts
        if not headers:
            continue
        if parser_config.progress_column_index >= 0 and len(headers) > parser_config.progress_column_index:
            return index, headers
        header_hit = _find_alias_index(headers, parser_config.progress_columns) is not None
        name_hit = _find_alias_index(headers, parser_config.name_columns) is not None
        any_header_cell = any(cell.is_header for cell in row.cells)
        if header_hit and (name_hit or any_header_cell):
            return index, headers
    return None, []


def _configured_or_detected_index(
    configured: int,
    headers: list[str],
    aliases: list[str],
    required: bool,
) -> int | None:
    if configured >= 0:
        return configured
    found = _find_alias_index(headers, aliases)
    if found is not None:
        return found
    if required:
        return None
    return None


def _find_alias_index(headers: list[str], aliases: list[str]) -> int | None:
    normalized_aliases = [_column_key(alias) for alias in aliases if alias]
    header_keys = [_column_key(header) for header in headers]
    for alias_key in normalized_aliases:
        for index, header_key in enumerate(header_keys):
            if alias_key and header_key == alias_key:
                return index
        for index, header_key in enumerate(header_keys):
            if not header_key:
                continue
            if alias_key and (alias_key in header_key or header_key in alias_key):
                return index
    return None


def _column_key(value: str) -> str:
    return re.sub(r"[\s:_：/\\|()\[\]【】<>《》-]+", "", value).casefold()


def _extract_key(
    links: list[Link],
    base_url: str,
    id_patterns: list[Pattern[str]],
) -> tuple[str, str]:
    detail_url = ""
    for link in links:
        href = link.href
        if not href:
            continue
        absolute = urljoin(base_url.rstrip("/") + "/", href)
        if not detail_url:
            detail_url = absolute
        for pattern in id_patterns:
            match = pattern.search(href)
            if match:
                return match.group(1), absolute
    return "", detail_url


def _extract_name(row: Row, name_idx: int | None) -> str:
    if name_idx is not None and len(row.cells) > name_idx:
        link_names = [link.text for link in row.cells[name_idx].links if link.text]
        if link_names:
            return max(link_names, key=len)
        return row.cells[name_idx].text
    link_names = [link.text for link in row.links if link.text]
    return max(link_names, key=len) if link_names else ""


def _fallback_key(name: str, raw_cells: list[str]) -> str:
    seed = "\n".join([name, *raw_cells])
    return "row-" + sha1(seed.encode("utf-8")).hexdigest()[:16]


def _dedupe(records: list[TorrentRecord]) -> list[TorrentRecord]:
    seen: set[str] = set()
    deduped: list[TorrentRecord] = []
    for record in records:
        if record.key in seen:
            continue
        seen.add(record.key)
        deduped.append(record)
    return deduped


def _looks_like_login_page(html: str, markers: list[str]) -> bool:
    lowered = html.casefold()
    marker_hits = sum(1 for marker in markers if marker and marker.casefold() in lowered)
    has_password_input = "type=\"password\"" in lowered or "type='password'" in lowered
    return has_password_input and marker_hits > 0
