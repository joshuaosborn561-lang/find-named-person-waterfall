"""Free site_staff tier, where-clause, single-name bank rows, spend pause."""

from people_waterfall.people import PersonHit
from people_waterfall.pricing import LiveRates, compute_tier_order, select_tiers
from people_waterfall.profile import parse_profile
from people_waterfall.source import where_to_filters
from people_waterfall.vendors.site_staff import (
    decode_cfemail,
    extract_emails,
    parse_html,
    pick_contact,
)
from people_waterfall.waterfall import should_pause_for_spend
from people_waterfall.write import name_bank_row

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


def test_single_name_banks_empty_last_name():
    person = PersonHit(first_name="Madonna", last_name="", title="Pastor")
    row = name_bank_row(client_tag="ifca_manhattan", domain="grace.org", person=person, source="site_staff")
    assert row["first_name"] == "Madonna"
    assert row["last_name"] == ""
    assert row["rejection_reason"] == "single_name"


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
