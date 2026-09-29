"""DiscoLike generate-contacts. One sequential task per domain list.

POST /v1/contacts/discover/generate then poll GET /v1/discogen/status/{task_id}.
Never start a second task while one is in flight — DiscoLike rate-limits them.
"""

from __future__ import annotations

import logging
import os
import re
import time
from dataclasses import dataclass, field
from typing import Any

from people_waterfall import http_client
from people_waterfall.config import settings
from people_waterfall.people import (
    PersonHit,
    looks_like_person,
    name_derived_from_company,
    split_name,
    url_host,
)
from people_waterfall.profile import ClientProfile, build_discolike_icp

log = logging.getLogger("people_waterfall.discolike")

DISCOGEN_GENERATE = "/contacts/discover/generate"
DISCOGEN_STATUS = "/discogen/status/{task_id}"
TASK_CAP = 5000
POLL_FAST_S = 30.0
POLL_SLOW_S = 180.0
POLL_FAST_BELOW = 500
QUERIES_LOW = 2
MAX_CONTACTS = 3
DONE_OK = frozenset({"completed", "succeeded", "success", "done"})
DONE_BAD = frozenset({"failed", "error", "aborted", "cancelled", "canceled", "timed_out", "timed-out"})


def queries_per_domain() -> int:
    raw = (os.environ.get("DISCOLIKE_QUERIES_PER_DOMAIN") or "").strip()
    if raw:
        try:
            return max(1, int(raw))
        except ValueError:
            pass
    return QUERIES_LOW


def serper_usd_per_query() -> float:
    raw = (os.environ.get("SERPER_USD_PER_QUERY") or "").strip()
    try:
        return float(raw) if raw else 0.001
    except ValueError:
        return 0.001


def discolike_usd_per_company() -> float:
    raw = (os.environ.get("DISCOLIKE_USD_PER_COMPANY") or "").strip()
    try:
        return float(raw) if raw else 0.0035
    except ValueError:
        return 0.0035


def unit_usd() -> float:
    return serper_usd_per_query() * queries_per_domain() + discolike_usd_per_company()


def poll_interval_s(n_domains: int) -> float:
    return POLL_FAST_S if n_domains < POLL_FAST_BELOW else POLL_SLOW_S


def poll_budget_s(n_domains: int) -> float:
    n = max(1, int(n_domains))
    interval = poll_interval_s(n)
    return min(6 * 3600, max(15 * 60, interval * max(n, 10)))


def _norm_host(value: str) -> str:
    raw = (value or "").strip().lower()
    raw = re.sub(r"^https?://", "", raw)
    raw = raw.split("/")[0].split(":")[0]
    if raw.startswith("www."):
        raw = raw[4:]
    return raw


def chunked(items: list[str], size: int = TASK_CAP) -> list[list[str]]:
    n = max(1, int(size))
    return [items[i : i + n] for i in range(0, len(items), n)]


@dataclass
class DiscoDomainResult:
    domain: str
    people: list[PersonHit] = field(default_factory=list)
    bank_only: list[PersonHit] = field(default_factory=list)
    email_pattern: str = ""
    email_pattern_confidence: float | None = None
    cost_usd: float = 0.0


