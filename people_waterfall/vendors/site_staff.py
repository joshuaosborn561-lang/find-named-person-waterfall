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

from ..people import PersonHit, looks_like_person, split_name
from ..profile import ClientProfile
from ..titles import apply_synonyms, normalize_title

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
ROLE_LOCALS = frozenset(
    {"pastor", "rabbi", "rector", "office", "priest", "minister", "vicar", "father"}
)
GENERIC_LOCALS = frozenset(
    {"info", "contact", "admin", "hello", "mail", "enquiries", "inquiries"}
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


def _host(url: str) -> str:
    host = (urlparse(url).hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    return host


def _same_domain(url: str, domain: str) -> bool:
    host = _host(url)
    root = (domain or "").lower().lstrip(".")
    return bool(host) and (host == root or host.endswith("." + root))


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
        if tag in {"h1", "h2", "h3", "h4", "h5"} and self._block is not None:
            self._block["heading"] = True

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
        if self._block is not None and tag in {"article", "li", "section", "div"}:
            self._block_depth -= 1
            if self._block_depth <= 0:
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
            self._block["text"].append(data.strip())
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


def _name_title_from_text(text: str) -> tuple[str, str]:
    lines = [part.strip(" -|•\t") for part in re.split(r"[\n|•]+", text) if part.strip()]
    if not lines:
        lines = [text.strip()]
    name = ""
    title = ""
    for line in lines:
        if len(line) > 80:
            continue
        first, last = split_name(line)
        if not name and looks_like_person(first, last) and not TITLE_HINT.search(line):
            name = f"{first} {last}".strip()
            continue
        if TITLE_HINT.search(line) and len(line) <= 80:
            title = line
            if name:
                break
    if not name:
        for line in lines:
            first, last = split_name(line)
            if looks_like_person(first, last):
                name = f"{first} {last}".strip()
                break
    return name, title


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

    def add(name: str, title: str, emails: list[str]) -> None:
        first, last = split_name(name)
        if not looks_like_person(first, last or ""):
            return
        key = (first.lower(), (last or "").lower(), (title or "").lower())
        if key in seen:
            return
        seen.add(key)
        people.append(
            {
                "first_name": first,
                "last_name": last,
                "title": title,
                "emails": emails,
                "page_url": page_url,
            }
        )

    for block in parser.blocks:
        name, title = _name_title_from_text(block["text"])
        emails = list(block.get("emails") or [])
        emails.extend(extract_emails(block["text"]))
        if name:
            add(name, title, emails)
    plain = re.sub(r"(?is)<(script|style).*?>.*?</\1>", " ", html or "")
    for cf in re.findall(r'data-cfemail=["\']([0-9a-fA-F]+)["\']', html or ""):
        decoded = decode_cfemail(cf)
        if decoded:
            plain += " " + decoded
    for href in re.findall(r'href=["\']mailto:([^"\'?\s]+)', html or "", flags=re.I):
        plain += " " + href
    plain = re.sub(r"(?i)<br\s*/?>|</p>|</div>|</li>|</h[1-6]>|</tr>", "\n", plain)
    plain = re.sub(r"<[^>]+>", " ", plain)
    for match in OBFUSCATED_RE.findall(plain):
        plain += f" {match[0]}@{match[1]}.{match[2]}"
    chunks = [chunk.strip() for chunk in re.split(r"\n+", plain) if chunk.strip()]
    window: list[str] = []
    for chunk in chunks:
        window.append(chunk)
        window = window[-4:]
        blob = " ".join(window)
        name, title = _name_title_from_text(blob)
        if name and title:
            add(name, title, extract_emails(blob))
    for blob in parser.ldjson:
        try:
            payload = json.loads(blob)
        except ValueError:
            continue
        found: list[dict[str, str]] = []
        _walk_jsonld(payload, found)
        for item in found:
            email = item.get("email") or ""
            add(item.get("name") or "", item.get("title") or "", extract_emails(email) or ([email] if "@" in email else []))
    return people


def local_matches(local: str, first: str, last: str) -> bool:
    loc = re.sub(r"[^a-z0-9]", "", (local or "").lower())
    first_n = re.sub(r"[^a-z]", "", (first or "").lower())
    last_n = re.sub(r"[^a-z]", "", (last or "").lower())
    if not loc or not first_n:
        return False
    cands = {first_n}
    if last_n:
        cands.update(
            {
                first_n + last_n,
                first_n[:1] + last_n,
                first_n + last_n[:1],
            }
        )
    return loc in cands


def _excluded(title: str, profile: ClientProfile) -> bool:
    norm = normalize_title(apply_synonyms(title, profile.title_synonyms))
    if not norm:
        return False
    for raw in profile.exclude_titles:
        excluded = normalize_title(apply_synonyms(raw, profile.title_synonyms))
        if excluded and (excluded == norm or excluded in norm):
            return True
    return False


def _rank(title: str, profile: ClientProfile) -> int | None:
    priority = profile.title_priority or profile.target_titles
    norm = normalize_title(apply_synonyms(title, profile.title_synonyms))
    if not norm or not priority:
        return None
    best: int | None = None
    for index, raw in enumerate(priority):
        cand = normalize_title(apply_synonyms(raw, profile.title_synonyms))
        if cand and (cand == norm or cand in norm):
            if best is None or index < best:
                best = index
    return best


def _email_kind(email: str) -> str:
    local = email.split("@", 1)[0].lower()
    local = re.sub(r"[^a-z]", "", local)
    if local in ROLE_LOCALS:
        return "role"
    if local in GENERIC_LOCALS:
        return "generic"
    return "personal"


@dataclass
class SiteDomainResult:
    domain: str
    person: PersonHit | None = None
    email_type: str = ""
    bank_only: bool = False
    page_url: str = ""


def pick_contact(
    candidates: list[dict[str, Any]],
    profile: ClientProfile,
    *,
    domain: str,
    company_name: str = "",
) -> SiteDomainResult:
    """One title-matched person per domain. Role mail only if no personal mail."""
    ranked: list[tuple[int, dict[str, Any]]] = []
    for cand in candidates:
        title = str(cand.get("title") or "")
        if _excluded(title, profile):
            continue
        rank = _rank(title, profile)
        if rank is None:
            continue
        ranked.append((rank, cand))
    if not ranked:
        return SiteDomainResult(domain=domain)
    ranked.sort(key=lambda item: item[0])
    best_rank = ranked[0][0]
    top = [cand for rank, cand in ranked if rank == best_rank]
    chosen = top[0]
    emails = []
    for cand in top:
        for email in cand.get("emails") or []:
            if email and email not in emails:
                emails.append(str(email).lower())
    # Also consider emails that sit on the chosen card only when top is one person.
    personal = []
    role = []
    generic = []
    first = str(chosen.get("first_name") or "")
    last = str(chosen.get("last_name") or "")
    for email in emails:
        kind = _email_kind(email)
        local = email.split("@", 1)[0]
        if kind == "personal" and (
            local_matches(local, first, last) or email in (chosen.get("emails") or [])
        ):
            personal.append(email)
        elif kind == "role":
            role.append(email)
        elif kind == "generic":
            generic.append(email)
        elif kind == "personal" and email in (chosen.get("emails") or []):
            personal.append(email)
    email = ""
    email_type = ""
    if personal:
        email = personal[0]
        email_type = "personal"
    elif role:
        email = role[0]
        email_type = "role"
    elif generic:
        email = generic[0]
        email_type = "generic"
    person = PersonHit(
        first_name=first,
        last_name=last,
        full_name=f"{first} {last}".strip(),
        title=apply_synonyms(str(chosen.get("title") or ""), profile.title_synonyms),
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
    if not last.strip():
        person.rejection_reason = "single_name"
        person.name_bank_status = "needs_email" if not email else ""
        return SiteDomainResult(
            domain=domain,
            person=person,
            email_type="",
            bank_only=True,
            page_url=person.page_url,
        )
    if not email:
        person.name_bank_status = "needs_email"
        return SiteDomainResult(
            domain=domain,
            person=person,
            email_type="",
            bank_only=True,
            page_url=person.page_url,
        )
    return SiteDomainResult(
        domain=domain,
        person=person,
        email_type=email_type,
        bank_only=False,
        page_url=person.page_url,
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
    candidates: list[dict[str, Any]] = []
    for row in _cached_pages(root):
        body = str(row.get("body_text") or "")
        page = str(row.get("url") or "")
        if body:
            candidates.extend(parse_html(body, page_url=page))
        for email in row.get("emails") or []:
            if isinstance(email, str) and candidates:
                candidates[-1].setdefault("emails", []).append(email.lower())
    homepage_html = ""
    base = ""
    for scheme in ("https", "http"):
        final, html = _fetch(f"{scheme}://{root}/")
        if html and _same_domain(final or f"{scheme}://{root}/", root):
            homepage_html = html
            base = final or f"{scheme}://{root}/"
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
            if not _same_domain(absolute, root):
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
                if guess not in seen_urls and _same_domain(guess, root):
                    extras.append(guess)
                    seen_urls.add(guess)
                if len(extras) >= MAX_EXTRA_PAGES:
                    break
        for url in extras[:MAX_EXTRA_PAGES]:
            final, html = _fetch(url)
            if html and _same_domain(final or url, root):
                pages.append((final or url, html))
    for page_url, html in pages:
        candidates.extend(parse_html(html, page_url=page_url))
    try:
        return pick_contact(candidates, profile, domain=root, company_name=company_name)
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
