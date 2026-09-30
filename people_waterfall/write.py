"""Write accepted people and name_bank rows. Never touch dl_status / sg_exclude / skip_*."""

from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Any, Callable

from . import supabase_sync
from .people import PersonHit, normalize_name
from .profile import ClientProfile
from .source import split_qualified
from .titles import TitleAudit

WRITE_CHUNK = 500
WRITE_ATTEMPTS = 3
CONTACTS_CONFLICT = "client_tag,domain,first_name_key,last_name_key"
NAME_BANK_COLUMNS = (
    "client_tag",
    "domain",
    "first_name",
    "last_name",
    "job_title",
    "linkedin_url",
    "source",
    "status",
    "rejection_reason",
)

CONTACT_COLUMNS = (
    "first_name",
    "last_name",
    "job_title",
    "title_match",
    "title_rank",
    "linkedin_url",
    "domain",
    "company_name",
    "source_tier",
    "source_confidence",
    "phone",
    "person_city",
    "person_state",
    "email",
)
FORBIDDEN = ("dl_status", "sg_exclude")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def contact_payload(
    person: PersonHit,
    audit: TitleAudit,
    *,
    client_tag: str,
    company_name: str,
    domain: str,
    source_tier: str,
    source_confidence: float,
) -> dict[str, Any]:
    row = {
        "first_name": person.first_name or None,
        "last_name": person.last_name or None,
        "job_title": person.title or None,
        "title_match": bool(audit.title_match),
        "title_rank": audit.title_rank,
        "linkedin_url": person.linkedin_url or None,
        "domain": (domain or person.domain or "").strip().lower() or "",
        "company_name": company_name or person.company_name or None,
        "source_tier": source_tier or person.source_tier,
        "source_confidence": source_confidence,
        "phone": person.phone or None,
        "person_city": person.person_city or None,
        "person_state": person.person_state or None,
        "email": person.email or None,
        "client_tag": client_tag,
        "updated_at": _now(),
        # Compatibility aliases on older wf_contacts tables.
        "cellphone": person.phone or None,
        "contact_city": person.person_city or None,
        "contact_state": person.person_state or None,
        "confidence": source_confidence,
        "source_tool": "people-waterfall",
    }
    for key in FORBIDDEN:
        row.pop(key, None)
    if any(k.startswith("skip_") for k in row):
        row = {k: v for k, v in row.items() if not k.startswith("skip_")}
    return row


def _rank_key(row: dict[str, Any]) -> tuple[int, int]:
    """Lower title_rank wins. A missing rank is worse than any real rank."""
    raw = row.get("title_rank")
    if raw is None or raw == "":
        return (1, 0)
    try:
        return (0, int(raw))
    except (TypeError, ValueError):
        return (1, 0)


def person_conflict_key(row: dict[str, Any]) -> tuple[str, str, str, str]:
    return (
        str(row.get("client_tag") or ""),
        str(row.get("domain") or "").strip().lower(),
        str(row.get("first_name") or "").strip().lower(),
        str(row.get("last_name") or "").strip().lower(),
    )


def dedupe_person_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """One row per (client_tag, domain, lower first, lower last). Best title_rank stays."""
    best: dict[tuple[str, str, str, str], dict[str, Any]] = {}
    order: list[tuple[str, str, str, str]] = []
    for row in rows:
        key = person_conflict_key(row)
        current = best.get(key)
        if current is None:
            best[key] = row
            order.append(key)
            continue
        if _rank_key(row) < _rank_key(current):
            best[key] = row
    return [best[key] for key in order]


def call_with_retry(fn: Callable[[], Any]) -> Any:
    delay = 0.5
    last: Exception | None = None
    for attempt in range(WRITE_ATTEMPTS):
        try:
            return fn()
        except Exception as exc:  # noqa: BLE001
            last = exc
            if attempt + 1 >= WRITE_ATTEMPTS:
                raise
            time.sleep(delay)
            delay *= 2
    if last:
        raise last
    return None


def _write_chunks(rows: list[dict[str, Any]], sender: Callable[[list[dict[str, Any]]], int]) -> int:
    written = 0
    for i in range(0, len(rows), WRITE_CHUNK):
        chunk = rows[i : i + WRITE_CHUNK]
        call_with_retry(lambda chunk=chunk: sender(chunk))
        written += len(chunk)
    return written


def _contact_keys(row: dict[str, Any]) -> dict[str, Any]:
    item = dict(row)
    item["domain"] = str(item.get("domain") or "").strip().lower()
    item["first_name_key"] = str(item.get("first_name") or "").strip().lower()
    item["last_name_key"] = str(item.get("last_name") or "").strip().lower()
    return item


