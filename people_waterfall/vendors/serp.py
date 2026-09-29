"""Apify SERP DM lookup. Four query styles, batched 90–110 per actor run.

A  site:linkedin.com/in "{company}" ("Owner" OR ...)
B  site:{domain} ("staff" OR "our team" OR "leadership" OR "about us")
C  site:facebook.com "{company}" "{city}"
D  "{company}" "{city}" ("administrator" OR ...target_titles) on aggregator hosts

All actor runs start with waitForFinish=0, then poll in parallel.
Unit cost stays $0.0045 per query.
"""

from __future__ import annotations

import logging
import os
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable
from urllib.parse import quote

from people_waterfall import http_client
from people_waterfall.config import settings
from people_waterfall.people import (
    PersonHit,
    company_matches,
    company_name_contains,
    looks_like_person,
    name_from_linkedin_slug,
    split_name,
    url_host,
)
from people_waterfall.profile import (
    DEFAULT_SERP_STYLES,
    ClientProfile,
    normalize_serp_styles,
)
from people_waterfall.titles import title_matches

log = logging.getLogger("people_waterfall.serp")

SERP_CHUNK = 100
SERP_CHUNK_MIN = 90
SERP_CHUNK_MAX = 110
SERP_POLL_INTERVAL_S = 5.0
SERP_POLL_MIN_S = 900.0
SERP_POLL_PER_QUERY_S = 50.0
SERP_POLL_CAP_S = 45 * 60
SERP_START_TIMEOUT_S = 30
# 0 = start every chunk at once (waitForFinish=0), then poll together.
SERP_DEFAULT_RUN_CONCURRENCY = 0
SERP_MEMORY_MB = 4096
DONE_OK = "SUCCEEDED"
DONE_BAD = frozenset({"FAILED", "ABORTED", "TIMED-OUT"})
DEFAULT_STYLES = DEFAULT_SERP_STYLES
STYLE_A_SITE = "site:linkedin.com/in"
STYLE_B_PHRASES = ("staff", "our team", "leadership", "about us")
AGGREGATOR_HOSTS = frozenset(
    {
        "zoominfo.com",
        "rocketreach.co",
        "signalhire.com",
    }
)
DIRECTORY_HOST_MARKERS = (
    "churchdirectory",
    "church-directory",
    "churchdir",
    "schooldirectory",
    "school-directory",
    "schooldir",
    "privateschoolreview",
    "greatschools",
)
EMAIL_RE = re.compile(r"[A-Z0-9._%+\-]+@[A-Z0-9.\-]+\.[A-Z]{2,}", re.I)
NAME_TOKEN = r"[A-Z][a-z]+(?:[-'][A-Z][a-z]+)?"
NAME_RE = re.compile(rf"\b({NAME_TOKEN}(?:\s+{NAME_TOKEN}){{1,2}})\b")
SPLIT_RE = re.compile(r"\s*[|\-–—·•:/]\s*")
FB_OWNED_BY = re.compile(
    r"(?:owned by|owner:|administrator:|director:)\s+"
    rf"({NAME_TOKEN}(?:\s+{NAME_TOKEN}){{1,2}})",
    re.I,
)
FB_IS_THE = re.compile(
    rf"({NAME_TOKEN}(?:\s+{NAME_TOKEN}){{1,2}})\s+is the\s+"
    r"(owner|administrator|director|president|pastor)\b",
    re.I,
)


def poll_budget_s(n_queries: int) -> float:
    n = max(1, int(n_queries))
    return min(SERP_POLL_CAP_S, max(SERP_POLL_MIN_S, n * SERP_POLL_PER_QUERY_S))


def serp_chunk_size() -> int:
    raw = (os.environ.get("SERP_CHUNK") or "").strip()
    if not raw:
        return SERP_CHUNK
    try:
        return max(SERP_CHUNK_MIN, min(SERP_CHUNK_MAX, int(raw)))
    except ValueError:
        return SERP_CHUNK


