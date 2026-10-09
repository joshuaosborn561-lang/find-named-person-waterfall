"""Prospeo person search. Title-filtered, batched websites, cover loop.

POST /search-person — 1 credit per page of 25 that returns ≥1 person.
Growth plan ~$0.0148/credit. Subdomains are rejected (INVALID_FILTERS);
normalize to the registrable domain. Re-query only companies not yet
covered. Keep at most 3 people per company. Never page through total_count.
"""

from __future__ import annotations

from typing import Any

from people_waterfall import http_client
from people_waterfall.config import settings
from people_waterfall.people import PersonHit, split_name
from people_waterfall.pricing import PROSPEO_MAX_PER_COMPANY, PROSPEO_PAGE_SIZE
from people_waterfall.profile import ClientProfile
from people_waterfall.site_quality import registrable_domain

MAX_WEBSITES = 500


def websites_of_company(block: Any) -> list[str]:
    """Registrable hosts from company.website / domain / other_websites."""
    if not isinstance(block, dict):
        return []
    raw: list[str] = []
    for key in ("website", "domain", "company_website", "company_domain"):
        val = block.get(key)
        if isinstance(val, str) and val.strip():
            raw.append(val)
    others = block.get("other_websites") or block.get("websites") or []
    if isinstance(others, str):
        raw.append(others)
    elif isinstance(others, list):
        raw.extend(str(item) for item in others if item)
    out: list[str] = []
    seen: set[str] = set()
    for item in raw:
        host = registrable_domain(item)
        if host and host not in seen:
            seen.add(host)
            out.append(host)
    return out


