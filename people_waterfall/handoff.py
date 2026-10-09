"""Hand title_match rows to Email Finder Waterfall in name_company mode."""

from __future__ import annotations

from typing import Any

import requests

from .config import settings
from .profile import ClientProfile


def _mcp_url(base: str) -> str:
    raw = (base or "").rstrip("/")
    if raw.endswith("/mcp"):
        return raw
    return f"{raw}/mcp"


def _mcp_call(url: str, name: str, arguments: dict[str, Any], *, timeout: int = 45) -> requests.Response:
    return requests.post(
        url,
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments},
        },
        timeout=timeout,
    )


def handoff_title_matches(
    profile: ClientProfile, *, max_tier: str = "aiark"
) -> dict[str, Any]:
    """Queue email resolution. People waterfall never finds the address.

    Non-builtin client tags 404 unless the email service has created
    public.{tag}_wf_contacts. Call ensure_client first, then enrich.
    max_tier comes from the caller or profile; default is aiark (never a
    hardcoded vendor name that this service no longer uses).
    """
    source_table = profile.contacts_table
    if "." not in source_table:
        source_table = f"public.{source_table}"
    where = "title_match is not null"
    ceiling = (max_tier or "").strip() or "aiark"
    payload = {
        "client_tag": profile.client_tag,
        "source_table": source_table,
        "where": where,
        "need": "email",
        "max_tier": ceiling,
        "estimate_only": False,
        "mode": "name_company",
        "require_title_match": True,
        "target_titles": ",".join(profile.target_titles),
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
    endpoint = _mcp_url(url)
    try:
        ensured = _mcp_call(
            endpoint,
            "ensure_client",
            {
                "client_tag": profile.client_tag,
                "target_titles": ",".join(profile.target_titles),
            },
        )
        if ensured.status_code >= 400:
            return {
                "queued": False,
                "reason": "ensure_client failed",
                "http_status": ensured.status_code,
                "source_table": source_table,
                "mode": "name_company",
            }
        r = _mcp_call(endpoint, "enrich_waterfall", payload)
        return {
            "queued": r.status_code < 400,
            "http_status": r.status_code,
            "ensured": True,
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
