"""AI Ark people search. Titles required. Cap 3 per company. Preview is estimate-only.

POST /v1/people — 0.5 credits per result (~$0.00183). Zero results cost $0.
POST /v2/people/preview — 1 credit per page of 25–100; masks last names and
hides domains, so it never writes contacts.
GET /v1/payments/credits — free.
"""

from __future__ import annotations

from typing import Any

from people_waterfall import http_client
from people_waterfall.config import settings
from people_waterfall.people import PersonHit, split_name
from people_waterfall.pricing import AIARK_MAX_PER_COMPANY
from people_waterfall.profile import ClientProfile

MAX_SIZE = 25


class TitlesRequired(ValueError):
    """Raised when aiark_people would run without a title filter."""


class AiArkPeopleClient:
    tier = "aiark_people"
    base_url = "https://api.ai-ark.com/api/developer-portal"

    def __init__(self, api_key: str | None = None, timeout: int = 45):
        self._api_key_override = api_key
        self.timeout = timeout
        self.calls = 0
        self.hits = 0
        self.last_credits_used: float | None = None

    @property
    def api_key(self) -> str:
        if self._api_key_override is not None:
            return self._api_key_override
        return settings.ai_ark_api_key or ""

    @property
    def enabled(self) -> bool:
        return bool(self.api_key)

    def _headers(self) -> dict[str, str]:
        return {
            "X-TOKEN": self.api_key,
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    def _credit_from_response(self, response: Any) -> float | None:
        if response is None:
            return None
        headers = getattr(response, "headers", None) or {}
        raw = headers.get("X-Credit") or headers.get("x-credit")
        if raw in (None, ""):
            return None
        try:
            return abs(float(raw))
        except (TypeError, ValueError):
            return None

    def _get(self, path: str) -> tuple[int, Any]:
        if not self.enabled:
            return 0, None
        url = f"{self.base_url}{path}"
        r = http_client.get(self.tier, url, headers=self._headers(), timeout=self.timeout)
        if r is None:
            return 0, None
        try:
            data = r.json()
        except ValueError:
            data = None
        return r.status_code, data

    def credits(self) -> dict[str, Any]:
        status, data = self._get("/v1/payments/credits")
        if status >= 400 or not isinstance(data, dict):
            status, data = self._get("/credits")
        return data if isinstance(data, dict) else {}

    def _post(self, path: str, body: dict[str, Any]) -> tuple[int, Any, float | None]:
        if not self.enabled:
            return 0, None, None
        self.calls += 1
        url = f"{self.base_url}{path}"
        r = http_client.post(
            self.tier,
            url,
            json=body,
            headers=self._headers(),
            timeout=self.timeout,
        )
        if r is None:
            return 0, None, None
        try:
            data = r.json()
        except ValueError:
            data = None
        return r.status_code, data, self._credit_from_response(r)

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
        person_id = str(
            row.get("id") or row.get("people_id") or profile.get("id") or ""
        ).strip()
        loc = profile.get("location") if isinstance(profile.get("location"), dict) else {}
        return PersonHit(
            first_name=first,
            last_name=last,
            full_name=full or f"{first} {last}".strip(),
            title=str(
                profile.get("title")
                or profile.get("headline")
                or row.get("title")
                or ""
            ),
            linkedin_url=str(
                link.get("linkedin")
                or row.get("linkedin_url")
                or profile.get("linkedin_url")
                or ""
            ),
            phone=str(
                profile.get("phone")
                or profile.get("mobile")
                or row.get("phone")
                or ""
            ).strip(),
            domain=str(
                profile.get("domain")
                or row.get("domain")
                or (row.get("account") or {}).get("domain")
                or ""
            ).strip().lower(),
            company_name=str(
                profile.get("company")
                or profile.get("company_name")
                or (row.get("account") or {}).get("name")
                or ""
            ),
            person_city=str(
                profile.get("city") or loc.get("city") or row.get("city") or ""
            ),
            person_state=str(
                profile.get("state")
                or loc.get("state")
                or loc.get("region")
                or row.get("state")
                or ""
            ),
            source_tier=self.tier,
            aiark_person_id=person_id,
            raw=row,
        )

    def _require_titles(self, titles: list[str] | None) -> list[str]:
        cleaned = [str(t).strip() for t in (titles or []) if str(t).strip()]
        if not cleaned:
            raise TitlesRequired(
                "aiark_people requires titles; refusing to search without a "
                "title filter (without a title the API returns whoever is first "
                "at the domain)"
            )
        return cleaned

    def _search_body(
        self,
        *,
        domain: str,
        titles: list[str],
        limit: int,
    ) -> dict[str, Any]:
        # Do not truncate. EMCOR has 36 titles and the API accepts them.
        return {
            "page": 0,
            "size": max(1, min(int(limit), MAX_SIZE)),
            "account": {"domain": {"any": {"include": [domain]}}},
            "contact": {
                "experience": {
                    "latest": {
                        "title": {
                            "any": {
                                "include": {
                                    "mode": "SMART",
                                    "content": list(titles),
                                }
                            }
                        }
                    }
                }
            },
        }

    def find_people(
        self,
        *,
        profile: ClientProfile | None = None,
        domain: str = "",
        company_name: str = "",
        city: str = "",
        state: str = "",
        titles: list[str] | None = None,
        limit: int = AIARK_MAX_PER_COMPANY,
        **_: Any,
    ) -> list[PersonHit]:
        if not self.enabled or not (domain or "").strip():
            return []
        wanted = self._require_titles(titles or (profile.target_titles if profile else None))
        self.last_credits_used = None
        body = self._search_body(
            domain=domain.strip().lower(),
            titles=wanted,
            limit=limit,
        )
        status, data, charged = self._post("/v1/people", body)
        self.last_credits_used = charged
        if status >= 400 or not isinstance(data, dict):
            return []
        content: Any = data.get("content") or data.get("data") or data.get("results") or []
        if isinstance(content, dict):
            content = content.get("content") or content.get("results") or []
        out: list[PersonHit] = []
        seen: set[tuple[str, str]] = set()
        cap = max(1, min(int(limit), MAX_SIZE))
        for row in content:
            if not isinstance(row, dict):
                continue
            person = self._person_from_row(row)
            if not person:
                continue
            key = (person.first_name.lower(), person.last_name.lower())
            if key in seen:
                continue
            seen.add(key)
            if not person.domain:
                person.domain = domain.strip().lower()
            if company_name and not person.company_name:
                person.company_name = company_name
            out.append(person)
            if len(out) >= cap:
                break
        if out:
            self.hits += 1
        if self.last_credits_used is None:
            self.last_credits_used = 0.5 * len(out)
        return out

    def preview_people(
        self,
        domains: list[str],
        *,
        titles: list[str] | None = None,
        profile: ClientProfile | None = None,
        page_size: int = 100,
    ) -> dict[str, Any] | None:
        """Size a title×domain list. Masked. Never write contacts.

        1 credit per page. Used only for estimates.
        """
        if not self.enabled:
            return None
        wanted = self._require_titles(titles or (profile.target_titles if profile else None))
        cleaned = [str(d).strip().lower() for d in domains if str(d).strip()]
        if not cleaned:
            return None
        body = {
            "page": 0,
            "size": max(25, min(int(page_size), 100)),
            "account": {"domain": {"any": {"include": cleaned}}},
            "contact": {
                "experience": {
                    "latest": {
                        "title": {
                            "any": {
                                "include": {
                                    "mode": "SMART",
                                    "content": list(wanted),
                                }
                            }
                        }
                    }
                }
            },
        }
        status, data, charged = self._post("/v2/people/preview", body)
        if status >= 400 or not isinstance(data, dict):
            return None
        return {"payload": data, "credits_used": charged}