def serp_run_concurrency(n_chunks: int = 1) -> int:
    raw = (os.environ.get("SERP_RUN_CONCURRENCY") or "").strip()
    if not raw:
        return max(1, int(n_chunks) or 1)
    try:
        return max(1, int(raw))
    except ValueError:
        return max(1, int(n_chunks) or 1)


def chunked(items: list[Any], size: int) -> Iterable[list[Any]]:
    n = max(1, int(size))
    for i in range(0, len(items), n):
        yield items[i : i + n]


def style_key(style: str) -> str:
    letter = (style or "a").strip().lower().removeprefix("serp_")
    if letter not in DEFAULT_STYLES:
        letter = "a"
    return f"serp_{letter}"


def enabled_serp_styles(profile: ClientProfile) -> list[str]:
    wanted = normalize_serp_styles(getattr(profile, "serp_styles", None))
    dropped = {
        str(x).strip().lower().removeprefix("serp_")
        for x in (profile.people_dropped_tiers or [])
    }
    return [s for s in wanted if s not in dropped and f"serp_{s}" not in dropped]


def _or_list(phrases: list[str]) -> str:
    quoted = [f'"{p.strip()}"' for p in phrases if (p or "").strip()]
    if not quoted:
        return ""
    if len(quoted) == 1:
        return quoted[0]
    return f"({' OR '.join(quoted)})"


def build_query_a(company_name: str, titles: list[str]) -> str:
    company = (company_name or "").strip()
    if not company:
        return ""
    titles_q = _or_list(titles)
    if titles_q:
        return f'{STYLE_A_SITE} "{company}" {titles_q}'
    return f'{STYLE_A_SITE} "{company}"'


def build_query(company_name: str, titles: list[str]) -> str:
    """Style A. Kept as the public alias tests and playbook already use."""
    return build_query_a(company_name, titles)


def build_query_b(domain: str) -> str:
    host = url_host(f"https://{domain}") if domain and "://" not in (domain or "") else url_host(domain)
    host = host or (domain or "").strip().lower().lstrip(".")
    if host.startswith("www."):
        host = host[4:]
    if not host:
        return ""
    return f"site:{host} {_or_list(list(STYLE_B_PHRASES))}"


def build_query_c(company_name: str, city: str = "") -> str:
    company = (company_name or "").strip()
    if not company:
        return ""
    city_q = (city or "").strip()
    if city_q:
        return f'site:facebook.com "{company}" "{city_q}"'
    return f'site:facebook.com "{company}"'


def build_query_d(company_name: str, city: str, titles: list[str]) -> str:
    company = (company_name or "").strip()
    if not company:
        return ""
    titles_q = _or_list(titles)
    city_q = (city or "").strip()
    parts = [f'"{company}"']
    if city_q:
        parts.append(f'"{city_q}"')
    if titles_q:
        parts.append(titles_q)
    return " ".join(parts)


def build_style_query(
    style: str,
    *,
    company_name: str,
    domain: str = "",
    city: str = "",
    titles: list[str] | None = None,
) -> str:
    letter = (style or "a").strip().lower().removeprefix("serp_")
    pool = [t for t in (titles or []) if t]
    if letter == "b":
        return build_query_b(domain)
    if letter == "c":
        return build_query_c(company_name, city)
    if letter == "d":
        return build_query_d(company_name, city, pool)
    return build_query_a(company_name, pool)


def style_applies(style: str, *, company_name: str, domain: str = "", city: str = "") -> bool:
    letter = (style or "").strip().lower().removeprefix("serp_")
    if letter == "a":
        return bool((company_name or "").strip())
    if letter == "b":
        return bool((domain or "").strip())
    if letter == "c":
        return bool((company_name or "").strip())
    if letter == "d":
        return bool((company_name or "").strip())
    return False


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


def extract_emails(*texts: str) -> str:
    for text in texts:
        found = EMAIL_RE.findall(text or "")
        if found:
            return found[0]
    return ""


