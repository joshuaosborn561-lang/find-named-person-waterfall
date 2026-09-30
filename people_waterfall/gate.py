"""Profile gate for one person. No vendor calls.

rejection_reason is one of title, seniority, geo, company.
An empty reason means the person passes.
"""

from __future__ import annotations

from dataclasses import dataclass

from .geo import GeoDecision, apply_person_geo
from .people import PersonHit, company_matches
from .profile import ClientProfile
from .titles import TitleAudit, audit_title

REJECTION_REASONS = ("title", "seniority", "geo", "company")


@dataclass(frozen=True)
class GateResult:
    reason: str
    audit: TitleAudit | None
    confidence: float


def _company_rejected(
    person: PersonHit,
    *,
    company_name: str,
    domain: str,
) -> bool:
    if company_matches(
        input_company=company_name,
        input_domain=domain,
        returned_company=person.company_name,
        returned_domain=person.domain,
    ):
        return False
    if domain and person.domain and person.domain != domain:
        return True
    if company_name and person.company_name:
        return True
    if domain and not person.domain and not person.company_name:
        person.domain = domain
        person.company_name = company_name
        return False
    return not company_matches(
        input_company=company_name,
        input_domain=domain,
        returned_company=person.company_name or company_name,
        returned_domain=person.domain or domain,
    )


def gate_person(
    person: PersonHit,
    profile: ClientProfile,
    *,
    company_name: str,
    domain: str,
    use_fallback: bool = False,
) -> GateResult:
    if _company_rejected(person, company_name=company_name, domain=domain):
        return GateResult("company", None, person.source_confidence)
    geo: GeoDecision = apply_person_geo(
        person_state=person.person_state,
        person_city=person.person_city,
        company_state="",
        geo=profile.geo,
        default_confidence=person.source_confidence,
    )
    if not geo.keep:
        return GateResult("geo", None, geo.confidence)
    audit = audit_title(
        person.title,
        target_titles=profile.target_titles,
        title_synonyms=profile.title_synonyms,
        title_exclude_regex=profile.title_exclude_regex,
        seniority_floor=profile.seniority_floor,
        fallback_titles=profile.fallback_titles,
        use_fallback=use_fallback,
    )
    if audit.title_match:
        return GateResult("", audit, geo.confidence)
    if audit.below_floor:
        return GateResult("seniority", audit, geo.confidence)
    return GateResult("title", audit, geo.confidence)
