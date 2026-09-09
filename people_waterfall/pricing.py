"""Live unit prices and cheapest-to-most-expensive sort.

Always-billed tiers sort on unit price per person.
Free-on-miss tiers sort on unit price times measured hit rate
(default half the unit price until a receipt measures it).
Ties break on measured rate. Free tiers stay first.
A receipt may drop a zero-yield tier; it never reorders the rule.
"""

from __future__ import annotations

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
    "getleads": {
        "needs": "domain",
        "billing": "free",
        "credits": 0.0,
        "unit_usd": 0.0,
        "receipt_lanes": ("domain",),
    },
    "smartlead": {
        "needs": "domain",
        "billing": "free",
        "credits": 0.0,
        "unit_usd": 0.0,
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
    "aiark": {
        "needs": "either",
        "billing": "always",
        "credits": 0.5,
        "unit_usd_low": 0.001,
        "unit_usd_high": 0.0049,
        "receipt_lanes": ("domain", "name"),
    },
    "serp": {
        "needs": "name",
        "billing": "always",
        "credits": 0.0,
        "unit_usd": 0.0045,
        "unit_usd_per_person": 0.03,
        "receipt_lanes": ("name",),
    },
    "prospeo": {
        "needs": "either",
        "billing": "free_on_miss",
        "credits": 1.0,
        "unit_usd_low": 0.007,
        "unit_usd_high": 0.039,
        "receipt_lanes": ("domain", "name"),
    },
    "leadmagic_role": {
        "needs": "either",
        "billing": "free_on_miss",
        "credits": 1.0,
        "unit_usd_low": 0.0104,
        "unit_usd_high": 0.0245,
        "receipt_lanes": ("domain", "name"),
        "search_may_be_free": True,
    },
}

TIER_ALIASES = {
    "local": "cache",
    "local_cache": "cache",
    "lm_employee": "leadmagic_employee",
    "employee_finder": "leadmagic_employee",
    "leadmagic": "leadmagic_role",
    "lm": "leadmagic_role",
    "find_people_by_role": "leadmagic_role",
    "search_people": "leadmagic_role",
    "ai_ark": "aiark",
    "ark": "aiark",
    "apify": "serp",
    "apify_serp": "serp",
}

DEFAULT_ORDER = [
    "cache",
    "getleads",
    "smartlead",
    "leadmagic_employee",
    "aiark",
    "serp",
    "prospeo",
    "leadmagic_role",
]


def normalize_tier(name: str) -> str:
    raw = (name or "").strip().lower()
    return TIER_ALIASES.get(raw, raw)


@dataclass
class LiveRates:
    leadmagic_credits: float | None = None
    leadmagic_plan: str = ""
    leadmagic_per_credit: float | None = None
    leadmagic_search_free: bool | None = None
    aiark_per_credit: float | None = None
    aiark_credits: float | None = None
    prospeo_per_credit: float | None = None
    prospeo_credits: float | None = None
    notes: list[str] = field(default_factory=list)

    def unit_usd(self, tier: str) -> float:
        meta = PUBLISHED.get(tier) or {}
        if meta.get("billing") == "free":
            return 0.0
        if tier == "leadmagic_role" and self.leadmagic_search_free:
            return 0.0
        if "unit_usd" in meta:
            return float(meta["unit_usd"])
        credits = float(meta.get("credits") or 0)
        if tier.startswith("leadmagic") and self.leadmagic_per_credit is not None:
            return credits * self.leadmagic_per_credit
        if tier == "aiark" and self.aiark_per_credit is not None:
            return credits * self.aiark_per_credit
        if tier == "prospeo" and self.prospeo_per_credit is not None:
            return credits * self.prospeo_per_credit
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
    if tier == "leadmagic_role" and rates.leadmagic_search_free:
        billing = "free"
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

    def _rate(name: str) -> float | None:
        block = measured.get(name)
        if isinstance(block, dict) and block.get("title_match_rate") is not None:
            return float(block["title_match_rate"])
        if isinstance(block, (int, float)):
            return float(block)
        return None

    names.sort(
        key=lambda t: sort_key(
            t, rates=rates, measured_rate=_rate(t), dropped=dropped
        )
    )
    out: list[dict[str, Any]] = []
    for name in names:
        meta = PUBLISHED[name]
        billing = str(meta.get("billing") or "always")
        if name == "leadmagic_role" and rates.leadmagic_search_free:
            billing = "free"
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


def max_tier_cutoff(max_tier: str, order: list[dict[str, Any]]) -> list[str]:
    if not (max_tier or "").strip() or max_tier in {"all", "*"}:
        return [row["tier"] for row in order if not row.get("dropped")]
    stop = normalize_tier(max_tier)
    names: list[str] = []
    for row in order:
        if row.get("dropped"):
            continue
        names.append(row["tier"])
        if row["tier"] == stop:
            break
    return names


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
