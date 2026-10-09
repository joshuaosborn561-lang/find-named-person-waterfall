"""Title-required, per-company cap, cost ceiling, legacy aliases."""

import pytest

from people_waterfall.people import PersonHit
from people_waterfall.pricing import LiveRates
from people_waterfall.profile import parse_profile
from people_waterfall.source import TableSource
from people_waterfall.vendors.aiark import AiArkPeopleClient, TitlesRequired
from people_waterfall.vendors.prospeo import (
    ProspeoSearchClient,
    search_person_filters,
    websites_of_company,
)
from people_waterfall.waterfall import VendorBundle, resolve_people
from people_waterfall.site_quality import registrable_domain


PROFILE = parse_profile(
    "emcor",
    {
        "target_titles": ["Facility Manager", "Director", "Administrator"],
        "title_synonyms": {},
        "vertical": "mechanical contractors",
        "contacts_table": "emcor_wf_contacts",
    },
)

GOLIATH = parse_profile(
    "goliath",
    {
        "target_titles": ["Owner", "President"],
        "people_tier_order": [
            {"tier": "site_staff"},
            {"tier": "cache"},
            {"tier": "getleads"},
            {"tier": "smartlead"},
            {"tier": "aiark"},
            {"tier": "serp"},
            {"tier": "prospeo"},
            {"tier": "leadmagic_employee"},
            {"tier": "leadmagic_role"},
        ],
        "contacts_table": "goliath_wf_contacts",
    },
)


class _Empty:
    enabled = True
    calls = 0

    def find_people(self, **kwargs):
        return []


class _NoopPaid:
    enabled = True
    last_credits_used = 0.0
    last_error = ""

    def __init__(self):
        self.seen: list[str] = []
        self.calls = 0
        self.bodies: list[dict] = []

    def find_people(self, **kwargs):
        self.seen.append(kwargs.get("domain") or "")
        self.calls += 1
        return []

    def search_people(self, domains, **kwargs):
        self.seen.extend(list(domains))
        self.calls += 1
        return {d: [] for d in domains}


class _Disco:
    enabled = True
    calls = 0
    hits = 0
    tasks = 0

    def resolve_domains(self, domains, **kwargs):
        from people_waterfall.vendors.discolike import DiscoDomainResult

        self.calls += len(domains)
        self.tasks += 1
        return {d: DiscoDomainResult(domain=d, cost_usd=0.0055) for d in domains}


def _run(monkeypatch, rows, vendors, profile=PROFILE, **kwargs):
    from people_waterfall import waterfall as wf

    src = TableSource(project_id="x", schema="public", table="emcor_companies", writeback=False)
    monkeypatch.setattr(wf, "preflight_job", lambda **k: {"ok": True, "writeback": "columns"})
    monkeypatch.setattr(wf, "get_profile", lambda tag: profile)
    monkeypatch.setattr(wf, "parse_source", lambda *a, **k: src)
    monkeypatch.setattr(wf, "count_source", lambda s: len(rows))
    monkeypatch.setattr(
        wf, "count_source_with_domain", lambda s: sum(1 for r in rows if r.get("domain"))
    )
    monkeypatch.setattr(wf, "iter_source", lambda s: iter(rows))
    monkeypatch.setattr(wf, "read_live_rates", lambda *a, **k: LiveRates())
    monkeypatch.setattr(wf, "ensure_people_writeback", lambda s: None)
    monkeypatch.setattr(wf, "load_known_names", lambda p: set())
    monkeypatch.setattr(wf, "write_contacts", lambda p, accepted: len(accepted))
    monkeypatch.setattr(wf, "write_name_bank_rows", lambda rows: len(rows))
    monkeypatch.setattr(wf, "handoff_title_matches", lambda p: {})
    monkeypatch.setattr(wf, "writeback_people", lambda *a, **k: None)
    monkeypatch.setattr(wf, "persist_discolike_icp", lambda *a, **k: None)
    monkeypatch.setattr(wf, "merge_people_measured_rates", lambda *a, **k: None)
    return resolve_people(
        source_table="public.emcor_companies",
        where="domain is not null",
        client_tag=profile.client_tag,
        write_supabase=False,
        vendors=vendors,
        **kwargs,
    )