def write_contacts(profile: ClientProfile, rows: list[dict[str, Any]]) -> int:
    if not rows:
        return 0
    table = profile.contacts_table_name
    try:
        supabase_sync.rpc(
            "pw_ensure_contacts_columns",
            {"p_table": table},
        )
    except RuntimeError:
        pass
    prepared = [_contact_keys(row) for row in dedupe_person_rows(rows)]
    return _write_chunks(
        prepared,
        lambda chunk: supabase_sync.rest_upsert(
            table,
            chunk,
            on_conflict=CONTACTS_CONFLICT,
            batch_size=WRITE_CHUNK,
        ),
    )


def name_bank_row(
    *,
    client_tag: str,
    domain: str,
    person: PersonHit,
    source: str,
) -> dict[str, Any]:
    return {
        "client_tag": client_tag,
        "domain": (domain or person.domain or "").strip().lower() or "",
        "first_name": person.first_name or None,
        "last_name": person.last_name or None,
        "job_title": person.title or None,
        "linkedin_url": person.linkedin_url or None,
        "source": source or person.source_tier,
        "status": "wrong_title",
        "rejection_reason": person.rejection_reason or "title",
    }


def write_name_bank_rows(rows: list[dict[str, Any]]) -> int:
    """Upsert name_bank rows. Raises on failure. Duplicate keys are merges."""
    if not rows:
        return 0
    prepared = []
    for row in dedupe_person_rows(rows):
        prepared.append({k: v for k, v in row.items() if k in NAME_BANK_COLUMNS})
    return _write_chunks(
        prepared,
        lambda chunk: supabase_sync.rest_upsert(
            "name_bank",
            chunk,
            on_conflict="client_tag,domain,first_name,last_name",
            batch_size=WRITE_CHUNK,
        ),
    )


def write_name_bank(
    *,
    client_tag: str,
    domain: str,
    person: PersonHit,
    source: str,
) -> None:
    write_name_bank_rows(
        [name_bank_row(client_tag=client_tag, domain=domain, person=person, source=source)]
    )


def _page_known(schema: str, table: str) -> list[dict[str, Any]]:
    """Read every row. ew_read_source caps a page at 500; follow the id cursor."""
    cursor: str | None = None
    out: list[dict[str, Any]] = []
    while True:
        data = supabase_sync.rpc(
            "ew_read_source",
            {
                "p_schema": schema,
                "p_table": table,
                "p_filters": [],
                "p_columns": ["id", "domain", "first_name", "last_name"],
                "p_key_column": "id",
                "p_after": cursor,
                "p_limit": 500,
            },
        )
        rows = [row for row in (data if isinstance(data, list) else []) if isinstance(row, dict)]
        if not rows:
            break
        out.extend(rows)
        if len(rows) < 500:
            break
        last_id = rows[-1].get("id")
        if last_id is None:
            break
        nxt = str(last_id)
        if nxt == cursor:
            break
        cursor = nxt
    return out


def load_known_names(profile: ClientProfile) -> set[tuple[str, str]]:
    """(domain, normalized name) already owned or globally suppressed.

    profile.contacts_table already includes the schema. Do not prefix public. again.
    """
    known: set[tuple[str, str]] = set()
    tables = [profile.contacts_table, *profile.cache_tables, "public.name_bank"]
    for qualified in tables:
        try:
            schema, table = split_qualified(qualified)
        except ValueError:
            continue
        if not table:
            continue
        try:
            rows = _page_known(schema, table)
        except RuntimeError:
            continue
        for raw in rows:
            domain = str(raw.get("domain") or "").strip().lower()
            key = normalize_name(
                str(raw.get("first_name") or ""), str(raw.get("last_name") or "")
            )
            if domain and key:
                known.add((domain, key))
    known.update(_suppression_names())
    return known


def _suppression_names() -> set[tuple[str, str]]:
    """Response-based only: positive replies, DNC, Wrong Person, public.suppression."""
    out: set[tuple[str, str]] = set()
    try:
        rows = supabase_sync.rest_select(
            "suppression",
            params={
                "select": "email_domain,first_name,last_name,reason",
                "or": (
                    "(reason.ilike.*do not contact*,reason.ilike.*wrong person*,"
                    "reason.ilike.*positive*,reason.ilike.*dnc*)"
                ),
                "limit": "2000",
            },
        )
    except RuntimeError:
        return out
    for raw in rows:
        domain = str(raw.get("email_domain") or "").strip().lower()
        key = normalize_name(
            str(raw.get("first_name") or ""), str(raw.get("last_name") or "")
        )
        if domain and key:
            out.add((domain, key))
    return out


def is_known(known: set[tuple[str, str]], domain: str, person: PersonHit) -> bool:
    dom = (domain or person.domain or "").strip().lower()
    key = person.name_key
    if not key:
        return False
    if (dom, key) in known:
        return True
    if ("", key) in known:
        return True
    return False
