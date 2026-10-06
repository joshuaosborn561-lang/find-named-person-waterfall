"""Free site_staff tier, where-clause, single-name bank rows, spend pause."""

from people_waterfall.people import PersonHit
from people_waterfall.pricing import LiveRates, compute_tier_order, select_tiers
from people_waterfall.profile import parse_profile
from people_waterfall.site_quality import (
    classify_email,
    email_allowed,
    name_is_valid,
    parse_name_line,
    same_site,
    should_pause_for_site_staff_quality,
    title_is_usable,
)
from people_waterfall.source import where_to_filters
from people_waterfall.vendors.site_staff import (
    decode_cfemail,
    extract_emails,
    parse_html,
    pick_contact,
    title_from_url,
)
from people_waterfall.waterfall import should_pause_for_spend
from people_waterfall.write import name_bank_row, write_name_bank_rows

PROFILE = parse_profile(
    "ifca_manhattan",
    {
        "title_synonyms": {"Rev.": "Pastor", "Sr. Pastor": "Senior Pastor"},
        "title_priority": ["Senior Pastor", "Pastor", "Rabbi"],
        "exclude_titles": ["Youth Pastor"],
        "target_titles": ["Senior Pastor", "Pastor", "Rabbi", "Youth Pastor"],
    },
)


def test_where_accepts_icp_and_nonblank_domain():
    filters = where_to_filters(
        "in_icp is not false and coalesce(domain,'')<>'' and wf_people_status = 'people_unresolved'"
    )
    assert filters[0] == {"col": "in_icp", "op": "is.not.false"}
    assert filters[1] == {"col": "domain", "op": "not.blank"}
    assert filters[2]["op"] == "eq"
    assert filters[2]["value"] == "people_unresolved"


def test_cfemail_xor_and_obfuscation():
    key = 0x22
    raw = bytes([key, *[byte ^ key for byte in b"jane@grace.org"]])
    assert decode_cfemail(raw.hex()) == "jane@grace.org"
    found = extract_emails("Write jane [at] grace [dot] org today")
    assert "jane@grace.org" in found


def test_site_staff_picks_one_personal_and_skips_excluded():
    html = """
    <div class="staff-card">
      <h3>Jane Doe</h3>
      <p>Sr. Pastor</p>
      <a href="mailto:jane.doe@grace.org">email</a>
    </div>
    <div class="staff-card">
      <h3>John Smith</h3>
      <p>Youth Pastor</p>
      <a href="mailto:john@grace.org">email</a>
    </div>
    """
    people = parse_html(html, page_url="https://grace.org/staff")
    picked = pick_contact(people, PROFILE, domain="grace.org", company_name="Grace")
    assert picked.person is not None
    assert picked.person.first_name == "Jane"
    assert picked.person.last_name == "Doe"
    assert picked.email_type == "personal"
    assert picked.person.email == "jane.doe@grace.org"
    assert picked.bank_only is False
    assert picked.person.page_url.endswith("/staff")


def test_role_inbox_attaches_when_no_personal():
    html = """
    <div class="staff-card">
      <h3>Jane Doe</h3>
      <p>Senior Pastor</p>
      <a href="mailto:pastor@grace.org">email</a>
    </div>
    """
    people = parse_html(html, page_url="https://grace.org/staff")
    picked = pick_contact(people, PROFILE, domain="grace.org")
    assert picked.email_type == "role"
    assert picked.person is not None
    assert picked.person.email == "pastor@grace.org"
    assert picked.bank_only is False


def test_title_without_email_is_banked():
    html = """
    <div class="staff-card">
      <h3>Jane Doe</h3>
      <p>Senior Pastor</p>
    </div>
    """
    people = parse_html(html, page_url="https://grace.org/about")
    picked = pick_contact(people, PROFILE, domain="grace.org")
    assert picked.bank_only is True
    assert picked.person is not None
    assert picked.person.name_bank_status == "needs_email"
    assert picked.person.email == ""


