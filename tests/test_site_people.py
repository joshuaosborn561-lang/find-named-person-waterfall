"""site_people load/pick/unpicked, DiscoLike filter, and missing-key fail-fast."""

import threading
import time

import pytest
import requests

from people_waterfall.people import PersonHit
from people_waterfall.preflight import ToolError, require_site_people_key, site_people_key
from people_waterfall.pricing import LiveRates
from people_waterfall.profile import parse_profile
from people_waterfall.source import TableSource
from people_waterfall.vendors import site_people as site_people_mod
from people_waterfall.vendors.site_people import (
    CUT_ORG_TYPES,
    SitePeopleBatchResult,
    SitePeopleClient,
    SitePeopleDomainResult,
    _HttpPressure,
    _SlotBusy,
    batch_name,
    discolike_eligible,
    plan_wave,
    pressure_backoff_s,
    title_ignored,
    whole_word_rank,
)
from people_waterfall.waterfall import VendorBundle, resolve_people
from tests.fakes import EmptyGetleads


PROFILE = parse_profile(
    "emcor",
    {
        "target_titles": ["Owner", "President", "Director"],
        "contacts_table": "emcor_wf_contacts",
    },
)


class _Empty:
    enabled = True

    def find_people(self, **kwargs):
        return []


class _Disco:
    enabled = True
    calls = 0
    seen: list[str]

    def __init__(self):
        self.seen = []
        self.calls = 0

    def resolve_domains(self, domains, **kwargs):
        from people_waterfall.vendors.discolike import DiscoDomainResult

        self.calls += len(domains)
        self.seen.extend(list(domains))
        return {d: DiscoDomainResult(domain=d, cost_usd=0.0055) for d in domains}


class _NoopPaid:
    enabled = True
    calls = 0
    last_credits_used = 0.0
    last_error = ""
    seen: list[str]

    def __init__(self):
        self.seen = []
        self.calls = 0

    def find_people(self, **kwargs):
        self.seen.append(kwargs.get("domain") or "")
        self.calls += 1
        return []

    def search_people(self, domains, **kwargs):
        self.seen.extend(list(domains))
        self.calls += 1
        return {d: [] for d in domains}


def _run(monkeypatch, rows, vendors, **kwargs):
    from people_waterfall import waterfall as wf

    src = TableSource(project_id="x", schema="public", table="emcor_companies", writeback=False)
    monkeypatch.setattr(wf, "preflight_job", lambda **k: {"ok": True, "writeback": "columns"})
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
    monkeypatch.setattr(wf, "write_name_bank_rows", lambda rows: len(rows))
    monkeypatch.setattr(wf, "handoff_title_matches", lambda p: {})
    monkeypatch.setattr(wf, "writeback_people", lambda *a, **k: None)
    monkeypatch.setattr(wf, "persist_discolike_icp", lambda *a, **k: None)
    monkeypatch.setattr(wf, "merge_people_measured_rates", lambda *a, **k: None)
    return resolve_people(
        source_table="public.emcor_companies",
        where="domain is not null",
        client_tag="emcor",
        write_supabase=False,
        vendors=vendors,
        **kwargs,
    )


def test_missing_site_people_key_fails_fast(monkeypatch):
    monkeypatch.delenv("SITE_PEOPLE_KEY", raising=False)
    assert site_people_key() == ""
    with pytest.raises(ToolError, match="SITE_PEOPLE_KEY is missing or empty") as exc:
        require_site_people_key()
    assert exc.value.stage == "site_people_key"


def test_empty_site_people_key_fails_fast(monkeypatch):
    monkeypatch.setenv("SITE_PEOPLE_KEY", "   ")
    with pytest.raises(ToolError, match="SITE_PEOPLE_KEY is missing or empty"):
        require_site_people_key()


