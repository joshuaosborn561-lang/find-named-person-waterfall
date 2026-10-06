"""Page source_table + where 500 at a time. Never return row payloads."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

from . import supabase_sync
from .config import DEFAULT_SUPABASE_PROJECT
from .geo import parse_city_state
from .people import conversational_company

PAGE_SIZE = 500
PEOPLE_WRITEBACK = (
    "wf_people_count",
    "wf_people_source",
    "wf_people_status",
    "wf_people_reason",
    "wf_email_pattern",
    "wf_email_pattern_conf",
)
FORBIDDEN_WRITE = frozenset(
    {
        "dl_status",
        "sg_exclude",
    }
)

_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_NOT_FALSE = re.compile(
    r"(?P<col>[A-Za-z_][A-Za-z0-9_]*)\s+is\s+not\s+false\b",
    re.I,
)
_NOT_BLANK = re.compile(
    r"""coalesce\(\s*(?P<col>[A-Za-z_][A-Za-z0-9_]*)\s*,\s*''\s*\)\s*(?:<>|!=)\s*''""",
    re.I | re.X,
)
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
        "clean_name",
        "dba",
        "trade_name",
        "doing_business_as",
        "common_name",
        "conversational_name",
        "company_name",
        "business_name",
        "contractor_name",
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
    client_tag: str = ""
    status_mode: str = "columns"

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
        blank = _NOT_BLANK.fullmatch(part)
        if blank:
            out.append({"col": blank.group("col"), "op": "not.blank"})
            continue
        not_false = _NOT_FALSE.fullmatch(part)
        if not_false:
            out.append({"col": not_false.group("col"), "op": "is.not.false"})
            continue
        m = _PRED.fullmatch(part)
        if not m:
            raise ValueError(
                "where only allows AND-combined predicates like "
                "\"wf_people_status is null\", \"in_icp is not false\", "
                "\"coalesce(domain,'')<>''\", or \"wf_domain_status = 'resolved'\""
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
    spoken = conversational_company(raw)
    company = spoken or str(item.get("company_name") or "").strip()
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


def filters_to_query(filters: list[dict[str, str]]) -> dict[str, str]:
    query: dict[str, str] = {}
    for item in filters:
        col = item.get("col") or ""
        op = item.get("op") or ""
        if not col:
            continue
        if op == "is.null":
            query[col] = "is.null"
        elif op == "not.is.null":
            query[col] = "not.is.null"
        elif op == "eq":
            query[col] = f"eq.{item.get('value', '')}"
        elif op == "neq":
            query[col] = f"neq.{item.get('value', '')}"
        elif op == "is.not.false":
            query[col] = "not.is.false"
        elif op == "not.blank":
            query[col] = "not.eq."
    return query


def count_source_exact(src: TableSource) -> int | None:
    """Cheap PostgREST count. Falls back to None for schemas PostgREST cannot see."""
    try:
        filters = where_to_filters(src.where)
    except ValueError:
        return None
    if any(item.get("op") == "not.blank" for item in filters):
        return None
    params = {"select": src.key_column or "id"}
    params.update(filters_to_query(filters))
    return supabase_sync.rest_exact_count(src.table, params=params, schema=src.schema)


def _and_where(*parts: str) -> str:
    bits = [p.strip() for p in parts if (p or "").strip()]
    return " and ".join(bits)


def count_source_with_domain(src: TableSource, cap: int = 50_000) -> int:
    extra = TableSource(
        project_id=src.project_id,
        schema=src.schema,
        table=src.table,
        where=_and_where(src.where, "domain is not null"),
        key_column=src.key_column,
        column_map=dict(src.column_map),
        limit=src.limit,
        writeback=False,
    )
    return count_source(extra, cap=cap)


def count_source(src: TableSource, cap: int = 50_000) -> int:
    exact = count_source_exact(src)
    if exact is not None:
        if src.limit is not None:
            return min(exact, int(src.limit))
        return exact
    n = 0
    for _ in iter_source(src):
        n += 1
        if n >= cap:
            break
    return n


def ensure_people_writeback(src: TableSource) -> None:
    if not src.writeback:
        return
    supabase_sync.rpc(
        "pw_ensure_people_writeback",
        {"p_schema": src.schema, "p_table": src.table},
    )


def writeback_people(
    src: TableSource,
    source_key: Any,
    *,
    count: int,
    source: str,
    status: str,
    reason: str = "",
    email_pattern: str = "",
    email_pattern_confidence: float | None = None,
) -> None:
    if not src.writeback:
        return
    if source_key in (None, ""):
        raise RuntimeError(f"cannot write {src.qualified} status without a source key")
    fields: dict[str, Any] = {
        "wf_people_count": count,
        "wf_people_source": source,
        "wf_people_status": status,
    }
    if reason:
        fields["wf_people_reason"] = reason
    if email_pattern:
        fields["wf_email_pattern"] = email_pattern
    if email_pattern_confidence is not None:
        fields["wf_email_pattern_conf"] = email_pattern_confidence
    for forbidden in FORBIDDEN_WRITE:
        fields.pop(forbidden, None)
    if src.status_mode == "sidecar":
        from datetime import datetime, timezone

        supabase_sync.rest_upsert(
            "wf_people_status",
            [
                {
                    "client_tag": src.client_tag or "",
                    "source_table": src.qualified,
                    "source_key": str(source_key),
                    "status": status,
                    "reason": reason or None,
                    "updated_at": datetime.now(timezone.utc).isoformat(),
                }
            ],
            on_conflict="client_tag,source_table,source_key",
        )
        return
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
