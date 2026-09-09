from people_waterfall.geo import apply_person_geo, parse_city_state
from people_waterfall.people import company_matches, name_from_linkedin_slug


def test_cap_keeps_out_of_geo_at_half_confidence():
    geo = {"states": ["TX"], "person_geo_mode": "cap"}
    decision = apply_person_geo(person_state="TX", geo=geo)
    assert decision.keep is True
    assert decision.confidence == 1.0
    away = apply_person_geo(person_state="NY", geo=geo)
    assert away.keep is True
    assert away.confidence == 0.5


def test_reject_drops_out_of_geo():
    geo = {"states": ["MA", "CT", "RI", "NH", "ME", "VT"], "person_geo_mode": "reject"}
    assert apply_person_geo(person_state="TX", geo=geo).keep is False
    assert apply_person_geo(person_state="MA", geo=geo).keep is True


def test_ignore_skips_check():
    geo = {"states": ["TX"], "person_geo_mode": "ignore"}
    assert apply_person_geo(person_state="AK", geo=geo).keep is True


def test_company_match_first_ten():
    assert company_matches(
        input_company="Austin Commercial",
        returned_company="Austin Commercial LP",
    )
    assert not company_matches(
        input_company="Austin Commercial",
        returned_company="Completely Different Inc",
    )


def test_company_match_domain():
    assert company_matches(
        input_company="Other",
        input_domain="acme.com",
        returned_company="zzz",
        returned_domain="acme.com",
    )


def test_linkedin_slug_needs_two_tokens():
    assert name_from_linkedin_slug("https://www.linkedin.com/in/jane-doe-12345") == (
        "Jane",
        "Doe",
    )
    assert name_from_linkedin_slug("https://www.linkedin.com/in/jane") == ("", "")


def test_parse_city_state():
    city, state = parse_city_state("123 Main St, Dallas, TX 75201")
    assert city == "Dallas"
    assert state == "TX"