def test_resolve_people_missing_key_never_calls_edge(monkeypatch):
    monkeypatch.delenv("SITE_PEOPLE_KEY", raising=False)
    called: list[str] = []

    class Spy:
        enabled = True

        def resolve_domains(self, *args, **kwargs):
            called.append("resolve")
            raise AssertionError("site_people must not run with an empty key")

        def _call_edge(self, *args, **kwargs):
            called.append("edge")
            raise AssertionError("edge must not run with an empty key")

    rows = [{"domain": "acme.com", "company_name": "Acme", "_source_key": "1"}]
    with pytest.raises(ToolError, match="SITE_PEOPLE_KEY is missing or empty"):
        _run(
            monkeypatch,
            rows,
            VendorBundle(
                cache=_Empty(),
                discolike=_Disco(),
                site_people=Spy(),
                getleads=EmptyGetleads(),
                prospeo=_NoopPaid(),
                aiark=_NoopPaid(),
            ),
            max_tier="site_people",
        )
    assert called == []


def test_resolve_people_empty_key_never_calls_edge(monkeypatch):
    monkeypatch.setenv("SITE_PEOPLE_KEY", "")
    rows = [{"domain": "acme.com", "company_name": "Acme", "_source_key": "1"}]
    client = SitePeopleClient()
    with pytest.raises(ToolError, match="SITE_PEOPLE_KEY is missing or empty"):
        _run(
            monkeypatch,
            rows,
            VendorBundle(
                cache=_Empty(),
                discolike=_Disco(),
                site_people=client,
                getleads=EmptyGetleads(),
                prospeo=_NoopPaid(),
                aiark=_NoopPaid(),
            ),
            max_tier="site_people",
        )
    assert client.calls == 0


def test_skip_site_people_does_not_require_key(monkeypatch):
    monkeypatch.delenv("SITE_PEOPLE_KEY", raising=False)
    rows = [{"domain": "acme.com", "company_name": "Acme", "_source_key": "1"}]
    result = _run(
        monkeypatch,
        rows,
        VendorBundle(
            cache=_Empty(),
            discolike=_Disco(),
            getleads=EmptyGetleads(),
            prospeo=_NoopPaid(),
            aiark=_NoopPaid(),
        ),
        skip_tiers="site_people",
        max_tier="cache",
        estimate_only=True,
    )
    assert "site_people" not in result["selected_tiers"]


def test_batch_name_is_counts_safe():
    assert batch_name("Emcor", "AbC123") == "fnp_emcor_abc123"


def test_title_ignore_and_custom_rank():
    assert title_ignored("Assistant Pastor") is True
    assert title_ignored("Owner") is False
    assert whole_word_rank("Owner / Operator", ["Owner", "President"]) == 1
    assert whole_word_rank("Vice President", ["President"]) is None
    assert whole_word_rank("Facilities Director", ["Owner", "Director"]) == 2


def test_discolike_skips_cut_org_and_picks():
    assert discolike_eligible("church", picked=False) is True
    assert discolike_eligible("", picked=False) is True
    assert discolike_eligible("school", picked=False) is False
    assert discolike_eligible("nonprofit", picked=False) is False
    assert discolike_eligible("church", picked=True) is False
    assert CUT_ORG_TYPES == {
        "school",
        "nonprofit",
        "association",
        "government",
        "directory_or_unrelated",
    }


def test_load_reuse_does_not_reset_fresh_http_status(monkeypatch):
    posted: list[dict] = []

    def fake_rpc(name, body=None):
        posted.append({"name": name, "body": body or {}})
        if name == "pw_site_people_load":
            return {"loaded": 2, "reused": 1, "reset": 1, "inserted": 0}
        if name == "pw_site_people_remaining":
            return {
                "fetch_remaining": 0,
                "extract_remaining": 0,
                "fetched_ok": 2,
                "extracted_ok": 1,
                "dollars": 0.0,
            }
        if name == "pw_site_people_roster":
            return []
        if name == "pw_site_people_picks":
            return []
        return {}

    monkeypatch.setenv("SITE_PEOPLE_KEY", "test-key")
    monkeypatch.setattr("people_waterfall.vendors.site_people.supabase_sync.rpc", fake_rpc)
    client = SitePeopleClient()
    loaded = client.load_domains(["acme.com", "beta.com"], "fnp_emcor_job1")
    assert loaded["reused"] == 1
    assert loaded["reset"] == 1
    assert posted[0]["name"] == "pw_site_people_load"
    assert posted[0]["body"]["p_reuse_days"] == 30
    assert posted[0]["body"]["p_batch"] == "fnp_emcor_job1"


