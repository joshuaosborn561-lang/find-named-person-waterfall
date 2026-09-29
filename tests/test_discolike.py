import pytest

from people_waterfall.people import PersonHit, name_derived_from_company
from people_waterfall.pricing import LiveRates
from people_waterfall.profile import build_discolike_icp, parse_profile
from people_waterfall.source import TableSource
from people_waterfall.vendors.discolike import (
    DiscoLikeClient,
    MAX_CONTACTS,
    chunked,
    poll_interval_s,
    unit_usd,
)
from people_waterfall.waterfall import VendorBundle, resolve_people


PROFILE = parse_profile(
    "emcor",
    {
        "target_titles": ["Facility Manager", "Director", "Administrator"],
        "title_synonyms": {},
        "vertical": "mechanical contractors",
        "contacts_table": "emcor_wf_contacts",
    },
)


class _Empty:
    enabled = True
    last_free = False
    runs = 0

    def find_people(self, **kwargs):
        return []

    def employee_finder(self, **kwargs):
        return []

class _Disco:
    enabled = True
    calls = 0
    hits = 0
    tasks = 0

    def resolve_domains(self, domains, **kwargs):
        from people_waterfall.vendors.discolike import DiscoDomainResult

        out = {}
        for domain in domains:
            people = []
            if domain == "acme.com":
                people = [
                    PersonHit(
                        first_name="Jane",
                        last_name="Doe",
                        title="Director",
                        company_name="Acme",
                        domain="acme.com",
                        linkedin_url="https://www.linkedin.com/in/jane-doe-1",
                        source_tier="discolike",
                    )
                ]
            out[domain] = DiscoDomainResult(
                domain=domain,
                people=people,
                email_pattern="{first}@acme.com" if domain == "acme.com" else "",
                email_pattern_confidence=0.8 if domain == "acme.com" else None,
                cost_usd=0.0055,
            )
        self.calls += len(domains)
        self.tasks += 1
        return out

    def find_people(self, **kwargs):
        packed = self.resolve_domains([kwargs.get("domain") or ""])
        row = packed.get(kwargs.get("domain") or "")
        return list(row.people) if row else []


def _bundle(disco=None) -> VendorBundle:
    return VendorBundle(
        cache=_Empty(),
        leadmagic=_Empty(),
        discolike=disco or _Disco(),
    )


def test_max_contacts_is_three():
    assert MAX_CONTACTS == 3


def test_unit_matches_published_defaults(monkeypatch):
    monkeypatch.delenv("SERPER_USD_PER_QUERY", raising=False)
    monkeypatch.delenv("DISCOLIKE_USD_PER_COMPANY", raising=False)
    assert unit_usd() == pytest.approx(0.0055)


def test_poll_interval_and_task_chunks():
    assert poll_interval_s(10) == 30
    assert poll_interval_s(500) == 180
    assert [len(c) for c in chunked(["a"] * 5001, 5000)] == [5000, 1]


def test_icp_from_titles_and_vertical():
    text = build_discolike_icp(PROFILE)
    assert "Facility Manager" in text
    assert "mechanical contractors" in text
    stored = parse_profile("emcor", {**PROFILE.raw, "discolike_icp_text": "Use this."})
    assert build_discolike_icp(stored) == "Use this."


def test_name_derived_from_company_or_domain():
    assert name_derived_from_company("Acme", "Roofing", "Acme Roofing", "acme.com")
    assert name_derived_from_company("Acme", "Team", "", "acme.com")
    assert not name_derived_from_company("Jane", "Doe", "Acme Roofing", "acme.com")


