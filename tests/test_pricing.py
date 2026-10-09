from people_waterfall.pricing import (
    DEFAULT_ORDER,
    PUBLISHED,
    LiveRates,
    classify_request_tiers,
    compute_tier_order,
    include_from_profile,
    plan_profile_tiers,
    select_tiers,
)


NEW_DEFAULT = [
    "site_staff",
    "cache",
    "discolike",
    "prospeo_search",
    "aiark_people",
]


def test_default_order_is_site_staff_then_paid_tiers():
    rates = LiveRates(aiark_per_credit=0.003667, prospeo_per_credit=0.0148)
    order = compute_tier_order(rates=rates)
    names = [r["tier"] for r in order if not r["dropped"]]
    assert names == NEW_DEFAULT
    assert DEFAULT_ORDER == NEW_DEFAULT
    assert set(PUBLISHED) == set(NEW_DEFAULT)
    assert "leadmagic_employee" not in PUBLISHED
    assert "leadmagic_role" not in PUBLISHED
    assert rates.unit_usd("site_staff") == 0.0
    assert select_tiers(order, max_tier="site_staff") == ["site_staff"]
    assert rates.unit_usd("prospeo_search") == 0.0148
    assert abs(rates.unit_usd("aiark_people") - 0.5 * 0.003667) < 1e-9


def test_discolike_unit_is_serper_times_two_plus_company_fee(monkeypatch):
    monkeypatch.setenv("SERPER_USD_PER_QUERY", "0.001")
    monkeypatch.setenv("DISCOLIKE_USD_PER_COMPANY", "0.0035")
    rates = LiveRates()
    assert rates.unit_usd("discolike") == 0.0055


def test_unknown_tiers_are_warned_not_silent():
    planned = plan_profile_tiers(
        [
            {"tier": "cache"},
            {"tier": "getleads"},
            {"tier": "smartlead"},
            {"tier": "aiark"},
            {"tier": "serp"},
            {"tier": "prospeo"},
            {"tier": "discolike"},
        ]
    )
    assert planned.include == ["cache", "discolike"]
    assert planned.unrecognized == ["getleads", "smartlead", "aiark", "serp", "prospeo"]
    assert planned.deprecated == []
    assert any("unrecognized" in note for note in planned.notes)
    rates = LiveRates()
    order = compute_tier_order(rates=rates, include=planned.include)
    names = [r["tier"] for r in order if not r["dropped"]]
    assert names == ["site_staff", "cache", "discolike"]


def test_legacy_leadmagic_names_are_noop_with_warning():
    planned = plan_profile_tiers(
        [
            {"tier": "site_staff"},
            {"tier": "cache"},
            {"tier": "discolike"},
            {"tier": "leadmagic_employee"},
            {"tier": "leadmagic_role"},
            {"tier": "lm"},
            {"tier": "employee_finder"},
        ]
    )
    assert planned.include == ["site_staff", "cache", "discolike"]
    assert "leadmagic_employee" in planned.deprecated
    assert "leadmagic_role" in planned.deprecated
    assert any("deprecated" in note for note in planned.notes)
    assert include_from_profile(planned.include) == ["site_staff", "cache", "discolike"]


def test_skip_tiers_legacy_leadmagic_is_noop():
    rates = LiveRates()
    order = compute_tier_order(rates=rates)
    names = select_tiers(order, skip_tiers="leadmagic_employee,lm,employee_finder")
    assert names == NEW_DEFAULT
    notes = classify_request_tiers(skip_tiers="leadmagic_employee")
    assert "leadmagic_employee" in notes.deprecated


def test_legacy_max_tier_does_not_window_or_fail():
    rates = LiveRates()
    order = compute_tier_order(rates=rates)
    names = select_tiers(order, max_tier="leadmagic")
    assert names == NEW_DEFAULT


def test_zero_yield_dropped_not_reordered():
    rates = LiveRates()
    order = compute_tier_order(
        rates=rates,
        measured_rates={"discolike": 0.0, "aiark_people": 0.2},
        dropped_tiers=["discolike"],
    )
    names = [r["tier"] for r in order if not r["dropped"]]
    assert "discolike" not in names
    assert names == ["site_staff", "cache", "prospeo_search", "aiark_people"]


def test_select_tiers_can_run_discolike_alone():
    rates = LiveRates()
    order = compute_tier_order(rates=rates)
    names = select_tiers(order, min_tier="discolike", max_tier="discolike")
    assert names == ["discolike"]


def test_min_tier_can_include_a_dropped_discolike():
    rates = LiveRates()
    order = compute_tier_order(rates=rates, dropped_tiers=["discolike"])
    assert "discolike" not in select_tiers(order)
    assert select_tiers(order, min_tier="discolike", max_tier="discolike") == [
        "discolike"
    ]