def test_picks_and_unpicked_and_backups(monkeypatch):
    monkeypatch.setenv("SITE_PEOPLE_KEY", "test-key")

    def fake_rpc(name, body=None):
        if name == "pw_site_people_load":
            return {"loaded": 2}
        if name == "pw_site_people_remaining":
            return {
                "fetch_remaining": 0,
                "extract_remaining": 0,
                "fetched_ok": 2,
                "extracted_ok": 2,
                "dollars": 0.12,
            }
        if name == "pw_site_people_roster":
            return [
                {
                    "domain": "acme.com",
                    "person_key": "jane",
                    "full_name": "Jane Doe",
                    "title": "Owner",
                    "org_name": "Acme Trade",
                    "org_type": "business",
                    "fetched": True,
                    "extracted": True,
                },
                {
                    "domain": "acme.com",
                    "person_key": "john",
                    "full_name": "John Doe",
                    "title": "Owner",
                    "org_name": "Acme Trade",
                    "org_type": "business",
                    "fetched": True,
                    "extracted": True,
                },
                {
                    "domain": "beta.com",
                    "org_name": "",
                    "org_type": "",
                    "fetched": True,
                    "extracted": False,
                },
            ]
        if name == "pw_site_people_picks":
            return [
                {
                    "domain": "acme.com",
                    "person_key": "jane",
                    "full_name": "Jane Doe",
                    "title": "Owner",
                    "org_name": "Acme Trade",
                    "ties": 2,
                    "rnk": 1,
                }
            ]
        return {}

    monkeypatch.setattr("people_waterfall.vendors.site_people.supabase_sync.rpc", fake_rpc)
    client = SitePeopleClient()
    packed = client.resolve_domains(
        ["acme.com", "beta.com"],
        profile=PROFILE,
        batch="fnp_emcor_job1",
    )
    assert packed.picked == 1
    assert packed.loaded == 2
    acme = packed.by_domain["acme.com"]
    assert acme.picked is True
    assert acme.pick is not None
    assert acme.pick.first_name == "Jane"
    assert acme.org_name == "Acme Trade"
    assert acme.ties == 2
    assert len(acme.backups) == 1
    assert acme.backups[0].backup is True
    beta = packed.by_domain["beta.com"]
    assert beta.picked is False
    assert discolike_eligible(beta.org_type, picked=beta.picked) is True


