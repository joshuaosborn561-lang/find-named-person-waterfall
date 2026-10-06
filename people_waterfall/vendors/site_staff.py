"""Free website staff tier. Reads public.site_pages, then fetches same-domain pages."""

from __future__ import annotations

import json
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from html.parser import HTMLParser
from typing import Any
from urllib.parse import urljoin, urlparse

import requests

from ..people import PersonHit
from ..profile import ClientProfile
from ..site_quality import (
    bankable_person,
    classify_email,
    email_allowed,
    local_matches,
    name_is_valid,
    parse_name_line,
    registrable_domain,
    same_site,
    title_excluded,
    title_is_usable,
    title_phrase_rank,
)
from ..titles import apply_synonyms

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
)
FETCH_TIMEOUT = 10
MAX_EXTRA_PAGES = 8
MAX_HTML = 1_500_000
WORKERS = 12
PRIORITY = (
    "staff",
    "our-staff",
    "team",
    "our-team",
    "leadership",
    "clergy",
    "pastors",
    "our-pastor",
    "pastor",
    "minister",
    "priest",
    "rector",
    "ministers",
    "rabbi",
    "our-rabbi",
    "about",
    "about-us",
    "who-we-are",
    "contact",
    "contact-us",
    "elders",
    "trustees",
    "board",
    "administration",
    "office",
)
GUESSED_PATHS = (
    "/staff",
    "/our-staff",
    "/team",
    "/our-team",
    "/leadership",
    "/about",
    "/about-us",
    "/contact",
)
EMAIL_RE = re.compile(r"[A-Z0-9._%+\-]+@[A-Z0-9.\-]+\.[A-Z]{2,}", re.I)
OBFUSCATED_RE = re.compile(
    r"""
    ([A-Z0-9._%+\-]+)
    \s*(?:\[at\]|\(at\)|\sat\s)\s*
    ([A-Z0-9.\-]+)
    \s*(?:\[dot\]|\(dot\)|\.)\s*
    ([A-Z]{2,})
    """,
    re.I | re.X,
)
TITLE_HINT = re.compile(
    r"\b(pastor|rabbi|rector|minister|priest|father|monsignor|vicar|"
    r"director|administrator|president|trustee|elder|chair)\b",
    re.I,
)


def decode_cfemail(hex_text: str) -> str:
    raw = (hex_text or "").strip()
    if len(raw) < 4 or len(raw) % 2:
        return ""
    try:
        data = bytes.fromhex(raw)
    except ValueError:
        return ""
    key = data[0]
    return bytes(byte ^ key for byte in data[1:]).decode("utf-8", "ignore")


def extract_emails(text: str) -> list[str]:
    found: list[str] = []
    seen: set[str] = set()

    def add(value: str) -> None:
        email = (value or "").strip().lower().strip(".,;:<>()[]\"'")
        if not email or "@" not in email or email in seen:
            return
        if email.startswith("example@") or email.endswith("@example.com"):
            return
        seen.add(email)
        found.append(email)

    for match in EMAIL_RE.findall(text or ""):
        add(match)
    for match in OBFUSCATED_RE.findall(text or ""):
        add(f"{match[0]}@{match[1]}.{match[2]}")
    return found


def _same_domain(url: str, domain: str) -> bool:
    return same_site(url, domain)


def _priority_score(url: str, anchor: str) -> int:
    blob = f"{url} {anchor}".lower().replace("_", "-")
    score = 0
    for index, token in enumerate(PRIORITY):
        if token in blob:
            score += 1000 - index
    return score