class ProspeoSearchClient:
    tier = "prospeo_search"
    base_url = "https://api.prospeo.io"

    def __init__(self, api_key: str | None = None, timeout: int = 60):
        self._api_key_override = api_key
        self.timeout = timeout
        self.calls = 0
        self.hits = 0
        self.pages = 0
        self.last_credits_used: float | None = None
        self.last_error = ""

    @property
    def api_key(self) -> str:
        if self._api_key_override is not None:
            return self._api_key_override
        return settings.prospeo_api_key or ""

    @property
    def enabled(self) -> bool:
        return bool(self.api_key)

    def _headers(self) -> dict[str, str]:
        return {
            "X-KEY": self.api_key,
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    def account_information(self) -> dict[str, Any]:
        if not self.enabled:
            return {}
        r = http_client.get(
            self.tier,
            f"{self.base_url}/account-information",
            headers=self._headers(),
            timeout=30,
        )
        if r is None:
            return {}
        try:
            data = r.json()
        except ValueError:
            return {}
        return data if isinstance(data, dict) else {}

    def _person_from_row(self, row: dict[str, Any]) -> PersonHit | None:
        person = row.get("person") if isinstance(row.get("person"), dict) else row
        first = str(
            person.get("first_name") or person.get("firstName") or ""
        ).strip()
        last = str(person.get("last_name") or person.get("lastName") or "").strip()
        full = str(
            person.get("full_name") or person.get("name") or person.get("fullName") or ""
        ).strip()
        if not first and full:
            first, last = split_name(full)
        if not (first or full):
            return None
        company = row.get("company") if isinstance(row.get("company"), dict) else {}
        hosts = websites_of_company(company)
        loc = person.get("location") if isinstance(person.get("location"), dict) else {}
        return PersonHit(
            first_name=first,
            last_name=last,
            full_name=full or f"{first} {last}".strip(),
            title=str(
                person.get("title")
                or person.get("job_title")
                or person.get("person_job_title")
                or ""
            ),
            linkedin_url=str(
                person.get("linkedin_url")
                or person.get("linkedin")
                or person.get("profile_url")
                or ""
            ),
            phone=str(person.get("phone") or person.get("mobile") or "").strip(),
            domain=(hosts[0] if hosts else ""),
            company_name=str(
                company.get("name")
                or company.get("company_name")
                or person.get("company_name")
                or ""
            ),
            person_city=str(person.get("city") or loc.get("city") or ""),
            person_state=str(
                person.get("state") or loc.get("state") or loc.get("region") or ""
            ),
            source_tier=self.tier,
            raw=row,
        )

    def _search_page(
        self,
        websites: list[str],
        titles: list[str],
    ) -> tuple[list[dict[str, Any]], bool]:
        """One page. Returns (rows, billed). billed is True when the page
        returned at least one person (1 credit)."""
        body = {
            "page": 1,
            "filters": {
                "company": {"websites": websites},
                "person_job_title": {
                    "include": list(titles),
                    "match_mode": "CONTAINS",
                },
            },
        }
        self.calls += 1
        self.pages += 1
        url = f"{self.base_url}/search-person"
        r = http_client.post(
            self.tier,
            url,
            json=body,
            headers=self._headers(),
            timeout=self.timeout,
        )
        if r is None:
            self.last_error = "prospeo search-person failed (no response)"
            return [], False
        try:
            data = r.json()
        except ValueError:
            self.last_error = "prospeo search-person returned non-JSON"
            return [], False
        if not isinstance(data, dict):
            return [], False
        error = data.get("error")
        code = str(data.get("error_code") or data.get("code") or "")
        if r.status_code >= 400 or error is True:
            self.last_error = f"prospeo search-person {r.status_code} {code}".strip()
            if "NO_RESULTS" in code.upper() or "NO_RESULT" in str(data).upper():
                return [], False
            return [], False
        rows = (
            data.get("results")
            or data.get("people")
            or data.get("data")
            or data.get("persons")
            or []
        )
        if isinstance(rows, dict):
            rows = rows.get("results") or rows.get("people") or []
        if not isinstance(rows, list):
            rows = []
        billed = len(rows) > 0
        return [row for row in rows if isinstance(row, dict)], billed

    def search_people(
        self,
        domains: list[str],
        *,
        profile: ClientProfile | None = None,
        titles: list[str] | None = None,
        companies: dict[str, str] | None = None,
        max_per_company: int = PROSPEO_MAX_PER_COMPANY,
    ) -> dict[str, list[PersonHit]]:
        """Cover-loop: re-query only companies not yet covered. Cap N/company."""
        wanted = [str(t).strip() for t in (titles or (profile.target_titles if profile else []) or []) if str(t).strip()]
        out: dict[str, list[PersonHit]] = {}
        original: dict[str, str] = {}
        uncovered: list[str] = []
        seen_host: set[str] = set()
        for raw in domains:
            host = registrable_domain(raw)
            if not host or host in seen_host:
                continue
            seen_host.add(host)
            uncovered.append(host)
            original[host] = str(raw or "").strip().lower() or host
            out[host] = []
            src = original[host]
            if src != host:
                out.setdefault(src, [])
        if not self.enabled:
            raise RuntimeError(
                "prospeo_search is selected but PROSPEO_API_KEY is missing; "
                "refusing to fall through"
            )
        if not uncovered:
            return {}
        if not wanted:
            # Title filter is required so large companies cannot flood the page.
            self.last_error = "prospeo_search called with no titles"
            return {key: [] for key in out}
        self.last_credits_used = 0.0
        cap = max(1, int(max_per_company))
        companies = companies or {}
        while uncovered:
            chunk = uncovered[:MAX_WEBSITES]
            rows, billed = self._search_page(chunk, wanted)
            if billed:
                self.last_credits_used = (self.last_credits_used or 0.0) + 1.0
            if not rows:
                break
            newly: set[str] = set()
            for row in rows:
                person = self._person_from_row(row)
                if not person:
                    continue
                company = row.get("company") if isinstance(row.get("company"), dict) else {}
                hosts = websites_of_company(company)
                if person.domain and person.domain not in hosts:
                    hosts.append(person.domain)
                matched = [h for h in hosts if h in out]
                if not matched:
                    continue
                target = matched[0]
                bucket = out[target]
                key = (person.first_name.lower(), person.last_name.lower())
                if any(
                    (p.first_name.lower(), p.last_name.lower()) == key for p in bucket
                ):
                    continue
                if len(bucket) >= cap:
                    newly.add(target)
                    continue
                if not person.company_name:
                    person.company_name = companies.get(target) or companies.get(
                        original.get(target, "")
                    ) or ""
                person.domain = original.get(target, target)
                bucket.append(person)
                newly.add(target)
                if bucket:
                    self.hits += 1
            if not newly:
                break
            uncovered = [host for host in uncovered if host not in newly]
        return out

    def find_people(
        self,
        *,
        profile: ClientProfile | None = None,
        domain: str = "",
        company_name: str = "",
        city: str = "",
        state: str = "",
        titles: list[str] | None = None,
        **_: Any,
    ) -> list[PersonHit]:
        host = registrable_domain(domain)
        if not host:
            return []
        packed = self.search_people(
            [host],
            profile=profile,
            titles=titles,
            companies={host: company_name} if company_name else None,
        )
        return list(packed.get(host) or packed.get(domain) or [])
