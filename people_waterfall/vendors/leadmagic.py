"""LeadMagic employee finder (0.05 cr/hit). Live /v1/credits is read at job start."""

from __future__ import annotations

from typing import Any

from people_waterfall import http_client
from people_waterfall.config import settings
from people_waterfall.people import PersonHit, person_from_row
from people_waterfall.profile import ClientProfile


class LeadMagicClient:
    tier = "leadmagic"
    base_url = "https://api.leadmagic.io"

    def __init__(self, api_key: str | None = None, timeout: int = 60):
        self.api_key = api_key if api_key is not None else settings.leadmagic_api_key
        self.timeout = timeout
        self.calls = 0
        self.hits = 0
        self.last_credits_used: float | None = None

    @property
    def enabled(self) -> bool:
        return bool(self.api_key)

    def _headers(self) -> dict[str, str]:
        return {
            "X-API-Key": self.api_key,
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    def _request(
        self, method: str, path: str, body: dict[str, Any] | None = None
    ) -> tuple[int, Any]:
        if not self.enabled:
            return 0, None
        url = f"{self.base_url}{path}"
        self.calls += 1
        if method == "GET":
            r = http_client.get(self.tier, url, headers=self._headers(), timeout=self.timeout)
        else:
            r = http_client.post(
                self.tier, url, json=body or {}, headers=self._headers(), timeout=self.timeout
            )
        if r is None:
            return 0, None
        try:
            data = r.json()
        except ValueError:
            data = None
        return r.status_code, data

    def credits(self) -> dict[str, Any]:
        status, data = self._request("GET", "/v1/credits")
        if status >= 400 or not isinstance(data, dict):
            status, data = self._request("GET", "/credits")
        if not isinstance(data, dict):
            return {}
        return data

    def _absorb(self, data: Any, source_tier: str, limit: int) -> list[PersonHit]:
        if data is None:
            return []
        if isinstance(data, dict):
            used = data.get("credits_used") or data.get("credits") or data.get("credit_used")
            if isinstance(used, (int, float)):
                self.last_credits_used = float(used)
            rows = (
                data.get("data")
                or data.get("people")
                or data.get("employees")
                or data.get("results")
                or data.get("contacts")
                or []
            )
        else:
            rows = data
        if isinstance(rows, dict):
            rows = rows.get("data") or rows.get("people") or rows.get("employees") or []
        out: list[PersonHit] = []
        seen: set[tuple[str, str]] = set()
        for row in rows:
            if not isinstance(row, dict):
                continue
            person = person_from_row(row, source_tier)
            if not person:
                continue
            key = (person.first_name.lower(), person.last_name.lower())
            if key in seen:
                continue
            seen.add(key)
            out.append(person)
            if len(out) >= limit:
                break
        return out

    def employee_finder(
        self,
        *,
        profile: ClientProfile,
        domain: str = "",
        company_name: str = "",
        city: str = "",
        state: str = "",
        titles: list[str] | None = None,
        limit: int = 50,
    ) -> list[PersonHit]:
        if not self.enabled or not domain:
            return []
        self.last_credits_used = None
        body: dict[str, Any] = {
            "company_domain": domain,
            "domain": domain,
            "company_name": company_name or domain,
            "per_page": min(limit, 100),
            "limit": min(limit, 100),
            "page": 1,
        }
        status, data = self._request("POST", "/v1/people/employee-finder", body)
        if status >= 400 or data is None:
            status, data = self._request("POST", "/employee-finder", body)
        people = self._absorb(data, "leadmagic_employee", limit)
        if people:
            self.hits += 1
        return people


def per_credit_from_payload(payload: dict[str, Any]) -> tuple[float | None, str]:
    plan = str(
        payload.get("plan")
        or payload.get("plan_name")
        or payload.get("tier")
        or ""
    )
    for key in ("credit_price", "price_per_credit", "per_credit", "usd_per_credit"):
        val = payload.get(key)
        if isinstance(val, (int, float)) and val > 0:
            return float(val), plan
    plan_l = plan.lower()
    mapping = {
        "basic": 0.024995,
        "essential": 0.0198,
        "growth": 0.01245,
        "professional": 0.00998,
        "ultimate": 0.00849,
    }
    for name, price in mapping.items():
        if name in plan_l:
            return price, plan
    return None, plan