class _PageParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.links: list[tuple[str, str]] = []
        self.cfemails: list[str] = []
        self.ldjson: list[str] = []
        self.blocks: list[dict[str, Any]] = []
        self._href: str | None = None
        self._anchor: list[str] = []
        self._skip_depth = 0
        self._script_ld = False
        self._script_buf: list[str] = []
        self._block: dict[str, Any] | None = None
        self._block_depth = 0
        self._line: list[str] = []

    def _flush_line(self) -> None:
        if self._block is None:
            return
        text = " ".join(part for part in self._line if part).strip()
        self._line = []
        if text:
            self._block["text"].append(text)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attr = {key.lower(): (value or "") for key, value in attrs}
        if tag in {"script", "style", "noscript"}:
            self._skip_depth += 1
            if tag == "script" and "ld+json" in attr.get("type", "").lower():
                self._script_ld = True
                self._script_buf = []
            return
        if self._skip_depth:
            return
        cf = attr.get("data-cfemail")
        if cf:
            self.cfemails.append(cf)
        if tag in {"br", "p", "div", "li", "article", "section", "h1", "h2", "h3", "h4", "h5", "tr"}:
            self._flush_line()
        if tag == "a":
            self._href = attr.get("href") or ""
            self._anchor = []
            if self._href.lower().startswith("mailto:"):
                email = self._href.split(":", 1)[1].split("?")[0].strip()
                if self._block is not None:
                    self._block["emails"].append(email)
                else:
                    self.cfemails.append(email)
        classes = attr.get("class", "").lower()
        interesting = tag in {"article", "li", "section"} or any(
            token in classes
            for token in ("staff", "team", "person", "member", "card", "clergy", "leader")
        )
        if interesting or (self._block is not None and tag == "div"):
            if self._block is None:
                self._block = {"text": [], "emails": []}
                self._block_depth = 0
            self._block_depth += 1

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style", "noscript"} and self._skip_depth:
            if tag == "script" and self._script_ld:
                self.ldjson.append("".join(self._script_buf))
                self._script_ld = False
            self._skip_depth = max(0, self._skip_depth - 1)
            return
        if self._skip_depth:
            return
        if tag == "a" and self._href is not None:
            self.links.append((self._href, "".join(self._anchor).strip()))
            self._href = None
            self._anchor = []
        if tag in {"p", "div", "li", "article", "section", "h1", "h2", "h3", "h4", "h5", "tr"}:
            self._flush_line()
        if self._block is not None and tag in {"article", "li", "section", "div"}:
            self._block_depth -= 1
            if self._block_depth <= 0:
                self._flush_line()
                text = "\n".join(self._block["text"]).strip()
                if text:
                    self.blocks.append({"text": text, "emails": list(self._block["emails"])})
                self._block = None

    def handle_data(self, data: str) -> None:
        if self._script_ld:
            self._script_buf.append(data)
            return
        if self._skip_depth:
            return
        if self._href is not None:
            self._anchor.append(data)
        if self._block is not None and data.strip():
            self._line.append(data.strip())
            self._block["emails"].extend(extract_emails(data))


def _walk_jsonld(node: Any, people: list[dict[str, str]]) -> None:
    if isinstance(node, list):
        for item in node:
            _walk_jsonld(item, people)
        return
    if not isinstance(node, dict):
        return
    kind = str(node.get("@type") or "")
    if "Person" in kind or node.get("name") and node.get("jobTitle"):
        email = node.get("email") or ""
        if isinstance(email, list):
            email = email[0] if email else ""
        people.append(
            {
                "name": str(node.get("name") or ""),
                "title": str(node.get("jobTitle") or node.get("description") or ""),
                "email": str(email or ""),
            }
        )
    for value in node.values():
        if isinstance(value, (dict, list)):
            _walk_jsonld(value, people)


def _people_from_lines(lines: list[str]) -> list[dict[str, str]]:
    out: list[dict[str, str]] = []
    for index, line in enumerate(lines):
        people = parse_name_line(line)
        if not people:
            continue
        adjacent = []
        if index > 0:
            adjacent.append(lines[index - 1])
        if index + 1 < len(lines):
            adjacent.append(lines[index + 1])
        title = ""
        same = re.split(r"\s*[,–—|-]\s*", line, maxsplit=1)
        if len(same) == 2 and title_is_usable(same[1]) and not parse_name_line(same[1]):
            title = same[1].strip()
        if not title:
            for neighbor in adjacent:
                if title_is_usable(neighbor) and not parse_name_line(neighbor):
                    title = neighbor
                    break
        for person in people:
            out.append(
                {
                    "first_name": person.first_name,
                    "last_name": person.last_name,
                    "honorific": person.honorific,
                    "title": title or person.title_hint,
                }
            )
    return out


def _name_title_from_text(text: str) -> list[dict[str, str]]:
    lines = [part.strip(" -|•\t") for part in re.split(r"[\n|•]+", text) if part.strip()]
    if not lines and text.strip():
        lines = [text.strip()]
    return _people_from_lines(lines)


_URL_TITLES = (
    ("senior-pastor", "Senior Pastor"),
    ("lead-pastor", "Lead Pastor"),
    ("executive-pastor", "Executive Pastor"),
    ("our-pastor", "Pastor"),
    ("our-rabbi", "Rabbi"),
    ("pastor", "Pastor"),
    ("rabbi", "Rabbi"),
    ("rector", "Rector"),
    ("minister", "Minister"),
    ("priest", "Priest"),
    ("clergy", "Pastor"),
)