def test_job_writes_unpicked_and_skips_disco_for_cut_org(monkeypatch):
    monkeypatch.setenv("SITE_PEOPLE_KEY", "test-key")
    disco = _Disco()

    class FakeSite:
        enabled = True
        calls = 0

        def resolve_domains(self, domains, **kwargs):
            self.calls += 1
            return SitePeopleBatchResult(
                by_domain={
                    "church.org": SitePeopleDomainResult(
                        domain="church.org",
                        pick=PersonHit(
                            first_name="Pat",
                            last_name="Lead",
                            title="Pastor",
                            domain="church.org",
                            company_name="Spoken Name",
                            source_tier="site_people",
                        ),
                        org_name="Spoken Name",
                        org_type="church",
                        picked=True,
                        extracted=True,
                    ),
                    "school.edu": SitePeopleDomainResult(
                        domain="school.edu",
                        org_type="school",
                        org_name="A School",
                        picked=False,
                        extracted=True,
                    ),
                    "open.com": SitePeopleDomainResult(
                        domain="open.com",
                        org_type="",
                        picked=False,
                        fetched=False,
                        extracted=False,
                    ),
                },
                loaded=3,
                fetched=2,
                extracted=2,
                picked=1,
            )

    rows = [
        {"domain": "church.org", "company_name": "Legal Church LLC", "_source_key": "1"},
        {"domain": "school.edu", "company_name": "Legal School", "_source_key": "2"},
        {"domain": "open.com", "company_name": "Open Co", "_source_key": "3"},
    ]
    result = _run(
        monkeypatch,
        rows,
        VendorBundle(
            cache=_Empty(),
            discolike=disco,
            site_people=FakeSite(),
            getleads=EmptyGetleads(),
            prospeo=_NoopPaid(),
            aiark=_NoopPaid(),
        ),
        max_tier="discolike",
    )
    assert result["site_people"]["picked"] == 1
    assert result["site_people"]["loaded"] == 3
    assert "school.edu" not in disco.seen
    assert "church.org" not in disco.seen
    assert "open.com" in disco.seen
    assert result["site_people"]["sent_on"] == 1


def test_client_refuses_empty_key_before_http(monkeypatch):
    monkeypatch.delenv("SITE_PEOPLE_KEY", raising=False)
    client = SitePeopleClient()
    with pytest.raises(ToolError, match="SITE_PEOPLE_KEY is missing or empty"):
        client.resolve_domains(["acme.com"], batch="fnp_x_1")
    assert client.calls == 0


class _Resp:
    def __init__(self, status_code: int, text: str = ""):
        self.status_code = status_code
        self.text = text
        self.headers = {}


def test_plan_wave_splits_small_flush_across_pool():
    assert plan_wave(50, 70, 6) == (6, 9)
    assert plan_wave(1000, 70, 6) == (6, 70)
    assert plan_wave(0, 70, 6) == (0, 70)
    assert plan_wave(3, 70, 6) == (3, 1)
    assert plan_wave(12, 70, 12) == (12, 1)


def test_pressure_backoff_includes_jitter(monkeypatch):
    monkeypatch.setattr(site_people_mod.random, "uniform", lambda a, b: 0.25)
    slot = pressure_backoff_s("slot", 1)
    http = pressure_backoff_s("http", 1)
    assert slot == pytest.approx(60.25)
    assert http == pytest.approx(5.25)
    assert pressure_backoff_s("http", 2) == pytest.approx(10.25)


def test_call_edge_classifies_slot_429_and_5xx(monkeypatch):
    monkeypatch.setenv("SITE_PEOPLE_KEY", "test-key")
    client = SitePeopleClient()

    monkeypatch.setattr(
        site_people_mod.requests,
        "get",
        lambda *a, **k: _Resp(200, "remaining connection slots are reserved"),
    )
    with pytest.raises(_SlotBusy):
        client._call_edge(mode="fetch", batch="fnp_t_1", n=8, w=8)

    monkeypatch.setattr(
        site_people_mod.requests, "get", lambda *a, **k: _Resp(429, "slow down")
    )
    with pytest.raises(_HttpPressure, match="429"):
        client._call_edge(mode="fetch", batch="fnp_t_1", n=8, w=8)

    monkeypatch.setattr(
        site_people_mod.requests, "get", lambda *a, **k: _Resp(503, "unavailable")
    )
    with pytest.raises(_HttpPressure, match="503"):
        client._call_edge(mode="extract", batch="fnp_t_1", n=8, w=8)

    monkeypatch.setattr(
        site_people_mod.requests, "get", lambda *a, **k: _Resp(400, "bad batch")
    )
    with pytest.raises(RuntimeError, match="400"):
        client._call_edge(mode="fetch", batch="fnp_t_1", n=8, w=8)


