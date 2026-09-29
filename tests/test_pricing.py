from people_waterfall.pricing import (
    DEFAULT_ORDER,
    LiveRates,
    compute_tier_order,
    include_from_profile,
    select_tiers,
    sort_key,
)


def test_default_order_is_cache_discolike_leadmagic_employee():
    rates = LiveRates(leadmagic_per_credit=0.0198)
    order = compute_tier_order(rates=rates)
    names = [r["tier"] for r in order if not r["dropped"]]
    assert names == ["cache", "discolike", "leadmagic_employee"]
    assert DEFAULT_ORDER == ["cache", "discolike", "leadmagic_employee"]
    assert "getleads" not in names
    assert "serp" not in names
    assert "aiark" not in names
    assert "prospeo" not in names
    assert "smartlead" not in names
    assert "leadmagic_role" not in names


def test_discolike_unit_is_serper_times_two_plus_company_fee(monkeypatch):
    monkeypatch.setenv("SERPER_USD_PER_QUERY", "0.001")
    monkeypatch.setenv("DISCOLIKE_USD_PER_COMPANY", "0.0035")
    rates = LiveRates()
    assert rates.unit_usd("discolike") == 0.0055


def test_explicit_people_tier_order_opts_serp_back_in():
    rates = LiveRates()
    include = include_from_profile(
        [{"tier": "cache"}, {"tier": "serp"}, {"tier": "discolike"}]
    )
    order = compute_tier_order(rates=rates, include=include)
    names = [r["tier"] for r in order if not r["dropped"]]
    assert names == ["cache", "serp", "discolike"]


def test_search_free_moves_role_finder_with_free_tier():
    rates = LiveRates(leadmagic_search_free=True, leadmagic_per_credit=0.0198)
    order = compute_tier_order(
        rates=rates, include=["cache", "leadmagic_role", "discolike"]
    )
    role = next(r for r in order if r["tier"] == "leadmagic_role")
    assert role["billing"] == "free"
    assert role["unit_usd"] == 0.0


def test_free_on_miss_uses_half_rate_until_measured():
    rates = LiveRates(prospeo_per_credit=0.02)
    key = sort_key("prospeo", rates=rates, measured_rate=None, dropped=set())
    assert key[1] == rates.unit_usd("prospeo") * 0.5


def test_zero_yield_dropped_not_reordered():
    rates = LiveRates(leadmagic_per_credit=0.01)
    order = compute_tier_order(
        rates=rates,
        measured_rates={"discolike": 0.0, "leadmagic_employee": 0.2},
        dropped_tiers=["discolike"],
    )
    names = [r["tier"] for r in order if not r["dropped"]]
    assert "discolike" not in names
    assert names == ["cache", "leadmagic_employee"]


def test_select_tiers_can_run_serp_alone():
    rates = LiveRates(leadmagic_per_credit=0.01)
    order = compute_tier_order(rates=rates)
    names = select_tiers(order, min_tier="serp", max_tier="serp")
    assert names == ["serp"]


def test_skip_tiers_drops_leadmagic_employee():
    rates = LiveRates(leadmagic_per_credit=0.01)
    order = compute_tier_order(rates=rates)
    names = select_tiers(order, skip_tiers="leadmagic_employee")
    assert "leadmagic_employee" not in names
    assert names == ["cache", "discolike"]


def test_min_tier_can_include_a_dropped_serp():
    rates = LiveRates()
    order = compute_tier_order(rates=rates, dropped_tiers=["serp"])
    assert "serp" not in select_tiers(order)
    assert select_tiers(order, min_tier="serp", max_tier="serp") == ["serp"]
