from people_waterfall.titles import audit_title, title_matches


SYNONYMS = {"PM": "Project Manager", "Super": "Superintendent", "Precon": "Preconstruction"}
TARGETS = [
    "Owner",
    "President",
    "Project Manager",
    "Superintendent",
    "Preconstruction Manager",
]


def test_synonym_pm_matches_project_manager():
    assert title_matches("Senior PM", "Project Manager", SYNONYMS)


def test_exclude_regex_drops_c_suite():
    audit = audit_title(
        "CEO",
        target_titles=["IT Manager", "CEO"],
        title_exclude_regex=r"CEO|CFO|COO|President",
    )
    assert audit.excluded is True
    assert audit.title_match is False


def test_title_rank_is_index():
    audit = audit_title("Superintendent", target_titles=TARGETS, title_synonyms=SYNONYMS)
    assert audit.title_match is True
    assert audit.title_rank == 3


def test_seniority_floor_rejects_below():
    audit = audit_title(
        "Coordinator",
        target_titles=["Coordinator", "Manager"],
        seniority_floor="manager",
    )
    assert audit.below_floor is True
    assert audit.title_match is False


def test_goliath_it_manager_kept_ceo_dropped():
    titles = ["IT Manager", "IT Director", "Sysadmin"]
    keep = audit_title(
        "IT Manager",
        target_titles=titles,
        title_exclude_regex=r"CEO|CFO|COO|President",
    )
    drop = audit_title(
        "Chief Executive Officer",
        target_titles=titles + ["CEO"],
        title_exclude_regex=r"CEO|CFO|COO|President",
    )
    assert keep.title_match is True
    assert drop.title_match is False


def test_exclude_president_does_not_drop_vice_president_of_it():
    audit = audit_title(
        "Vice President of IT",
        target_titles=["VP of IT", "IT Manager"],
        title_exclude_regex=r"CEO|CFO|COO|President",
    )
    assert audit.excluded is False
    assert audit.title_match is True
