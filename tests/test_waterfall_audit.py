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
    assert bank == []


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
