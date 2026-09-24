"""Apify SERP DM lookup by company name + target titles.

Query:
  site:linkedin.com/in "{company_name}" ("Owner" OR "President" OR ...)

Keep a hit only when personalInfo.companyName contains the queried company
name and personalInfo.jobTitle matches a target title (after synonyms).
Parse the person name from the LinkedIn slug; drop under two tokens.

Batches the way domain-waterfall does: up to 100 queries per actor run,
two runs at a time, poll to SUCCEEDED, map dataset items back by
searchQuery.term. Unit cost stays per query, not per run.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from typing import Any, Iterable
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

log = logging.getLogger("people_waterfall.serp")

SERP_CHUNK = 100
SERP_POLL_INTERVAL_S = 5.0
SERP_POLL_MIN_S = 900.0
SERP_POLL_PER_QUERY_S = 50.0
SERP_POLL_CAP_S = 45 * 60
SERP_START_TIMEOUT_S = 30
SERP_DEFAULT_RUN_CONCURRENCY = 2
SERP_MEMORY_MB = 4096
DONE_OK = "SUCCEEDED"
DONE_BAD = frozenset({"FAILED", "ABORTED", "TIMED-OUT"})


def build_query(company_name: str, titles: list[str]) -> str:
    company = (company_name or "").strip()
    quoted = [f'"{t.strip()}"' for t in titles if (t or "").strip()]
    if not company:
        return ""
    if quoted:
        return f'site:linkedin.com/in "{company}" ({" OR ".join(quoted)})'
    return f'site:linkedin.com/in "{company}"'


def poll_budget_s(n_queries: int) -> float:
    n = max(1, int(n_queries))
    return min(SERP_POLL_CAP_S, max(SERP_POLL_MIN_S, n * SERP_POLL_PER_QUERY_S))


def serp_run_concurrency() -> int:
    raw = (os.environ.get("SERP_RUN_CONCURRENCY") or "").strip()
    if not raw:
        return SERP_DEFAULT_RUN_CONCURRENCY
    try:
        return max(1, int(raw))
    except ValueError:
        return SERP_DEFAULT_RUN_CONCURRENCY


def chunked(items: list[Any], size: int) -> Iterable[list[Any]]:
    n = max(1, int(size))
    for i in range(0, len(items), n):
        yield items[i : i + n]


def _query_term(item: dict[str, Any]) -> str:
    raw = item.get("searchQuery")
    if raw is None:
        raw = item.get("query")
    if isinstance(raw, dict):
        return str(raw.get("term") or raw.get("query") or "").strip()
    return str(raw or "").strip()


def _norm_q(text: str) -> str:
    return " ".join(str(text or "").replace('"', " ").split()).lower()


def group_items_by_query(items: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for item in items:
        if not isinstance(item, dict):
            continue
        term = _query_term(item)
        if not term:
            continue
        grouped.setdefault(term, []).append(item)
    return grouped


def items_for_query(
    grouped: dict[str, list[dict[str, Any]]], query: str
) -> list[dict[str, Any]]:
    if query in grouped:
        return grouped[query]
    want = _norm_q(query)
    for key, rows in grouped.items():
        if _norm_q(key) == want:
            return rows
    return []


def _job_title_matches(job_title: str, profile: ClientProfile, titles: list[str]) -> bool:
    pool = [t for t in (titles or list(profile.target_titles)) if t]
    return any(title_matches(job_title, t, profile.title_synonyms) for t in pool)


@dataclass
class SerpQuery:
    key: str
    query: str
    company_name: str
    titles: list[str] = field(default_factory=list)


@dataclass
class SerpQueryResult:
    key: str
    query: str
    people: list[PersonHit]
    cost_usd: float
    run_id: str = ""
    started: bool = False
    company_matched: int = 0


class SerpClient:
    tier = "serp"
    base_url = "https://api.apify.com/v2"

    def __init__(self, token: str | None = None, timeout: int = 45):
        self.token = token if token is not None else settings.apify_token
        self.actor = settings.apify_serp_actor or "apify/google-search-scraper"
        self.timeout = timeout
        self.calls = 0
        self.hits = 0
        self.runs = 0
        self.pending_runs: list[str] = []
        self._lock = threading.Lock()

    @property
    def enabled(self) -> bool:
        return bool(self.token)

    def _actor_id(self) -> str:
        return quote(self.actor, safe="")

    def _note_start(self, n_queries: int, run_id: str) -> None:
        with self._lock:
            self.calls += n_queries
            self.runs += 1
            if run_id:
                self.pending_runs.append(run_id)

    def start_query(self, query: str) -> str | None:
        return self.start_queries([query] if query else [])

    def start_queries(self, queries: list[str]) -> str | None:
        cleaned = [q.strip() for q in queries if (q or "").strip()]
        if not self.enabled or not cleaned:
            return None
        url = (
            f"{self.base_url}/acts/{self._actor_id()}/runs"
            f"?token={self.token}&waitForFinish=0&memory={SERP_MEMORY_MB}"
        )
        r = http_client.post(
            self.tier,
            url,
            json={
                "queries": "\n".join(cleaned),
                "maxPagesPerQuery": 1,
                "resultsPerPage": 10,
                "mobileResults": False,
                "languageCode": "en",
                "countryCode": "us",
                "saveHtml": False,
                "saveHtmlToKeyValueStore": False,
            },
            headers={"Content-Type": "application/json"},
            timeout=min(self.timeout, SERP_START_TIMEOUT_S)
            if self.timeout
            else SERP_START_TIMEOUT_S,
        )
        if r is None or r.status_code >= 400:
            return None
        try:
            data = r.json()
        except ValueError:
            return None
        run = data.get("data") if isinstance(data, dict) else None
        run_id = str((run or {}).get("id") or "") if isinstance(run, dict) else ""
        if not run_id:
            return None
        self._note_start(len(cleaned), run_id)
        log.info("serp started run_id=%s queries=%s", run_id, len(cleaned))
        return run_id

    def poll_run(
        self,
        run_id: str,
        *,
        timeout_s: float | None = None,
        n_queries: int = 1,
    ) -> list[dict[str, Any]]:
        budget = float(timeout_s) if timeout_s is not None else poll_budget_s(n_queries)
        deadline = time.time() + budget
        while time.time() < deadline:
            r = http_client.get(
                self.tier,
                f"{self.base_url}/actor-runs/{run_id}?token={self.token}",
                timeout=20,
            )
            if r is None:
                time.sleep(SERP_POLL_INTERVAL_S)
                continue
            try:
                data = r.json()
            except ValueError:
                time.sleep(SERP_POLL_INTERVAL_S)
                continue
            run = data.get("data") if isinstance(data, dict) else {}
            status = str((run or {}).get("status") or "")
            if status == DONE_OK:
                dataset_id = str((run or {}).get("defaultDatasetId") or "")
                if not dataset_id:
                    return []
                items = http_client.get(
                    self.tier,
                    f"{self.base_url}/datasets/{dataset_id}/items?token={self.token}&clean=true",
                    timeout=45,
                )
                if items is None:
                    return []
                try:
                    payload = items.json()
                except ValueError:
                    return []
                return payload if isinstance(payload, list) else []
            if status in DONE_BAD:
                return []
            time.sleep(SERP_POLL_INTERVAL_S)
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

    def count_company_matches(
        self, items: list[dict[str, Any]], company_name: str
    ) -> int:
        """Hits whose personalInfo.companyName contains the queried company.

        Title is ignored. Used to decide whether a fallback_titles query is
        worth sending.
        """
        n = 0
        for row in self._organic(items):
            personal = row.get("personalInfo")
            if not isinstance(personal, dict):
                continue
            if company_name_contains(str(personal.get("companyName") or ""), company_name):
                n += 1
        return n

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

    def _empty_results(
        self, jobs: list[SerpQuery], *, unit: float, run_id: str = "", started: bool = False
    ) -> list[SerpQueryResult]:
        cost = unit if started else 0.0
        return [
            SerpQueryResult(
                key=job.key,
                query=job.query,
                people=[],
                cost_usd=cost,
                run_id=run_id,
                started=started,
                company_matched=0,
            )
            for job in jobs
        ]

    def _resolve_chunk(
        self,
        jobs: list[SerpQuery],
        *,
        profile: ClientProfile,
        unit: float,
    ) -> list[SerpQueryResult]:
        queries = [job.query for job in jobs]
        run_id = self.start_queries(queries)
        if not run_id:
            return self._empty_results(jobs, unit=unit, started=False)
        items = self.poll_run(run_id, n_queries=len(queries))
        grouped = group_items_by_query(items if isinstance(items, list) else [])
        packed: list[SerpQueryResult] = []
        for job in jobs:
            matched = items_for_query(grouped, job.query)
            people = self.parse_people(
                matched,
                company_name=job.company_name,
                profile=profile,
                titles=job.titles or None,
            )
            company_matched = self.count_company_matches(matched, job.company_name)
            if people:
                with self._lock:
                    self.hits += 1
            packed.append(
                SerpQueryResult(
                    key=job.key,
                    query=job.query,
                    people=people,
                    cost_usd=unit,
                    run_id=run_id,
                    started=True,
                    company_matched=company_matched,
                )
            )
        return packed

    def resolve_queries(
        self,
        jobs: list[SerpQuery],
        *,
        profile: ClientProfile,
        unit: float = 0.0045,
        concurrency: int | None = None,
        chunk_size: int | None = None,
        on_chunk: Any | None = None,
    ) -> list[SerpQueryResult]:
        """Start up to 100-query actor runs, 2 at a time, map by searchQuery.term.

        on_chunk(packed) fires as each actor run lands so the waterfall can
        write back and advance the counter before the rest of the job finishes.
        """
        if not self.enabled or not jobs:
            empty = self._empty_results(jobs, unit=unit, started=False)
            if on_chunk and empty:
                on_chunk(empty)
            return empty
        size = SERP_CHUNK if chunk_size is None else max(1, int(chunk_size))
        chunks = list(chunked(jobs, size))
        workers = concurrency if concurrency is not None else serp_run_concurrency()
        workers = max(1, min(int(workers), len(chunks)))

        def _run(chunk: list[SerpQuery]) -> list[SerpQueryResult]:
            packed = self._resolve_chunk(chunk, profile=profile, unit=unit)
            if on_chunk:
                on_chunk(packed)
            return packed

        if workers == 1:
            out: list[SerpQueryResult] = []
            for chunk in chunks:
                out.extend(_run(chunk))
            return out

        by_index: dict[int, list[SerpQueryResult]] = {}
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futs = {
                pool.submit(_run, chunk): i for i, chunk in enumerate(chunks)
            }
            for fut in as_completed(futs):
                by_index[futs[fut]] = fut.result()
        out = []
        for i in range(len(chunks)):
            out.extend(by_index.get(i) or self._empty_results(chunks[i], unit=unit))
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
        if not query:
            return []
        packed = self.resolve_queries(
            [SerpQuery(key="one", query=query, company_name=company_name, titles=titles)],
            profile=profile,
            concurrency=1,
            chunk_size=1,
        )
        people = packed[0].people if packed else []
        return people[:limit]
