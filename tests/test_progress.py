from people_waterfall.progress import build_counter, counter_from_stats
from people_waterfall.source import filters_to_query, where_to_filters


def test_counter_pct_and_message():
    counter = build_counter(done=12, total=100, title_matched=4, phase="running")
    assert counter["done"] == 12
    assert counter["total"] == 100
    assert counter["remaining"] == 88
    assert counter["pct"] == 12.0
    assert counter["title_matched"] == 4
    assert counter["message"] == "running: 12/100 companies (12.0%)"


def test_counter_unknown_total():
    counter = build_counter(done=3, total=None, phase="running")
    assert counter["remaining"] is None
    assert counter["pct"] is None
    assert "3 companies processed" in counter["message"]


def test_counter_from_stats():
    counter = counter_from_stats(
        {
            "companies": 2,
            "title_matched": 5,
            "name_bank": 1,
            "companies_with_people": 1,
            "companies_unresolved": 1,
        },
        total=10,
        phase="running",
    )
    assert counter["done"] == 2
    assert counter["remaining"] == 8
    assert counter["pct"] == 20.0


def test_empty_queue_is_100_percent():
    counter = build_counter(done=0, total=0, phase="completed")
    assert counter["pct"] == 100.0
    assert counter["remaining"] == 0


def test_where_filters_become_postgrest_count_query():
    filters = where_to_filters("wf_people_status is null and state = 'TX'")
    query = filters_to_query(filters)
    assert query["wf_people_status"] == "is.null"
    assert query["state"] == "eq.TX"
