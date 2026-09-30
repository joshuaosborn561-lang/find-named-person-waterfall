"""DiscoLike persist fixture. Must pass before a deploy."""

import pytest

from people_waterfall.people import PersonHit
from people_waterfall.pricing import LiveRates
from people_waterfall.profile import parse_profile
from people_waterfall.source import TableSource
from people_waterfall.vendors.discolike import DiscoDomainResult
from people_waterfall.waterfall import VendorBundle, resolve_people
from people_waterfall.write import dedupe_person_rows, load_known_names

PROFILE = parse_profile(
    "emcor",
    {
        "target_titles": ["Facility Manager", "Director", "Administrator"],
        "title_synonyms": {},
        "vertical": "mechanical contractors",
        "contacts_table": "public.emcor_wf_contacts",
    },
)


class _Empty:
    enabled = True

    def find_people(self, **kwargs):
        return []

    def employee_finder(self, **kwargs):
        return []


class _Disco:
    def __init__(self):
        self.enabled = True
        self.calls = 0
        self.tasks = 0
        self.seen: list[str] = []

    def resolve_domains(self, domains, **kwargs):
        out = {}
        for domain in domains:
            host = str(domain or "").strip().lower()
            if not host or host in out:
                continue
            people = [
                PersonHit(
                    first_name="Pat",
                    last_name=host.split(".")[0].title(),
                    title="Director",
                    company_name="Co",
                    domain=host,
                    source_tier="discolike",
                )
            ]
            bank = [
                PersonHit(
                    first_name="Casey",
                    last_name="Clerk",
                    title="Clerk",
                    company_name="Co",
                    domain=host,
                    source_tier="discolike",
                )
            ]
            if host == "shared.example":
                # Same person twice. Must become one contact.
                people = [
                    PersonHit(
                        first_name="Jane",
                        last_name="Doe",
                        title="Director",
                        company_name="Shared Co",
                        domain=host,
                        source_tier="discolike",
                    ),
                    PersonHit(
                        first_name="Jane",
                        last_name="Doe",
                        title="Director",
                        company_name="Shared Co",
                        domain=host,
                        source_tier="discolike",
                    ),
                ]
            out[host] = DiscoDomainResult(
                domain=host,
                people=people,
                bank_only=bank,
                email_pattern="{first}@" + host,
                email_pattern_confidence=0.8,
                cost_usd=0.0055,
            )
        self.seen.extend(list(out))
        self.calls += len(out)
        self.tasks += 1
        return out


def _rows() -> list[dict]:
    rows = [
        {"domain": "shared.example", "company_name": "Shared Co", "_source_key": "1"},
        {"domain": "shared.example", "company_name": "Shared Co", "_source_key": "2"},
        {"domain": "", "company_name": "No Domain LLC", "_source_key": "3"},
    ]
    for n in range(4, 21):
        rows.append(
            {
                "domain": f"co{n}.example",
                "company_name": f"Co {n}",
                "_source_key": str(n),
            }
        )
    assert len(rows) == 20
    return rows


def test_discolike_fixture_writes_once_per_domain(monkeypatch):
    from people_waterfall import waterfall as wf

    rows = _rows()
    disco = _Disco()
    contacts: list[dict] = []
    bank: list[dict] = []
    statuses: list[dict] = []
    order: list[str] = []
    src = TableSource(project_id="x", schema="public", table="emcor_companies", writeback=True)

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
    monkeypatch.setattr(wf, "handoff_title_matches", lambda p: {})
    monkeypatch.setattr(wf, "persist_discolike_icp", lambda *a, **k: None)
    monkeypatch.setattr(wf, "merge_people_measured_rates", lambda *a, **k: None)

    def _contacts(profile, accepted):
        order.append("contacts")
        contacts.extend(accepted)
        return len(accepted)

    def _bank(accepted):
        order.append("name_bank")
        bank.extend(accepted)
        return len(accepted)

    def _writeback(_src, source_key, **kwargs):
        order.append("writeback")
        statuses.append({"key": source_key, **kwargs})

    monkeypatch.setattr(wf, "write_contacts", _contacts)
    monkeypatch.setattr(wf, "write_name_bank_rows", _bank)
    monkeypatch.setattr(wf, "writeback_people", _writeback)

    result = resolve_people(
        source_table="public.emcor_companies",
        where="pilot_batch = 'dl1k'",
        client_tag="emcor",
        min_tier="discolike",
        max_tier="discolike",
        write_supabase=True,
        vendors=VendorBundle(cache=_Empty(), leadmagic=_Empty(), discolike=disco),
    )

    assert result["counts"]["written"] > 0
    assert len(contacts) > 0
    assert len(bank) > 0
    contact_keys = [
        (
            str(row.get("domain") or "").lower(),
            str(row.get("first_name") or "").lower(),
            str(row.get("last_name") or "").lower(),
        )
        for row in contacts
    ]
    assert len(contact_keys) == len(set(contact_keys))
    shared = [key for key in contact_keys if key[0] == "shared.example"]
    assert shared == [("shared.example", "jane", "doe")]
    bank_keys = [
        (
            str(row.get("domain") or "").lower(),
            str(row.get("first_name") or "").lower(),
            str(row.get("last_name") or "").lower(),
        )
        for row in bank
    ]
    assert len(bank_keys) == len(set(bank_keys))
    assert ("shared.example", "casey", "clerk") in bank_keys
    assert {row["key"] for row in statuses} == {row["_source_key"] for row in rows}
    assert all(row.get("status") for row in statuses)
    calls = int(result["per_tier"]["discolike"]["calls"])
    assert calls == disco.calls
    assert result["spent_usd"] == pytest.approx(calls * 0.0055)
    assert calls == 18
    first_contacts = order.index("contacts")
    first_bank = order.index("name_bank")
    assert first_contacts < first_bank
    assert "writeback" in order[first_bank + 1 :]


