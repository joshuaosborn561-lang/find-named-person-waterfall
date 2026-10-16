"""014 PW site_people RPCs must resolve `batch` as the PL/pgSQL variable."""

from pathlib import Path

MIGRATION = Path("supabase/migrations/014_site_people_rpcs.sql")
FUNCTIONS = (
    "pw_site_people_load",
    "pw_site_people_remaining",
    "pw_site_people_roster",
    "pw_site_people_picks",
)


def _function_body(sql: str, name: str) -> str:
    needle = f"create or replace function public.{name}("
    start = sql.lower().find(needle)
    assert start >= 0, f"missing function {name}"
    rest = sql[start:]
    end = rest.lower().find("$function$;")
    assert end >= 0, f"unclosed function {name}"
    return rest[: end + len("$function$;")]


def _body_after_dollar(fn_sql: str) -> str:
    marker = "$function$"
    start = fn_sql.lower().find(marker)
    assert start >= 0
    inner = fn_sql[start + len(marker) :]
    end = inner.lower().find(marker)
    assert end >= 0
    return inner[:end]


def test_014_all_four_functions_set_variable_conflict_use_variable():
    sql = MIGRATION.read_text()
    assert sql.count("#variable_conflict use_variable") == 4
    for name in FUNCTIONS:
        body = _function_body(sql, name)
        inner = _body_after_dollar(body)
        first = inner.strip().splitlines()[0].strip()
        assert first == "#variable_conflict use_variable", name
        assert "DECLARE" in body
        assert "batch text;" in body
