"""Live unit prices for the people-tier list.

Default order is site_staff → cache → discolike → prospeo_search →
aiark_people. site_staff is free. A receipt may drop a zero-yield
tier; it never reorders the declared sequence.

Legacy LeadMagic tier names are accepted as no-ops (never silent
substitutes for a paid vendor). Unrecognized names in a custom
people_tier_order are warned, not dropped silently.
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass, field
from typing import Any

log = logging.getLogger("people_waterfall.pricing")

# Josh: AI Ark $220/mo for 60k = $0.003667/cr. Prospeo Growth yearly
# $888 / 60k = $0.0148/cr. Used until a live account rate is read.
AIARK_USD_PER_CREDIT = 0.003667
PROSPEO_USD_PER_CREDIT = 0.0148
AIARK_CREDITS_PER_RESULT = 0.5
AIARK_MAX_PER_COMPANY = 3
PROSPEO_PAGE_SIZE = 25
PROSPEO_MAX_PER_COMPANY = 3

PUBLISHED: dict[str, dict[str, Any]] = {
    "site_staff": {
        "needs": "domain",
        "billing": "free",
        "credits": 0.0,
        "unit_usd": 0.0,
        "receipt_lanes": ("domain",),
    },
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
    "prospeo_search": {
        "needs": "domain",
        "billing": "always",
        "credits": 1.0,
        "unit_usd": PROSPEO_USD_PER_CREDIT,
        "receipt_lanes": ("domain",),
    },
    "aiark_people": {
        "needs": "domain",
        "billing": "per_result",
        "credits": AIARK_CREDITS_PER_RESULT,
        "unit_usd": AIARK_CREDITS_PER_RESULT * AIARK_USD_PER_CREDIT,
        "receipt_lanes": ("domain",),
    },
}

# Canonical legacy names. Aliases below normalize to these, then the
# job drops them. They must not map onto aiark_people / prospeo_search.
LEGACY_NOOP = frozenset(
    {
        "leadmagic_employee",
        "leadmagic_role",
        "leadmagic_search_free",
    }
)

TIER_ALIASES = {
    "local": "cache",
    "local_cache": "cache",
    "lm_employee": "leadmagic_employee",
    "employee_finder": "leadmagic_employee",
    "leadmagic": "leadmagic_employee",
    "lm": "leadmagic_employee",
    "lead_magic": "leadmagic_employee",
    "leadmagic_role_finder": "leadmagic_role",
    "role_finder": "leadmagic_role",
    "lm_role": "leadmagic_role",
    "lm_search": "leadmagic_search_free",
    "disco": "discolike",
    "discogen": "discolike",
    "site": "site_staff",
    "website": "site_staff",
    "staff": "site_staff",
}

DEFAULT_ORDER = [
    "site_staff",
    "cache",
    "discolike",
    "prospeo_search",
    "aiark_people",
]


def normalize_tier(name: str) -> str:
    raw = (name or "").strip().lower()
    return TIER_ALIASES.get(raw, raw)


def is_legacy_noop(name: str) -> bool:
    return normalize_tier(name) in LEGACY_NOOP


@dataclass
class ProfileTiers:
    """Resolved include list plus the names a custom order dropped."""

    include: list[str] | None
    deprecated: list[str] = field(default_factory=list)
    unrecognized: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


def _note_dropped(kind: str, names: list[str]) -> list[str]:
    if not names:
        return []
    joined = ", ".join(names)
    if kind == "deprecated":
        msg = (
            f"deprecated people tiers ignored (no-op, not substituted): {joined}"
        )
    else:
        msg = (
            f"unrecognized people tiers dropped (not in published order): {joined}"
        )
    log.warning(msg)
    return [msg]


@dataclass
class LiveRates:
    aiark_credits: float | None = None
    aiark_per_credit: float | None = None
    prospeo_credits: float | None = None
    prospeo_per_credit: float | None = None
    prospeo_plan: str = ""
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
        credits = float(meta.get("credits") or 0)
        if tier == "prospeo_search":
            per = self.prospeo_per_credit
            if per is None:
                per = PROSPEO_USD_PER_CREDIT
            return credits * per
        if tier == "aiark_people":
            per = self.aiark_per_credit
            if per is None:
                per = AIARK_USD_PER_CREDIT
            return credits * per
        if "unit_usd" in meta:
            return float(meta["unit_usd"])
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
    dropped = {normalize_tier(t) for t in (dropped_tiers or []) if not is_legacy_noop(t)}
    pool = [normalize_tier(t) for t in (include or DEFAULT_ORDER)]
    seen: set[str] = set()
    names: list[str] = []
    for name in pool:
        if name in seen or name not in PUBLISHED or is_legacy_noop(name):
            continue
        seen.add(name)
        names.append(name)
    # site_staff is free and runs before cache / any paid tier.
    # An explicit people_tier_order that omits it still gets it first,
    # unless a receipt dropped it. Paid order after that is unchanged.
    if "site_staff" in dropped and "site_staff" in names:
        names = [name for name in names if name != "site_staff"]
    elif "site_staff" not in dropped:
        names = ["site_staff", *[name for name in names if name != "site_staff"]]
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


def plan_profile_tiers(people_tier_order: list[Any] | None) -> ProfileTiers:
    """Explicit people_tier_order names, else None so DEFAULT_ORDER is used.

    Legacy LeadMagic names and names that are not in PUBLISHED are dropped
    with an explicit warning. They are never substituted for a live vendor.
    """
    if not people_tier_order:
        return ProfileTiers(include=None)
    names: list[str] = []
    seen: set[str] = set()
    deprecated: list[str] = []
    unrecognized: list[str] = []
    for row in people_tier_order:
        if isinstance(row, dict):
            raw = str(row.get("tier") or "")
        else:
            raw = str(row or "")
        name = normalize_tier(raw)
        if not name or name in seen:
            continue
        seen.add(name)
        if is_legacy_noop(name):
            deprecated.append(name)
            continue
        if name not in PUBLISHED:
            unrecognized.append(name)
            continue
        names.append(name)
    notes = _note_dropped("deprecated", deprecated) + _note_dropped(
        "unrecognized", unrecognized
    )
    return ProfileTiers(
        include=names or None,
        deprecated=deprecated,
        unrecognized=unrecognized,
        notes=notes,
    )


def include_from_profile(people_tier_order: list[Any] | None) -> list[str] | None:
    """Explicit people_tier_order names, else None so DEFAULT_ORDER is used."""
    return plan_profile_tiers(people_tier_order).include


def parse_tier_list(value: str | list[str] | None) -> list[str]:
    """Normalize a comma/list of tier names. Legacy names are kept so
    skip_tiers=leadmagic_employee is a harmless no-op against the live order.
    """
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


def classify_request_tiers(
    *,
    min_tier: str = "",
    max_tier: str = "",
    skip_tiers: str | list[str] | None = None,
) -> ProfileTiers:
    """Flag legacy names on the request window. They do not fail the job."""
    deprecated: list[str] = []
    unrecognized: list[str] = []
    for raw in (min_tier, max_tier):
        name = normalize_tier(raw) if (raw or "").strip() and raw not in {"all", "*"} else ""
        if not name:
            continue
        if is_legacy_noop(name):
            deprecated.append(name)
        elif name not in PUBLISHED:
            unrecognized.append(name)
    for name in parse_tier_list(skip_tiers):
        if is_legacy_noop(name) and name not in deprecated:
            deprecated.append(name)
    notes = _note_dropped("deprecated", deprecated) + _note_dropped(
        "unrecognized", unrecognized
    )
    return ProfileTiers(
        include=None,
        deprecated=deprecated,
        unrecognized=unrecognized,
        notes=notes,
    )


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

    Legacy LeadMagic names are no-ops: they do not appear in the window and
    they do not fail the job.
    """
    skip = {name for name in parse_tier_list(skip_tiers) if not is_legacy_noop(name)}
    start_raw = (min_tier or "").strip()
    stop_raw = (max_tier or "").strip()
    start = normalize_tier(start_raw) if start_raw and start_raw not in {"all", "*"} else ""
    stop = normalize_tier(stop_raw) if stop_raw and stop_raw not in {"all", "*"} else ""
    if start and is_legacy_noop(start):
        start = ""
    if stop and is_legacy_noop(stop):
        stop = ""

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
    return [name for name in names if name not in skip and not is_legacy_noop(name)]


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


def estimate_credits(tier: str, domain_rows: int) -> float:
    """Worst-case credits for a paid people tier on N domain rows."""
    n = max(0, int(domain_rows))
    if tier == "prospeo_search":
        # 1 credit per page of 25; cover-loop worst case is 1 credit / domain.
        return float(n)
    if tier == "aiark_people":
        return n * AIARK_CREDITS_PER_RESULT * AIARK_MAX_PER_COMPANY
    return 0.0
