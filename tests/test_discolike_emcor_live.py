"""Live DiscoLike check against 20 emcor companies.

Skipped unless DISCOLIKE_API_KEY and SUPABASE_SERVICE_ROLE_KEY are set.
"""

from __future__ import annotations

import os

import pytest

from people_waterfall import supabase_sync
from people_waterfall.source import parse_source
from people_waterfall.waterfall import resolve_people


pytestmark = pytest.mark.skipif(
    not (os.environ.get("DISCOLIKE_API_KEY") and os.environ.get("SUPABASE_SERVICE_ROLE_KEY")),
    reason="DISCOLIKE_API_KEY and SUPABASE_SERVICE_ROLE_KEY are required",
)


def test_discolike_only_on_twenty_emcor_rows(monkeypatch):
    from people_waterfall import waterfall as wf

    real_parse = parse_source

    def limited(source_table, where="", **kwargs):
        src = real_parse(source_table, where, **kwargs)
        src.limit = 20
        return src

    monkeypatch.setattr(wf, "parse_source", limited)
    where = "domain is not null and wf_people_status = 'people_unresolved'"
    before = supabase_sync.rest_select(
        "emcor_companies",
        params={
            "select": "id,domain,wf_people_status",
            "domain": "not.is.null",
            "wf_people_status": "eq.people_unresolved",
            "order": "id.asc",
            "limit": "20",
        },
    )
    assert len(before) == 20
    ids = [str(row["id"]) for row in before]

    result = resolve_people(
        source_table="public.emcor_companies",
        where=where,
        client_tag="emcor",
        min_tier="discolike",
        max_tier="discolike",
        estimate_only=False,
        write_supabase=True,
    )

    assert result["per_tier"]["discolike"]["calls"] > 0
    assert result["counts"]["written"] > 0
    assert result["status"] == "completed"
    assert result["counter"]["done"] == 20

    id_list = ",".join(ids)
    after = supabase_sync.rest_select(
        "emcor_companies",
        params={
            "select": "id,wf_people_status,wf_people_source",
            "id": f"in.({id_list})",
        },
    )
    by_id = {str(row["id"]): row for row in after}
    updated = 0
    for row_id in ids:
        status = str((by_id.get(row_id) or {}).get("wf_people_status") or "")
        source = str((by_id.get(row_id) or {}).get("wf_people_source") or "")
        if status and status != "people_unresolved":
            updated += 1
        elif source == "discolike":
            updated += 1
    assert updated > 0