def test_goliath_custom_order_warns_on_unrecognized_and_legacy(monkeypatch):
    rows = [{"domain": "acme.com", "company_name": "Acme", "_source_key": "1"}]
    result = _run(
        monkeypatch,
        rows,
        VendorBundle(cache=_Empty(), discolike=_Disco(), prospeo=_NoopPaid(), aiark=_NoopPaid()),
        profile=GOLIATH,
        estimate_only=True,
    )
    assert "getleads" in result["unrecognized_tiers"]
    assert "aiark" in result["unrecognized_tiers"]
    assert "prospeo" in result["unrecognized_tiers"]
    assert "leadmagic_employee" in result["deprecated_tiers"]
    assert "leadmagic_role" in result["deprecated_tiers"]
    assert any("unrecognized" in w for w in result["warnings"])
    assert any("deprecated" in w for w in result["warnings"])
    assert result["selected_tiers"] == ["site_staff", "cache"]


def test_legacy_skip_does_not_fail_the_job(monkeypatch):
    rows = [{"domain": "acme.com", "company_name": "Acme", "_source_key": "1"}]
    result = _run(
        monkeypatch,
        rows,
        VendorBundle(cache=_Empty(), discolike=_Disco(), prospeo=_NoopPaid(), aiark=_NoopPaid()),
        skip_tiers="leadmagic_employee,lm",
        max_tier="discolike",
    )
    assert result["status"] == "completed"
    assert "leadmagic_employee" in result["deprecated_tiers"]
    assert "leadmagic_employee" not in result["selected_tiers"]
    assert result["per_tier"]["discolike"]["calls"] == 1


def test_aiark_refuses_without_titles():
    client = AiArkPeopleClient(api_key="tok")
    with pytest.raises(TitlesRequired, match="requires titles"):
        client.find_people(domain="acme.com", titles=[])


def test_aiark_job_refuses_without_profile_titles(monkeypatch):
    bare = parse_profile("emcor", {"contacts_table": "emcor_wf_contacts", "target_titles": []})
    rows = [{"domain": "acme.com", "company_name": "Acme", "_source_key": "1"}]
    with pytest.raises(TitlesRequired, match="no target_titles"):
        _run(
            monkeypatch,
            rows,
            VendorBundle(cache=_Empty(), discolike=_Disco(), prospeo=_NoopPaid(), aiark=_NoopPaid()),
            profile=bare,
            min_tier="aiark_people",
            max_tier="aiark_people",
        )


def test_aiark_does_not_truncate_titles_and_caps_three(monkeypatch):
    client = AiArkPeopleClient(api_key="tok")
    posted: list[dict] = []
    titles = [f"Title {i}" for i in range(36)]

    class _Resp:
        status_code = 200
        headers = {"X-Credit": "-1.5"}

        def json(self):
            return {
                "content": [
                    {
                        "id": f"p{i}",
                        "profile": {
                            "first_name": "Pat",
                            "last_name": f"Person{i}",
                            "title": "Director",
                        },
                        "link": {"linkedin": f"https://www.linkedin.com/in/pat-{i}"},
                    }
                    for i in range(8)
                ]
            }

    def fake_post(tier, url, **kwargs):
        posted.append(kwargs.get("json") or {})
        return _Resp()

    monkeypatch.setattr("people_waterfall.vendors.aiark.http_client.post", fake_post)
    people = client.find_people(domain="acme.com", titles=titles, limit=3)
    assert len(people) == 3
    sent = posted[0]["contact"]["experience"]["latest"]["title"]["any"]["include"]["content"]
    assert sent == titles
    assert posted[0]["size"] == 3
    assert client.last_credits_used == 1.5
    assert people[0].aiark_person_id == "p0"


def test_prospeo_strips_subdomains_and_caps_three(monkeypatch):
    assert registrable_domain("shop.acme.co.uk") == "acme.co.uk"
    client = ProspeoSearchClient(api_key="tok")
    posted: list[dict] = []

    class _Resp:
        status_code = 200

        def json(self):
            return {
                "results": [
                    {
                        "person": {
                            "first_name": "Ann",
                            "last_name": f"Owner{i}",
                            "title": "Owner",
                            "linkedin_url": f"https://www.linkedin.com/in/ann-{i}",
                        },
                        "company": {
                            "website": "www.acme.com",
                            "domain": "acme.com",
                            "other_websites": ["shop.acme.com"],
                        },
                    }
                    for i in range(6)
                ]
            }

    def fake_post(tier, url, **kwargs):
        posted.append(kwargs.get("json") or {})
        return _Resp()

    monkeypatch.setattr("people_waterfall.vendors.prospeo.http_client.post", fake_post)
    packed = client.search_people(
        ["shop.acme.com"],
        titles=["Owner"],
        max_per_company=3,
    )
    websites = posted[0]["filters"]["company"]["websites"]
    assert websites == {"include": ["acme.com"]}
    assert len(packed["acme.com"]) == 3
    assert client.last_credits_used == 1.0