def test_paid_vendor_single_name_still_builds_bank_row():
    person = PersonHit(first_name="Madonna", last_name="", title="Pastor")
    row = name_bank_row(client_tag="ifca_manhattan", domain="grace.org", person=person, source="discolike")
    assert row["first_name"] == "Madonna"
    assert row["last_name"] == ""
    assert row["rejection_reason"] == "single_name"


def test_site_staff_name_bank_drops_invalid_names(monkeypatch):
    sent: list[list[dict]] = []

    def _upsert(table, batch, **kwargs):
        sent.append(list(batch))
        return len(batch)

    monkeypatch.setattr("people_waterfall.write.supabase_sync.rest_upsert", _upsert)
    write_name_bank_rows(
        [
            {
                "client_tag": "ifca_manhattan",
                "domain": "grace.org",
                "first_name": "Click",
                "last_name": "Leadership",
                "job_title": "Pastor",
                "source": "site_staff",
                "status": "needs_email",
            },
            {
                "client_tag": "ifca_manhattan",
                "domain": "grace.org",
                "first_name": "Madonna",
                "last_name": "",
                "job_title": "Pastor",
                "source": "site_staff",
                "status": "needs_email",
            },
            {
                "client_tag": "ifca_manhattan",
                "domain": "grace.org",
                "first_name": "Jane",
                "last_name": "Doe",
                "job_title": "Senior Pastor",
                "source": "site_staff",
                "status": "needs_email",
            },
        ]
    )
    assert sent and [row["first_name"] for row in sent[0]] == ["Jane"]


def test_max_tier_site_staff_excludes_paid_tiers():
    order = compute_tier_order(rates=LiveRates(leadmagic_per_credit=0.01))
    assert select_tiers(order, max_tier="site_staff") == ["site_staff"]
    paid = [row["tier"] for row in order if row["tier"] != "site_staff"]
    assert paid == ["cache", "discolike", "leadmagic_employee"]


def test_spend_pause_after_100_companies_with_no_contacts():
    assert should_pause_for_spend(99, 0.5, 0) is False
    assert should_pause_for_spend(100, 0.5, 0) is True
    assert should_pause_for_spend(120, 0.0, 0) is False
    assert should_pause_for_spend(120, 0.5, 1) is False


def test_garbage_nav_and_event_names_are_rejected():
    for raw in (
        "Click leadership",
        "Member Rabbi",
        "Saturday Vigil",
        "CHURCH HISTORY",
        "WELCOME A.M.",
        "Full Time",
        "Instructor, Training)",
    ):
        assert parse_name_line(raw) == [], raw
        parts = raw.replace(")", "").split()
        if len(parts) >= 2:
            assert name_is_valid(parts[0], parts[-1]) is False


def test_couple_shares_last_name_and_honorifics():
    people = parse_name_line("Pastor Gary & Rev. Doreen Comis")
    assert [(p.first_name, p.last_name, p.honorific) for p in people] == [
        ("Gary", "Comis", "Pastor"),
        ("Doreen", "Comis", "Rev."),
    ]


def test_heading_and_other_person_titles_are_not_paired():
    html = """
    <div class="staff-card">
      <h2>PASTOR'S BIBLE STUDY</h2>
      <h3>Meet the Rabbi</h3>
      <p>Ly Bedaña, Director of Music</p>
      <p>Rabbi Leana Moritt</p>
    </div>
    """
    people = parse_html(html, page_url="https://betheljc.org/events")
    names = {(p["first_name"], p["last_name"]) for p in people}
    assert ("Leana", "Moritt") in names
    leana = next(p for p in people if p["last_name"] == "Moritt")
    assert leana["honorific"] == "Rabbi"
    assert title_is_usable(leana["title"])
    assert "study" not in leana["title"].lower()
    assert "meet" not in leana["title"].lower()
    music = [p for p in people if p["last_name"].startswith("Beda")]
    picked = pick_contact(people, PROFILE, domain="betheljc.org")
    assert picked.person is not None
    assert picked.person.first_name == "Leana"
    assert picked.person.last_name == "Moritt"
    if music:
        assert pick_contact(music, PROFILE, domain="betheljc.org").person is None