def title_from_url(url: str) -> str:
    path = (urlparse(url).path or "").lower().replace("_", "-")
    for token, title in _URL_TITLES:
        if token in path:
            return title
    return ""


def parse_html(html: str, *, page_url: str = "") -> list[dict[str, Any]]:
    """Return candidate people from one page. Each has name, title, emails, page_url."""
    parser = _PageParser()
    try:
        parser.feed(html or "")
        parser.close()
    except Exception:
        parser.blocks = []
    people: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    url_title = title_from_url(page_url)

    def add(
        first: str,
        last: str,
        title: str,
        emails: list[str],
        honorific: str = "",
    ) -> None:
        if not name_is_valid(first, last):
            return
        chosen_title = title if title_is_usable(title) else (honorific and title_is_usable(honorific) and honorific) or ""
        if not chosen_title and url_title and title_is_usable(url_title):
            chosen_title = url_title
        if not chosen_title:
            return
        key = (first.lower(), last.lower(), chosen_title.lower())
        if key in seen:
            return
        seen.add(key)
        people.append(
            {
                "first_name": first,
                "last_name": last,
                "honorific": honorific,
                "title": chosen_title,
                "emails": emails,
                "page_url": page_url,
            }
        )

    for block in parser.blocks:
        emails = list(block.get("emails") or [])
        emails.extend(extract_emails(block["text"]))
        for item in _name_title_from_text(block["text"]):
            add(
                item["first_name"],
                item["last_name"],
                item.get("title") or "",
                emails,
                item.get("honorific") or "",
            )
    plain = re.sub(r"(?is)<(script|style).*?>.*?</\1>", " ", html or "")
    for cf in re.findall(r'data-cfemail=["\']([0-9a-fA-F]+)["\']', html or ""):
        decoded = decode_cfemail(cf)
        if decoded:
            plain += " " + decoded
    for href in re.findall(r'href=["\']mailto:([^"\'?\s]+)', html or "", flags=re.I):
        plain += " " + href
    plain = re.sub(r"(?i)<br\s*/?>|</p>|</div>|</li>|</h[1-6]>|</tr>", "\n", plain)
    plain = re.sub(r"<[^>]+>", " ", plain)
    lines = [chunk.strip() for chunk in re.split(r"\n+", plain) if chunk.strip()]
    for index, line in enumerate(lines):
        prev_line = lines[index - 1] if index else ""
        next_line = lines[index + 1] if index + 1 < len(lines) else ""
        blob = "\n".join(part for part in (prev_line, line, next_line) if part)
        for item in _people_from_lines([prev_line, line, next_line] if (prev_line or next_line) else [line]):
            add(
                item["first_name"],
                item["last_name"],
                item.get("title") or "",
                extract_emails(blob),
                item.get("honorific") or "",
            )
    for blob in parser.ldjson:
        try:
            payload = json.loads(blob)
        except ValueError:
            continue
        found: list[dict[str, str]] = []
        _walk_jsonld(payload, found)
        for item in found:
            email = item.get("email") or ""
            parsed = parse_name_line(item.get("name") or "")
            if not parsed:
                continue
            person = parsed[0]
            add(
                person.first_name,
                person.last_name,
                item.get("title") or person.title_hint,
                extract_emails(email) or ([email] if "@" in email else []),
                person.honorific,
            )
    return people


def _rank(title: str, profile: ClientProfile) -> int | None:
    if title_excluded(title, profile.exclude_titles):
        return None
    return title_phrase_rank(
        title,
        list(profile.title_priority or profile.target_titles),
        profile.title_synonyms,
    )


@dataclass
class SiteDomainResult:
    domain: str
    person: PersonHit | None = None
    email_type: str = ""
    bank_only: bool = False
    page_url: str = ""
    generic_emails: list[str] = field(default_factory=list)


