"""Free site_people tier. Loads domains into topup, then polls the edge function.

SITE_PEOPLE_KEY is read from the process environment only — never stored on
Settings (repr leak) and never printed. An empty key fails before any HTTP.
"""

from __future__ import annotations

import logging
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlencode

from people_waterfall import http_client, supabase_sync
from people_waterfall.config import settings
from people_waterfall.people import PersonHit, split_name
from people_waterfall.preflight import require_site_people_key, site_people_key
from people_waterfall.profile import ClientProfile
from people_waterfall.site_quality import registrable_domain

log = logging.getLogger("people_waterfall.site_people")

REUSE_DAYS = 30
FETCH_N = 70
FETCH_W = 8
EXTRACT_N = 50
EXTRACT_W = 10
FETCH_IN_FLIGHT = 3
EXTRACT_IN_FLIGHT = 2
SLOT_BACKOFF_S = 60.0
SLOT_MARKER = "remaining connection slots are reserved"
CUT_ORG_TYPES = frozenset(
    {
        "school",
        "nonprofit",
        "association",
        "government",
        "directory_or_unrelated",
    }
)
IGNORE_TITLE_WORDS = frozenset(
    {
        "vice",
        "assistant",
        "associate",
        "youth",
        "student",
        "students",
        "children",
        "kids",
        "worship",
        "music",
        "emeritus",
        "emerita",
        "retired",
        "former",
        "interim",
        "intern",
        "deputy",
        "volunteer",
    }
)
_WORD = re.compile(r"\w+", re.I)


def batch_name(client_tag: str, job_id: str) -> str:
    tag = re.sub(r"[^a-z0-9_]", "", (client_tag or "").strip().lower())
    jid = re.sub(r"[^a-z0-9_]", "", (job_id or "").strip().lower())
    return f"fnp_{tag}_{jid}"


def _redact(text: str, key: str) -> str:
    raw = text or ""
    if key:
        raw = raw.replace(key, "[redacted]")
    return raw[:400]


def title_ignored(title: str) -> bool:
    words = {tok.lower() for tok in _WORD.findall(title or "")}
    return bool(words & IGNORE_TITLE_WORDS)


def whole_word_rank(title: str, titles: list[str]) -> int | None:
    """1-based index of the first site_pick_titles hit. None if ignored/miss."""
    if title_ignored(title):
        return None
    hay = {tok.lower() for tok in _WORD.findall(title or "")}
    if not hay:
        return None
    for index, wanted in enumerate(titles):
        tokens = [tok.lower() for tok in _WORD.findall(wanted or "") if tok]
        if tokens and all(tok in hay for tok in tokens):
            return index + 1
    return None


def discolike_eligible(org_type: str, *, picked: bool) -> bool:
    """DiscoLike runs on unpicked rows. Cut org types are skipped. Unreadable still go."""
    if picked:
        return False
    return (org_type or "").strip().lower() not in CUT_ORG_TYPES


@dataclass
class SitePeopleDomainResult:
    domain: str
    pick: PersonHit | None = None
    backups: list[PersonHit] = field(default_factory=list)
    org_name: str = ""
    org_type: str = ""
    picked: bool = False
    ties: int = 0
    fetched: bool = False
    extracted: bool = False
    dollars: float = 0.0


@dataclass
class SitePeopleBatchResult:
    by_domain: dict[str, SitePeopleDomainResult] = field(default_factory=dict)
    loaded: int = 0
    fetched: int = 0
    extracted: int = 0
    picked: int = 0
    dollars: float = 0.0
    batch: str = ""


