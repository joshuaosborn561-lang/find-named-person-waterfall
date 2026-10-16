"""Verify profile, source, contacts, and writeback columns before any paid call."""

from __future__ import annotations

import os
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

SITE_PAGES_REQUIRED = ("url", "body_text", "emails", "title")


class ToolError(Exception):
    def __init__(self, message: str, stage: str) -> None:
        self.stage = stage
        super().__init__(message)


def site_people_key() -> str:
    """Read SITE_PEOPLE_KEY from the process env only. Never log the value."""
    return (os.environ.get("SITE_PEOPLE_KEY") or "").strip()


def require_site_people_key() -> str:
    """Fail fast when site_people would run with a missing or empty key."""
    key = site_people_key()
    if not key:
        raise ToolError(
            "SITE_PEOPLE_KEY is missing or empty; site_people cannot run with an empty key",
            "site_people_key",
        )
    return key


def preflight_selected_tiers(tiers: list[str]) -> None:
    """Refuse a selected site_people window before any vendor call."""
    if "site_people" in {str(t) for t in tiers}:
        require_site_people_key()


def preflight_site_staff_columns() -> None:
    """site_staff cannot run when public.site_pages is missing required columns."""
    try:
        cols = _columns("public", "site_pages")
    except ToolError:
        raise
    missing = [name for name in SITE_PAGES_REQUIRED if name not in cols]
    if missing:
        raise ToolError(
            "site_staff requires public.site_pages columns "
            f"{', '.join(SITE_PAGES_REQUIRED)}; missing {', '.join(missing)}",
            "site_staff",
        )


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
