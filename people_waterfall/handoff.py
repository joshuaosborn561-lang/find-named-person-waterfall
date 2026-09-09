"""Hand title_match rows to Email Finder Waterfall in name_company mode."""

from __future__ import annotations

from typing import Any

from .config import settings
from .profile import ClientProfile


def handoff_title_matches(profile: ClientProfile) -> dict[str, Any]:
    """Queue email resolution. People waterfall never finds the address.

    When EMAIL_WATERFALL_URL is set the HTTP MCP is invoked. Otherwise we
    record the source_table the email service should read.
    """
    source_table = f"public.{profile.contacts_table}"
    where = "title_match = 'true'"
    payload = {
        "client_tag": profile.client_tag,
        "source_table": source_table,
        "where": "title_match is not null",
        "need": "email",
        "max_tier": "leadmagic",
        "estimate_only": False,
        "mode": "name_company",
        "require_title_match": True,
    }
    url = settings.email_waterfall_url
    if not url:
        return {
            "queued": False,
            "reason": "EMAIL_WATERFALL_URL unset",
            "source_table": source_table,
            "where": where,
            "mode": "name_company",
        }
    try:
        import requests

        r = requests.post(
            f"{url}/mcp",
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": "enrich_waterfall", "arguments": payload},
            },
            timeout=30,
        )
        return {
            "queued": r.status_code < 400,
            "http_status": r.status_code,
            "source_table": source_table,
            "mode": "name_company",
        }
    except Exception as exc:  # noqa: BLE001
        return {
            "queued": False,
            "reason": f"{type(exc).__name__}: {exc}",
            "source_table": source_table,
            "mode": "name_company",
        }
