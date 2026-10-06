"""site_staff quality gates: same-site, person names, titles, email typing."""

from __future__ import annotations

import html
import re
from dataclasses import dataclass
from urllib.parse import urlparse

from .titles import apply_synonyms, normalize_title

MULTI_TLDS = frozenset(
    {
        "ac.uk",
        "co.uk",
        "gov.uk",
        "org.uk",
        "com.au",
        "net.au",
        "org.au",
        "co.nz",
        "org.nz",
        "co.za",
        "com.br",
        "com.mx",
    }
)

NAME_STOPLIST = frozenset(
    {
        "click",
        "member",
        "portal",
        "join",
        "welcome",
        "church",
        "history",
        "saturday",
        "sunday",
        "vigil",
        "mass",
        "service",
        "worship",
        "bible",
        "study",
        "full",
        "time",
        "about",
        "contact",
        "meet",
        "our",
        "the",
        "message",
        "from",
        "staff",
        "leadership",
        "ministry",
        "ministries",
        "schedule",
        "events",
        "giving",
        "donate",
        "home",
        "menu",
        "a.m.",
        "a.m",
        "am",
        "p.m.",
        "p.m",
        "pm",
        "in",
        "this",
        "section",
        "annual",
        "reports",
        "email",
        "when",
        "what",
        "we",
        "believe",
        "next",
        "steps",
        "mission",
        "statement",
        "process",
        "application",
        "speaker",
        "trustees",
        "board",
        "directors",
        "new",
        "life",
        "god",
        "moves",
        "ordination",
        "saint",
        "st",
        "st.",
        "january",
        "february",
        "march",
        "april",
        "june",
        "july",
        "august",
        "september",
        "october",
        "november",
        "december",
        "first",
        "second",
        "third",
        "fourth",
        "yahrzeit",
        "siyum",
    }
)

HONORIFICS: tuple[tuple[str, str], ...] = (
    ("monsignor", "Msgr."),
    ("msgr.", "Msgr."),
    ("msgr", "Msgr."),
    ("reverend", "Rev."),
    ("rev.", "Rev."),
    ("rev", "Rev."),
    ("pastors", "Pastor"),
    ("pastor", "Pastor"),
    ("father", "Father"),
    ("fr.", "Fr."),
    ("fr", "Fr."),
    ("rabbi", "Rabbi"),
    ("cantor", "Cantor"),
    ("deacon", "Deacon"),
    ("bishop", "Bishop"),
    ("archpriest", "Father"),
    ("archimandrite", "Father"),
    ("dr.", "Dr."),
    ("dr", "Dr."),
)

HONORIFIC_AS_TITLE = {
    "Pastor": "Pastor",
    "Rev.": "Pastor",
    "Father": "Pastor",
    "Fr.": "Pastor",
    "Rabbi": "Rabbi",
    "Cantor": "Cantor",
    "Deacon": "Deacon",
    "Bishop": "Bishop",
    "Msgr.": "Pastor",
}

TITLE_REJECT = frozenset(
    {
        "study",
        "class",
        "service",
        "schedule",
        "message",
        "meet",
        "welcome",
        "portal",
        "join",
        "speaker",
        "email",
        "application",
        "reports",
        "section",
        "yahrzeit",
        "siyum",
    }
)

DEFAULT_EXCLUDE_TITLES = (
    "music",
    "youth",
    "children",
    "worship",
    "preschool",
    "religious education",
    "treasurer",
    "organist",
    "choir",
    "sexton",
    "custodian",
)

ROLE_LOCALS = frozenset(
    {
        "pastor",
        "rabbi",
        "rector",
        "father",
        "reverend",
        "senior",
        "executive",
        "admin",
    }
)

NAME_ROLE_WORDS = frozenset(
    {
        "pastor",
        "pastors",
        "rabbi",
        "rector",
        "minister",
        "priest",
        "director",
        "administrator",
        "admin",
        "president",
        "trustee",
        "elder",
        "chair",
        "treasurer",
        "organist",
        "sexton",
        "custodian",
        "deacon",
        "bishop",
        "cantor",
        "father",
        "reverend",
        "senior",
        "executive",
        "youth",
        "associate",
        "assistant",
        "intern",
        "coordinator",
        "manager",
        "lead",
        "clergy",
        "vicar",
        "monsignor",
        "msgr",
        "rev",
        "fr",
        "dr",
        "instructor",
        "training",
    }
)

_TOKEN_OK = re.compile(
    r"^[A-ZÀ-Ý](?:[A-Za-zÀ-ÿ''’\-]*|[.])\.?$"
)
_HAS_DIGIT = re.compile(r"\d")
_BAD_PUNCT = re.compile(r"[^A-Za-zÀ-ÿ.''’\-\s]")
_WORD = re.compile(r"\b[^\s]+\b")


