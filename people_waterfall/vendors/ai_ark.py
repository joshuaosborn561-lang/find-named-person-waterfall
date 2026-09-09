"""AI Ark people_search. Never call without title. Profile-only (no email)."""

from __future__ import annotations

from typing import Any

from people_waterfall import http_client
from people_waterfall.config import settings
from people_waterfall.people import PersonHit, split_name
from people_waterfall.profile import ClientProfile


class AiArkClient:
    tier = "aiark"
    base_url = "https://api.ai-ark.com/api/developer-portal"

    def __init__(self, api_key: str | None = None, timeout: int = 45):
        self.api_key = api_key if api_key is not None else settings.ai_ark_api_key
        self.timeout = timeout
        self.calls = 0
        self.hits = 0

    @property
    def enabled(self) -> bool:
        return bool(self.api_key)

    def _headers(self) -> dict[str, str]:
        return {
            "X-TOKEN": self.api_key,
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    def credits(self) -> dict[str, Any]:
        r = http_client.get(
            self.tier,
            f"{self.base_url}/v1/credits",
            headers=self._headers(),
            timeout=15,
        )
        if r is None or r.status_code >= 400:
            r = http_client.get(
                self.tier,
                f"{self.base_url}/v2/account",
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

    def _post(self, path: str, body: dict[str, Any]) -> tuple[int, Any]:
        if not self.enabled:
            return 0, None
        self.calls += 1
        r = http_client.post(
            self.tier,
            f"{self.base_url}{path}",
            json=body,
            headers=self._headers(),
            timeout=self.timeout,
        )
        if r is None:
            return 0, None
        try:
            data = r.json()
        except ValueError:
            data = None
        return r.status_code, data

    def _person_from_row(self, row: dict[str, Any]) -> PersonHit | None:
        profile = row.get("profile") if isinstance(row.get("profile"), dict) else row
        first = str(profile.get("first_name") or "").strip()
        last = str(profile.get("last_name") or "").strip()
        full = str(profile.get("full_name") or "").strip()
        if "," in last:
            last = last.split(",", 1)[0].strip()
        if not first and full:
            first, last = split_name(full)
        if not (first or full):
            return None
        link = row.get("link") if isinstance(row.get("link"), dict) else {}
        loc = profile.get("location") if isinstance(profile.get("location"), dict) else {}
        return PersonHit(
            first_name=first,
            last_name=last,
            full_name=full or f"{first} {last}".strip(),
            title=str(profile.get("title") or profile.get("headline") or row.get("title") or ""),
            linkedin_url=str(
                link.get("linkedin") or row.get("linkedin_url") or profile.get("linkedin_url") or ""
            ),
            phone=str(profile.get("phone") or profile.get("mobile") or row.get("phone") or ""),
            domain=str(row.get("domain") or profile.get("domain") or ""),
            company_name=str(
                profile.get("company")
                or profile.get("company_name")
                or row.get("companyName")
                or ""
            ),
            person_city=str(loc.get("city") or profile.get("city") or ""),
            person_state=str(loc.get("state") or loc.get("region") or profile.get("state") or ""),
            source_tier=self.tier,
            raw=row,
        )

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
        titles = [t for t in (titles or list(profile.target_titles)) if t]
        if not self.enabled or not titles:
            return []
        if not (domain or company_name):
            return []
        contact: dict[str, Any] = {
            "experience": {
                "latest": {
                    "title": {
                        "any": {
                            "include": {
                                "mode": "SMART",
                                "content": titles[:12],
                            }
                        }
                    }
                }
            }
        }
        floor = (profile.seniority_floor or "").strip()
        if floor:
            contact["experience"]["latest"]["seniority"] = {
                "any": {"include": [floor]}
            }
        body: dict[str, Any] = {
            "page": 0,
            "size": max(1, min(int(limit), 25)),
            "contact": contact,
        }
        if domain:
            body["account"] = {"domain": {"any": {"include": [domain]}}}
        else:
            body["account"] = {
                "name": {
                    "any": {"include": {"mode": "SMART", "content": [company_name]}}
                }
            }
        geo = profile.geo or {}
        center = geo.get("center") if isinstance(geo.get("center"), dict) else None
        if center and center.get("lat") is not None and geo.get("radius"):
            body["location"] = {
                "geo": {
                    "lat": center.get("lat"),
                    "lng": center.get("lng") or center.get("lon"),
                    "radius": geo.get("radius"),
                }
            }
        elif city or state:
            loc = ", ".join(p for p in (city, state) if p)
            if loc:
                body["location"] = {"text": loc}
        status, data = self._post("/v1/people", body)
        if status >= 400 or not isinstance(data, dict):
            return []
        content: Any = data.get("content") or data.get("data") or data.get("results") or []
        if isinstance(content, dict):
            content = content.get("content") or content.get("results") or []
        out: list[PersonHit] = []
        for row in content[:limit]:
            if not isinstance(row, dict):
                continue
            person = self._person_from_row(row)
            if person:
                out.append(person)
        if out:
            self.hits += 1
        return out


def per_credit_from_payload(payload: dict[str, Any]) -> tuple[float | None, float | None]:
    credits = None
    per = None
    for key in ("credits", "balance", "remaining", "credits_remaining"):
        val = payload.get(key)
        if isinstance(val, (int, float)):
            credits = float(val)
            break
        if isinstance(val, dict):
            inner = val.get("remaining") or val.get("balance")
            if isinstance(inner, (int, float)):
                credits = float(inner)
                break
    for key in ("credit_price", "price_per_credit", "usd_per_credit"):
        val = payload.get(key)
        if isinstance(val, (int, float)) and val > 0:
            per = float(val)
            break
    return per, credits
