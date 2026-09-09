"""Page source_table + where 500 at a time. Never return row payloads."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

from . import supabase_sync
from .config import DEFAULT_SUPABASE_PROJECT
from .geo import parse_city_state

PAGE_SIZE = 500
PEOPLE_WRITEBACK = (
    "wf_people_count",
    "wf_people_source",
    "wf_people_status",
)
FORBIDDEN_WRITE = frozenset(
    {
        "dl_status",
        "sg_exclude",
    }
)

_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_PRED = re.compile(
    r"""
    (?P<col>[A-Za-z_][A-Za-z0-9_]*)
    \s+
    (?:
        (?P<null>is\s+not\s+null|is\s+null)
        |
        (?P<op>=|!=|<>)
        \s+
        (?:'(?P<q>(?:[^']|'')*)'|(?P<num>-?\d+(?:\.\d+)?))
    )
    """,
    re.I | re.X,
)

FIELD_CANDIDATES: dict[str, tuple[str, ...]] = {
    "company_name": (
        "company_name",
        "company",
        "business_name",
        "contractor_name",
        "clean_name",
        "name",
    ),
    "domain": ("domain", "website"),
    "city": ("city", "address_city", "contact_city"),
    "state": ("state", "address_state", "contact_state"),
    "phone": ("phone", "cellphone", "mobile"),
    "first_name": ("first_name",),
    "last_name": ("last_name",),
    "place_id": ("place_id",),
}


@dataclass
class TableSource:
    project_id: str
    schema: str = "public"
    table: str = ""
    where: str = ""
    key_column: str = "id"
    column_map: dict[str, str] = field(default_factory=dict)
    limit: int | None = None
    writeback: bool = True

    @property
    def qualified(self) -> str:
        return f"{self.schema}.{self.table}"


def split_qualified(name: str, default_schema: str = "public") -> tuple[str, str]:
    raw = (name or "").strip()
    default = (default_schema or "public").strip() or "public"
    if not raw:
        return default, ""
    if raw.count(".") == 1:
        schema, table = raw.split(".", 1)
        return schema.strip(), table.strip()
    if "." in raw:
        raise ValueError("source_table must be table or schema.table")
    return default, raw


def parse_source(
    source_table: str,
    where: str = "",
    *,
    project_id: str = DEFAULT_SUPABASE_PROJECT,
    writeback: bool = True,
    key_column: str = "id",
    limit: int | None = None,
) -> TableSource:
    schema, table = split_qualified(source_table)
    if not table or not _IDENT.match(table) or not _IDENT.match(schema):
        raise ValueError("source_table must be table or schema.table")
    if key_column and not _IDENT.match(key_column):
        raise ValueError("key_column must be an identifier")
    return TableSource(
        project_id=project_id or DEFAULT_SUPABASE_PROJECT,
        schema=schema,
        table=table,
        where=(where or "").strip(),
        key_column=key_column or "id",
        writeback=writeback,
        limit=limit,
    )


def where_to_filters(where: str) -> list[dict[str, str]]:
    text = (where or "").strip()
    if not text:
        return []
    parts = re.split(r"\s+and\s+", text, flags=re.I)
    out: list[dict[str, str]] = []
    for part in parts:
        part = part.strip().rstrip(";")
        if not part:
            continue
        m = _PRED.fullmatch(part)
        if not m:
            raise ValueError(
                "where only allows AND-combined predicates like "
                "\"wf_people_status is null\" or \"wf_domain_status = 'resolved'\""
            )
        col = m.group("col")
        if m.group("null"):
            null_op = m.group("null").lower()
            out.append(
                {
                    "col": col,
                    "op": "is.null" if null_op == "is null" else "not.is.null",
                }
            )
            continue
        op = m.group("op")
        value = m.group("q")
        if value is not None:
            value = value.replace("''", "'")
        else:
            value = m.group("num")
        out.append({"col": col, "op": "eq" if op == "=" else "neq", "value": value})
    return out


def list_columns(src: TableSource) -> set[str]:
    try:
        data = supabase_sync.rpc(
            "ew_source_columns",
            {"p_schema": src.schema, "p_table": src.table},
        )
    except RuntimeError:
        return set()
    if isinstance(data, list):
        if data and isinstance(data[0], dict):
            inner = data[0].get("ew_source_columns") or data
            if isinstance(inner, list):
                return {str(c) for c in inner if c}
        return {str(c) for c in data if c}
    return set()


def discover_column_map(src: TableSource) -> None:
    cols = list_columns(src)
    mapping: dict[str, str] = dict(src.column_map)
    for field_name, candidates in FIELD_CANDIDATES.items():
        current = mapping.get(field_name)
        if current and (not cols or current in cols):
            continue
        for cand in candidates:
            if not cols or cand in cols:
                mapping[field_name] = cand
                if cols:
                    break
    if "id" not in cols and cols:
        for cand in ("place_id", "domain", "company_name", "contractor_name"):
            if cand in cols:
                src.key_column = cand
                break
    src.column_map = mapping


def _normalize_domain(value: str) -> str:
    raw = (value or "").strip().lower()
    raw = re.sub(r"^https?://", "", raw)
    raw = raw.split("/")[0]
    raw = raw.split(":")[0]
    if raw.startswith("www."):
        raw = raw[4:]
    return raw


def _map_row(src: TableSource, raw: dict[str, Any]) -> dict[str, Any]:
    item: dict[str, Any] = {"_source_key": raw.get(src.key_column)}
    for field_name, col in src.column_map.items():
        item[field_name] = raw.get(col)
    item["domain"] = _normalize_domain(str(item.get("domain") or ""))
    company = str(item.get("company_name") or "").strip()
    item["company_name"] = company
    city = str(item.get("city") or "").strip()
    state = str(item.get("state") or "").strip()
    if (not city or not state) and raw.get("address"):
        parsed_city, parsed_state = parse_city_state(str(raw.get("address") or ""))
        item["city"] = city or parsed_city
        item["state"] = state or parsed_state
    return item


def fetch_page(src: TableSource, *, cursor: str | None = None) -> list[dict[str, Any]]:
    discover_column_map(src)
    cols = {src.key_column, *src.column_map.values()}
    if "address" not in cols:
        # city/state may be embedded; include when present
        present = list_columns(src)
        if "address" in present:
            cols.add("address")
    filters = where_to_filters(src.where)
    limit = PAGE_SIZE if src.limit is None else min(int(src.limit), PAGE_SIZE)
    data = supabase_sync.rpc(
        "ew_read_source",
        {
            "p_schema": src.schema,
            "p_table": src.table,
            "p_filters": filters,
            "p_columns": sorted(cols),
            "p_key_column": src.key_column,
            "p_after": cursor,
            "p_limit": limit,
        },
    )
    rows: list[Any]
    if isinstance(data, list):
        rows = data
    elif isinstance(data, dict):
        rows = data.get("ew_read_source") or data.get("data") or []
        if isinstance(rows, str):
            try:
                rows = json.loads(rows)
            except ValueError:
                rows = []
    else:
        rows = []
    if isinstance(rows, dict):
        rows = rows.get("data") or []
    out: list[dict[str, Any]] = []
    for raw in rows:
        if isinstance(raw, dict):
            out.append(_map_row(src, raw))
    return out


def iter_source(src: TableSource) -> Any:
    cursor: str | None = None
    yielded = 0
    while True:
        remaining = None
        if src.limit is not None:
            remaining = src.limit - yielded
            if remaining <= 0:
                break
        page_src = src
        if remaining is not None:
            page_src = TableSource(**{**src.__dict__, "limit": min(remaining, PAGE_SIZE)})
        rows = fetch_page(page_src, cursor=cursor)
        if not rows:
            break
        for row in rows:
            yield row
            yielded += 1
            key = row.get("_source_key")
            if key is not None:
                cursor = str(key)
        if len(rows) < PAGE_SIZE:
            break


def count_source(src: TableSource, cap: int = 50_000) -> int:
    n = 0
    for _ in iter_source(src):
        n += 1
        if n >= cap:
            break
    return n


def ensure_people_writeback(src: TableSource) -> None:
    if not src.writeback:
        return
    try:
        supabase_sync.rpc(
            "pw_ensure_people_writeback",
            {"p_schema": src.schema, "p_table": src.table},
        )
    except RuntimeError:
        return


def writeback_people(
    src: TableSource,
    source_key: Any,
    *,
    count: int,
    source: str,
    status: str,
) -> None:
    if not src.writeback or source_key in (None, ""):
        return
    fields = {
        "wf_people_count": count,
        "wf_people_source": source,
        "wf_people_status": status,
    }
    for forbidden in FORBIDDEN_WRITE:
        fields.pop(forbidden, None)
    try:
        supabase_sync.rpc(
            "ew_patch_source",
            {
                "p_schema": src.schema,
                "p_table": src.table,
                "p_key_column": src.key_column,
                "p_key": str(source_key),
                "p_fields": fields,
            },
        )
    except RuntimeError:
        return
