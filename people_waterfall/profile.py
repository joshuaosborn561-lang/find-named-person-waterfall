"""Read public.wf_client_profiles. Nothing industry-specific lives here."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

from . import supabase_sync
from .config import settings

_TAG_RE = re.compile(r"^[a-z][a-z0-9_]{0,46}$")
RESERVED = frozenset(
    {
        "lp",
        "public",
        "gc",
        "storage",
        "auth",
        "shared",
        "common",
        "default",
        "all",
    }
)


def normalize_client_tag(value: str | None) -> str:
    raw = (value or "").strip().lower()
    if not raw:
        raise ValueError("client_tag is required")
    safe = re.sub(r"[^a-z0-9_]", "", raw)
    if safe in RESERVED or safe.startswith("pg_"):
        raise ValueError(f"client_tag {value!r} is reserved")
    if not _TAG_RE.match(safe):
        raise ValueError(f"client_tag must be snake_case; got {value!r}")
    return safe


def _as_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [p.strip() for p in value.split(",") if p.strip()]
    if isinstance(value, (list, tuple)):
        return [str(x).strip() for x in value if str(x).strip()]
    return []


def _as_str_map(value: Any) -> dict[str, str]:
    if not isinstance(value, dict):
        return {}
    return {str(k): str(v) for k, v in value.items() if k and v}


def _table_ref(value: Any) -> dict[str, str]:
    if isinstance(value, str) and value.strip():
        raw = value.strip()
        if "." in raw:
            schema, table = raw.split(".", 1)
            return {"schema": schema, "table": table, "qualified": raw}
        return {"schema": "public", "table": raw, "qualified": f"public.{raw}"}
    if isinstance(value, dict):
        schema = str(value.get("schema") or "public").strip() or "public"
        table = str(value.get("table") or "").strip()
        where = str(value.get("where") or "").strip()
        out = {
            "schema": schema,
            "table": table,
            "qualified": f"{schema}.{table}" if table else "",
        }
        if where:
            out["where"] = where
        return out
    return {}


@dataclass
class ClientProfile:
    client_tag: str
    target_titles: list[str] = field(default_factory=list)
    title_synonyms: dict[str, str] = field(default_factory=dict)
    title_exclude_regex: str = ""
    seniority_floor: str = ""
    fallback_titles: list[str] = field(default_factory=list)
    geo: dict[str, Any] = field(default_factory=dict)
    company_size: list[str] = field(default_factory=list)
    employee_profiles_min: int | None = None
    employee_profiles_max: int | None = None
    ground_truth: dict[str, Any] = field(default_factory=dict)
    cache_tables: list[str] = field(default_factory=list)
    tier_order: list[dict[str, Any]] = field(default_factory=list)
    dropped_tiers: list[str] = field(default_factory=list)
    measured_rates: dict[str, Any] = field(default_factory=dict)
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def contacts_table(self) -> str:
        return f"{self.client_tag}_wf_contacts"

    @property
    def person_geo_mode(self) -> str:
        return str(self.geo.get("person_geo_mode") or "ignore").lower()

    def contacts_table_ref(self) -> dict[str, str]:
        return _table_ref(self.ground_truth.get("contacts_table"))

    def companies_no_domain_ref(self) -> dict[str, str]:
        return _table_ref(self.ground_truth.get("companies_no_domain"))

    def to_public(self) -> dict[str, Any]:
        return {
            "client_tag": self.client_tag,
            "target_titles": list(self.target_titles),
            "title_synonyms": dict(self.title_synonyms),
            "title_exclude_regex": self.title_exclude_regex,
            "seniority_floor": self.seniority_floor,
            "fallback_titles": list(self.fallback_titles),
            "geo": dict(self.geo),
            "company_size": list(self.company_size),
            "employee_profiles_min": self.employee_profiles_min,
            "employee_profiles_max": self.employee_profiles_max,
            "ground_truth": dict(self.ground_truth),
            "cache_tables": list(self.cache_tables),
            "tier_order": list(self.tier_order),
            "dropped_tiers": list(self.dropped_tiers),
            "measured_rates": dict(self.measured_rates),
            "contacts_table": f"public.{self.contacts_table}",
        }


def parse_profile(client_tag: str, doc: dict[str, Any] | None) -> ClientProfile:
    doc = doc or {}
    geo = doc.get("geo") if isinstance(doc.get("geo"), dict) else {}
    gt = doc.get("ground_truth") if isinstance(doc.get("ground_truth"), dict) else {}
    emp_min = doc.get("employee_profiles_min")
    emp_max = doc.get("employee_profiles_max")
    return ClientProfile(
        client_tag=client_tag,
        target_titles=_as_list(doc.get("target_titles")),
        title_synonyms=_as_str_map(doc.get("title_synonyms")),
        title_exclude_regex=str(doc.get("title_exclude_regex") or ""),
        seniority_floor=str(doc.get("seniority_floor") or ""),
        fallback_titles=_as_list(doc.get("fallback_titles")),
        geo=dict(geo),
        company_size=_as_list(doc.get("company_size")),
        employee_profiles_min=int(emp_min) if emp_min not in (None, "") else None,
        employee_profiles_max=int(emp_max) if emp_max not in (None, "") else None,
        ground_truth=dict(gt),
        cache_tables=_as_list(doc.get("cache_tables")),
        tier_order=list(doc.get("tier_order") or [])
        if isinstance(doc.get("tier_order"), list)
        else [],
        dropped_tiers=_as_list(doc.get("dropped_tiers")),
        measured_rates=dict(doc.get("measured_rates") or {})
        if isinstance(doc.get("measured_rates"), dict)
        else {},
        raw=doc,
    )


def get_profile(client_tag: str) -> ClientProfile:
    tag = normalize_client_tag(client_tag)
    if not settings.supabase_configured:
        raise RuntimeError("Supabase is not configured; cannot read wf_client_profiles")
    rows = supabase_sync.rest_select(
        "wf_client_profiles",
        params={"client_tag": f"eq.{tag}", "select": "*"},
    )
    if not rows:
        raise ValueError(
            f"No wf_client_profiles row for {tag!r}. "
            "Domain Waterfall ensure_profile creates this document."
        )
    row = rows[0]
    doc = row.get("profile") or row.get("document") or row
    if isinstance(doc, str):
        try:
            doc = json.loads(doc)
        except ValueError:
            doc = {}
    if not isinstance(doc, dict):
        doc = {}
    # Columns may sit beside the JSON document.
    merged = dict(doc)
    for key in (
        "target_titles",
        "title_synonyms",
        "title_exclude_regex",
        "seniority_floor",
        "fallback_titles",
        "geo",
        "company_size",
        "employee_profiles_min",
        "employee_profiles_max",
        "ground_truth",
        "cache_tables",
        "tier_order",
        "dropped_tiers",
        "measured_rates",
    ):
        if key in row and row[key] not in (None, "", [], {}):
            merged[key] = row[key]
    return parse_profile(tag, merged)


def update_profile_metrics(
    client_tag: str,
    *,
    tier_order: list[dict[str, Any]],
    dropped_tiers: list[str],
    measured_rates: dict[str, Any],
) -> None:
    tag = normalize_client_tag(client_tag)
    current = get_profile(tag)
    supabase_sync.rest_patch(
        "wf_client_profiles",
        params={"client_tag": f"eq.{tag}"},
        body={
            "profile": {
                **current.raw,
                "tier_order": tier_order,
                "dropped_tiers": dropped_tiers,
                "measured_rates": measured_rates,
            },
        },
    )