def is_aggregator_host(host: str) -> bool:
    h = (host or "").lower()
    if not h:
        return False
    for root in AGGREGATOR_HOSTS:
        if h == root or h.endswith("." + root):
            return True
    return any(marker in h for marker in DIRECTORY_HOST_MARKERS)


def _title_from_blob(blob: str, profile: ClientProfile, titles: list[str]) -> str:
    pool = [t for t in (titles or list(profile.target_titles)) if t]
    for title in pool:
        if title_matches(blob, title, profile.title_synonyms):
            return title
    lowered = (blob or "").lower()
    for word in (
        "owner",
        "president",
        "administrator",
        "director",
        "pastor",
        "principal",
        "manager",
        "coo",
        "cfo",
        "ceo",
    ):
        if re.search(rf"\b{re.escape(word)}\b", lowered):
            return word.title() if word not in {"coo", "cfo", "ceo"} else word.upper()
    return ""


def _people_from_text(
    *,
    text: str,
    url: str,
    company_name: str,
    domain: str,
    source_tier: str,
    profile: ClientProfile,
    titles: list[str],
    email: str = "",
    raw: dict[str, Any] | None = None,
) -> list[PersonHit]:
    out: list[PersonHit] = []
    seen: set[tuple[str, str]] = set()
    blob = " ".join((text or "").split())
    if not blob:
        return out
    job_title = _title_from_blob(blob, profile, titles)
    names: list[tuple[str, str, str]] = []
    for match in FB_OWNED_BY.finditer(blob):
        first, last = split_name(match.group(1))
        role = "Owner"
        names.append((first, last, role))
    for match in FB_IS_THE.finditer(blob):
        first, last = split_name(match.group(1))
        names.append((first, last, match.group(2)))
    if not names:
        for part in SPLIT_RE.split(blob):
            part = part.strip()
            m = NAME_RE.search(part)
            if not m:
                continue
            first, last = split_name(m.group(1))
            if looks_like_person(first, last) and not any(
                first.lower() == existing[0].lower() for existing in names
            ):
                names.append(
                    (first, last, job_title or _title_from_blob(part, profile, titles))
                )
        if not names:
            for match in NAME_RE.finditer(blob):
                first, last = split_name(match.group(1))
                if looks_like_person(first, last):
                    names.append((first, last, job_title))
    host = url_host(url)
    for first, last, title in names:
        if not looks_like_person(first, last):
            continue
        full = f"{first} {last}".strip()
        company_first = (company_name or "").split()[:1]
        if company_name and (
            company_name_contains(full, company_name)
            or company_name_contains(company_name, full)
            or (company_first and first.lower() == company_first[0].lower())
        ):
            continue
        key = (first.lower(), last.lower())
        if key in seen:
            continue
        seen.add(key)
        out.append(
            PersonHit(
                first_name=first,
                last_name=last,
                full_name=f"{first} {last}",
                title=title,
                linkedin_url=url if "linkedin.com/in" in (url or "").lower() else "",
                domain=host or domain,
                company_name=company_name,
                source_tier=source_tier,
                email=email,
                raw=raw or {},
            )
        )
    return out


@dataclass
class SerpQuery:
    key: str
    query: str
    company_name: str
    titles: list[str] = field(default_factory=list)
    style: str = "a"
    domain: str = ""
    city: str = ""


