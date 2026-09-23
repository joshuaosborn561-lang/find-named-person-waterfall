import pytest

from people_waterfall.people import PersonHit
from people_waterfall.pricing import LiveRates
from people_waterfall.profile import parse_profile
from people_waterfall.source import TableSource
from people_waterfall.vendors.serp import SerpClient, build_query
from people_waterfall.waterfall import VendorBundle, resolve_people


PROFILE = parse_profile(
    "demo",
    {
        "target_titles": ["Owner", "President"],
        "title_synonyms": {},
        "contacts_table": "demo_wf_contacts",
    },
)


class _Empty:
    enabled = True
    last_free = False

    def find_people(self, **kwargs):
        return []

    def employee_finder(self, **kwargs):
        return []

    def find_people_by_role(self, **kwargs):
        return []


class _HitGetLeads(_Empty):
    def find_people(self, **kwargs):
        company = kwargs.get("company_name") or ""
        if company != "Cheap Hit LLC":
            return []
        return [
            PersonHit(
                first_name="Ada",
                last_name="Lovelace",
                title="Owner",
                company_name=company,
                domain=kwargs.get("domain") or "",
                source_tier="getleads",
            )
        ]


class _HitProspeo(_Empty):
    def find_people(self, **kwargs):
        company = kwargs.get("company_name") or ""
        return [
            PersonHit(
                first_name="Pat",
                last_name="Lee",
                title="Owner",
                company_name=company,
                domain=kwargs.get("domain") or "",
                source_tier="prospeo",
            )
        ]


class _BatchSerp(SerpClient):
    def __init__(self):
        super().__init__(token="tok")
        self.batches: list[list[str]] = []

    def start_queries(self, queries):
        cleaned = [q for q in queries if q]
        self.batches.append(list(cleaned))
        self._note_start(len(cleaned), f"run-{len(self.batches)}")
        return f"run-{len(self.batches)}"

    def poll_run(self, run_id, *, timeout_s=None, n_queries=1):
        items = []
        for batch in self.batches:
            for query in batch:
                if "Acme Roofing" in query:
                    items.append(
                        {
                            "searchQuery": {"term": query},
                            "organicResults": [
                                {
                                    "url": "https://www.linkedin.com/in/jane-doe-1",
                                    "personalInfo": {
                                        "companyName": "Acme Roofing",
                                        "jobTitle": "Owner",
                                    },
                                }
                            ],
                        }
                    )
                else:
                    items.append({"searchQuery": {"term": query}, "organicResults": []})
        return items


def _bundle(*, getleads=None, serp=None, prospeo=None) -> VendorBundle:
    return VendorBundle(
        cache=_Empty(),
        getleads=getleads or _Empty(),
        smartlead=_Empty(),
        leadmagic=_Empty(),
        aiark=_Empty(),
        serp=serp or _BatchSerp(),
        prospeo=prospeo or _Empty(),
    )


def _run(monkeypatch, rows, vendors, **kwargs):
    from people_waterfall import waterfall as wf

    src = TableSource(project_id="x", schema="public", table="demo", writeback=False)
    monkeypatch.setattr(wf, "get_profile", lambda tag: PROFILE)
    monkeypatch.setattr(wf, "parse_source", lambda *a, **k: src)
    monkeypatch.setattr(wf, "count_source", lambda s: len(rows))
    monkeypatch.setattr(wf, "iter_source", lambda s: iter(rows))
    monkeypatch.setattr(wf, "read_live_rates", lambda *a, **k: LiveRates())
    return resolve_people(
        source_table="public.demo",
        client_tag="demo",
        write_supabase=False,
        vendors=vendors,
        **kwargs,
    )


def _company(name, *, key=None, domain=""):
    return {
        "_source_key": key or name,
        "company_name": name,
        "domain": domain,
        "city": "",
        "state": "",
    }


def test_serp_only_job_batches_queries_and_bills_per_query(monkeypatch):
    serp = _BatchSerp()
    rows = [
        _company("Acme Roofing", key="1"),
        _company("Beta Builders", key="2"),
        _company("Gamma Homes", key="3"),
    ]
    result = _run(
        monkeypatch,
        rows,
        _bundle(serp=serp),
        min_tier="serp",
        max_tier="serp",
    )
    assert len(serp.batches) == 1
    assert len(serp.batches[0]) == 3
    assert serp.runs == 1
    assert result["serp_actor_runs"] == 1
    assert result["per_tier"]["serp"]["calls"] == 3
    assert result["per_tier"]["serp"]["usd"] == pytest.approx(0.0135)
    assert result["spent_usd"] == pytest.approx(0.0135)
    assert result["counts"]["resolved"] == 1
    assert result["counts"]["people_unresolved"] == 2
    assert result["counts"]["title_matched"] == 1
    assert build_query("Acme Roofing", PROFILE.target_titles) in serp.batches[0]


def test_cheaper_tier_hit_skips_serp_batch(monkeypatch):
    serp = _BatchSerp()
    rows = [
        _company("Cheap Hit LLC", key="1", domain="cheap.com"),
        _company("Acme Roofing", key="2", domain="acme.com"),
    ]
    result = _run(
        monkeypatch,
        rows,
        _bundle(getleads=_HitGetLeads(), serp=serp),
        max_tier="serp",
    )
    assert len(serp.batches) == 1
    assert len(serp.batches[0]) == 1
    assert "Cheap Hit" not in serp.batches[0][0]
    assert result["per_tier"]["getleads"]["title_matched"] == 1
    assert result["per_tier"]["serp"]["calls"] == 1
    assert result["counts"]["resolved"] == 2


def test_serp_miss_still_runs_later_paid_tier(monkeypatch):
    serp = _BatchSerp()
    rows = [_company("Beta Builders", key="1")]
    result = _run(
        monkeypatch,
        rows,
        _bundle(serp=serp, prospeo=_HitProspeo()),
        min_tier="serp",
        max_tier="prospeo",
    )
    assert serp.runs == 1
    assert result["per_tier"]["serp"]["calls"] == 1
    assert result["per_tier"]["serp"]["title_matched"] == 0
    assert result["per_tier"]["prospeo"]["title_matched"] == 1
    assert result["counts"]["resolved"] == 1
    assert result["counts"]["title_matched"] == 1
