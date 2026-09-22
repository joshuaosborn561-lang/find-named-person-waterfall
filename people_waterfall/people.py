"""Person model, company-match, name normalization, current-employment gate."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

ENTITY_MARKERS = re.compile(
    r"\b(llc|inc\.?|corp\.?|ltd\.?|company|group|construction|builders?"
    r"|contractors?|services|partners?|development|corporation)\b",
    re.I,
)
TITLE_ONLY = re.compile(
    r"^(project\s+manager|manager|president|ceo|owner|director|estimator|"
    r"superintendent|administrator|assistant|coordinator|engineer|"
    r"general\s+manager|gm)$",
    re.I,
)


def split_name(full: str) -> tuple[str, str]:
    parts = [p for p in (full or "").strip().split() if p]
    if not parts:
        return "", ""
    if len(parts) == 1:
        return parts[0], ""
    return parts[0], parts[-1]


def normalize_name(first: str, last: str = "") -> str:
    first_n = re.sub(r"[^a-z]", "", (first or "").lower())
    last_n = re.sub(r"[^a-z]", "", (last or "").lower())
    return f"{first_n} {last_n}".strip()


def looks_like_person(first: str, last: str = "") -> bool:
    first = (first or "").strip()
    last = (last or "").strip()
    name = f"{first} {last}".strip()
    if len(name) < 3:
        return False
    if TITLE_ONLY.match(name):
        return False
    if ENTITY_MARKERS.search(name):
        return False
    if first.lower() in {"project", "general", "construction", "the", "our"}:
        return False
    if not re.search(r"[A-Za-z]{2,}", first):
        return False
    last_alpha = re.sub(r"[^A-Za-z]", "", last)
    if last and len(last_alpha) < 2:
        return False
    return True


def company_prefix(name: str, n: int = 10) -> str:
    raw = re.sub(r"[^a-z0-9]", "", (name or "").lower())
    return raw[:n]


def company_matches(
    *,
    input_company: str,
    input_domain: str = "",
    returned_company: str = "",
    returned_domain: str = "",
) -> bool:
    """Vendor company contains the first ten alnum chars of the input, or domains equal."""
    in_dom = (input_domain or "").strip().lower().lstrip(".")
    out_dom = (returned_domain or "").strip().lower().lstrip(".")
    if in_dom and out_dom and in_dom == out_dom:
        return True
    prefix = company_prefix(input_company)
    if not prefix:
        return bool(in_dom and out_dom and in_dom == out_dom)
    hay = re.sub(r"[^a-z0-9]", "", (returned_company or "").lower())
    if not hay:
        return False
    return prefix in hay


def company_name_contains(returned_company: str, queried_company: str) -> bool:
    """True when personalInfo.companyName contains the queried company name."""
    hay = " ".join((returned_company or "").lower().split())
    needle = " ".join((queried_company or "").lower().split())
    if not hay or not needle:
        return False
    if needle in hay:
        return True
    hay_c = re.sub(r"[^a-z0-9]", "", hay)
    needle_c = re.sub(r"[^a-z0-9]", "", needle)
    return bool(needle_c) and needle_c in hay_c


def is_current_employment(row: dict[str, Any]) -> bool | None:
    """Prefer current roles. None = vendor did not expose currency."""
    for key in (
        "is_current",
        "current",
        "isCurrent",
        "current_position",
        "is_current_position",
    ):
        if key in row:
            val = row.get(key)
            if isinstance(val, bool):
                return val
            if isinstance(val, str) and val.strip().lower() in {"true", "1", "yes", "current"}:
                return True
            if isinstance(val, str) and val.strip().lower() in {"false", "0", "no", "former", "past"}:
                return False
    status = str(row.get("employment_status") or row.get("job_status") or "").lower()
    if status in {"current", "present", "active"}:
        return True
    if status in {"former", "past", "inactive", "retired"}:
        return False
    exp = row.get("experience") or row.get("current_experience")
    if isinstance(exp, dict):
        latest = exp.get("latest") or exp.get("current") or exp
        if isinstance(latest, dict):
            end = str(latest.get("end_date") or latest.get("endDate") or "").lower()
            if end in {"", "present", "current", "now", "none", "null"}:
                return True
            if end:
                return False
    return None


def name_from_linkedin_slug(url: str) -> tuple[str, str]:
    """Parse first/last from /in/jane-doe-123. Drop under two tokens."""
    raw = (url or "").strip()
    if not raw:
        return "", ""
    m = re.search(r"linkedin\.com/in/([^/?#]+)", raw, re.I)
    if not m:
        return "", ""
    slug = re.sub(r"-+$", "", m.group(1))
    slug = re.sub(r"-\d{4,}$", "", slug)
    tokens = [t for t in re.split(r"[-_]+", slug) if t and not t.isdigit()]
    tokens = [t for t in tokens if not re.fullmatch(r"[a-z0-9]{8,}", t)]
    if len(tokens) < 2:
        return "", ""
    first = tokens[0].replace("%20", " ").strip()
    last = tokens[1].replace("%20", " ").strip()
    if not first or not last:
        return "", ""
    return first.title(), last.title()


@dataclass
class PersonHit:
    first_name: str = ""
    last_name: str = ""
    full_name: str = ""
    title: str = ""
    linkedin_url: str = ""
    phone: str = ""
    domain: str = ""
    company_name: str = ""
    person_city: str = ""
    person_state: str = ""
    source_tier: str = ""
    source_confidence: float = 1.0
    is_current: bool | None = None
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def name(self) -> str:
        if self.full_name:
            return self.full_name
        return f"{self.first_name} {self.last_name}".strip()

    @property
    def name_key(self) -> str:
        return normalize_name(self.first_name, self.last_name)


def person_from_row(row: dict[str, Any], source_tier: str) -> PersonHit | None:
    first = str(row.get("first_name") or row.get("firstName") or "").strip()
    last = str(row.get("last_name") or row.get("lastName") or "").strip()
    full = str(row.get("full_name") or row.get("fullName") or row.get("name") or "").strip()
    if not first and full:
        first, last = split_name(full)
    if not (first or full):
        li = str(row.get("linkedin_url") or row.get("linkedin") or "")
        first, last = name_from_linkedin_slug(li)
        if first:
            full = f"{first} {last}".strip()
    if not (first or full):
        return None
    loc = row.get("location") if isinstance(row.get("location"), dict) else {}
    return PersonHit(
        first_name=first,
        last_name=last,
        full_name=full or f"{first} {last}".strip(),
        title=str(
            row.get("title")
            or row.get("job_title")
            or row.get("jobTitle")
            or row.get("headline")
            or ""
        ),
        linkedin_url=str(
            row.get("linkedin_url")
            or row.get("linkedin")
            or row.get("profile_url")
            or row.get("profileUrl")
            or ""
        ),
        phone=str(
            row.get("phone") or row.get("mobile") or row.get("cellphone") or ""
        ).strip(),
        domain=str(row.get("domain") or row.get("company_domain") or "").strip().lower(),
        company_name=str(
            row.get("company_name")
            or row.get("companyName")
            or row.get("company")
            or ""
        ),
        person_city=str(
            row.get("person_city")
            or row.get("city")
            or row.get("contact_city")
            or loc.get("city")
            or ""
        ),
        person_state=str(
            row.get("person_state")
            or row.get("state")
            or row.get("contact_state")
            or loc.get("state")
            or loc.get("region")
            or ""
        ),
        source_tier=source_tier,
        is_current=is_current_employment(row),
        raw=row,
    )
