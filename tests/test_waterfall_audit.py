from people_waterfall.people import PersonHit
from people_waterfall.profile import parse_profile
from people_waterfall.waterfall import _audit_people


def _profile(**kwargs):
    doc = {
        "target_titles": ["Project Manager", "IT Manager"],
        "title_synonyms": {"PM": "Project Manager"},
        "title_exclude_regex": "CEO|CFO|COO|President",
        "geo": {"states": ["TX"], "person_geo_mode": "cap"},
        **kwargs,
    }
    return parse_profile("peterson_roof", doc)


def test_wrong_title_goes_to_bank_not_accepted():
    profile = _profile()
    people = [
        PersonHit(
            first_name="Ada",
            last_name="Lovelace",
            title="Marketing Coordinator",
            company_name="Acme Roofing",
            domain="acme.com",
        )
    ]
    accepted, bank = _audit_people(
        people, profile=profile, company_name="Acme Roofing", domain="acme.com", use_fallback=False
    )
    assert accepted == []
    assert len(bank) == 1


def test_company_mismatch_rejected():
    profile = _profile()
    people = [
        PersonHit(
            first_name="Ada",
            last_name="Lovelace",
            title="Project Manager",
            company_name="Totally Different LLC",
            domain="other.com",
        )
    ]
    accepted, bank = _audit_people(
        people, profile=profile, company_name="Acme Roofing", domain="acme.com", use_fallback=False
    )
    assert accepted == []
    assert len(bank) == 1
    assert bank[0].rejection_reason == "company"


def test_literal_target_passes_seniority_floor():
    profile = _profile(
        target_titles=["Lead Pastor", "Pastor", "Dentist"],
        seniority_floor="manager",
    )
    people = [
        PersonHit(
            first_name="Ada",
            last_name="Lovelace",
            title=title,
            company_name="Acme Roofing",
            domain="acme.com",
        )
        for title in ("Lead Pastor", "Pastor", "Dentist")
    ]
    accepted, bank = _audit_people(
        people, profile=profile, company_name="Acme Roofing", domain="acme.com", use_fallback=False
    )
    assert [row[0].title for row in accepted] == ["Lead Pastor", "Pastor", "Dentist"]
    assert bank == []


def test_geo_reject_is_banked_but_blank_location_passes():
    profile = _profile(geo={"states": ["TX"], "person_geo_mode": "reject"})
    away = PersonHit(
        first_name="Ada",
        last_name="Lovelace",
        title="Project Manager",
        company_name="Acme Roofing",
        domain="acme.com",
        person_state="NY",
        person_city="New York",
    )
    blank = PersonHit(
        first_name="Grace",
        last_name="Hopper",
        title="Project Manager",
        company_name="Acme Roofing",
        domain="acme.com",
    )
    accepted, bank = _audit_people(
        [away, blank],
        profile=profile,
        company_name="Acme Roofing",
        domain="acme.com",
        use_fallback=False,
    )
    assert [person.title for person, _audit, _conf in accepted] == ["Project Manager"]
    assert [person.rejection_reason for person in bank] == ["geo"]


def test_title_match_accepted():
    profile = _profile()
    people = [
        PersonHit(
            first_name="Ada",
            last_name="Lovelace",
            title="Senior PM",
            company_name="Acme Roofing Inc",
            domain="acme.com",
        )
    ]
    accepted, bank = _audit_people(
        people, profile=profile, company_name="Acme Roofing", domain="acme.com", use_fallback=False
    )
    assert len(accepted) == 1
    assert accepted[0][1].title_match is True
    assert bank == []