def test_poll_fetch_runs_edge_calls_in_parallel(monkeypatch):
    monkeypatch.setenv("SITE_PEOPLE_KEY", "test-key")
    monkeypatch.setenv("SITE_PEOPLE_CONCURRENCY", "6")
    left = {"n": 12}
    lock = threading.Lock()
    active = {"n": 0, "max": 0}
    ns: list[int] = []

    def fake_remaining(batch):
        with lock:
            return {"fetch_remaining": left["n"], "extract_remaining": 0}

    def fake_edge(*, mode, batch, n, w):
        with lock:
            active["n"] += 1
            active["max"] = max(active["max"], active["n"])
            ns.append(n)
            take = min(n, left["n"])
            left["n"] -= take
        time.sleep(0.08)
        with lock:
            active["n"] -= 1
        return "ok"

    client = SitePeopleClient()
    monkeypatch.setattr(client, "remaining", fake_remaining)
    monkeypatch.setattr(client, "_call_edge", fake_edge)
    client.poll_fetch("fnp_t_pool")
    assert left["n"] == 0
    assert active["max"] >= 2
    assert active["max"] <= 6
    assert client.wave_sizes[0] <= 6
    assert ns
    assert max(ns) <= 70


def test_slot_pressure_backs_off_and_reduces_limit(monkeypatch):
    monkeypatch.setenv("SITE_PEOPLE_KEY", "test-key")
    monkeypatch.setenv("SITE_PEOPLE_CONCURRENCY", "6")
    sleeps: list[float] = []
    monkeypatch.setattr(site_people_mod.time, "sleep", lambda s: sleeps.append(s))
    monkeypatch.setattr(site_people_mod.random, "uniform", lambda a, b: 0.0)
    left = {"n": 1}
    pressure = {"n": 1}

    def fake_remaining(batch):
        return {"fetch_remaining": left["n"]}

    def fake_edge(*, mode, batch, n, w):
        if pressure["n"] > 0:
            pressure["n"] -= 1
            raise _SlotBusy("remaining connection slots are reserved")
        take = min(n, left["n"])
        left["n"] -= take
        return "ok"

    client = SitePeopleClient()
    monkeypatch.setattr(client, "remaining", fake_remaining)
    monkeypatch.setattr(client, "_call_edge", fake_edge)
    client.poll_fetch("fnp_t_slot")
    assert sleeps
    assert sleeps[0] >= 60.0
    assert client._live_limit == 3
    assert left["n"] == 0


def test_http_pressure_reduces_limit_instead_of_fanning_out(monkeypatch):
    monkeypatch.setenv("SITE_PEOPLE_KEY", "test-key")
    monkeypatch.setenv("SITE_PEOPLE_CONCURRENCY", "8")
    sleeps: list[float] = []
    monkeypatch.setattr(site_people_mod.time, "sleep", lambda s: sleeps.append(s))
    monkeypatch.setattr(site_people_mod.random, "uniform", lambda a, b: 0.0)
    left = {"n": 1}
    pressure = {"n": 1}

    def fake_remaining(batch):
        return {"fetch_remaining": left["n"]}

    def fake_edge(*, mode, batch, n, w):
        if pressure["n"] > 0:
            pressure["n"] -= 1
            raise _HttpPressure("site-people fetch failed (429): slow down")
        left["n"] -= min(n, left["n"])
        return "ok"

    client = SitePeopleClient()
    monkeypatch.setattr(client, "remaining", fake_remaining)
    monkeypatch.setattr(client, "_call_edge", fake_edge)
    client.poll_fetch("fnp_t_429")
    assert sleeps
    assert client._live_limit == 4
    client.poll_extract("fnp_t_429")
    assert client._live_limit == 4


