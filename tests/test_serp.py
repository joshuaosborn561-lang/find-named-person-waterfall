import threading
import time

from people_waterfall.pricing import LiveRates, compute_tier_order, select_tiers
from people_waterfall.profile import parse_profile
from people_waterfall.vendors.serp import (
    SERP_DEFAULT_RUN_CONCURRENCY,
    SERP_MEMORY_MB,
    SerpClient,
    SerpQuery,
    build_query,
    group_items_by_query,
    items_for_query,
    poll_budget_s,
)
from people_waterfall.waterfall import _lane_order, _split_lane


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


def test_split_lane_isolates_serp():
    before, has_serp, after = _split_lane(["cache", "aiark", "serp", "prospeo"])
    assert before == ["cache", "aiark"]
    assert has_serp is True
    assert after == ["prospeo"]
    assert _split_lane(["cache", "aiark"]) == (["cache", "aiark"], False, [])


def test_poll_budget_grows_with_batch_and_caps():
    assert poll_budget_s(1) == 900
    assert poll_budget_s(20) == 1000
    assert poll_budget_s(100) == 45 * 60


def test_items_for_query_maps_search_query_term():
    grouped = group_items_by_query(
        [
            {"searchQuery": {"term": 'site:linkedin.com/in "Acme"'}, "organicResults": [{"n": 1}]},
            {"searchQuery": {"term": 'site:linkedin.com/in "Beta"'}, "organicResults": [{"n": 2}]},
            {"query": "plain", "organicResults": [{"n": 3}]},
        ]
    )
    acme = items_for_query(grouped, 'site:linkedin.com/in "Acme"')
    assert acme and acme[0]["organicResults"][0]["n"] == 1
    assert items_for_query(grouped, 'site:linkedin.com/in  "acme"')[0]["organicResults"][0]["n"] == 1
    assert items_for_query(grouped, "plain")[0]["organicResults"][0]["n"] == 3
    assert items_for_query(grouped, "missing") == []


def test_start_queries_joins_newline_and_counts_per_query(monkeypatch):
    captured: dict = {}

    class _Resp:
        status_code = 201

        def json(self):
            return {"data": {"id": "run-abc"}}

    def fake_post(tier, url, **kwargs):
        captured["url"] = url
        captured["json"] = kwargs["json"]
        return _Resp()

    monkeypatch.setattr("people_waterfall.vendors.serp.http_client.post", fake_post)
    client = SerpClient(token="tok")
    run_id = client.start_queries(["q1", "q2", "q3"])
    assert run_id == "run-abc"
    assert captured["json"]["queries"] == "q1\nq2\nq3"
    assert captured["json"]["saveHtml"] is False
    assert captured["json"]["saveHtmlToKeyValueStore"] is False
    assert f"memory={SERP_MEMORY_MB}" in captured["url"]
    assert "waitForFinish=0" in captured["url"]
    assert client.calls == 3
    assert client.runs == 1


def test_resolve_queries_maps_by_term_and_bills_unit_per_query():
    acme_q = build_query("Acme Roofing", PROFILE.target_titles)
    beta_q = build_query("Beta Builders", PROFILE.target_titles)
    client = SerpClient(token="tok")
    client.start_queries = lambda queries: "run-1"  # type: ignore[method-assign]
    client.poll_run = lambda run_id, timeout_s=None, n_queries=1: [  # type: ignore[method-assign]
        {
            "searchQuery": {"term": acme_q},
            "organicResults": [
                {
                    "url": "https://www.linkedin.com/in/jane-doe-1",
                    "personalInfo": {"companyName": "Acme Roofing", "jobTitle": "Owner"},
                }
            ],
        },
        {
            "searchQuery": {"term": beta_q},
            "organicResults": [
                {
                    "url": "https://www.linkedin.com/in/pat-lee-3",
                    "personalInfo": {"companyName": "Beta Builders", "jobTitle": "Sales"},
                }
            ],
        },
    ]
    packed = client.resolve_queries(
        [
            SerpQuery(key="acme", query=acme_q, company_name="Acme Roofing", titles=PROFILE.target_titles),
            SerpQuery(key="beta", query=beta_q, company_name="Beta Builders", titles=PROFILE.target_titles),
        ],
        profile=PROFILE,
        unit=0.0045,
        concurrency=1,
    )
    by_key = {row.key: row for row in packed}
    assert len(by_key["acme"].people) == 1
    assert by_key["acme"].people[0].first_name.lower() == "jane"
    assert by_key["beta"].people == []
    assert by_key["acme"].cost_usd == 0.0045
    assert by_key["beta"].cost_usd == 0.0045
    assert sum(row.cost_usd for row in packed) == 0.009


def test_resolve_queries_starts_two_runs_concurrently():
    client = SerpClient(token="tok")
    batches: list[list[str]] = []
    lock = threading.Lock()
    in_flight = 0
    max_flight = 0

    def start(queries):
        nonlocal in_flight, max_flight
        with lock:
            batches.append(list(queries))
            in_flight += 1
            max_flight = max(max_flight, in_flight)
        time.sleep(0.05)
        with lock:
            in_flight -= 1
        return f"run-{len(batches)}"

    client.start_queries = start  # type: ignore[method-assign]
    client.poll_run = lambda run_id, timeout_s=None, n_queries=1: []  # type: ignore[method-assign]
    jobs = [
        SerpQuery(key=str(i), query=f"q{i}", company_name=f"Co {i}", titles=["Owner"])
        for i in range(5)
    ]
    packed = client.resolve_queries(jobs, profile=PROFILE, unit=0.0045, concurrency=2, chunk_size=2)
    assert SERP_DEFAULT_RUN_CONCURRENCY == 2
    assert [len(b) for b in batches] == [2, 2, 1]
    assert max_flight == 2
    assert len(packed) == 5
    assert all(row.cost_usd == 0.0045 for row in packed)
