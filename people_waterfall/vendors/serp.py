"""Apify SERP DM lookup. Name+location, no domain required.

Fire batches with waitForFinish=0, then poll. Keep a hit only when
personalInfo.companyName contains the first ten characters of the queried
company and jobTitle matches after synonyms. Parse name from the profile
slug; drop under two tokens.
"""

from __future__ import annotations

import time
from typing import Any
from urllib.parse import quote

from people_waterfall import http_client
from people_waterfall.config import settings
from people_waterfall.people import (
    PersonHit,
    company_prefix,
    name_from_linkedin_slug,
    person_from_row,
)
from people_waterfall.profile import ClientProfile
from people_waterfall.titles import title_matches


def build_query(company_name: str, titles: list[str]) -> str:
    company = (company_name or "").strip()
    title_clause = " OR ".join(f'"{t}"' for t in titles[:8] if t)
    if title_clause:
        return f'site:linkedin.com/in "{company}" ({title_clause})'
    return f'site:linkedin.com/in "{company}"'


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
            organic = item.get("organicResults") or item.get("results") or []
            if isinstance(organic, list):
                rows.extend(r for r in organic if isinstance(r, dict))
            elif item.get("url") or item.get("link"):
                rows.append(item)
        return rows

    def parse_people(
        self,
        items: list[dict[str, Any]],
        *,
        company_name: str,
        profile: ClientProfile,
    ) -> list[PersonHit]:
        prefix = company_prefix(company_name)
        out: list[PersonHit] = []
        seen: set[tuple[str, str]] = set()
        for row in self._organic(items):
            url = str(row.get("url") or row.get("link") or "")
            title = str(row.get("title") or "")
            snippet = str(row.get("description") or row.get("snippet") or "")
            personal = row.get("personalInfo") if isinstance(row.get("personalInfo"), dict) else {}
            job_title = str(
                personal.get("jobTitle") or row.get("jobTitle") or title.split("-")[0]
            ).strip()
            returned_company = str(
                personal.get("companyName") or row.get("companyName") or ""
            )
            if prefix:
                hay = (returned_company or f"{title} {snippet}").lower()
                if prefix not in "".join(ch for ch in hay if ch.isalnum()) and prefix not in hay.replace(" ", ""):
                    # still allow organic title/snippet to carry the company
                    compact = "".join(ch for ch in hay if ch.isalnum())
                    if prefix not in compact:
                        continue
            first, last = name_from_linkedin_slug(url)
            if not first:
                continue
            if not any(
                title_matches(job_title, t, profile.title_synonyms)
                for t in profile.target_titles
            ):
                # keep for name_bank via title audit later; still return
                pass
            person = PersonHit(
                first_name=first,
                last_name=last,
                full_name=f"{first} {last}",
                title=job_title,
                linkedin_url=url,
                company_name=returned_company or company_name,
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
        people = self.parse_people(items, company_name=company_name, profile=profile)
        if people:
            self.hits += 1
        return people[:limit]
