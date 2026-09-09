"""Write accepted people and name_bank rows. Never touch dl_status / sg_exclude / skip_*."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from . import supabase_sync
from .people import PersonHit, normalize_name
from .profile import ClientProfile
from .titles import TitleAudit

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
        "domain": (domain or person.domain or "").strip().lower() or None,
        "company_name": company_name or person.company_name or None,
        "source_tier": source_tier or person.source_tier,
        "source_confidence": source_confidence,
        "phone": person.phone or None,
        "person_city": person.person_city or None,
        "person_state": person.person_state or None,
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


def write_contacts(profile: ClientProfile, rows: list[dict[str, Any]]) -> int:
    if not rows:
        return 0
    try:
        supabase_sync.rpc(
            "pw_ensure_contacts_columns",
            {"p_table": profile.contacts_table},
        )
    except RuntimeError:
        pass
    return supabase_sync.rest_insert(profile.contacts_table, rows)


def write_name_bank(
    *,
    client_tag: str,
    domain: str,
    person: PersonHit,
    source: str,
) -> None:
    row = {
        "client_tag": client_tag,
        "domain": (domain or person.domain or "").strip().lower() or None,
        "first_name": person.first_name or None,
        "last_name": person.last_name or None,
        "job_title": person.title or None,
        "linkedin_url": person.linkedin_url or None,
        "source": source or person.source_tier,
        "status": "wrong_title",
    }
    try:
        supabase_sync.rest_insert("name_bank", [row])
    except RuntimeError:
        return


def load_known_names(profile: ClientProfile) -> set[tuple[str, str]]:
    """(domain, normalized name) already owned or globally suppressed."""
    known: set[tuple[str, str]] = set()
    tables = [f"public.{profile.contacts_table}", *profile.cache_tables, "public.name_bank"]
    for qualified in tables:
        schema, table = (
            qualified.split(".", 1) if "." in qualified else ("public", qualified)
        )
        try:
            data = supabase_sync.rpc(
                "ew_read_source",
                {
                    "p_schema": schema,
                    "p_table": table,
                    "p_filters": [],
                    "p_columns": ["domain", "first_name", "last_name"],
                    "p_key_column": "domain",
                    "p_after": None,
                    "p_limit": 500,
                },
            )
        except RuntimeError:
            continue
        rows = data if isinstance(data, list) else []
        # Page a few times; cache check is best-effort, not a full dump.
        for raw in rows[:500]:
            if not isinstance(raw, dict):
                continue
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
