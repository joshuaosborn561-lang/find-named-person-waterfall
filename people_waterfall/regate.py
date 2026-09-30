"""Re-apply the current profile gate to name_bank. No vendor calls."""

from __future__ import annotations

from typing import Any

from . import supabase_sync
from .gate import REJECTION_REASONS, gate_person
from .people import PersonHit
from .profile import ClientProfile, get_profile, normalize_client_tag
from .titles import TitleAudit
from .write import contact_payload, write_contacts

PAGE = 500


def _page(table: str, params: dict[str, str]) -> list[dict[str, Any]]:
    rows = supabase_sync.rest_select(table, params=params)
    return [row for row in rows if isinstance(row, dict)]


def _known_contacts(profile: ClientProfile) -> set[tuple[str, str, str]]:
    known: set[tuple[str, str, str]] = set()
    cursor = 0
    while True:
        rows = _page(
            profile.contacts_table_name,
            {
                "select": "id,domain,first_name,last_name",
                "id": f"gt.{cursor}",
                "order": "id.asc",
                "limit": str(PAGE),
            },
        )
        if not rows:
            break
        for row in rows:
            cursor = int(row.get("id") or cursor)
            known.add(
                (
                    str(row.get("domain") or "").strip().lower(),
                    str(row.get("first_name") or "").strip().lower(),
                    str(row.get("last_name") or "").strip().lower(),
                )
            )
        if len(rows) < PAGE:
            break
    return known


def _person_from_bank(row: dict[str, Any]) -> PersonHit:
    domain = str(row.get("domain") or "").strip().lower()
    return PersonHit(
        first_name=str(row.get("first_name") or "").strip(),
        last_name=str(row.get("last_name") or "").strip(),
        title=str(row.get("job_title") or "").strip(),
        linkedin_url=str(row.get("linkedin_url") or "").strip(),
        domain=domain,
        source_tier=str(row.get("source") or "").strip(),
        rejection_reason=str(row.get("rejection_reason") or "").strip(),
    )


def regate_name_bank(client_tag: str) -> dict[str, Any]:
    """Promote banked rows that pass the current profile. Free."""
    tag = normalize_client_tag(client_tag)
    profile = get_profile(tag)
    known = _known_contacts(profile)
    examined = 0
    promoted = 0
    already = 0
    written = 0
    by_reason = {reason: 0 for reason in REJECTION_REASONS}
    cursor = 0
    while True:
        rows = _page(
            "name_bank",
            {
                "select": (
                    "id,client_tag,domain,first_name,last_name,job_title,"
                    "linkedin_url,source,status,rejection_reason"
                ),
                "client_tag": f"eq.{tag}",
                "or": "(status.is.null,status.neq.promoted)",
                "id": f"gt.{cursor}",
                "order": "id.asc",
                "limit": str(PAGE),
            },
        )
        if not rows:
            break
        passes: list[tuple[dict[str, Any], PersonHit, TitleAudit, float]] = []
        updates: list[dict[str, Any]] = []
        for row in rows:
            cursor = int(row.get("id") or cursor)
            examined += 1
            person = _person_from_bank(row)
            decision = gate_person(
                person,
                profile,
                company_name="",
                domain=person.domain,
                use_fallback=False,
            )
            key = (
                person.domain,
                person.first_name.strip().lower(),
                person.last_name.strip().lower(),
            )
            if not decision.reason and decision.audit is not None:
                promoted += 1
                if key in known:
                    already += 1
                else:
                    passes.append((row, person, decision.audit, decision.confidence))
                    known.add(key)
                updates.append(
                    {
                        "client_tag": tag,
                        "domain": person.domain,
                        "first_name": person.first_name,
                        "last_name": person.last_name,
                        "job_title": person.title or None,
                        "linkedin_url": person.linkedin_url or None,
                        "source": person.source_tier or None,
                        "status": "promoted",
                        "rejection_reason": None,
                    }
                )
                continue
            reason = decision.reason or "title"
            by_reason[reason] = by_reason.get(reason, 0) + 1
            person.rejection_reason = reason
            updates.append(
                {
                    "client_tag": tag,
                    "domain": person.domain,
                    "first_name": person.first_name,
                    "last_name": person.last_name,
                    "job_title": person.title or None,
                    "linkedin_url": person.linkedin_url or None,
                    "source": person.source_tier or None,
                    "status": "wrong_title",
                    "rejection_reason": reason,
                }
            )
        if passes:
            payloads = [
                contact_payload(
                    person,
                    audit,
                    client_tag=tag,
                    company_name="",
                    domain=person.domain,
                    source_tier=person.source_tier or "name_bank",
                    source_confidence=confidence,
                )
                for _row, person, audit, confidence in passes
            ]
            written += write_contacts(profile, payloads)
        if updates:
            supabase_sync.rest_upsert(
                "name_bank",
                updates,
                on_conflict="client_tag,domain,first_name,last_name",
            )
        if len(rows) < PAGE:
            break
    return {
        "ok": True,
        "client_tag": tag,
        "examined": examined,
        "promoted": promoted,
        "already_in_contacts": already,
        "written": written,
        "still_banked": examined - promoted,
        "by_reason": by_reason,
    }
