"""013 must add aiark_person_id without depending on the unique index."""

from pathlib import Path

MIGRATION = Path("supabase/migrations/013_drop_leadmagic_people_tiers.sql")


def _function_body(sql: str, name: str) -> str:
    needle = f"create or replace function public.{name}("
    start = sql.lower().find(needle)
    assert start >= 0, f"missing function {name}"
    rest = sql[start:]
    end = rest.lower().find("$function$;")
    assert end >= 0, f"unclosed function {name}"
    return rest[: end + len("$function$;")]


def test_013_column_helper_adds_aiark_person_id_without_unique_index():
    sql = MIGRATION.read_text()
    columns = _function_body(sql, "pw_ensure_contacts_columns")
    assert "aiark_person_id" in columns
    assert "ADD COLUMN IF NOT EXISTS aiark_person_id" in columns
    assert "CREATE UNIQUE INDEX" not in columns
    assert "CREATE INDEX" not in columns


def test_013_unique_index_helper_skips_and_reports_duplicates():
    sql = MIGRATION.read_text()
    index_fn = _function_body(sql, "pw_ensure_contacts_unique_index")
    columns = _function_body(sql, "pw_ensure_contacts_columns")
    assert index_fn != columns
    assert "skipped_duplicates" in index_fn
    assert "unique_violation" in index_fn
    assert "CREATE UNIQUE INDEX IF NOT EXISTS" in index_fn
    assert "exception" in index_fn.lower()
    assert sql.lower().find("pw_ensure_contacts_columns") < sql.lower().find(
        "pw_ensure_contacts_unique_index"
    )