def test_off_site_emails_are_dropped():
    people = [
        {
            "first_name": "Jane",
            "last_name": "Doe",
            "honorific": "Pastor",
            "title": "Pastor",
            "emails": ["pastor@templebethelnj.org", "jane.doe@grace.org"],
            "page_url": "https://grace.org/staff",
        }
    ]
    picked = pick_contact(
        people,
        PROFILE,
        domain="grace.org",
        domain_emails=["office@jobs.sbc.net", "info@grace.org"],
        allowed_email_domains={"grace.org"},
    )
    assert picked.person is not None
    assert picked.person.email == "jane.doe@grace.org"
    assert picked.email_type == "personal"
    assert picked.generic_emails == ["info@grace.org"]
    assert same_site("https://www.grace.org/staff", "grace.org")
    assert same_site("https://jobs.sbc.net/apply", "grace.org") is False
    assert email_allowed("office@templebethelnj.org", {"grace.org"}) is False


def test_email_types_personal_role_generic():
    assert classify_email("rabbimoritt@betheljc.org", first="Leana", last="Moritt", honorific="Rabbi") == "personal"
    assert classify_email("pastorsmith@grace.org", first="Jane", last="Smith", honorific="Pastor") == "personal"
    assert classify_email("jane.doe@grace.org", first="Jane", last="Doe") == "personal"
    assert classify_email("pastor@grace.org", first="Jane", last="Doe") == "role"
    assert classify_email("religiouseducation@grace.org", first="Jane", last="Doe") == "generic"
    assert classify_email("treasurer@grace.org", first="Jane", last="Doe") == "generic"
    assert classify_email("office@grace.org", first="Jane", last="Doe") == "generic"


def test_generic_email_is_not_written_on_the_person():
    html = """
    <div class="staff-card">
      <h3>Rabbi Leana Moritt</h3>
      <p>Rabbi</p>
      <a href="mailto:religiouseducation@betheljc.org">email</a>
    </div>
    """
    people = parse_html(html, page_url="https://betheljc.org/staff")
    picked = pick_contact(people, PROFILE, domain="betheljc.org")
    assert picked.person is not None
    assert picked.person.email == ""
    assert picked.bank_only is True
    assert "religiouseducation@betheljc.org" in picked.generic_emails


def test_heading_phrases_and_event_titles_are_rejected():
    for raw in (
        "In This Section",
        "St. Mary's Annual Reports",
        "When God Moves",
        "What We Believe",
        "Next Steps",
        "Mission Statement",
        "ORDINATION PROCESS",
        "New Life Trustees",
        "Summer Programming",
    ):
        assert parse_name_line(raw) == [], raw
    assert title_is_usable("Email us at: parishoffice@stmarysharlem.org or rector@stmarysharlem.org") is False
    assert title_is_usable("Rabbi Chaim Wakslak's Second Yahrzeit Siyum, January 28, 2022") is False
    assert title_is_usable("Speaker: Pastor Richmond Aboagye") is False
    assert title_is_usable("Our Senior Pastor") is False
    assert title_is_usable("Sr. Pastor") is True
    assert title_from_url(
        "https://www.yilb.org/our-shul/rabbi-chaim-wakslaks-second-yahrzeit-siyum-january-28-2022/"
    ) == ""
    assert title_from_url("https://faithcc.com/our-pastor") == "Pastor"


def test_pastorkeith_is_personal():
    assert classify_email("pastorkeith@abundantlifewyckoff.org", first="Keith", last="Moody", honorific="Rev.") == "personal"


def test_site_staff_quality_pause():
    assert should_pause_for_site_staff_quality(49, 0, 10) is False
    assert should_pause_for_site_staff_quality(50, 0, 0) is False
    assert should_pause_for_site_staff_quality(50, 8, 10) is False
    assert should_pause_for_site_staff_quality(50, 7, 10) is True