def test_write_contacts_upserts_on_person_key(monkeypatch):
    from people_waterfall import write as write_mod

    seen: dict = {}

    def rpc(name, body):
        assert name == "pw_ensure_contacts_columns"
        assert body["p_table"] == "emcor_wf_contacts"

    def upsert(table, rows, *, on_conflict, batch_size=200):
        seen["table"] = table
        seen["on_conflict"] = on_conflict
        seen["batch_size"] = batch_size
        seen["rows"] = rows
        return len(rows)

    monkeypatch.setattr(write_mod.supabase_sync, "rpc", rpc)
    monkeypatch.setattr(write_mod.supabase_sync, "rest_upsert", upsert)
    monkeypatch.setattr(write_mod.supabase_sync, "rest_insert", lambda *a, **k: (_ for _ in ()).throw(AssertionError("insert")))
    profile = parse_profile("emcor", {"contacts_table": "public.emcor_wf_contacts"})
    n = write_mod.write_contacts(
        profile,
        [
            {
                "client_tag": "emcor",
                "domain": "A.com",
                "first_name": "Jane",
                "last_name": "Doe",
                "title_rank": 3,
            },
            {
                "client_tag": "emcor",
                "domain": "a.com",
                "first_name": "JANE",
                "last_name": "doe",
                "title_rank": 0,
            },
        ],
    )
    assert n == 1
    assert seen["table"] == "emcor_wf_contacts"
    assert seen["on_conflict"] == "client_tag,domain,first_name_key,last_name_key"
    assert seen["batch_size"] == 500
    assert seen["rows"][0]["first_name_key"] == "jane"
    assert seen["rows"][0]["last_name_key"] == "doe"
    assert seen["rows"][0]["title_rank"] == 0


def test_dedupe_keeps_best_title_rank():
    rows = [
        {"client_tag": "emcor", "domain": "A.com", "first_name": "Jane", "last_name": "Doe", "title_rank": 4},
        {"client_tag": "emcor", "domain": "a.com", "first_name": "JANE", "last_name": "doe", "title_rank": 1},
        {"client_tag": "emcor", "domain": "a.com", "first_name": "Jane", "last_name": "Doe", "title_rank": None},
        {"client_tag": "emcor", "domain": "b.com", "first_name": "Sam", "last_name": "Lee", "title_rank": 2},
    ]
    kept = dedupe_person_rows(rows)
    assert len(kept) == 2
    jane = next(row for row in kept if str(row["last_name"]).lower() == "doe")
    assert jane["title_rank"] == 1


def test_load_known_names_does_not_prefix_schema_and_pages(monkeypatch):
    from people_waterfall import write as write_mod

    profile = parse_profile("emcor", {"contacts_table": "public.emcor_wf_contacts", "cache_tables": []})
    calls: list[dict] = []

    def rpc(name, body):
        assert name == "ew_read_source"
        calls.append(dict(body))
        table = body["p_table"]
        if table == "emcor_wf_contacts" and body["p_after"] is None:
            return [
                {"id": i, "domain": "a.com", "first_name": "Ann", "last_name": f"P{i}"}
                for i in range(1, 501)
            ]
        if table == "emcor_wf_contacts":
            return [{"id": 501, "domain": "b.com", "first_name": "Bea", "last_name": "Post"}]
        return []

    monkeypatch.setattr(write_mod.supabase_sync, "rpc", rpc)
    monkeypatch.setattr(write_mod, "_suppression_names", lambda: set())
    known = load_known_names(profile)
    contact_calls = [c for c in calls if c["p_table"] == "emcor_wf_contacts"]
    assert contact_calls[0]["p_schema"] == "public"
    assert contact_calls[0]["p_table"] == "emcor_wf_contacts"
    assert contact_calls[0]["p_key_column"] == "id"
    assert len(contact_calls) == 2
    assert ("a.com", "ann p") in known
    assert ("b.com", "bea post") in known
    assert all(c["p_schema"] != "public" or not str(c["p_table"]).startswith("public") for c in calls)