def host_of(url: str) -> str:
    raw = (url or "").strip()
    if "@" in raw and "://" not in raw:
        raw = raw.split("@", 1)[-1]
    host = (urlparse(raw if "://" in raw else f"https://{raw}").hostname or "").lower()
    host = host.split(":")[0]
    if host.startswith("www."):
        host = host[4:]
    return host


def registrable_domain(value: str) -> str:
    host = host_of(value)
    if not host:
        host = (value or "").strip().lower().lstrip(".")
        if host.startswith("www."):
            host = host[4:]
        host = host.split("/")[0].split(":")[0]
    parts = [p for p in host.split(".") if p]
    if len(parts) < 2:
        return host
    last2 = ".".join(parts[-2:])
    if last2 in MULTI_TLDS and len(parts) >= 3:
        return ".".join(parts[-3:])
    return last2


def same_site(url: str, domain: str) -> bool:
    left = registrable_domain(url)
    right = registrable_domain(domain)
    return bool(left and right and left == right)


def email_allowed(email: str, allowed_domains: set[str]) -> bool:
    if "@" not in (email or ""):
        return False
    host = email.rsplit("@", 1)[-1]
    root = registrable_domain(host)
    allowed = {registrable_domain(item) for item in allowed_domains if item}
    return bool(root) and root in allowed


def _norm_token(token: str) -> str:
    return (token or "").strip().strip(".,;:()[]\"'").lower()


def _is_honorific(token: str) -> str:
    key = _norm_token(token).rstrip(".")
    for raw, label in HONORIFICS:
        if key == raw.rstrip(".") or _norm_token(token) == raw:
            return label
    return ""


def _token_capitalized(token: str) -> bool:
    piece = (token or "").strip().strip(",;")
    if not piece or _HAS_DIGIT.search(piece):
        return False
    if _BAD_PUNCT.search(piece.replace(".", "").replace("-", "").replace("'", "").replace("’", "")):
        return False
    return bool(_TOKEN_OK.match(piece))


def _name_tokens_ok(tokens: list[str]) -> bool:
    if not (2 <= len(tokens) <= 4):
        return False
    for token in tokens:
        key = _norm_token(token).rstrip(".")
        if key in NAME_STOPLIST or key in NAME_ROLE_WORDS:
            return False
        if not _token_capitalized(token):
            return False
    return True


@dataclass(frozen=True)
class ParsedPerson:
    first_name: str
    last_name: str
    honorific: str = ""
    title_hint: str = ""

    @property
    def tokens(self) -> list[str]:
        return [t for t in (self.first_name, self.last_name) if t]


def name_is_valid(first: str, last: str, middle: str = "") -> bool:
    tokens = [t for t in (first, middle, last) if (t or "").strip()]
    if last and " " in last.strip():
        tokens = [first, *last.split(), *([middle] if middle else [])]
        tokens = [t for t in tokens if t]
    return _name_tokens_ok(tokens)


def _given_name_only(tokens: list[str]) -> ParsedPerson | None:
    """First name plus honorific, used when a couple shares a last name."""
    clean = [t.strip(" ,;:") for t in tokens if t and t.strip(" ,;:")]
    honor = ""
    while clean:
        label = _is_honorific(clean[0])
        if not label:
            break
        honor = honor or label
        clean = clean[1:]
    if len(clean) != 1:
        return None
    if _norm_token(clean[0]) in NAME_STOPLIST or not _token_capitalized(clean[0]):
        return None
    return ParsedPerson(
        first_name=clean[0],
        last_name="",
        honorific=honor,
        title_hint=HONORIFIC_AS_TITLE.get(honor, ""),
    )


def _person_from_tokens(tokens: list[str], honorific: str = "") -> ParsedPerson | None:
    clean = [t.strip(" ,;:") for t in tokens if t and t.strip(" ,;:")]
    honor = honorific
    while clean:
        label = _is_honorific(clean[0])
        if not label:
            break
        honor = honor or label
        clean = clean[1:]
    if not _name_tokens_ok(clean):
        return None
    first = clean[0]
    last = clean[-1]
    if len(clean) == 3:
        last = " ".join(clean[1:])
        if not name_is_valid(first, last):
            last = clean[-1]
    elif len(clean) == 4:
        last = " ".join(clean[1:])
        if not name_is_valid(first, last):
            return None
    return ParsedPerson(
        first_name=first,
        last_name=last,
        honorific=honor,
        title_hint=HONORIFIC_AS_TITLE.get(honor, ""),
    )


