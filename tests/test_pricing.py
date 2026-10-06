from people_waterfall.pricing import (
    DEFAULT_ORDER,
    PUBLISHED,
    LiveRates,
    compute_tier_order,
    include_from_profile,
    select_tiers,
)


def test_default_order_is_site_staff_then_paid_tiers():
    rates = LiveRates(leadmagic_per_credit=0.0198)
    order = compute_tier_order(rates=rates)
    names = [r["tier"] for r in order if not r["dropped"]]
    assert names == ["site_staff", "cache", "discolike", "leadmagic_employee"]
    assert DEFAULT_ORDER == ["site_staff", "cache", "discolike", "leadmagic_employee"]
    assert [name for name in DEFAULT_ORDER if name != "site_staff"] == [
        "cache",
        "discolike",
        "leadmagic_employee",
    ]
    assert set(PUBLISHED) == {"site_staff", "cache", "discolike", "leadmagic_employee"}
    assert rates.unit_usd("site_staff") == 0.0
    assert select_tiers(order, max_tier="site_staff") == ["site_staff"]


def test_discolike_unit_is_serper_times_two_plus_company_fee(monkeypatch):
    monkeypatch.setenv("SERPER_USD_PER_QUERY", "0.001")
    monkeypatch.setenv("DISCOLIKE_USD_PER_COMPANY", "0.0035")
    rates = LiveRates()
    assert rates.unit_usd("discolike") == 0.0055


def test_unknown_tiers_are_ignored_in_people_tier_order():
    rates = LiveRates()
    include = include_from_profile(
        [{"tier": "cache"}, {"tier": "serp"}, {"tier": "discolike"}]
    )
    order = compute_tier_order(rates=rates, include=include)
    names = [r["tier"] for r in order if not r["dropped"]]
    assert names == ["site_staff", "cache", "discolike"]


def test_zero_yield_dropped_not_reordered():
    rates = LiveRates(leadmagic_per_credit=0.01)
    order = compute_tier_order(
        rates=rates,
        measured_rates={"discolike": 0.0, "leadmagic_employee": 0.2},
        dropped_tiers=["discolike"],
    )
    names = [r["tier"] for r in order if not r["dropped"]]
    assert "discolike" not in names
    assert names == ["site_staff", "cache", "leadmagic_employee"]


def test_select_tiers_can_run_discolike_alone():
    rates = LiveRates(leadmagic_per_credit=0.01)
    order = compute_tier_order(rates=rates)
    names = select_tiers(order, min_tier="discolike", max_tier="discolike")
    assert names == ["discolike"]


def test_skip_tiers_drops_leadmagic_employee():
    rates = LiveRates(leadmagic_per_credit=0.01)
    order = compute_tier_order(rates=rates)
    names = select_tiers(order, skip_tiers="leadmagic_employee")
    assert "leadmagic_employee" not in names
    assert names == ["site_staff", "cache", "discolike"]


def test_min_tier_can_include_a_dropped_discolike():
    rates = LiveRates()
    order = compute_tier_order(rates=rates, dropped_tiers=["discolike"])
    assert "discolike" not in select_tiers(order)
    assert select_tiers(order, min_tier="discolike", max_tier="discolike") == [
        "discolike"
    ]
