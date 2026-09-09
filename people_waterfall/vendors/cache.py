"""Local cache: profile cache_tables + public.name_bank. $0."""

from __future__ import annotations

from typing import Any

from people_waterfall import supabase_sync
from people_waterfall.people import PersonHit, person_from_row
from people_waterfall.profile import ClientProfile
from people_waterfall.source import split_qualified


class CacheClient:
    tier = "cache"

    def __init__(self) -> None:
        self.calls = 0
        self.hits = 0

    @property
    def enabled(self) -> bool:
        return True

    def find_people(
        self,
        *,
        profile: ClientProfile,
        domain: str = "",
        company_name: str = "",
        city: str = "",
        state: str = "",
        titles: list[str] | None = None,
    ) -> list[PersonHit]:
        self.calls += 1
        tables = [*profile.cache_tables, "public.name_bank"]
        found: list[PersonHit] = []
        seen: set[tuple[str, str]] = set()
        domain_n = (domain or "").strip().lower()
        prefix = (company_name or "").strip().lower()[:10]
        for qualified in tables:
            schema, table = split_qualified(qualified)
            filters: list[dict[str, str]] = []
            if domain_n:
                filters.append({"col": "domain", "op": "eq", "value": domain_n})
            try:
                data = supabase_sync.rpc(
                    "ew_read_source",
                    {
                        "p_schema": schema,
                        "p_table": table,
                        "p_filters": filters,
                        "p_columns": [
                            "domain",
                            "first_name",
                            "last_name",
                            "job_title",
                            "linkedin_url",
                            "contact_city",
                            "contact_state",
                        ],
                        "p_key_column": "domain" if domain_n else "first_name",
                        "p_after": None,
                        "p_limit": 100,
                    },
                )
            except RuntimeError:
                continue
            rows = data if isinstance(data, list) else []
            for raw in rows:
                if not isinstance(raw, dict):
                    continue
                if not domain_n and prefix:
                    # name-only cache: keep rows whose domain is empty
                    raw_domain = str(raw.get("domain") or "").strip()
                    if raw_domain:
                        continue
                person = person_from_row(raw, self.tier)
                if not person:
                    continue
                key = (person.first_name.lower(), person.last_name.lower())
                if key in seen:
                    continue
                seen.add(key)
                found.append(person)
        if found:
            self.hits += 1
        return found