def pick_contact(
    candidates: list[dict[str, Any]],
    profile: ClientProfile,
    *,
    domain: str,
    company_name: str = "",
    domain_emails: list[str] | None = None,
    allowed_email_domains: set[str] | None = None,
) -> SiteDomainResult:
    """One title-matched person per domain. Generic mail stays on the company."""
    allowed = set(allowed_email_domains or [])
    allowed.add(registrable_domain(domain))
    ranked: list[tuple[int, dict[str, Any]]] = []
    for cand in candidates:
        first = str(cand.get("first_name") or "")
        last = str(cand.get("last_name") or "")
        title = str(cand.get("title") or "")
        if not bankable_person(first, last, title):
            continue
        rank = _rank(title, profile)
        if rank is None:
            continue
        ranked.append((rank, cand))
    generic_all = [
        str(email).lower()
        for email in (domain_emails or [])
        if email and email_allowed(email, allowed)
    ]
    if not ranked:
        return SiteDomainResult(
            domain=domain,
            generic_emails=[
                email
                for email in generic_all
                if classify_email(email) == "generic"
            ],
        )
    ranked.sort(key=lambda item: item[0])
    best_rank = ranked[0][0]
    top = [cand for rank, cand in ranked if rank == best_rank]
    chosen = top[0]
    first = str(chosen.get("first_name") or "")
    last = str(chosen.get("last_name") or "")
    honorific = str(chosen.get("honorific") or "")
    emails: list[str] = []
    for cand in top:
        for email in cand.get("emails") or []:
            raw = str(email).lower()
            if raw and raw not in emails and email_allowed(raw, allowed):
                emails.append(raw)
    pool = list(emails)
    for email in domain_emails or []:
        raw = str(email).lower()
        if raw and raw not in pool and email_allowed(raw, allowed):
            pool.append(raw)
    personal: list[str] = []
    role: list[str] = []
    generic: list[str] = []
    for email in pool:
        kind = classify_email(email, first=first, last=last, honorific=honorific)
        if kind == "personal":
            personal.append(email)
        elif kind == "role":
            role.append(email)
        else:
            generic.append(email)
    email = ""
    email_type = ""
    if personal:
        email = personal[0]
        email_type = "personal"
    elif role:
        email = role[0]
        email_type = "role"
    person = PersonHit(
        first_name=first,
        last_name=last,
        full_name=f"{first} {last}".strip(),
        title=apply_synonyms(str(chosen.get("title") or ""), profile.title_synonyms),
        honorific=honorific,
        domain=domain,
        company_name=company_name,
        email=email,
        email_type=email_type,
        page_url=str(chosen.get("page_url") or ""),
        source_tier="site_staff",
        source_confidence=0.7 if email else 0.5,
        title_rank=best_rank,
        is_current=None,
    )
    if not email:
        person.name_bank_status = "needs_email"
        return SiteDomainResult(
            domain=domain,
            person=person,
            email_type="",
            bank_only=True,
            page_url=person.page_url,
            generic_emails=generic,
        )
    return SiteDomainResult(
        domain=domain,
        person=person,
        email_type=email_type,
        bank_only=False,
        page_url=person.page_url,
        generic_emails=generic,
    )


def _fetch(url: str) -> tuple[str, str]:
    headers = {"User-Agent": UA, "Accept": "text/html,application/xhtml+xml"}
    last_error = ""
    for attempt in range(2):
        try:
            response = requests.get(
                url,
                headers=headers,
                timeout=FETCH_TIMEOUT,
                allow_redirects=True,
            )
        except requests.RequestException as exc:
            last_error = str(exc)
            break
        if response.status_code in {429, 503} and attempt == 0:
            continue
        if response.status_code >= 400:
            return "", ""
        text = response.text or ""
        if len(text) > MAX_HTML:
            text = text[:MAX_HTML]
        return str(response.url or url), text
    return "", last_error


def _cached_pages(domain: str) -> list[dict[str, Any]]:
    try:
        from .. import supabase_sync

        rows = supabase_sync.rest_select(
            "site_pages",
            params={
                "domain": f"eq.{domain}",
                "select": "url,body_text,emails,title",
                "limit": "20",
            },
        )
    except Exception:
        return []
    return rows or []