def test_parse_results_gates(monkeypatch):
    client = DiscoLikeClient(api_key="tok")
    payload = {
        "status": "completed",
        "results": {
            "acme.com": {
                "email_pattern": "{first}@acme.com",
                "email_pattern_confidence": 0.7,
                "contacts": [
                    {
                        "name": "Jane Doe",
                        "title": "Director",
                        "discovered_domain": "acme.com",
                        "linkedin_url": "https://www.linkedin.com/in/jane-doe",
                        "match_status": "match",
                    },
                    {
                        "name": "Sam Sale",
                        "title": "Director",
                        "discovered_domain": "other.com",
                        "match_status": "match",
                    },
                    {
                        "name": "Acme Roofing",
                        "title": "Director",
                        "discovered_domain": "acme.com",
                        "match_status": "match",
                    },
                    {
                        "name": "Pat",
                        "title": "Director",
                        "discovered_domain": "acme.com",
                        "match_status": "match",
                    },
                    {
                        "name": "Unsure Person",
                        "title": "Director",
                        "discovered_domain": "acme.com",
                        "match_status": "unsure",
                    },
                    {
                        "name": "No Match",
                        "title": "Director",
                        "discovered_domain": "acme.com",
                        "match_status": "not_match",
                    },
                ],
            }
        },
    }
    parsed = client.parse_results(
        payload, profile=PROFILE, companies={"acme.com": "Acme Roofing"}
    )
    row = parsed["acme.com"]
    assert [p.first_name for p in row.people] == ["Jane"]
    assert [p.first_name for p in row.bank_only] == ["Unsure"]
    assert row.email_pattern == "{first}@acme.com"


def _run(monkeypatch, rows, vendors, **kwargs):
    from people_waterfall import waterfall as wf

    src = TableSource(project_id="x", schema="public", table="emcor_companies", writeback=False)
    monkeypatch.setattr(wf, "get_profile", lambda tag: PROFILE)
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
    monkeypatch.setattr(wf, "write_name_bank", lambda **k: None)
    monkeypatch.setattr(wf, "handoff_title_matches", lambda p: {})
    monkeypatch.setattr(wf, "writeback_people", lambda *a, **k: None)
    monkeypatch.setattr(wf, "persist_discolike_icp", lambda *a, **k: None)
    monkeypatch.setattr(wf, "merge_people_measured_rates", lambda *a, **k: None)
    return resolve_people(
        source_table="public.emcor_companies",
        where="domain is not null and wf_people_status = 'people_unresolved'",
        client_tag="emcor",
        write_supabase=False,
        vendors=vendors,
        **kwargs,
    )


def test_estimate_only_emcor_unresolved_selects_and_prices_discolike(monkeypatch):
    rows = [
        {"domain": "acme.com", "company_name": "Acme", "_source_key": "1"},
        {"domain": "beta.com", "company_name": "Beta", "_source_key": "2"},
        {"domain": "", "company_name": "No Domain LLC", "_source_key": "3"},
    ]
    result = _run(
        monkeypatch,
        rows,
        _bundle(),
        estimate_only=True,
    )
    assert result["estimate_only"] is True
    assert result["source_table"] == "public.emcor_companies"
    assert "discolike" in result["selected_tiers"]
    assert result["selected_tiers"] == ["cache", "discolike", "leadmagic_employee"]
    disco = next(t for t in result["tiers"] if t["tier"] == "discolike")
    assert disco["rows"] == 2
    assert disco["estimated_usd"] == pytest.approx(0.0055 * 2)
    assert disco["unit_usd"] == pytest.approx(0.0055)


def test_job_writes_title_match_and_skips_no_domain(monkeypatch):
    rows = [
        {"domain": "acme.com", "company_name": "Acme", "_source_key": "1"},
        {"domain": "", "company_name": "No Domain LLC", "_source_key": "2"},
    ]
    result = _run(monkeypatch, rows, _bundle(), max_tier="discolike")
    assert result["counts"]["resolved"] == 1
    assert result["counts"]["people_unresolved"] == 1
    assert result["per_tier"]["discolike"]["title_matched"] == 1
    assert result["per_tier"]["discolike"]["calls"] == 1
    assert result["spent_usd"] == pytest.approx(0.0055)
