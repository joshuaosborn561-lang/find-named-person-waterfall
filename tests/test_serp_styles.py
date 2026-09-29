from people_waterfall.people import conversational_company
from people_waterfall.profile import parse_profile
from people_waterfall.vendors.serp import (
    SerpClient,
    build_query_b,
    build_query_c,
    build_query_d,
    enabled_serp_styles,
    extract_emails,
    is_aggregator_host,
    style_applies,
)


PROFILE = parse_profile(
    "demo",
    {
        "target_titles": ["Owner", "Administrator", "Office Manager", "Director"],
        "title_synonyms": {"admin": "Administrator"},
        "title_exclude_regex": "Intern",
        "serp_styles": ["a", "b", "c", "d"],
    },
)


def test_build_query_styles():
    assert build_query_b("acme.com") == (
        'site:acme.com ("staff" OR "our team" OR "leadership" OR "about us")'
    )
    assert build_query_c("Acme Roofing", "Austin") == (
        'site:facebook.com "Acme Roofing" "Austin"'
    )
    assert build_query_c("Acme Roofing") == 'site:facebook.com "Acme Roofing"'
    query_d = build_query_d("Acme Roofing", "Austin", PROFILE.target_titles)
    assert query_d.startswith('"Acme Roofing" "Austin"')
    assert '"Owner"' in query_d
    assert '"Administrator"' in query_d
    assert not query_d.startswith('site:')


def test_style_b_skipped_without_domain():
    assert style_applies("b", company_name="Acme", domain="") is False
    assert style_applies("b", company_name="Acme", domain="acme.com") is True
    assert style_applies("c", company_name="Acme", city="") is True
    assert style_applies("d", company_name="Acme") is True


def test_parse_style_b_reads_title_and_snippet():
    client = SerpClient(token="")
    items = [
        {
            "organicResults": [
                {
                    "url": "https://acme.com/our-team",
                    "title": "Jane Doe | Owner | Acme Roofing",
                    "description": "Jane leads the company from Austin.",
                },
                {
                    "url": "https://other.com/staff",
                    "title": "Pat Lee | Owner | Other Co",
                    "description": "Not our company.",
                },
            ]
        }
    ]
    people = client.parse_style(
        items,
        style="b",
        company_name="Acme Roofing",
        profile=PROFILE,
        domain="acme.com",
    )
    assert len(people) == 1
    assert people[0].first_name.lower() == "jane"
    assert people[0].source_tier == "serp_b"


def test_parse_style_c_facebook_about():
    client = SerpClient(token="")
    items = [
        {
            "organicResults": [
                {
                    "url": "https://www.facebook.com/acmeroofing",
                    "title": "Acme Roofing | Austin TX",
                    "description": "Owned by John Smith. Family-owned since 1998.",
                }
            ]
        }
    ]
    people = client.parse_style(
        items,
        style="c",
        company_name="Acme Roofing",
        profile=PROFILE,
        domain="acme.com",
    )
    assert len(people) == 1
    assert people[0].first_name.lower() == "john"
    assert people[0].last_name.lower() == "smith"
    assert people[0].source_tier == "serp_c"
    assert people[0].domain == "acme.com"


def test_parse_style_d_aggregator_and_email():
    client = SerpClient(token="")
    items = [
        {
            "organicResults": [
                {
                    "url": "https://www.zoominfo.com/c/acme-roofing/123",
                    "title": "Mary Jones - Administrator at Acme Roofing | ZoomInfo",
                    "snippet": "Email mary.jones@acmeroofing.com · Austin TX",
                },
                {
                    "url": "https://linkedin.com/in/skip-me",
                    "title": "Skip Me - Administrator at Acme Roofing",
                    "snippet": "skip@acme.com",
                },
            ]
        }
    ]
    people = client.parse_style(
        items,
        style="d",
        company_name="Acme Roofing",
        profile=PROFILE,
        domain="acme.com",
    )
    assert len(people) == 1
    assert people[0].first_name.lower() == "mary"
    assert people[0].email.lower() == "mary.jones@acmeroofing.com"
    assert people[0].source_tier == "serp_d"
    assert is_aggregator_host("www.zoominfo.com")
    assert is_aggregator_host("directory.greatschools.org")
    assert not is_aggregator_host("linkedin.com")
    assert extract_emails("reach us at desk@school.org today") == "desk@school.org"


def test_enabled_styles_honor_profile_and_drops():
    limited = parse_profile("demo", {"serp_styles": ["a", "c"], "people_dropped_tiers": ["serp_c"]})
    assert enabled_serp_styles(limited) == ["a"]
    assert enabled_serp_styles(PROFILE) == ["a", "b", "c", "d"]


def test_conversational_company_skips_legal_name():
    row = {
        "legal_name": "Acme Roofing Holdings LLC",
        "clean_name": "Acme Roofing",
        "company_name": "Acme Roofing Holdings LLC",
    }
    assert conversational_company(row) == "Acme Roofing"
    assert conversational_company({"legal_name": "Nope LLC"}) == ""


def test_find_people_stops_after_title_match(monkeypatch):
    client = SerpClient(token="tok")
    seen: list[str] = []

    def fake_search(style, **kwargs):
        seen.append(style)
        if style == "a":
            from people_waterfall.people import PersonHit

            return [
                PersonHit(
                    first_name="Jane",
                    last_name="Doe",
                    title="Owner",
                    company_name="Acme Roofing",
                    domain="acme.com",
                    source_tier="serp_a",
                )
            ]
        raise AssertionError(f"style {style} should not run")

    client.search_style = fake_search  # type: ignore[method-assign]
    people = client.find_people(
        profile=PROFILE,
        company_name="Acme Roofing",
        domain="acme.com",
        city="Austin",
    )
    assert seen == ["a"]
    assert len(people) == 1


def test_find_people_continues_until_title_match(monkeypatch):
    client = SerpClient(token="tok")
    seen: list[str] = []

    def fake_search(style, **kwargs):
        from people_waterfall.people import PersonHit

        seen.append(style)
        if style == "c":
            return [
                PersonHit(
                    first_name="John",
                    last_name="Smith",
                    title="Owner",
                    company_name="Acme Roofing",
                    domain="acme.com",
                    source_tier="serp_c",
                )
            ]
        return [
            PersonHit(
                first_name="Sam",
                last_name="Sale",
                title="Sales",
                company_name="Acme Roofing",
                domain="acme.com",
                source_tier=f"serp_{style}",
            )
        ]

    client.search_style = fake_search  # type: ignore[method-assign]
    people = client.find_people(
        profile=PROFILE,
        company_name="Acme Roofing",
        domain="acme.com",
        city="Austin",
    )
    assert seen == ["a", "b", "c"]
    assert people[-1].source_tier == "serp_c"