class DiscoLikeClient:
    tier = "discolike"
    base_url = "https://api.discolike.com/v1"

    def __init__(self, api_key: str | None = None, timeout: int = 45):
        self.api_key = api_key if api_key is not None else settings.discolike_api_key
        self.timeout = timeout
        self.calls = 0
        self.hits = 0
        self.tasks = 0

    @property
    def enabled(self) -> bool:
        return bool(self.api_key)

    def _headers(self) -> dict[str, str]:
        return {
            "x-discolike-key": self.api_key,
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    def start_generate(
        self,
        domains: list[str],
        *,
        icp_text: str,
        max_contacts: int = MAX_CONTACTS,
    ) -> str | None:
        cleaned = [_norm_host(d) for d in domains if _norm_host(d)]
        if not self.enabled or not cleaned or not (icp_text or "").strip():
            return None
        url = f"{self.base_url}{DISCOGEN_GENERATE}"
        r = http_client.post(
            self.tier,
            url,
            json={
                "icp_text": icp_text.strip(),
                "domains": cleaned,
                "integration_id": "native",
                "search_context_size": "low",
                "max_contacts_per_domain": max(1, int(max_contacts)),
                "find_emails": False,
            },
            headers=self._headers(),
            timeout=min(self.timeout, 60),
        )
        if r is None or r.status_code >= 400:
            return None
        try:
            data = r.json()
        except ValueError:
            return None
        if not isinstance(data, dict):
            return None
        task_id = str(
            data.get("task_id") or data.get("id") or (data.get("data") or {}).get("task_id") or ""
        )
        if not task_id:
            return None
        self.calls += len(cleaned)
        self.tasks += 1
        log.info("discolike started task_id=%s domains=%s", task_id, len(cleaned))
        return task_id

    def poll_task(self, task_id: str, *, n_domains: int = 1) -> dict[str, Any]:
        budget = poll_budget_s(n_domains)
        interval = poll_interval_s(n_domains)
        deadline = time.time() + budget
        url = f"{self.base_url}{DISCOGEN_STATUS.format(task_id=task_id)}"
        while time.time() < deadline:
            r = http_client.get(self.tier, url, headers=self._headers(), timeout=30)
            if r is None:
                time.sleep(interval)
                continue
            try:
                data = r.json()
            except ValueError:
                time.sleep(interval)
                continue
            if not isinstance(data, dict):
                time.sleep(interval)
                continue
            status = str(data.get("status") or "").strip().lower()
            if status in DONE_OK:
                return data
            if status in DONE_BAD:
                return {}
            time.sleep(interval)
        return {}

    def parse_results(
        self,
        payload: dict[str, Any],
        *,
        profile: ClientProfile,
        companies: dict[str, str] | None = None,
    ) -> dict[str, DiscoDomainResult]:
        grouped = _results_by_domain(payload)
        out: dict[str, DiscoDomainResult] = {}
        companies = companies or {}
        for domain, block in grouped.items():
            host = _norm_host(domain)
            company = companies.get(host) or str(block.get("company_name") or "")
            pattern = str(block.get("email_pattern") or "")
            conf = _as_float(block.get("email_pattern_confidence"))
            people: list[PersonHit] = []
            bank_only: list[PersonHit] = []
            for row in _contacts_of(block):
                person, dest = self._person_from_contact(
                    row,
                    input_domain=host,
                    company_name=company,
                    email_pattern=pattern,
                    email_pattern_confidence=conf,
                )
                if person is None or dest == "drop":
                    continue
                if dest == "bank":
                    bank_only.append(person)
                else:
                    people.append(person)
            if people:
                self.hits += 1
            out[host] = DiscoDomainResult(
                domain=host,
                people=people,
                bank_only=bank_only,
                email_pattern=pattern,
                email_pattern_confidence=conf,
            )
        return out

    def _person_from_contact(
        self,
        row: dict[str, Any],
        *,
        input_domain: str,
        company_name: str,
        email_pattern: str,
        email_pattern_confidence: float | None,
    ) -> tuple[PersonHit | None, str]:
        status = str(row.get("match_status") or row.get("matchStatus") or "").strip().lower()
        if status in {"not_match", "nomatch", "no_match", "mismatch"}:
            return None, "drop"
        name = str(row.get("name") or row.get("full_name") or "").strip()
        first, last = split_name(name)
        if not first or not last:
            return None, "drop"
        if not looks_like_person(first, last):
            return None, "drop"
        if name_derived_from_company(first, last, company_name, input_domain):
            return None, "drop"
        discovered = _norm_host(
            str(row.get("discovered_domain") or row.get("domain") or row.get("company_domain") or "")
        )
        source_host = url_host(str(row.get("source_url") or row.get("url") or row.get("page_url") or ""))
        own_page = bool(source_host and (source_host == input_domain or source_host.endswith("." + input_domain)))
        if discovered and discovered != input_domain and not own_page:
            return None, "drop"
        if not discovered and not own_page:
            # Native extractor keyed this row under the input domain.
            discovered = input_domain
        loc = row.get("location") if isinstance(row.get("location"), dict) else {}
        linkedin = str(
            row.get("linkedin_url")
            or row.get("linkedin")
            or _first_linkedin(row.get("social_urls"))
            or ""
        )
        person = PersonHit(
            first_name=first,
            last_name=last,
            full_name=name,
            title=str(row.get("title") or row.get("job_title") or ""),
            linkedin_url=linkedin,
            domain=input_domain,
            company_name=company_name,
            person_city=str(row.get("city") or loc.get("city") or ""),
            person_state=str(row.get("state") or loc.get("state") or loc.get("region") or ""),
            source_tier="discolike",
            raw=row,
        )
        if status in {"unsure", "unknown", "maybe"}:
            return person, "bank"
        return person, "keep"

    def resolve_domains(
        self,
        domains: list[str],
        *,
        profile: ClientProfile,
        companies: dict[str, str] | None = None,
        unit: float | None = None,
        max_contacts: int = MAX_CONTACTS,
        icp_text: str = "",
    ) -> dict[str, DiscoDomainResult]:
        """One sequential task per ≤5000-domain chunk. Never concurrent."""
        cleaned: list[str] = []
        seen: set[str] = set()
        for raw in domains:
            host = _norm_host(raw)
            if not host or host in seen:
                continue
            seen.add(host)
            cleaned.append(host)
        empty = {d: DiscoDomainResult(domain=d) for d in cleaned}
        if not self.enabled or not cleaned:
            return empty
        icp = (icp_text or "").strip() or build_discolike_icp(profile)
        price = unit if unit is not None else unit_usd()
        out: dict[str, DiscoDomainResult] = {}
        for chunk in chunked(cleaned, TASK_CAP):
            task_id = self.start_generate(chunk, icp_text=icp, max_contacts=max_contacts)
            if not task_id:
                for domain in chunk:
                    out.setdefault(domain, DiscoDomainResult(domain=domain))
                continue
            payload = self.poll_task(task_id, n_domains=len(chunk))
            parsed = self.parse_results(payload, profile=profile, companies=companies)
            for domain in chunk:
                row = parsed.get(domain) or DiscoDomainResult(domain=domain)
                row.cost_usd = price
                out[domain] = row
        return out

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
        host = _norm_host(domain)
        if not self.enabled or not host:
            return []
        packed = self.resolve_domains(
            [host],
            profile=profile,
            companies={host: company_name} if company_name else None,
        )
        row = packed.get(host)
        return list(row.people) if row else []


def _as_float(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _first_linkedin(value: Any) -> str:
    if isinstance(value, str) and "linkedin.com" in value.lower():
        return value
    if isinstance(value, list):
        for item in value:
            if isinstance(item, str) and "linkedin.com" in item.lower():
                return item
    return ""


def _contacts_of(block: Any) -> list[dict[str, Any]]:
    if isinstance(block, list):
        return [r for r in block if isinstance(r, dict)]
    if not isinstance(block, dict):
        return []
    for key in ("contacts", "people", "results", "rows"):
        raw = block.get(key)
        if isinstance(raw, list):
            return [r for r in raw if isinstance(r, dict)]
    if block.get("name") or block.get("title"):
        return [block]
    return []


def _results_by_domain(payload: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(payload, dict):
        return {}
    for key in ("results", "data", "domains", "interim_results"):
        raw = payload.get(key)
        if isinstance(raw, dict):
            if _looks_grouped(raw):
                return raw
            inner = raw.get("results") if isinstance(raw.get("results"), dict) else None
            if inner and _looks_grouped(inner):
                return inner
        if isinstance(raw, list):
            out: dict[str, Any] = {}
            for item in raw:
                if not isinstance(item, dict):
                    continue
                host = _norm_host(str(item.get("domain") or item.get("discovered_domain") or ""))
                if host:
                    out[host] = item
            if out:
                return out
    return {}


def _looks_grouped(raw: dict[str, Any]) -> bool:
    if not raw:
        return False
    sample = next(iter(raw.values()), None)
    return isinstance(sample, (dict, list))

