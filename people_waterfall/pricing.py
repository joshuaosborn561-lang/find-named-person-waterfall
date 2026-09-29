"""Live unit prices for the people-tier list.

Default order is cache → discolike → leadmagic_employee. A receipt may
drop a zero-yield tier; it never reorders the declared sequence.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from typing import Any

# Published 2026-09-09 midpoints used only until the live account rate is read.
PUBLISHED: dict[str, dict[str, Any]] = {
    "cache": {
        "needs": "either",
        "billing": "free",
        "credits": 0.0,
        "unit_usd": 0.0,
        "receipt_lanes": ("domain", "name"),
    },
    "discolike": {
        "needs": "domain",
        "billing": "always",
        "credits": 0.0,
        "receipt_lanes": ("domain",),
    },
    "leadmagic_employee": {
        "needs": "domain",
        "billing": "always",
        "credits": 0.05,
        "unit_usd_low": 0.00052,
        "unit_usd_high": 0.0012,
        "receipt_lanes": ("domain",),
    },
}

TIER_ALIASES = {
    "local": "cache",
    "local_cache": "cache",
    "lm_employee": "leadmagic_employee",
    "employee_finder": "leadmagic_employee",
    "leadmagic": "leadmagic_employee",
    "lm": "leadmagic_employee",
    "disco": "discolike",
    "discogen": "discolike",
}

DEFAULT_ORDER = [
    "cache",
    "discolike",
    "leadmagic_employee",
]


def normalize_tier(name: str) -> str:
    raw = (name or "").strip().lower()
    return TIER_ALIASES.get(raw, raw)


@dataclass
class LiveRates:
    leadmagic_credits: float | None = None
    leadmagic_plan: str = ""
    leadmagic_per_credit: float | None = None
    notes: list[str] = field(default_factory=list)

    def unit_usd(self, tier: str) -> float:
        meta = PUBLISHED.get(tier) or {}
        if meta.get("billing") == "free":
            return 0.0
        if tier == "discolike":
            try:
                serper = float(os.environ.get("SERPER_USD_PER_QUERY") or 0.001)
            except ValueError:
                serper = 0.001
            try:
                company = float(os.environ.get("DISCOLIKE_USD_PER_COMPANY") or 0.0035)
            except ValueError:
                company = 0.0035
            return serper * 2 + company
        if "unit_usd" in meta:
            return float(meta["unit_usd"])
        credits = float(meta.get("credits") or 0)
        if tier.startswith("leadmagic") and self.leadmagic_per_credit is not None:
            return credits * self.leadmagic_per_credit
        low = meta.get("unit_usd_low")
        high = meta.get("unit_usd_high")
        if low is not None and high is not None:
            return (float(low) + float(high)) / 2.0
        return float(meta.get("unit_usd_per_person") or 0.0)


def sort_key(
    tier: str,
    *,
    rates: LiveRates,
    measured_rate: float | None,
    dropped: set[str],
) -> tuple[int, float, float]:
    """(bucket, sort_price, -measured_rate). Lower is cheaper / earlier."""
    if tier in dropped:
        return (9, 1e9, 0.0)
    meta = PUBLISHED.get(tier) or {}
    billing = str(meta.get("billing") or "always")
    unit = rates.unit_usd(tier)
    rate = measured_rate if measured_rate is not None else 0.5
    if billing == "free":
        return (0, 0.0, -(measured_rate or 0.0))
    if billing == "free_on_miss":
        return (1, unit * rate, -rate)
    return (1, unit, -(measured_rate or 0.0))


def compute_tier_order(
    *,
    rates: LiveRates,
    measured_rates: dict[str, Any] | None = None,
    dropped_tiers: list[str] | None = None,
    include: list[str] | None = None,
) -> list[dict[str, Any]]:
    measured = measured_rates or {}
    dropped = {normalize_tier(t) for t in (dropped_tiers or [])}
    pool = [normalize_tier(t) for t in (include or DEFAULT_ORDER)]
    seen: set[str] = set()
    names: list[str] = []
    for name in pool:
        if name in seen or name not in PUBLISHED:
            continue
        seen.add(name)
        names.append(name)
    # Default (and explicit people_tier_order) keep declared sequence.
    # Receipt may drop a zero-yield tier; it does not cheapest-sort this list.

    def _rate(name: str) -> float | None:
        block = measured.get(name)
        if isinstance(block, dict) and block.get("title_match_rate") is not None:
            return float(block["title_match_rate"])
        if isinstance(block, (int, float)):
            return float(block)
        return None

    out: list[dict[str, Any]] = []
    for name in names:
        meta = PUBLISHED[name]
        billing = str(meta.get("billing") or "always")
        out.append(
            {
                "tier": name,
                "needs": meta.get("needs"),
                "billing": billing,
                "unit_usd": rates.unit_usd(name),
                "credits": 0.0 if billing == "free" else meta.get("credits"),
                "measured_rate": _rate(name),
                "dropped": name in dropped,
                "receipt_lanes": list(meta.get("receipt_lanes") or []),
            }
        )
    return out


def include_from_profile(people_tier_order: list[Any] | None) -> list[str] | None:
    """Explicit people_tier_order names, else None so DEFAULT_ORDER is used."""
    if not people_tier_order:
        return None
    names: list[str] = []
    seen: set[str] = set()
    for row in people_tier_order:
        if isinstance(row, dict):
            raw = str(row.get("tier") or "")
        else:
            raw = str(row or "")
        name = normalize_tier(raw)
        if not name or name in seen or name not in PUBLISHED:
            continue
        seen.add(name)
        names.append(name)
    return names or None


def parse_tier_list(value: str | list[str] | None) -> list[str]:
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        parts = [str(x) for x in value]
    else:
        parts = re.split(r"[,\s]+", str(value))
    out: list[str] = []
    seen: set[str] = set()
    for part in parts:
        name = normalize_tier(part)
        if not name or name in seen:
            continue
        seen.add(name)
        out.append(name)
    return out


def select_tiers(
    order: list[dict[str, Any]],
    *,
    max_tier: str = "all",
    min_tier: str = "",
    skip_tiers: str | list[str] | None = None,
) -> list[str]:
    """Window the people-tier list.

    min_tier / max_tier slice the live order (inclusive). skip_tiers removes
    named tiers. An explicit min or max can include a dropped tier so a job
    can run that tier alone after a receipt dropped it.
    """
    skip = set(parse_tier_list(skip_tiers))
    start_raw = (min_tier or "").strip()
    stop_raw = (max_tier or "").strip()
    start = normalize_tier(start_raw) if start_raw and start_raw not in {"all", "*"} else ""
    stop = normalize_tier(stop_raw) if stop_raw and stop_raw not in {"all", "*"} else ""

    if start or stop:
        names = [row["tier"] for row in order]
        for extra in (start, stop):
            if extra and extra in PUBLISHED and extra not in names:
                names.append(extra)
    else:
        names = [row["tier"] for row in order if not row.get("dropped")]

    if start and start in names:
        names = names[names.index(start) :]
    if stop and stop in names:
        names = names[: names.index(stop) + 1]
    return [name for name in names if name not in skip]


def max_tier_cutoff(max_tier: str, order: list[dict[str, Any]]) -> list[str]:
    return select_tiers(order, max_tier=max_tier)


def lane_tiers(order: list[dict[str, Any]], lane: str) -> list[str]:
    """domain lane = domain or either. name lane = name or either."""
    out: list[str] = []
    for row in order:
        if row.get("dropped"):
            continue
        needs = row.get("needs")
        if lane == "domain" and needs in {"domain", "either"}:
            out.append(row["tier"])
        elif lane == "name" and needs in {"name", "either"}:
            out.append(row["tier"])
    return out