def test_start_generate_persists_task_before_return(monkeypatch):
    from people_waterfall.vendors import discolike as mod
    from people_waterfall.vendors.discolike import DiscoLikeClient

    client = DiscoLikeClient(api_key="tok")
    seen: list[str] = []
    client.on_task_started = seen.append

    class _Resp:
        def __init__(self, payload, status=200):
            self.status_code = status
            self._payload = payload
            self.text = ""

        def json(self):
            return self._payload

    monkeypatch.setattr(
        mod.http_client,
        "get",
        lambda *a, **k: _Resp({"providers": [{"provider": "serper", "integration_id": "serper-1"}]}),
    )
    monkeypatch.setattr(mod.http_client, "post", lambda *a, **k: _Resp({"task_id": "task-9"}))
    assert client.start_generate(["acme.com"], icp_text="directors") == "task-9"
    assert seen == ["task-9"]


def test_resume_rereads_status_without_generate(monkeypatch):
    from people_waterfall import waterfall as wf
    from people_waterfall.vendors import discolike as mod

    posts: list[str] = []
    gets: list[str] = []

    class _Resp:
        def __init__(self, payload, status=200):
            self.status_code = status
            self._payload = payload
            self.text = ""

        def json(self):
            return self._payload

    def fake_get(tier, url, **kwargs):
        gets.append(url)
        return _Resp(
            {
                "status": "completed",
                "results": {
                    "acme.com": {
                        "contacts": [
                            {
                                "name": "Jane Doe",
                                "title": "Director",
                                "discovered_domain": "acme.com",
                                "match_status": "match",
                            }
                        ]
                    }
                },
            }
        )

    def fake_post(tier, url, **kwargs):
        posts.append(url)
        raise AssertionError("resume must not start a generate")

    monkeypatch.setattr(mod.http_client, "get", fake_get)
    monkeypatch.setattr(mod.http_client, "post", fake_post)
    from dataclasses import replace

    from people_waterfall import config as cfg

    monkeypatch.setattr(cfg, "settings", replace(cfg.settings, discolike_api_key="tok"))

    rows = [{"domain": "acme.com", "company_name": "Acme", "_source_key": "9"}]
    src = TableSource(project_id="x", schema="public", table="emcor_companies", writeback=True)
    monkeypatch.setattr(wf, "get_profile", lambda tag: PROFILE)
    monkeypatch.setattr(wf, "parse_source", lambda *a, **k: src)
    monkeypatch.setattr(wf, "count_source", lambda s: 1)
    monkeypatch.setattr(wf, "count_source_with_domain", lambda s: 1)
    monkeypatch.setattr(wf, "iter_source", lambda s: iter(rows))
    monkeypatch.setattr(wf, "read_live_rates", lambda *a, **k: LiveRates())
    monkeypatch.setattr(wf, "ensure_people_writeback", lambda s: None)
    monkeypatch.setattr(wf, "load_known_names", lambda p: set())
    monkeypatch.setattr(wf, "handoff_title_matches", lambda p: {})
    monkeypatch.setattr(wf, "persist_discolike_icp", lambda *a, **k: None)
    monkeypatch.setattr(wf, "merge_people_measured_rates", lambda *a, **k: None)
    monkeypatch.setattr(wf, "write_contacts", lambda p, rows: len(rows))
    monkeypatch.setattr(wf, "write_name_bank_rows", lambda rows: len(rows))
    monkeypatch.setattr(wf, "writeback_people", lambda *a, **k: None)

    result = wf.resume_discolike_task(
        "c059d0fa-2ed2-42f9-98fe-ce9850bcfa47",
        "emcor",
        "public.emcor_companies",
        "pilot_batch = 'dl1k'",
    )
    assert posts == []
    assert any("/discogen/status/c059d0fa-2ed2-42f9-98fe-ce9850bcfa47" in url for url in gets)
    assert result["spent_usd"] == 0.0
    assert result["billing"] == "free_reread"
    assert result["counts"]["written"] == 1
    assert result["resumed_task_id"] == "c059d0fa-2ed2-42f9-98fe-ce9850bcfa47"
