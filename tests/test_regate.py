from people_waterfall.people import PersonHit
from people_waterfall.profile import parse_profile
from people_waterfall.regate import regate_name_bank
from people_waterfall.write import name_bank_row


PROFILE = parse_profile(
    "emcor",
    {
        "target_titles": ["Lead Pastor", "Pastor", "Dentist", "Owner"],
        "seniority_floor": "manager",
        "title_exclude_regex": r"(assistant|associate|youth|worship)",
        "contacts_table": "emcor_wf_contacts",
    },
)


def test_name_bank_row_records_rejection_reason():
    person = PersonHit(first_name="Ada", last_name="Lovelace", title="Youth Pastor")
    person.rejection_reason = "title"
    row = name_bank_row(client_tag="emcor", domain="acme.com", person=person, source="discolike")
    assert row["rejection_reason"] == "title"
    assert row["status"] == "wrong_title"


def test_regate_promotes_literal_titles_without_vendors(monkeypatch):
    from people_waterfall import regate as mod

    bank = [
        {
            "id": 1,
            "client_tag": "emcor",
            "domain": "church.org",
            "first_name": "Ann",
            "last_name": "Lee",
            "job_title": "Lead Pastor",
            "linkedin_url": "",
            "source": "discolike",
            "status": "wrong_title",
            "rejection_reason": None,
        },
        {
            "id": 2,
            "client_tag": "emcor",
            "domain": "dental.com",
            "first_name": "Bo",
            "last_name": "Kay",
            "job_title": "Dentist",
            "linkedin_url": "",
            "source": "discolike",
            "status": "wrong_title",
        },
        {
            "id": 3,
            "client_tag": "emcor",
            "domain": "church.org",
            "first_name": "Cy",
            "last_name": "Ng",
            "job_title": "Youth Pastor",
            "linkedin_url": "",
            "source": "discolike",
            "status": "wrong_title",
        },
    ]
    contacts: list[dict] = []
    updates: list[dict] = []

    def select(table, params=None):
        params = params or {}
        if table == "emcor_wf_contacts":
            return []
        assert table == "name_bank"
        return bank

    def insert(profile, rows):
        assert profile.contacts_table == "public.emcor_wf_contacts"
        assert profile.contacts_table_name == "emcor_wf_contacts"
        contacts.extend(rows)
        return len(rows)

    def upsert(table, rows, on_conflict):
        assert table == "name_bank"
        assert on_conflict == "client_tag,domain,first_name,last_name"
        updates.extend(rows)
        return len(rows)

    monkeypatch.setattr(mod, "get_profile", lambda tag: PROFILE)
    monkeypatch.setattr(mod.supabase_sync, "rest_select", select)
    monkeypatch.setattr(mod, "write_contacts", insert)
    monkeypatch.setattr(mod.supabase_sync, "rest_upsert", upsert)

    result = regate_name_bank("emcor")
    assert result["examined"] == 3
    assert result["promoted"] == 2
    assert result["written"] == 2
    assert result["by_reason"]["title"] == 1
    assert {row["job_title"] for row in contacts} == {"Lead Pastor", "Dentist"}
    promoted = [row for row in updates if row["status"] == "promoted"]
    assert {row["job_title"] for row in promoted} == {"Lead Pastor", "Dentist"}
    still = [row for row in updates if row["status"] == "wrong_title"]
    assert still[0]["rejection_reason"] == "title"
    assert still[0]["job_title"] == "Youth Pastor"