def parse_name_line(text: str) -> list[ParsedPerson]:
    raw = re.sub(r"\s+", " ", html.unescape(text or "").strip())
    raw = raw.strip(" -|•\t")
    if not raw or len(raw) > 80:
        return []
    if _HAS_DIGIT.search(raw):
        return []
    couple = re.split(r"\s+(?:&|and)\s+", raw, maxsplit=1)
    if len(couple) == 2:
        left = parse_name_line(couple[0])
        right = parse_name_line(couple[1])
        if not left:
            partial = _given_name_only(couple[0].split())
            if partial:
                left = [partial]
        if left and right:
            if not left[0].last_name and right[0].last_name:
                left = [
                    ParsedPerson(
                        first_name=left[0].first_name,
                        last_name=right[0].last_name,
                        honorific=left[0].honorific,
                        title_hint=left[0].title_hint or right[0].title_hint,
                    )
                ]
            if name_is_valid(left[0].first_name, left[0].last_name) and name_is_valid(
                right[0].first_name, right[0].last_name
            ):
                return [left[0], right[0]]
        return []
    parts = [p.strip() for p in re.split(r"\s*[,–—|-]\s*", raw) if p.strip()]
    if len(parts) >= 2:
        people = _person_from_tokens(parts[0].split())
        if people:
            return [people]
    tokens = raw.split()
    person = _person_from_tokens(tokens)
    return [person] if person else []


def title_is_usable(title: str) -> bool:
    raw = html.unescape((title or "").strip())
    if not raw or len(raw) > 80:
        return False
    if "@" in raw or _HAS_DIGIT.search(raw):
        return False
    if raw.count(" ") >= 8:
        return False
    if parse_name_line(raw):
        return False
    norm = normalize_title(raw)
    if not norm:
        return False
    for word in TITLE_REJECT:
        if re.search(rf"\b{re.escape(word)}\b", norm):
            return False
    words = [w for w in norm.split() if w]
    if words and all(w in NAME_STOPLIST or w in TITLE_REJECT for w in words):
        return False
    if words and words[0] in {"our", "the", "a", "an", "this"}:
        return False
    return True


def title_excluded(title: str, extra: list[str] | None = None) -> bool:
    norm = normalize_title(title)
    if not norm:
        return False
    pool = list(DEFAULT_EXCLUDE_TITLES)
    for item in extra or []:
        if item:
            pool.append(item)
    for raw in pool:
        cand = normalize_title(raw)
        if cand and re.search(rf"\b{re.escape(cand)}\b", norm):
            return True
    return False


def title_phrase_rank(title: str, targets: list[str], synonyms: dict[str, str] | None = None) -> int | None:
    synonyms = synonyms or {}
    if not title_is_usable(title):
        return None
    if title_excluded(title):
        return None
    norm = normalize_title(apply_synonyms(title, synonyms))
    best: int | None = None
    for index, raw in enumerate(targets):
        cand = normalize_title(apply_synonyms(raw, synonyms))
        if not cand:
            continue
        if re.search(rf"\b{re.escape(cand)}\b", norm):
            if best is None or index < best:
                best = index
    return best


def local_matches(local: str, first: str, last: str, honorific: str = "") -> bool:
    loc = re.sub(r"[^a-z0-9]", "", (local or "").lower())
    first_n = re.sub(r"[^a-z]", "", (first or "").lower())
    last_n = re.sub(r"[^a-z]", "", (last or "").lower())
    if not loc:
        return False
    cands: set[str] = set()
    if first_n:
        cands.add(first_n)
    if last_n:
        cands.add(last_n)
    if first_n and last_n:
        cands.update(
            {
                first_n + last_n,
                first_n[:1] + last_n,
                first_n + last_n[:1],
            }
        )
    hon = re.sub(r"[^a-z]", "", (honorific or "").lower())
    if hon and last_n:
        cands.add(hon + last_n)
    if hon and first_n:
        cands.add(hon + first_n)
    for role in ROLE_LOCALS:
        if last_n:
            cands.add(role + last_n)
        if first_n:
            cands.add(role + first_n)
    return loc in cands


def classify_email(
    email: str,
    *,
    first: str = "",
    last: str = "",
    honorific: str = "",
) -> str:
    local = (email or "").split("@", 1)[0].lower()
    compact = re.sub(r"[^a-z0-9]", "", local)
    if local_matches(local, first, last, honorific):
        return "personal"
    if compact in ROLE_LOCALS:
        return "role"
    for role in ROLE_LOCALS:
        if compact == role or compact.startswith(role) and len(compact) <= len(role) + 2:
            return "role"
    return "generic"


def should_pause_for_site_staff_quality(
    domains_seen: int, valid_written: int, written: int, *, threshold: float = 0.8, n: int = 50
) -> bool:
    if domains_seen < n or written <= 0:
        return False
    return (valid_written / written) < threshold


def bankable_person(first: str, last: str, title: str) -> bool:
    return name_is_valid(first, last) and title_is_usable(title)
