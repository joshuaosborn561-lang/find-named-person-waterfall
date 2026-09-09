"""Title audit. Synonyms, targets, and exclude regex come from the profile."""

from __future__ import annotations

import re
from dataclasses import dataclass

_SHORT = re.compile(r"^[a-z]{1,3}$")
_NON_ALNUM = re.compile(r"[^a-z0-9\s]")
_WS = re.compile(r"\s+")

# Linguistic expansions only — not industry lists.
_GENERIC_ALIASES: dict[str, tuple[str, ...]] = {
    "vice president": ("vice president", "vp"),
    "vp": ("vp", "vice president"),
    "ceo": ("ceo", "chief executive"),
    "coo": ("coo", "chief operating"),
    "cfo": ("cfo", "chief financial"),
    "cto": ("cto", "chief technology"),
    "cio": ("cio", "chief information"),
    "ciso": ("ciso", "chief information security"),
}

SENIORITY_RANKS: dict[str, int] = {
    "intern": 10,
    "assistant": 20,
    "coordinator": 30,
    "specialist": 40,
    "associate": 45,
    "analyst": 50,
    "engineer": 55,
    "administrator": 60,
    "admin": 60,
    "lead": 70,
    "supervisor": 75,
    "manager": 80,
    "senior manager": 85,
    "director": 90,
    "head": 92,
    "vp": 95,
    "vice president": 95,
    "principal": 96,
    "partner": 96,
    "president": 98,
    "owner": 99,
    "founder": 99,
    "c-suite": 100,
    "c suite": 100,
    "chief": 100,
}


def normalize_title(title: str) -> str:
    t = (title or "").lower()
    t = t.replace("&", " and ")
    t = _NON_ALNUM.sub(" ", t)
    t = _WS.sub(" ", t).strip()
    t = t.replace("vice president", "vp")
    t = t.replace("chief executive officer", "ceo")
    t = t.replace("chief executive", "ceo")
    t = t.replace("chief operating officer", "coo")
    t = t.replace("chief operating", "coo")
    t = t.replace("chief financial officer", "cfo")
    t = t.replace("chief financial", "cfo")
    t = t.replace("chief technology officer", "cto")
    t = t.replace("chief technology", "cto")
    t = t.replace("chief information security officer", "ciso")
    t = t.replace("chief information officer", "cio")
    return t


def apply_synonyms(title: str, synonyms: dict[str, str]) -> str:
    """Replace whole-token synonyms from the profile, longest key first."""
    if not title:
        return ""
    raw = title.strip()
    if not synonyms:
        return raw
    items = sorted(synonyms.items(), key=lambda kv: len(kv[0]), reverse=True)
    out = raw
    for src, dest in items:
        src_s = (src or "").strip()
        dest_s = (dest or "").strip()
        if not src_s or not dest_s:
            continue
        out = re.sub(rf"\b{re.escape(src_s)}\b", dest_s, out, flags=re.I)
    return out


def _aliases(target: str, synonyms: dict[str, str]) -> tuple[str, ...]:
    expanded = apply_synonyms(target, synonyms)
    key = normalize_title(expanded)
    extra = _GENERIC_ALIASES.get(key, ())
    rev = tuple(
        src for src, dest in synonyms.items() if normalize_title(dest) == key
    )
    return (target, expanded, *extra, *rev)


def title_matches(title: str, target: str, synonyms: dict[str, str] | None = None) -> bool:
    synonyms = synonyms or {}
    nt = normalize_title(apply_synonyms(title, synonyms))
    if not nt:
        return False
    for alias in _aliases(target, synonyms):
        na = normalize_title(apply_synonyms(alias, synonyms))
        if not na:
            continue
        if _SHORT.match(na) or na in {"gm", "vp", "ceo", "coo", "cfo", "cto", "cio"}:
            if re.search(rf"\b{re.escape(na)}\b", nt):
                return True
            continue
        if na in nt:
            return True
    return False


def excluded_by_regex(title: str, pattern: str) -> bool:
    raw = (pattern or "").strip()
    if not raw:
        return False
    # Simple alternations get word boundaries so "President" does not eat "VP".
    if "|" in raw and "\\" not in raw and "(" not in raw:
        parts = [p.strip() for p in raw.split("|") if p.strip()]
        raw = "|".join(rf"\b{re.escape(p)}\b" for p in parts)
    try:
        return bool(re.search(raw, title or "", re.I))
    except re.error:
        return False


def seniority_value(title: str) -> int:
    nt = normalize_title(title)
    if not nt:
        return 0
    best = 0
    for token, rank in SENIORITY_RANKS.items():
        if token in nt:
            best = max(best, rank)
    return best


def meets_seniority_floor(title: str, floor: str) -> bool:
    floor_s = (floor or "").strip()
    if not floor_s:
        return True
    needed = SENIORITY_RANKS.get(normalize_title(floor_s), 0)
    if needed <= 0:
        return True
    return seniority_value(title) >= needed


@dataclass(frozen=True)
class TitleAudit:
    title_match: bool
    title_rank: int | None
    normalized_title: str
    excluded: bool
    below_floor: bool


def audit_title(
    job_title: str,
    *,
    target_titles: list[str],
    title_synonyms: dict[str, str] | None = None,
    title_exclude_regex: str = "",
    seniority_floor: str = "",
    fallback_titles: list[str] | None = None,
    use_fallback: bool = False,
) -> TitleAudit:
    """Match against target_titles (or fallback_titles on a second pass).

    title_rank is the 0-based index in target_titles. Fallback hits keep
    that index when the fallback title also appears in target_titles;
    otherwise rank is len(target_titles) + fallback index.
    """
    synonyms = title_synonyms or {}
    normalized = apply_synonyms(job_title, synonyms)
    # Exclude against the normalized title only. Raw "Vice President of IT"
    # contains the word President; normalize_title rewrites that to "vp".
    if excluded_by_regex(normalize_title(normalized), title_exclude_regex):
        return TitleAudit(False, None, normalized, True, False)
    if not meets_seniority_floor(normalized, seniority_floor):
        return TitleAudit(False, None, normalized, False, True)

    pool = list(fallback_titles or []) if use_fallback else list(target_titles)
    for i, target in enumerate(pool):
        if title_matches(job_title, target, synonyms) or title_matches(
            normalized, target, synonyms
        ):
            if use_fallback:
                try:
                    rank = list(target_titles).index(target)
                except ValueError:
                    rank = len(target_titles) + i
            else:
                rank = i
            return TitleAudit(True, rank, normalized, False, False)
    return TitleAudit(False, None, normalized, False, False)
