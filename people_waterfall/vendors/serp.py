"""Apify SERP DM lookup by company name + target titles.

Query:
  site:linkedin.com/in "{company_name}" ("Owner" OR "President" OR ...)

Keep a hit only when personalInfo.companyName contains the queried company
name and personalInfo.jobTitle matches a target title (after synonyms).
Parse the person name from the LinkedIn slug; drop under two tokens.
"""

from __future__ import annotations

import time
from typing import Any
from urllib.parse import quote

from people_waterfall import http_client
from people_waterfall.config import settings
from people_waterfall.people import (
    PersonHit,
    company_name_contains,
    name_from_linkedin_slug,
)
from people_waterfall.profile import ClientProfile
from people_waterfall.titles import title_matches


def build_query(company_name: str, titles: list[str]) -> str:
    company = (company_name or "").strip()
    quoted = [f'"{t.strip()}"' for t in titles if (t or "").strip()]
    if not company:
        return ""
    if quoted:
        return f'site:linkedin.com/in "{company}" ({" OR ".join(quoted)})'
    return f'site:linkedin.com/in "{company}"'


def _job_title_matches(job_title: str, profile: ClientProfile, titles: list[str]) -> bool:
    pool = [t for t in (titles or list(profile.target_titles)) if t]
    return any(title_matches(job_title, t, profile.title_synonyms) for t in pool)


class SerpClient:
    tier = "serp"
    base_url = "https://api.apify.com/v2"

    def __init__(self, token: str | None = None, timeout: int = 45):
        self.token = token if token is not None else settings.apify_token
        self.actor = settings.apify_serp_actor or "apify/google-search-scraper"
        self.timeout = timeout
        self.calls = 0
        self.hits = 0
        self.pending_runs: list[str] = []

    @property
    def enabled(self) -> bool:
        return bool(self.token)

    def _actor_id(self) -> str:
        return quote(self.actor, safe="")

    def start_query(self, query: str) -> str | None:
        if not self.enabled or not query:
            return None
        self.calls += 1
        url = (
            f"{self.base_url}/acts/{self._actor_id()}/runs"
            f"?token={self.token}&waitForFinish=0"
        )
        r = http_client.post(
            self.tier,
            url,
            json={
                "queries": query,
                "maxPagesPerQuery": 1,
                "resultsPerPage": 10,
                "mobileResults": False,
                "languageCode": "en",
                "countryCode": "us",
            },
            headers={"Content-Type": "application/json"},
            timeout=self.timeout,
        )
        if r is None or r.status_code >= 400:
            return None
        try:
            data = r.json()
        except ValueError:
            return None
        run = data.get("data") if isinstance(data, dict) else None
        run_id = str((run or {}).get("id") or "") if isinstance(run, dict) else ""
        if run_id:
            self.pending_runs.append(run_id)
        return run_id or None

    def poll_run(self, run_id: str, *, timeout_s: int = 180) -> list[dict[str, Any]]:
        deadline = time.time() + timeout_s
        while time.time() < deadline:
            r = http_client.get(
                self.tier,
                f"{self.base_url}/actor-runs/{run_id}?token={self.token}",
                timeout=20,
            )
            if r is None:
                time.sleep(5)
                continue
            try:
                data = r.json()
            except ValueError:
                time.sleep(5)
                continue
            run = data.get("data") if isinstance(data, dict) else {}
            status = str((run or {}).get("status") or "")
            if status in {"SUCCEEDED", "FAILED", "ABORTED", "TIMED-OUT"}:
                if status != "SUCCEEDED":
                    return []
                dataset_id = str((run or {}).get("defaultDatasetId") or "")
                if not dataset_id:
                    return []
                items = http_client.get(
                    self.tier,
                    f"{self.base_url}/datasets/{dataset_id}/items?token={self.token}",
                    timeout=30,
                )
                if items is None:
                    return []
                try:
                    payload = items.json()
                except ValueError:
                    return []
                return payload if isinstance(payload, list) else []
            time.sleep(5)
        return []

    def _organic(self, items: list[dict[str, Any]]) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            organic = item.get("organicResults")
            if organic is None:
                organic = item.get("results")
            if isinstance(organic, list) and organic:
                rows.extend(r for r in organic if isinstance(r, dict))
            elif item.get("url") or item.get("link") or isinstance(item.get("personalInfo"), dict):
                rows.append(item)
        return rows

    def parse_people(
        self,
        items: list[dict[str, Any]],
        *,
        company_name: str,
        profile: ClientProfile,
        titles: list[str] | None = None,
    ) -> list[PersonHit]:
        pool = list(titles or profile.target_titles)
        out: list[PersonHit] = []
        seen: set[tuple[str, str]] = set()
        for row in self._organic(items):
            personal = row.get("personalInfo")
            if not isinstance(personal, dict):
                continue
            returned_company = str(personal.get("companyName") or "").strip()
            job_title = str(personal.get("jobTitle") or "").strip()
            if not company_name_contains(returned_company, company_name):
                continue
            if not _job_title_matches(job_title, profile, pool):
                continue
            url = str(row.get("url") or row.get("link") or "")
            first, last = name_from_linkedin_slug(url)
            if not first:
                continue
            person = PersonHit(
                first_name=first,
                last_name=last,
                full_name=f"{first} {last}",
                title=job_title,
                linkedin_url=url,
                company_name=returned_company,
                source_tier=self.tier,
                raw=row,
            )
            key = (person.first_name.lower(), person.last_name.lower())
            if key in seen:
                continue
            seen.add(key)
            out.append(person)
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
        limit: int = 10,
    ) -> list[PersonHit]:
        if not self.enabled or not company_name:
            return []
        titles = titles or list(profile.target_titles)
        query = build_query(company_name, titles)
        run_id = self.start_query(query)
        if not run_id:
            return []
        items = self.poll_run(run_id)
        people = self.parse_people(
            items, company_name=company_name, profile=profile, titles=titles
        )
        if people:
            self.hits += 1
        return people[:limit]
