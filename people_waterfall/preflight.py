"""Verify profile, source, contacts, and writeback columns before any paid call."""

from __future__ import annotations

from typing import Any

from . import supabase_sync
from .profile import ClientProfile, get_profile, normalize_client_tag
from .source import (
    PEOPLE_WRITEBACK,
    TableSource,
    discover_column_map,
    ensure_people_writeback,
    list_columns,
    parse_source,
    where_to_filters,
)


class ToolError(Exception):
    def __init__(self, message: str, stage: str) -> None:
        self.stage = stage
        super().__init__(message)


def _columns(schema: str, table: str) -> set[str]:
    src = TableSource(project_id="", schema=schema, table=table, writeback=False)
    try:
        cols = list_columns(src)
    except Exception as exc:  # noqa: BLE001
        raise ToolError(f"{type(exc).__name__}: {exc}", "source_table") from exc
    if not cols:
        raise ToolError(f"{schema}.{table} is not readable", "source_table")
    return cols


def preflight_job(
    *,
    client_tag: str,
    source_table: str,
    where: str = "",
    alter: bool = False,
    profile: ClientProfile | None = None,
    src: TableSource | None = None,
) -> dict[str, Any]:
    """Load the profile and prove the source can be read.

    alter=True adds missing wf_people_* columns. If that cannot be done,
    later writeback uses public.wf_people_status. estimate_only passes
    alter=False and does not change schema.
    """
    try:
        tag = normalize_client_tag(client_tag)
    except Exception as exc:  # noqa: BLE001
        raise ToolError(f"{type(exc).__name__}: {exc}", "profile") from exc
    try:
        loaded = profile or get_profile(tag)
    except Exception as exc:  # noqa: BLE001
        raise ToolError(f"{type(exc).__name__}: {exc}", "profile") from exc
    try:
        parsed = src or parse_source(source_table, where, writeback=alter)
    except Exception as exc:  # noqa: BLE001
        raise ToolError(f"{type(exc).__name__}: {exc}", "source_table") from exc
    parsed.client_tag = tag
    try:
        where_to_filters(parsed.where)
    except Exception as exc:  # noqa: BLE001
        raise ToolError(f"{type(exc).__name__}: {exc}", "where") from exc
    try:
        source_cols = _columns(parsed.schema, parsed.table)
        discover_column_map(parsed)
    except ToolError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise ToolError(f"{type(exc).__name__}: {exc}", "source_table") from exc
    contacts = loaded.contacts_table
    if "." in contacts:
        cschema, ctable = contacts.split(".", 1)
    else:
        cschema, ctable = "public", contacts
    try:
        _columns(cschema, ctable)
    except ToolError as exc:
        raise ToolError(str(exc), "contacts_table") from exc
    except Exception as exc:  # noqa: BLE001
        raise ToolError(f"{type(exc).__name__}: {exc}", "contacts_table") from exc

    mode = "unchecked"
    if alter and parsed.writeback:
        try:
            ensure_people_writeback(parsed)
            after = list_columns(parsed)
            missing = [name for name in PEOPLE_WRITEBACK if name not in after]
            if missing:
                raise RuntimeError(
                    f"{parsed.qualified} missing {', '.join(missing)} after ensure"
                )
            if not after:
                raise RuntimeError(f"{parsed.qualified} columns unreadable after ensure")
            parsed.status_mode = "columns"
            mode = "columns"
        except Exception as exc:  # noqa: BLE001
            try:
                supabase_sync.rest_select(
                    "wf_people_status",
                    params={"select": "client_tag", "limit": "1"},
                )
            except Exception as side_exc:  # noqa: BLE001
                raise ToolError(
                    f"{type(exc).__name__}: {exc}; sidecar: {type(side_exc).__name__}: {side_exc}",
                    "preflight",
                ) from exc
            parsed.status_mode = "sidecar"
            mode = "sidecar"
    return {
        "ok": True,
        "stage": "preflight",
        "client_tag": tag,
        "source_table": parsed.qualified,
        "contacts_table": loaded.contacts_table,
        "source_columns": sorted(source_cols),
        "writeback": mode,
    }
