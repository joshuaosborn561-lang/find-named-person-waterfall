"""Person geography vs the profile geo object. Modes: reject | cap | ignore."""

from __future__ import annotations

import re
from dataclasses import dataclass

US_STATE_ABBR = {
    "alabama": "AL",
    "alaska": "AK",
    "arizona": "AZ",
    "arkansas": "AR",
    "california": "CA",
    "colorado": "CO",
    "connecticut": "CT",
    "delaware": "DE",
    "florida": "FL",
    "georgia": "GA",
    "hawaii": "HI",
    "idaho": "ID",
    "illinois": "IL",
    "indiana": "IN",
    "iowa": "IA",
    "kansas": "KS",
    "kentucky": "KY",
    "louisiana": "LA",
    "maine": "ME",
    "maryland": "MD",
    "massachusetts": "MA",
    "michigan": "MI",
    "minnesota": "MN",
    "mississippi": "MS",
    "missouri": "MO",
    "montana": "MT",
    "nebraska": "NE",
    "nevada": "NV",
    "new hampshire": "NH",
    "new jersey": "NJ",
    "new mexico": "NM",
    "new york": "NY",
    "north carolina": "NC",
    "north dakota": "ND",
    "ohio": "OH",
    "oklahoma": "OK",
    "oregon": "OR",
    "pennsylvania": "PA",
    "rhode island": "RI",
    "south carolina": "SC",
    "south dakota": "SD",
    "tennessee": "TN",
    "texas": "TX",
    "utah": "UT",
    "vermont": "VT",
    "virginia": "VA",
    "washington": "WA",
    "west virginia": "WV",
    "wisconsin": "WI",
    "wyoming": "WY",
    "district of columbia": "DC",
}

NEW_ENGLAND = frozenset({"CT", "MA", "ME", "NH", "RI", "VT"})


def normalize_state(value: str) -> str:
    raw = (value or "").strip()
    if not raw:
        return ""
    if len(raw) == 2 and raw.isalpha():
        return raw.upper()
    return US_STATE_ABBR.get(raw.lower(), raw[:2].upper() if len(raw) >= 2 else raw.upper())


def allowed_states(geo: dict | None) -> set[str]:
    geo = geo or {}
    out: set[str] = set()
    for item in geo.get("states") or []:
        n = normalize_state(str(item))
        if n:
            out.add(n)
    return out


@dataclass(frozen=True)
class GeoDecision:
    keep: bool
    confidence: float
    in_geo: bool | None


def person_in_geo(
    *,
    person_state: str,
    person_city: str = "",
    company_state: str = "",
    geo: dict | None = None,
) -> bool | None:
    """None = no signal. True/False when we can decide."""
    geo = geo or {}
    states = allowed_states(geo)
    person = normalize_state(person_state)
    if not person:
        # A Houston LinkedIn location is the usual signal. No person state
        # is not proof either way.
        return None
    if not states:
        return None
    return person in states


def apply_person_geo(
    *,
    person_state: str,
    person_city: str = "",
    company_state: str = "",
    geo: dict | None = None,
    default_confidence: float = 1.0,
) -> GeoDecision:
    geo = geo or {}
    mode = str(geo.get("person_geo_mode") or "ignore").strip().lower()
    if mode not in {"reject", "cap", "ignore"}:
        mode = "ignore"
    if mode == "ignore":
        return GeoDecision(True, default_confidence, None)
    signal = person_in_geo(
        person_state=person_state,
        person_city=person_city,
        company_state=company_state,
        geo=geo,
    )
    if signal is None or signal is True:
        return GeoDecision(True, default_confidence, signal)
    if mode == "reject":
        return GeoDecision(False, default_confidence, False)
    return GeoDecision(True, min(default_confidence, 0.5), False)


_CITY_STATE = re.compile(
    r"(?P<city>[A-Za-z .'-]+),\s*(?P<state>[A-Za-z]{2}|[A-Za-z ]+)(?:\s+\d{5})?$"
)


def parse_city_state(address: str) -> tuple[str, str]:
    raw = (address or "").strip()
    if not raw:
        return "", ""
    m = _CITY_STATE.search(raw)
    if not m:
        return "", ""
    return m.group("city").strip(), normalize_state(m.group("state"))
