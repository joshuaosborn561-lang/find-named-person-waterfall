from people_waterfall.pricing import LiveRates, compute_tier_order, select_tiers
from people_waterfall.profile import parse_profile
from people_waterfall.vendors.serp import SerpClient, build_query
from people_waterfall.waterfall import _lane_order


PROFILE = parse_profile(
    "demo",
    {
        "target_titles": ["Owner", "President", "General Manager", "Project Manager"],
        "title_synonyms": {"GM": "General Manager"},
    },
)


def test_build_query_uses_all_target_titles():
    query = build_query("Acme Roofing", PROFILE.target_titles)
    assert query.startswith('site:linkedin.com/in "Acme Roofing"')
    assert '("Owner" OR "President" OR "General Manager" OR "Project Manager")' in query


def test_parse_people_requires_personal_info_company_and_title():
    client = SerpClient(token="")
    items = [
        {
            "organicResults": [
                {
                    "url": "https://www.linkedin.com/in/jane-doe-1",
                    "title": "Jane Doe - Owner - Other Co",
                    "personalInfo": {
                        "companyName": "Acme Roofing LLC",
                        "jobTitle": "Owner",
                    },
                },
                {
                    "url": "https://www.linkedin.com/in/john-smith-2",
                    "title": "John Smith - Owner - Acme Roofing",
                    "personalInfo": {
                        "companyName": "Different Builders",
                        "jobTitle": "Owner",
                    },
                },
                {
                    "url": "https://www.linkedin.com/in/pat-lee-3",
                    "title": "Pat Lee - Sales - Acme Roofing",
                    "personalInfo": {
                        "companyName": "Acme Roofing",
                        "jobTitle": "Sales Associate",
                    },
                },
                {
                    "url": "https://www.linkedin.com/in/no-personal-4",
                    "title": "Owner at Acme Roofing",
                },
            ]
        }
    ]
    people = client.parse_people(items, company_name="Acme Roofing", profile=PROFILE)
    assert len(people) == 1
    assert people[0].first_name.lower() == "jane"
    assert people[0].title == "Owner"


def test_parse_people_accepts_gm_synonym():
    client = SerpClient(token="")
    items = [
        {
            "url": "https://www.linkedin.com/in/alex-ray-9",
            "personalInfo": {"companyName": "Acme Roofing", "jobTitle": "GM"},
        }
    ]
    people = client.parse_people(items, company_name="Acme Roofing", profile=PROFILE)
    assert len(people) == 1
    assert people[0].title == "GM"


def test_serp_runs_on_domain_lane_when_windowed():
    order = compute_tier_order(rates=LiveRates())
    allowed = select_tiers(order, min_tier="serp", max_tier="serp")
    assert _lane_order(allowed, order, "domain", "Acme Roofing") == ["serp"]
    assert _lane_order(allowed, order, "domain", "") == []
