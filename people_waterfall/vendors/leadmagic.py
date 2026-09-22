"""LeadMagic employee finder (0.05 cr) and role/people search.

Live /v1/credits is read at job start. If people search spends no credits
on this plan, find_people_by_role is treated as free.
"""

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

    def find_people_by_role(
        self,
        *,
        profile: ClientProfile,
        domain: str = "",
        company_name: str = "",
        city: str = "",
        state: str = "",
        titles: list[str] | None = None,
        limit: int = 20,
    ) -> list[PersonHit]:
        if not self.enabled or not (domain or company_name):
            return []
        titles = titles or list(profile.target_titles)
        location = " ".join(p for p in (city, state) if p).strip()
        found: list[PersonHit] = []
        seen: set[tuple[str, str]] = set()
        # Group titles so we do not fire one call per title on a long list.
        groups: list[list[str]] = []
        chunk: list[str] = []
        for title in titles:
            chunk.append(title)
            if len(chunk) >= 4:
                groups.append(chunk)
                chunk = []
        if chunk:
            groups.append(chunk)
        for group in groups[:6]:
            self.last_credits_used = None
            body: dict[str, Any] = {
                "job_title": ", ".join(group),
                "company_name": company_name or domain,
            }
            if domain:
                body["company_domain"] = domain
                body["domain"] = domain
            if location:
                body["location"] = location
            status, data = self._request("POST", "/v1/people/role-finder", body)
            if status >= 400 or data is None:
                status, data = self._request("POST", "/v1/people/search", body)
            if status >= 400 or data is None:
                status, data = self._request("POST", "/role-finder", body)
            for person in self._absorb(data, "leadmagic_role", limit):
                key = (person.first_name.lower(), person.last_name.lower())
                if key in seen:
                    continue
                seen.add(key)
                found.append(person)
            if len(found) >= limit:
                break
        if found:
            self.hits += 1
        return found[:limit]

    def probe_search_free(self) -> bool | None:
        """Compare credits before/after a miss-safe role search. None if unknown."""
        before = self.credits()
        bal_before = _credit_balance(before)
        if bal_before is None:
            return None
        status, data = self._request(
            "POST",
            "/v1/people/role-finder",
            {
                "job_title": "Project Manager",
                "company_name": "ZZZ Nonexistent Company XYZ 2099",
                "location": "Dallas TX",
            },
        )
        after = self.credits()
        bal_after = _credit_balance(after)
        used = None
        if isinstance(data, dict):
            raw_used = data.get("credits_used") or data.get("credits")
            if isinstance(raw_used, (int, float)):
                used = float(raw_used)
        if used is not None:
            return used == 0
        if bal_after is None:
            return None
        return bal_after >= bal_before


def _credit_balance(payload: dict[str, Any]) -> float | None:
    for key in ("credits", "remaining", "balance", "credits_remaining", "available"):
        val = payload.get(key)
        if isinstance(val, (int, float)):
            return float(val)
        if isinstance(val, dict):
            inner = val.get("remaining") or val.get("balance") or val.get("credits")
            if isinstance(inner, (int, float)):
                return float(inner)
    return None


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
    # Infer from plan name using the published table.
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