@dataclass
class SerpQueryResult:
    key: str
    query: str
    people: list[PersonHit]
    cost_usd: float
    run_id: str = ""
    started: bool = False
    company_matched: int = 0
    style: str = "a"


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
        self,
        items: list[dict[str, Any]],
        company_name: str,
        *,
        domain: str = "",
        style: str = "a",
    ) -> int:
        n = 0
        letter = (style or "a").strip().lower()
        for row in self._organic(items):
            if self._row_company_match(row, company_name, domain, letter):
                n += 1
        return n

    def _row_company_match(
        self, row: dict[str, Any], company_name: str, domain: str, style: str
    ) -> bool:
        url = str(row.get("url") or row.get("link") or "")
        host = url_host(url)
        personal = row.get("personalInfo") if isinstance(row.get("personalInfo"), dict) else {}
        returned = str((personal or {}).get("companyName") or "").strip()
        title_text = str(row.get("title") or "")
        snippet = str(
            row.get("description")
            or row.get("snippet")
            or row.get("text")
            or ""
        )
        if style == "a":
            if company_matches(
                input_company=company_name,
                input_domain=domain,
                returned_company=returned,
                returned_domain="",
            ):
                return True
            return company_name_contains(returned, company_name)
        blob = " ".join(part for part in (returned, title_text, snippet) if part)
        return company_matches(
            input_company=company_name,
            input_domain=domain,
            returned_company=blob or returned,
            returned_domain=host,
        )

    def parse_people(
        self,
        items: list[dict[str, Any]],
        *,
        company_name: str,
        profile: ClientProfile,
        titles: list[str] | None = None,
    ) -> list[PersonHit]:
        return self.parse_style(
            items,
            style="a",
            company_name=company_name,
            profile=profile,
            titles=titles,
            require_title=True,
        )

    def parse_style(
        self,
        items: list[dict[str, Any]],
        *,
        style: str,
        company_name: str,
        profile: ClientProfile,
        titles: list[str] | None = None,
        domain: str = "",
        require_title: bool = False,
    ) -> list[PersonHit]:
        letter = (style or "a").strip().lower().removeprefix("serp_")
        source = style_key(letter)
        pool = list(titles or profile.target_titles)
        out: list[PersonHit] = []
        seen: set[tuple[str, str]] = set()
        for row in self._organic(items):
            url = str(row.get("url") or row.get("link") or "")
            host = url_host(url)
            if letter == "d" and not is_aggregator_host(host):
                continue
            if letter == "c" and "facebook.com" not in host:
                continue
            if not self._row_company_match(row, company_name, domain, letter):
                continue
            title_text = str(row.get("title") or "")
            snippet = str(
                row.get("description")
                or row.get("snippet")
                or row.get("text")
                or ""
            )
            email = extract_emails(title_text, snippet, str(row.get("url") or ""))
            people: list[PersonHit] = []
            if letter == "a":
                personal = row.get("personalInfo")
                if isinstance(personal, dict):
                    returned_company = str(personal.get("companyName") or "").strip()
                    job_title = str(personal.get("jobTitle") or "").strip()
                    if require_title and not _job_title_matches(job_title, profile, pool):
                        continue
                    first, last = name_from_linkedin_slug(url)
                    if first:
                        people.append(
                            PersonHit(
                                first_name=first,
                                last_name=last,
                                full_name=f"{first} {last}",
                                title=job_title,
                                linkedin_url=url,
                                company_name=returned_company,
                                domain=host,
                                source_tier=source,
                                email=email,
                                raw=row,
                            )
                        )
            else:
                people = _people_from_text(
                    text=f"{title_text} {snippet}",
                    url=url,
                    company_name=company_name,
                    domain=domain or host,
                    source_tier=source,
                    profile=profile,
                    titles=pool,
                    email=email,
                    raw=row,
                )
            for person in people:
                if letter in {"c", "d"}:
                    if domain:
                        person.domain = domain
                    if company_name and not company_matches(
                        input_company=company_name,
                        input_domain=domain,
                        returned_company=person.company_name,
                        returned_domain=person.domain,
                    ):
                        person.company_name = company_name
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
                style=job.style,
            )
            for job in jobs
        ]

    def _pack_chunk(
        self,
        jobs: list[SerpQuery],
        items: list[dict[str, Any]],
        *,
        profile: ClientProfile,
        unit: float,
        run_id: str,
        started: bool,
    ) -> list[SerpQueryResult]:
        grouped = group_items_by_query(items if isinstance(items, list) else [])
        packed: list[SerpQueryResult] = []
        for job in jobs:
            matched = items_for_query(grouped, job.query) if started else []
            people = self.parse_style(
                matched,
                style=job.style,
                company_name=job.company_name,
                profile=profile,
                titles=job.titles or None,
                domain=job.domain,
                require_title=job.style == "a",
            )
            company_matched = self.count_company_matches(
                matched, job.company_name, domain=job.domain, style=job.style
            )
            if people:
                with self._lock:
                    self.hits += 1
            packed.append(
                SerpQueryResult(
                    key=job.key,
                    query=job.query,
                    people=people,
                    cost_usd=unit if started else 0.0,
                    run_id=run_id,
                    started=started,
                    company_matched=company_matched,
                    style=job.style,
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
        on_chunk: Callable[[list[SerpQueryResult]], None] | None = None,
    ) -> list[SerpQueryResult]:
        """Start every 90–110 query run at once, then poll them together."""
        if not self.enabled or not jobs:
            empty = self._empty_results(jobs, unit=unit, started=False)
            if on_chunk and empty:
                on_chunk(empty)
            return empty
        size = serp_chunk_size() if chunk_size is None else max(1, int(chunk_size))
        chunks = list(chunked(jobs, size))
        started: list[tuple[list[SerpQuery], str | None]] = []
        for chunk in chunks:
            run_id = self.start_queries([job.query for job in chunk])
            started.append((chunk, run_id))

        workers = concurrency if concurrency is not None else serp_run_concurrency(len(started))
        workers = max(1, min(int(workers), len(started) or 1))

        def _poll(pair: tuple[list[SerpQuery], str | None]) -> list[SerpQueryResult]:
            chunk, run_id = pair
            if not run_id:
                packed = self._empty_results(chunk, unit=unit, started=False)
            else:
                items = self.poll_run(run_id, n_queries=len(chunk))
                packed = self._pack_chunk(
                    chunk,
                    items,
                    profile=profile,
                    unit=unit,
                    run_id=run_id,
                    started=True,
                )
            if on_chunk:
                on_chunk(packed)
            return packed

        if workers == 1:
            out: list[SerpQueryResult] = []
            for pair in started:
                out.extend(_poll(pair))
            return out

        by_index: dict[int, list[SerpQueryResult]] = {}
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futs = {pool.submit(_poll, pair): i for i, pair in enumerate(started)}
            for fut in as_completed(futs):
                by_index[futs[fut]] = fut.result()
        out = []
        for i, (chunk, _) in enumerate(started):
            out.extend(by_index.get(i) or self._empty_results(chunk, unit=unit))
        return out

    def search_style(
        self,
        style: str,
        *,
        profile: ClientProfile,
        company_name: str,
        domain: str = "",
        city: str = "",
        titles: list[str] | None = None,
    ) -> list[PersonHit]:
        titles = titles or list(profile.target_titles)
        if not self.enabled or not style_applies(
            style, company_name=company_name, domain=domain, city=city
        ):
            return []
        query = build_style_query(
            style, company_name=company_name, domain=domain, city=city, titles=titles
        )
        if not query:
            return []
        packed = self.resolve_queries(
            [
                SerpQuery(
                    key="one",
                    query=query,
                    company_name=company_name,
                    titles=titles,
                    style=(style or "a").strip().lower().removeprefix("serp_"),
                    domain=domain,
                    city=city,
                )
            ],
            profile=profile,
            concurrency=1,
            chunk_size=SERP_CHUNK_MIN,
        )
        return list(packed[0].people) if packed else []

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
        people: list[PersonHit] = []
        for style in enabled_serp_styles(profile):
            if not style_applies(style, company_name=company_name, domain=domain, city=city):
                continue
            found = self.search_style(
                style,
                profile=profile,
                company_name=company_name,
                domain=domain,
                city=city,
                titles=titles,
            )
            people.extend(found)
            if any(_job_title_matches(p.title, profile, titles) for p in found):
                break
        return people[:limit]