def resolve_domain(domain: str, profile: ClientProfile, company_name: str = "") -> SiteDomainResult:
    root = (domain or "").strip().lower()
    if root.startswith("www."):
        root = root[4:]
    if not root:
        return SiteDomainResult(domain=domain)
    allowed_email = {registrable_domain(root)}
    site_roots = {registrable_domain(root)}

    def on_site(url: str) -> bool:
        return any(same_site(url, item) for item in site_roots if item)

    candidates: list[dict[str, Any]] = []
    domain_emails: list[str] = []
    homepage_html = ""
    base = ""
    for scheme in ("https", "http"):
        requested = f"{scheme}://{root}/"
        final, html = _fetch(requested)
        if not html:
            continue
        landing = final or requested
        land_root = registrable_domain(landing)
        if land_root and land_root not in site_roots:
            # Homepage of the source domain redirected to its canonical host.
            allowed_email.add(land_root)
            site_roots.add(land_root)
        homepage_html = html
        base = landing
        break
    pages: list[tuple[str, str]] = []
    if homepage_html:
        pages.append((base, homepage_html))
        parser = _PageParser()
        try:
            parser.feed(homepage_html)
        except Exception:
            parser.links = []
        ranked: list[tuple[int, str]] = []
        seen_urls: set[str] = set()
        for href, anchor in parser.links:
            absolute = urljoin(base, href)
            if not on_site(absolute):
                continue
            clean = absolute.split("#")[0]
            if clean in seen_urls:
                continue
            score = _priority_score(clean, anchor)
            if score <= 0:
                continue
            seen_urls.add(clean)
            ranked.append((score, clean))
        ranked.sort(key=lambda item: item[0], reverse=True)
        extras = [url for _score, url in ranked[:MAX_EXTRA_PAGES]]
        if len(extras) < MAX_EXTRA_PAGES:
            for path in GUESSED_PATHS:
                guess = urljoin(base, path)
                if guess not in seen_urls and on_site(guess):
                    extras.append(guess)
                    seen_urls.add(guess)
                if len(extras) >= MAX_EXTRA_PAGES:
                    break
        for url in extras[:MAX_EXTRA_PAGES]:
            if not on_site(url):
                continue
            final, html = _fetch(url)
            landing = final or url
            if html and on_site(landing):
                pages.append((landing, html))
    for row in _cached_pages(root):
        body = str(row.get("body_text") or "")
        page = str(row.get("url") or "")
        if page and not on_site(page):
            continue
        if body:
            candidates.extend(parse_html(body, page_url=page))
        for email in row.get("emails") or []:
            if not isinstance(email, str) or not email_allowed(email, allowed_email):
                continue
            if candidates:
                candidates[-1].setdefault("emails", []).append(email.lower())
            if email.lower() not in domain_emails:
                domain_emails.append(email.lower())
    for page_url, html in pages:
        candidates.extend(parse_html(html, page_url=page_url))
        for email in extract_emails(html):
            if email not in domain_emails and email_allowed(email, allowed_email):
                domain_emails.append(email)
        for href in re.findall(r'href=["\']mailto:([^"\'?\s]+)', html or "", flags=re.I):
            email = href.strip().lower()
            if email and email not in domain_emails and email_allowed(email, allowed_email):
                domain_emails.append(email)
    for cand in candidates:
        if cand.get("emails"):
            continue
        matched = [
            email
            for email in domain_emails
            if email_allowed(email, allowed_email)
            and local_matches(
                email.split("@", 1)[0],
                str(cand.get("first_name") or ""),
                str(cand.get("last_name") or ""),
                str(cand.get("honorific") or ""),
            )
        ]
        if matched:
            cand["emails"] = matched
    try:
        return pick_contact(
            candidates,
            profile,
            domain=root,
            company_name=company_name,
            domain_emails=domain_emails,
            allowed_email_domains=allowed_email,
        )
    except Exception:
        return SiteDomainResult(domain=root)


@dataclass
class SiteStaffClient:
    enabled: bool = True
    calls: int = 0
    last_error: str = ""
    workers: int = WORKERS

    def find_people(self, **kwargs: Any) -> list[PersonHit]:
        if not self.enabled:
            return []
        domain = str(kwargs.get("domain") or "")
        profile = kwargs.get("profile")
        if profile is None:
            return []
        result = resolve_domain(domain, profile, str(kwargs.get("company_name") or ""))
        self.calls += 1
        if result.person and not result.bank_only:
            return [result.person]
        return []

    def resolve_domains(
        self,
        domains: list[str],
        *,
        profile: ClientProfile,
        companies: dict[str, str] | None = None,
    ) -> dict[str, SiteDomainResult]:
        if not self.enabled:
            return {}
        names = companies or {}
        unique: list[str] = []
        seen: set[str] = set()
        for domain in domains:
            host = (domain or "").strip().lower()
            if host.startswith("www."):
                host = host[4:]
            if not host or host in seen:
                continue
            seen.add(host)
            unique.append(host)
        out: dict[str, SiteDomainResult] = {}
        if not unique:
            return out

        def _one(host: str) -> SiteDomainResult:
            try:
                return resolve_domain(host, profile, names.get(host, ""))
            except Exception as exc:  # noqa: BLE001
                self.last_error = f"{type(exc).__name__}: {exc}"
                return SiteDomainResult(domain=host)

        with ThreadPoolExecutor(max_workers=max(1, self.workers)) as pool:
            futures = {pool.submit(_one, host): host for host in unique}
            for future in as_completed(futures):
                host = futures[future]
                try:
                    out[host] = future.result()
                except Exception as exc:  # noqa: BLE001
                    self.last_error = f"{type(exc).__name__}: {exc}"
                    out[host] = SiteDomainResult(domain=host)
                self.calls += 1
        return out