def test_reduced_limit_does_not_fan_out(monkeypatch):
    monkeypatch.setenv("SITE_PEOPLE_KEY", "test-key")
    monkeypatch.setenv("SITE_PEOPLE_CONCURRENCY", "6")
    left = {"n": 8}
    lock = threading.Lock()
    active = {"n": 0, "max": 0}

    def fake_remaining(batch):
        with lock:
            return {"fetch_remaining": left["n"]}

    def fake_edge(*, mode, batch, n, w):
        with lock:
            active["n"] += 1
            active["max"] = max(active["max"], active["n"])
            left["n"] -= min(n, left["n"])
        time.sleep(0.05)
        with lock:
            active["n"] -= 1
        return "ok"

    client = SitePeopleClient()
    client._live_limit = 1
    monkeypatch.setattr(client, "remaining", fake_remaining)
    monkeypatch.setattr(client, "_call_edge", fake_edge)
    client.poll_fetch("fnp_t_nofan")
    assert left["n"] == 0
    assert active["max"] == 1
    assert all(size <= 1 for size in client.wave_sizes)


def test_pressure_exhaust_raises(monkeypatch):
    monkeypatch.setenv("SITE_PEOPLE_KEY", "test-key")
    monkeypatch.setenv("SITE_PEOPLE_CONCURRENCY", "4")
    monkeypatch.setattr(site_people_mod.time, "sleep", lambda s: None)
    monkeypatch.setattr(site_people_mod.random, "uniform", lambda a, b: 0.0)

    def fake_remaining(batch):
        return {"fetch_remaining": 1}

    def fake_edge(*, mode, batch, n, w):
        raise _SlotBusy("remaining connection slots are reserved")

    client = SitePeopleClient()
    monkeypatch.setattr(client, "remaining", fake_remaining)
    monkeypatch.setattr(client, "_call_edge", fake_edge)
    with pytest.raises(RuntimeError, match="exhausted connection-slot backoff"):
        client.poll_fetch("fnp_t_exhaust")
    assert client._live_limit == 1


def test_resolve_domains_keeps_db_writes_batched(monkeypatch):
    monkeypatch.setenv("SITE_PEOPLE_KEY", "test-key")
    posted: list[str] = []
    hosts = [f"site-{i}.test" for i in range(6)]

    def fake_rpc(name, body=None):
        posted.append(name)
        if name == "pw_site_people_load":
            assert body["p_domains"] == hosts
            return {"loaded": len(hosts), "reused": 0, "reset": 0, "inserted": len(hosts)}
        if name == "pw_site_people_remaining":
            return {
                "fetch_remaining": 0,
                "extract_remaining": 0,
                "fetched_ok": len(hosts),
                "extracted_ok": len(hosts),
                "dollars": 0.0,
            }
        if name == "pw_site_people_roster":
            return [
                {
                    "domain": host,
                    "person_key": "",
                    "full_name": "",
                    "title": "",
                    "org_name": "",
                    "org_type": "",
                    "fetched": True,
                    "extracted": True,
                }
                for host in hosts
            ]
        if name == "pw_site_people_picks":
            return []
        return {}

    monkeypatch.setattr("people_waterfall.vendors.site_people.supabase_sync.rpc", fake_rpc)
    client = SitePeopleClient()
    packed = client.resolve_domains(hosts, batch="fnp_t_batch")
    assert packed.loaded == 6
    assert posted.count("pw_site_people_load") == 1
    assert posted.count("pw_site_people_roster") == 1
    assert posted.count("pw_site_people_picks") == 1
    assert "pw_site_people_load" == posted[0]


def test_call_edge_treats_request_exception_as_pressure(monkeypatch):
    monkeypatch.setenv("SITE_PEOPLE_KEY", "test-key")
    client = SitePeopleClient()

    def boom(*args, **kwargs):
        raise requests.ConnectionError("down")

    monkeypatch.setattr(site_people_mod.requests, "get", boom)
    with pytest.raises(_HttpPressure, match="request failed"):
        client._call_edge(mode="fetch", batch="fnp_t_1", n=8, w=8)
    assert "test-key" not in (client.last_error or "")