def test_prospeo_cover_loop_requeries_only_uncovered(monkeypatch):
    client = ProspeoSearchClient(api_key="tok")
    pages: list[list[str]] = []

    class _Resp:
        def __init__(self, rows):
            self.status_code = 200
            self._rows = rows

        def json(self):
            return {"results": self._rows}

    def fake_post(tier, url, **kwargs):
        sites = list((kwargs.get("json") or {})["filters"]["company"]["websites"]["include"])
        pages.append(sites)
        if "beta.com" in sites and len(pages) == 1:
            return _Resp(
                [
                    {
                        "person": {
                            "first_name": "Bea",
                            "last_name": "Beta",
                            "title": "Owner",
                        },
                        "company": {"website": "beta.com", "domain": "beta.com"},
                    }
                ]
            )
        if pages[-1] == ["acme.com"]:
            return _Resp(
                [
                    {
                        "person": {
                            "first_name": "Ann",
                            "last_name": "Acme",
                            "title": "Owner",
                        },
                        "company": {"website": "acme.com", "domain": "acme.com"},
                    }
                ]
            )
        return _Resp([])

    monkeypatch.setattr("people_waterfall.vendors.prospeo.http_client.post", fake_post)
    packed = client.search_people(["acme.com", "beta.com"], titles=["Owner"])
    assert pages[0] == ["acme.com", "beta.com"]
    assert pages[1] == ["acme.com"]
    assert [p.first_name for p in packed["beta.com"]] == ["Bea"]
    assert [p.first_name for p in packed["acme.com"]] == ["Ann"]


def test_approve_cost_ceiling_defers_before_spend(monkeypatch):
    rows = [
        {"domain": "acme.com", "company_name": "Acme", "_source_key": "1"},
        {"domain": "beta.com", "company_name": "Beta", "_source_key": "2"},
    ]
    result = _run(
        monkeypatch,
        rows,
        VendorBundle(cache=_Empty(), discolike=_Disco(), prospeo=_NoopPaid(), aiark=_NoopPaid()),
        estimate_only=True,
        approve_cost_usd=0.001,
    )
    assert result["status"] == "deferred"
    assert result["reason"] == "estimated_usd exceeds approve_cost_usd"
    assert result["estimated_usd"] > 0.001

    ran = _run(
        monkeypatch,
        rows,
        VendorBundle(cache=_Empty(), discolike=_Disco(), prospeo=_NoopPaid(), aiark=_NoopPaid()),
        estimate_only=False,
        approve_cost_usd=0.001,
    )
    assert ran["status"] == "deferred"
    assert ran["reason"] == "estimated_usd exceeds approve_cost_usd"
    assert ran.get("spent_usd", 0) in (0, 0.0) or "per_tier" not in ran


def test_websites_of_company_uses_other_websites():
    hosts = websites_of_company(
        {"website": "https://www.foo.com/about", "other_websites": ["shop.foo.com"]}
    )
    assert hosts == ["foo.com"]


def test_prospeo_search_page_sends_websites_include(monkeypatch):
    """Docs: filters.company.websites.include — a bare list matches nothing."""
    body = search_person_filters(["acme.com", "beta.com"], ["Owner", "President"])
    assert body["company"] == {"websites": {"include": ["acme.com", "beta.com"]}}
    assert body["company"]["websites"] != ["acme.com", "beta.com"]
    assert body["person_job_title"]["include"] == ["Owner", "President"]

    client = ProspeoSearchClient(api_key="tok")
    posted: list[dict] = []

    class _Resp:
        status_code = 200

        def json(self):
            return {"results": []}

    def fake_post(tier, url, **kwargs):
        posted.append(kwargs.get("json") or {})
        return _Resp()

    monkeypatch.setattr("people_waterfall.vendors.prospeo.http_client.post", fake_post)
    client._search_page(["acme.com"], ["Owner"])
    sent = posted[0]
    assert sent["page"] == 1
    assert sent["filters"]["company"] == {"websites": {"include": ["acme.com"]}}
    assert "include" in sent["filters"]["company"]["websites"]
