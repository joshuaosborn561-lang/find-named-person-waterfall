"""First-class progress counter for MCP + HTTP job polling."""

from __future__ import annotations

from typing import Any


def build_counter(
    *,
    done: int,
    total: int | None,
    title_matched: int = 0,
    name_bank: int = 0,
    companies_with_people: int = 0,
    companies_unresolved: int = 0,
    phase: str = "running",
) -> dict[str, Any]:
    remaining: int | None
    pct: float | None
    if total is None or total < 0:
        remaining = None
        pct = None
        message = f"{phase}: {done} companies processed"
    else:
        remaining = max(0, total - done)
        pct = round(100.0 * done / total, 1) if total else 100.0
        message = f"{phase}: {done}/{total} companies ({pct}%)"
    return {
        "done": int(done),
        "total": total,
        "remaining": remaining,
        "pct": pct,
        "title_matched": int(title_matched),
        "name_bank": int(name_bank),
        "companies_with_people": int(companies_with_people),
        "companies_unresolved": int(companies_unresolved),
        "phase": phase,
        "message": message,
    }


def counter_from_stats(stats: dict[str, Any], *, total: int | None, phase: str = "running") -> dict[str, Any]:
    return build_counter(
        done=int(stats.get("companies") or 0),
        total=total,
        title_matched=int(stats.get("title_matched") or 0),
        name_bank=int(stats.get("name_bank") or 0),
        companies_with_people=int(stats.get("companies_with_people") or 0),
        companies_unresolved=int(stats.get("companies_unresolved") or 0),
        phase=phase,
    )