class SitePeopleClient:
    tier = "site_people"

    def __init__(self, *, enabled: bool | None = None, timeout: int = 120):
        self.timeout = timeout
        self.calls = 0
        self.last_error = ""
        self._enabled_override = enabled

    @property
    def enabled(self) -> bool:
        if self._enabled_override is not None:
            return bool(self._enabled_override)
        return bool(site_people_key())

    def _edge_url(self, *, mode: str, batch: str, n: int, w: int, key: str) -> str:
        base = (settings.supabase_url or "").rstrip("/")
        qs = urlencode({"mode": mode, "batch": batch, "n": n, "w": w, "k": key})
        return f"{base}/functions/v1/site-people?{qs}"

    def _call_edge(self, *, mode: str, batch: str, n: int, w: int) -> str:
        key = require_site_people_key()
        url = self._edge_url(mode=mode, batch=batch, n=n, w=w, key=key)
        self.calls += 1
        response = http_client.get(
            self.tier,
            url,
            timeout=self.timeout,
        )
        body = ""
        if response is not None:
            try:
                body = response.text or ""
            except Exception:  # noqa: BLE001
                body = ""
            if response.status_code >= 400:
                self.last_error = _redact(body or f"http {response.status_code}", key)
                raise RuntimeError(
                    f"site-people {mode} failed ({response.status_code}): {self.last_error}"
                )
        else:
            self.last_error = "site-people request failed"
            raise RuntimeError(self.last_error)
        if SLOT_MARKER in body.lower() or SLOT_MARKER in (self.last_error or "").lower():
            raise _SlotBusy(_redact(body, key))
        return body

    def load_domains(self, domains: list[str], batch: str) -> dict[str, Any]:
        hosts = _unique_hosts(domains)
        if not hosts:
            return {"loaded": 0, "reused": 0, "reset": 0}
        data = supabase_sync.rpc(
            "pw_site_people_load",
            {"p_domains": hosts, "p_batch": batch, "p_reuse_days": REUSE_DAYS},
        )
        if isinstance(data, list) and data and isinstance(data[0], dict):
            data = data[0]
        if not isinstance(data, dict):
            data = {"loaded": len(hosts)}
        return data

    def remaining(self, batch: str) -> dict[str, Any]:
        data = supabase_sync.rpc("pw_site_people_remaining", {"p_batch": batch})
        if isinstance(data, list) and data and isinstance(data[0], dict):
            data = data[0]
        return data if isinstance(data, dict) else {}

    def _poll(self, *, mode: str, batch: str, n: int, w: int, inflight: int, remaining_key: str) -> None:
        busy_sleeps = 0
        while True:
            stats = self.remaining(batch)
            left = int(stats.get(remaining_key) or 0)
            if left <= 0:
                return
            workers = min(inflight, max(1, (left + n - 1) // n))
            try:
                if workers <= 1:
                    self._call_edge(mode=mode, batch=batch, n=n, w=w)
                else:
                    with ThreadPoolExecutor(max_workers=workers) as pool:
                        futs = [
                            pool.submit(self._call_edge, mode=mode, batch=batch, n=n, w=w)
                            for _ in range(workers)
                        ]
                        for fut in as_completed(futs):
                            fut.result()
            except _SlotBusy:
                busy_sleeps += 1
                log.warning("site-people %s backing off %ss (slot reserved)", mode, SLOT_BACKOFF_S)
                time.sleep(SLOT_BACKOFF_S)
                if busy_sleeps >= 8:
                    raise RuntimeError(
                        f"site-people {mode} exhausted connection-slot backoff"
                    )
                continue
            busy_sleeps = 0

    def poll_fetch(self, batch: str) -> None:
        self._poll(
            mode="fetch",
            batch=batch,
            n=FETCH_N,
            w=FETCH_W,
            inflight=FETCH_IN_FLIGHT,
            remaining_key="fetch_remaining",
        )

    def poll_extract(self, batch: str) -> None:
        self._poll(
            mode="extract",
            batch=batch,
            n=EXTRACT_N,
            w=EXTRACT_W,
            inflight=EXTRACT_IN_FLIGHT,
            remaining_key="extract_remaining",
        )

    def roster(self, batch: str) -> list[dict[str, Any]]:
        data = supabase_sync.rpc("pw_site_people_roster", {"p_batch": batch})
        if isinstance(data, list):
            return [row for row in data if isinstance(row, dict)]
        return []

    def picks(self, batch: str) -> list[dict[str, Any]]:
        data = supabase_sync.rpc("pw_site_people_picks", {"p_batch": batch})
        if isinstance(data, list):
            return [row for row in data if isinstance(row, dict)]
        return []

    def resolve_domains(
        self,
        domains: list[str],
        *,
        profile: ClientProfile | None = None,
        companies: dict[str, str] | None = None,
        batch: str = "",
        job_id: str = "",
        client_tag: str = "",
    ) -> SitePeopleBatchResult:
        require_site_people_key()
        if not self.enabled:
            raise RuntimeError(
                "site_people is selected but SITE_PEOPLE_KEY is missing; "
                "refusing to run with an empty key"
            )
        tag = client_tag or (profile.client_tag if profile is not None else "")
        name = batch or batch_name(tag, job_id)
        hosts = _unique_hosts(domains)
        out = SitePeopleBatchResult(batch=name)
        if not hosts:
            return out
        loaded = self.load_domains(hosts, name)
        out.loaded = int(loaded.get("loaded") or loaded.get("domains") or len(hosts))
        self.poll_fetch(name)
        self.poll_extract(name)
        stats = self.remaining(name)
        out.fetched = int(stats.get("fetched_ok") or 0)
        out.extracted = int(stats.get("extracted_ok") or 0)
        out.dollars = float(stats.get("dollars") or 0)
        titles = list((profile.site_pick_titles if profile is not None else []) or [])
        roster = self.roster(name)
        by_host: dict[str, list[dict[str, Any]]] = {}
        org: dict[str, dict[str, Any]] = {}
        for row in roster:
            host = registrable_domain(str(row.get("domain") or ""))
            if not host:
                continue
            by_host.setdefault(host, []).append(row)
            org[host] = {
                "org_name": str(row.get("org_name") or ""),
                "org_type": str(row.get("org_type") or ""),
                "fetched": bool(row.get("fetched")),
                "extracted": bool(row.get("extracted")),
                "dollars": float(row.get("dollars") or 0),
            }
        pick_rows = self.picks(name) if not titles else []
        picks_by_host: dict[str, list[dict[str, Any]]] = {}
        if titles:
            picks_by_host = _custom_picks(by_host, titles)
        else:
            for row in pick_rows:
                host = registrable_domain(str(row.get("domain") or ""))
                if host:
                    picks_by_host.setdefault(host, []).append(row)
        companies = companies or {}
        for host in hosts:
            meta = org.get(host) or {}
            result = SitePeopleDomainResult(
                domain=host,
                org_name=str(meta.get("org_name") or companies.get(host) or ""),
                org_type=str(meta.get("org_type") or ""),
                fetched=bool(meta.get("fetched")),
                extracted=bool(meta.get("extracted")),
                dollars=float(meta.get("dollars") or 0),
            )
            chosen = list(picks_by_host.get(host) or [])
            if chosen:
                winner = chosen[0]
                ties = int(winner.get("ties") or len(chosen) or 1)
                result.ties = ties
                result.picked = True
                result.pick = _person_from_pick(
                    winner, host, result.org_name, source="site_people"
                )
                if ties > 1:
                    extras = chosen[1:] if len(chosen) > 1 else []
                    if not extras:
                        winner_key = str(winner.get("person_key") or "")
                        winner_rank = winner.get("rnk")
                        extras = []
                        for row in by_host.get(host, []):
                            if str(row.get("person_key") or "") == winner_key:
                                continue
                            if not str(row.get("full_name") or row.get("person_key") or ""):
                                continue
                            row_rank = row.get("rnk")
                            if winner_rank is None or row_rank is None or row_rank == winner_rank:
                                extras.append(row)
                    for row in extras:
                        person = _person_from_pick(
                            row, host, result.org_name, source="site_people"
                        )
                        person.backup = True
                        result.backups.append(person)
                out.picked += 1
            out.by_domain[host] = result
        return out


def _unique_hosts(domains: list[str]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for raw in domains:
        host = registrable_domain(str(raw or ""))
        if not host or host in seen:
            continue
        seen.add(host)
        out.append(host)
    return out


def _person_from_pick(
    row: dict[str, Any], domain: str, company: str, *, source: str
) -> PersonHit:
    full = str(row.get("full_name") or row.get("name") or "").strip()
    first = str(row.get("first_name") or "").strip()
    last = str(row.get("last_name") or "").strip()
    if not first and full:
        first, last = split_name(full)
    return PersonHit(
        first_name=first,
        last_name=last,
        full_name=full or f"{first} {last}".strip(),
        title=str(row.get("title") or row.get("job_title") or ""),
        domain=domain,
        company_name=str(row.get("org_name") or company or ""),
        page_url=str(row.get("page_url") or ""),
        source_tier=source,
        source_confidence=1.0,
        raw=row,
    )


def _custom_picks(
    by_host: dict[str, list[dict[str, Any]]], titles: list[str]
) -> dict[str, list[dict[str, Any]]]:
    out: dict[str, list[dict[str, Any]]] = {}
    for host, rows in by_host.items():
        org_type = str((rows[0].get("org_type") if rows else "") or "").lower()
        if org_type in CUT_ORG_TYPES:
            continue
        ranked: list[tuple[int, dict[str, Any]]] = []
        for row in rows:
            if (row.get("full_name") or "") and not re.search(
                r"^\S+\s+\S+", str(row.get("full_name") or "")
            ):
                continue
            rank = whole_word_rank(str(row.get("title") or ""), titles)
            if rank is None:
                continue
            copy = dict(row)
            copy["rnk"] = rank
            ranked.append((rank, copy))
        if not ranked:
            continue
        best = min(item[0] for item in ranked)
        winners = [row for rank, row in ranked if rank == best]
        for row in winners:
            row["ties"] = len(winners)
        winners.sort(key=lambda r: str(r.get("person_key") or r.get("full_name") or ""))
        out[host] = winners
    return out


class _SlotBusy(RuntimeError):
    pass
