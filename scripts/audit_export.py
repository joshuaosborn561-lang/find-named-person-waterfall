#!/usr/bin/env python3
"""Audit a GetLeads people CSV. Prints counts only; writes JSON for ingest."""

from __future__ import annotations

import csv
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from people_waterfall.geo import apply_person_geo
from people_waterfall.people import company_matches, looks_like_person
from people_waterfall.titles import audit_title

PETERSON = {
    "target_titles": [
        "Owner",
        "President",
        "Principal",
        "Partner",
        "Vice President",
        "Project Manager",
        "Senior Project Manager",
        "Project Executive",
        "Estimator",
        "Chief Estimator",
        "Senior Estimator",
        "Superintendent",
        "General Superintendent",
        "Preconstruction Manager",
        "Director of Preconstruction",
        "Purchasing Manager",
        "Operations Manager",
    ],
    "title_synonyms": {
        "PM": "Project Manager",
        "Super": "Superintendent",
        "Precon": "Preconstruction",
    },
    "title_exclude_regex": "",
    "geo": {"states": ["TX"], "person_geo_mode": "cap"},
}

GOLIATH = {
    "target_titles": [
        "IT Director",
        "Director of IT",
        "Director of Information Technology",
        "Director of Technology",
        "VP of IT",
        "VP of Information Technology",
        "IT Manager",
        "Head of IT",
        "Head of Information Technology",
        "System Administrator",
        "Sysadmin",
        "Systems Administrator",
        "Network Administrator",
        "IT Administrator",
        "IT Admin",
        "Infrastructure Manager",
        "Security Manager",
        "Help Desk Manager",
    ],
    "title_synonyms": {},
    "title_exclude_regex": "CEO|CFO|COO|President",
    "geo": {"person_geo_mode": "ignore"},
}

PROFILES = {"peterson_roof": PETERSON, "goliath": GOLIATH}

COL = {
    "first": ("First Name", "first_name"),
    "last": ("Last Name", "last_name"),
    "title": ("Current Job Title", "Job Title", "job_title"),
    "li": ("Contact LinkedIn URL", "Person LinkedIn", "linkedin_url"),
    "domain": ("Company Domain", "domain"),
    "company": ("Company Name", "company_name"),
    "city": ("Contact City", "city"),
    "state": ("Contact State", "State", "state"),
    "current": ("Currently in Role", "is_current"),
}


def pick(row: dict, keys: tuple[str, ...]) -> str:
    for key in keys:
        if key in row and row[key] not in (None, ""):
            return str(row[key]).strip()
    return ""


def audit_file(path: Path, client_tag: str) -> dict:
    profile = PROFILES[client_tag]
    accepted: list[dict] = []
    banked: list[dict] = []
    stats = {
        "rows": 0,
        "not_person": 0,
        "company_mismatch": 0,
        "geo_reject": 0,
        "geo_capped": 0,
        "title_matched": 0,
        "wrong_title": 0,
        "excluded": 0,
        "former": 0,
        "domains": set(),
        "companies_with_match": set(),
        "c_suite_excluded": 0,
    }
    with path.open(newline="", encoding="utf-8-sig") as fh:
        for raw in csv.DictReader(fh):
            stats["rows"] += 1
            first = pick(raw, COL["first"])
            last = pick(raw, COL["last"])
            title = pick(raw, COL["title"])
            domain = pick(raw, COL["domain"]).lower()
            company = pick(raw, COL["company"])
            city = pick(raw, COL["city"])
            state = pick(raw, COL["state"])
            current = pick(raw, COL["current"]).lower()
            if current in {"false", "0", "no", "former", "past"}:
                stats["former"] += 1
                continue
            if not looks_like_person(first, last):
                stats["not_person"] += 1
                continue
            if not company_matches(
                input_company=company,
                input_domain=domain,
                returned_company=company,
                returned_domain=domain,
            ) and not domain:
                stats["company_mismatch"] += 1
                continue
            # Input company is the vendor company here; domain equality holds
            # when the export is scoped to the queued domains.
            geo = apply_person_geo(
                person_state=state,
                person_city=city,
                geo=profile["geo"],
            )
            if not geo.keep:
                stats["geo_reject"] += 1
                continue
            audit = audit_title(
                title,
                target_titles=profile["target_titles"],
                title_synonyms=profile["title_synonyms"],
                title_exclude_regex=profile["title_exclude_regex"],
            )
            row = {
                "first_name": first,
                "last_name": last,
                "job_title": title,
                "title_match": bool(audit.title_match),
                "title_rank": audit.title_rank,
                "linkedin_url": pick(raw, COL["li"]),
                "domain": domain,
                "company_name": company,
                "source_tier": "getleads",
                "source_confidence": geo.confidence,
                "phone": "",
                "person_city": city,
                "person_state": state,
                "company_match": True,
            }
            stats["domains"].add(domain)
            if geo.confidence < 1:
                stats["geo_capped"] += 1
            if audit.excluded:
                stats["excluded"] += 1
                stats["c_suite_excluded"] += 1
                banked.append(row)
                continue
            if audit.title_match:
                stats["title_matched"] += 1
                stats["companies_with_match"].add(domain or company.lower())
                accepted.append(row)
            else:
                stats["wrong_title"] += 1
                banked.append(row)
    out_dir = Path("/tmp/pw-exports")
    (out_dir / f"{client_tag}_accepted.json").write_text(json.dumps(accepted))
    (out_dir / f"{client_tag}_banked.json").write_text(json.dumps(banked))
    return {
        "client_tag": client_tag,
        "rows": stats["rows"],
        "former_dropped": stats["former"],
        "not_person": stats["not_person"],
        "company_mismatch": stats["company_mismatch"],
        "geo_reject": stats["geo_reject"],
        "geo_capped": stats["geo_capped"],
        "title_matched": stats["title_matched"],
        "wrong_title": stats["wrong_title"],
        "excluded": stats["excluded"],
        "c_suite_excluded": stats["c_suite_excluded"],
        "domains_with_people": len(stats["domains"]),
        "companies_with_title_match": len(stats["companies_with_match"]),
        "accepted_path": str(out_dir / f"{client_tag}_accepted.json"),
        "banked_path": str(out_dir / f"{client_tag}_banked.json"),
    }


def main() -> None:
    path = Path(sys.argv[1])
    tag = sys.argv[2]
    print(json.dumps(audit_file(path, tag), indent=2))


if __name__ == "__main__":
    main()
