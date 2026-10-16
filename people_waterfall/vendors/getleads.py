"""GetLeads people search by domain. Plan-credit tier; never persist emails.

~4 requests/second. HTTP 429 and a body containing "Too many requests"
are rate limits (backoff + retry), not a circuit breaker.
"""

from __future__ import annotations

import logging
import time
from typing import Any

from people_waterfall import http_client
from people_waterfall.config import settings
from people_waterfall.people import PersonHit, split_name
from people_waterfall.profile import ClientProfile
from people_waterfall.site_quality import registrable_domain

log = logging.getLogger("people_waterfall.getleads")

SEARCH_URL = "https://app.getleads.io/api/v1/contacts/search"
MAX_PER_COMPANY = 5
MIN_INTERVAL_S = 0.25
RATE_MARKERS = ("too many requests", "rate limit", "rate_limit")


class GetLeadsPeopleClient:
    tier = "getleads"

    def __init__(self, api_key: str | None = None, timeout: int = 45):
        self._api_key_override = api_key
        self.timeout = timeout
        self.calls = 0
        self.hits = 0
        self.last_error = ""
        self._last_call_at = 0.0

    @property
    def api_key(self) -> str:
        if self._api_key_override is not None:
            return (self._api_key_override or "").strip()
        return (settings.getleads_api_key or "").strip()

    @property
    def enabled(self) -> bool:
        return bool(self.api_key)

    def _pace(self) -> None:
        wait = MIN_INTERVAL_S - (time.monotonic() - self._last_call_at)
        if wait > 0:
            time.sleep(wait)

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    def search_one(self, domain: str, *, titles: list[str] | None = None) -> list[PersonHit]:
        host = registrable_domain(domain)
        if not host or not self.enabled:
            return []
        body: dict[str, Any] = {
            "domains": [host],
            "limit": MAX_PER_COMPANY,
            "require_email": False,
            "columns": [
                "first_name",
                "last_name",
                "full_name",
                "current_title",
                "linkedin_url",
                "company_name",
                "company_domain",
            ],
        }
        wanted = [t for t in (titles or []) if t]
        if wanted:
            body["job_titles"] = wanted
        last_error = ""
        for attempt in range(4):
            self._pace()
            self.calls += 1
            self._last_call_at = time.monotonic()
            response = http_client.post(
                self.tier,
                SEARCH_URL,
                json=body,
                headers=self._headers(),
                timeout=self.timeout,
            )
            if response is None:
                last_error = "getleads request failed"
                time.sleep(min(8.0, 1.5 * (attempt + 1)))
                continue
            text = ""
            try:
                text = response.text or ""
            except Exception:  # noqa: BLE001
                text = ""
            lowered = text.lower()
            rate = response.status_code == 429 or any(m in lowered for m in RATE_MARKERS)
            if rate:
                last_error = "getleads rate-limited"
                time.sleep(min(16.0, 2.0 * (attempt + 1)))
                continue
            if response.status_code >= 400:
                last_error = f"getleads http {response.status_code}"
                self.last_error = last_error
                return []
            try:
                payload = response.json()
            except ValueError:
                last_error = "getleads invalid json"
                self.last_error = last_error
                return []
            people = _people_from_payload(payload, host)
            self.hits += len(people)
            return people[:MAX_PER_COMPANY]
        self.last_error = last_error or "getleads exhausted retries"
        return []

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
        _ = (profile, company_name, city, state)
        return self.search_one(domain, titles=titles)

    def search_people(
        self,
        domains: list[str],
        *,
        profile: ClientProfile | None = None,
        titles: list[str] | None = None,
        companies: dict[str, str] | None = None,
    ) -> dict[str, list[PersonHit]]:
        _ = (profile, companies)
        packed: dict[str, list[PersonHit]] = {}
        seen: set[str] = set()
        for raw in domains:
            host = registrable_domain(str(raw or ""))
            if not host or host in seen:
                continue
            seen.add(host)
            packed[host] = self.search_one(host, titles=titles)
            packed[raw] = packed[host]
        return packed


def _people_from_payload(payload: Any, domain: str) -> list[PersonHit]:
    rows: list[Any]
    if isinstance(payload, list):
        rows = payload
    elif isinstance(payload, dict):
        for key in ("contacts", "results", "items", "data"):
            val = payload.get(key)
            if isinstance(val, list):
                rows = val
                break
        else:
            inner = payload.get("result")
            rows = inner if isinstance(inner, list) else []
    else:
        rows = []
    out: list[PersonHit] = []
    seen: set[str] = set()
    for row in rows:
        if not isinstance(row, dict):
            continue
        first = str(row.get("first_name") or row.get("firstName") or "").strip()
        last = str(row.get("last_name") or row.get("lastName") or "").strip()
        full = str(row.get("full_name") or row.get("fullName") or row.get("name") or "").strip()
        if not first and full:
            first, last = split_name(full)
        if not first:
            continue
        key = f"{first.lower()}|{last.lower()}"
        if key in seen:
            continue
        seen.add(key)
        out.append(
            PersonHit(
                first_name=first,
                last_name=last,
                full_name=full or f"{first} {last}".strip(),
                title=str(
                    row.get("current_title")
                    or row.get("job_title")
                    or row.get("title")
                    or ""
                ),
                linkedin_url=str(
                    row.get("linkedin_url") or row.get("linkedin") or ""
                ),
                domain=str(row.get("company_domain") or row.get("domain") or domain),
                company_name=str(row.get("company_name") or row.get("org_name") or ""),
                source_tier="getleads",
                # Emails are never persisted from this tier.
                email="",
                raw={k: v for k, v in row.items() if k not in {"email", "work_email", "emails"}},
            )
        )
    return out
