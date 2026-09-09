from people_waterfall.pricing import LiveRates, compute_tier_order, sort_key


def test_free_tiers_sort_first():
    rates = LiveRates(leadmagic_per_credit=0.0198, aiark_per_credit=0.004, prospeo_per_credit=0.02)
    order = compute_tier_order(rates=rates)
    names = [r["tier"] for r in order if not r["dropped"]]
    assert names[0] == "cache"
    assert names[1] in {"getleads", "smartlead"}
    assert names.index("leadmagic_employee") < names.index("aiark")
    assert names.index("aiark") < names.index("prospeo")


def test_search_free_moves_role_finder_with_free_tier():
    rates = LiveRates(leadmagic_search_free=True, leadmagic_per_credit=0.0198)
    order = compute_tier_order(rates=rates)
    role = next(r for r in order if r["tier"] == "leadmagic_role")
    assert role["billing"] == "free"
    assert role["unit_usd"] == 0.0


def test_free_on_miss_uses_half_rate_until_measured():
    rates = LiveRates(prospeo_per_credit=0.02)
    key = sort_key("prospeo", rates=rates, measured_rate=None, dropped=set())
    assert key[1] == rates.unit_usd("prospeo") * 0.5


def test_zero_yield_dropped_not_reordered():
    rates = LiveRates(leadmagic_per_credit=0.01, aiark_per_credit=0.002)
    order = compute_tier_order(
        rates=rates,
        measured_rates={"serp": 0.0, "aiark": 0.2},
        dropped_tiers=["serp"],
    )
    names = [r["tier"] for r in order if not r["dropped"]]
    assert "serp" not in names
    assert names.index("leadmagic_employee") < names.index("aiark")
