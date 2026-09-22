"""Smartlead included-plan lookup. Domain required. $0 from the 50k pool.

Returns a name (and an email we discard). 429 is backoff, never disable.
Throttles at ten concurrent via VendorGate.
"""

from __future__ import annotations

import threading
from typing import Any

from people_waterfall import http_client
from people_waterfall.config import settings
from people_waterfall.people import PersonHit, person_from_row, split_name
from people_waterfall.profile import ClientProfile

FIND_EMAILS_PATH = "/search-contacts/find-emails"
ANALYTICS_PATH = "/search-analytics"


class _SharedCredits:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.available: int | None = None
        self.total: int | None = None
        self.used: int | None = None
        self.exhausted = False


_CREDITS = _SharedCredits()


class SmartleadClient:
    tier = "smartlead"

    def __init__(self, api_key: str | None = None, timeout: int = 45):
        self.api_key = api_key if api_key is not None else settings.smartlead_api_key
        self.base_url = settings.smartlead_base_url.rstrip("/")
        self.timeout = timeout
        self.calls = 0
        self.hits = 0

    @property
    def enabled(self) -> bool:
        return bool(self.api_key)

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.api_key}",
            "x-api-key": self.api_key,
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    def credit_snapshot(self) -> dict[str, Any]:
        with _CREDITS.lock:
            return {
                "available": _CREDITS.available,
                "total": _CREDITS.total,
                "used": _CREDITS.used,
                "exhausted": _CREDITS.exhausted,
            }

    def refresh_credits(self) -> None:
        if not self.enabled:
            return
        r = http_client.get(
            self.tier,
            f"{self.base_url}{ANALYTICS_PATH}",
            headers=self._headers(),
            timeout=8,
        )
        if r is None or r.status_code >= 400:
            return
        try:
            data = r.json()
        except ValueError:
            return
        if not isinstance(data, dict):
            return
        with _CREDITS.lock:
            _CREDITS.available = data.get("availableCredits") or data.get("available")
            _CREDITS.total = data.get("credits_total") or data.get("totalCredits")
            _CREDITS.used = data.get("credits_used") or data.get("usedCredits")
            avail = _CREDITS.available
            _CREDITS.exhausted = avail is not None and int(avail) <= 0

    def find_people(
        self,
        *,
        profile: ClientProfile,
        domain: str = "",
        company_name: str = "",
        city: str = "",
        state: str = "",
        titles: list[str] | None = None,
        first_name: str = "",
        last_name: str = "",
        limit: int = 10,
    ) -> list[PersonHit]:
        """Name+domain lookup. Domain-only rows skip — Smartlead is not a roster search."""
        if not self.enabled or not domain:
            return []
        first = (first_name or "").strip()
        last = (last_name or "").strip()
        if not first or not last:
            return []
        with _CREDITS.lock:
            if _CREDITS.exhausted:
                return []
        self.calls += 1
        r = http_client.post(
            self.tier,
            f"{self.base_url}{FIND_EMAILS_PATH}",
            json={
                "first_name": first,
                "last_name": last,
                "domain": domain,
                "company_name": company_name or domain,
            },
            headers=self._headers(),
            timeout=self.timeout,
        )
        if r is None or r.status_code >= 400:
            return []
        try:
            data = r.json()
        except ValueError:
            return []
        if not isinstance(data, dict):
            return []
        # Email is discarded — this resolver never stores it.
        name = str(data.get("name") or data.get("full_name") or "").strip()
        fn = str(data.get("first_name") or first).strip()
        ln = str(data.get("last_name") or last).strip()
        if name and not (fn and ln):
            fn, ln = split_name(name)
        title = str(data.get("title") or data.get("job_title") or "").strip()
        if not fn:
            return []
        person = PersonHit(
            first_name=fn,
            last_name=ln,
            full_name=name or f"{fn} {ln}".strip(),
            title=title,
            linkedin_url=str(data.get("linkedin_url") or ""),
            domain=domain,
            company_name=company_name,
            source_tier=self.tier,
            raw={k: v for k, v in data.items() if k not in {"email", "work_email"}},
        )
        self.hits += 1
        return [person][:limit]
