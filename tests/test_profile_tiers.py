from people_waterfall.profile import parse_profile


def test_domain_dropped_tiers_are_not_inherited():
    profile = parse_profile(
        "peterson_roof",
        {
            "tier_order": ["maps", "discolike", "prospeo", "aiark", "serp"],
            "dropped_tiers": ["cache", "aiark", "leadmagic"],
            "measured_rates": {
                "getleads": {"title_matched": 771, "companies": 100, "title_match_rate": 0.3},
                "maps": {"hit_rate": 0.9},
            },
        },
    )
    assert profile.people_dropped_tiers == []
    assert profile.people_tier_order == []
    assert "getleads" in profile.people_measured_rates
    assert "maps" not in profile.people_measured_rates
    assert profile.domain_tier_order[0] == "maps"


def test_people_keys_win_over_domain_keys():
    profile = parse_profile(
        "goliath",
        {
            "dropped_tiers": ["cache", "aiark"],
            "people_dropped_tiers": ["serp"],
            "people_tier_order": [{"tier": "getleads", "billing": "free"}],
            "people_measured_rates": {"getleads": {"title_matched": 89}},
            "measured_rates": {"maps": {}},
        },
    )
    assert profile.people_dropped_tiers == ["serp"]
    assert profile.people_tier_order[0]["tier"] == "getleads"
    assert profile.people_measured_rates["getleads"]["title_matched"] == 89


def test_domain_string_order_is_not_treated_as_people_order():
    profile = parse_profile(
        "peterson_roof",
        {"people_tier_order": ["maps", {"tier": "getleads"}, {"tier": "unknown"}]},
    )
    assert [row["tier"] for row in profile.people_tier_order] == ["getleads"]
