"""Prospeo search-person. Free on miss (NO_RESULTS). 1 credit per hitting page."""

from __future__ import annotations

from typing import Any

from people_waterfall import http_client
from people_waterfall.config import settings
from people_waterfall.people import PersonHit, split_name
from people_waterfall.profile import ClientProfile


class ProspeoClient:
    tier = "prospeo"
    base_url = "https://api.prospeo.io"

    def __init__(self, api_key: str | None = None, timeout: int = 45):
        self.api_key = api_key if api_key is not None else settings.prospeo_api_key
        self.timeout = timeout
        self.calls = 0
        self.hits = 0
        self.last_free = False

    @property
    def enabled(self) -> bool:
        return bool(self.api_key)

    def _headers(self) -> dict[str, str]:
        return {
            "X-KEY": self.api_key,
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    def credits(self) -> dict[str, Any]:
        r = http_client.get(
            self.tier,
            f"{self.base_url}/account-information",
            headers=self._headers(),
            timeout=15,
        )
        if r is None:
            r = http_client.post(
                self.tier,
                f"{self.base_url}/account-information",
                json={},
                headers=self._headers(),
                timeout=15,
            )
        if r is None:
            return {}
        try:
            data = r.json()
        except ValueError:
            return {}
        return data if isinstance(data, dict) else {}

    def find_people(
        self,
        *,
        profile: ClientProfile,
        domain: str = "",
        company_name: str = "",
        city: str = "",
        state: str = "",
        titles: list[str] | None = None,
        limit: int = 10,
    ) -> list[PersonHit]:
        if not self.enabled or not (domain or company_name):
            return []
        titles = [t for t in (titles or list(profile.target_titles)) if t]
        if not titles:
            return []
        filters: dict[str, Any] = {
            "person_job_title": {
                "include": titles[:20],
                "match_mode": "CONTAINS",
            }
        }
        company: dict[str, Any] = {}
        if domain:
            company["websites"] = {"include": [domain]}
        if company_name:
            company["names"] = {"include": [company_name]}
        if company:
            filters["company"] = company
        loc = ", ".join(p for p in (city, state) if p)
        if loc:
            filters["person_location_search"] = {"include": [loc]}
        self.calls += 1
        self.last_free = False
        r = http_client.post(
            self.tier,
            f"{self.base_url}/search-person",
            json={"page": 1, "filters": filters},
            headers=self._headers(),
            timeout=self.timeout,
        )
        if r is None:
            return []
        try:
            body = r.json()
        except ValueError:
            return []
        if not isinstance(body, dict):
            return []
        if body.get("error") is True:
            # NO_RESULTS is a miss — free.
            return []
        self.last_free = bool(body.get("free"))
        out: list[PersonHit] = []
        for row in body.get("results") or []:
            if not isinstance(row, dict):
                continue
            person = row.get("person") if isinstance(row.get("person"), dict) else row
            company_obj = row.get("company") if isinstance(row.get("company"), dict) else {}
            first = str(person.get("first_name") or "").strip()
            last = str(person.get("last_name") or "").strip()
            full = str(person.get("full_name") or person.get("name") or "").strip()
            if not first and full:
                first, last = split_name(full)
            if not first:
                continue
            loc_obj = person.get("location") if isinstance(person.get("location"), dict) else {}
            out.append(
                PersonHit(
                    first_name=first,
                    last_name=last,
                    full_name=full or f"{first} {last}".strip(),
                    title=str(person.get("job_title") or person.get("title") or ""),
                    linkedin_url=str(person.get("linkedin_url") or person.get("linkedin") or ""),
                    domain=str(company_obj.get("website") or company_obj.get("domain") or domain),
                    company_name=str(company_obj.get("name") or company_name),
                    person_city=str(loc_obj.get("city") or ""),
                    person_state=str(loc_obj.get("state") or loc_obj.get("region") or ""),
                    source_tier=self.tier,
                    raw=row,
                )
            )
            if len(out) >= limit:
                break
        if out:
            self.hits += 1
        return out


def per_credit_from_payload(payload: dict[str, Any]) -> tuple[float | None, float | None]:
    credits = None
    per = None
    block = payload.get("account") if isinstance(payload.get("account"), dict) else payload
    for key in ("credits", "remaining_credits", "balance"):
        val = block.get(key)
        if isinstance(val, (int, float)):
            credits = float(val)
            break
    for key in ("credit_price", "price_per_credit"):
        val = block.get(key)
        if isinstance(val, (int, float)) and val > 0:
            per = float(val)
            break
    return per, credits
