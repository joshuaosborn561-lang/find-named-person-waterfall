"""PostgREST helpers. Never return lead payloads to MCP callers."""

from __future__ import annotations

import json
from typing import Any
from urllib import error, parse, request

from .config import DEFAULT_SUPABASE_PROJECT, load_settings

BATCH_SIZE = 200


def supabase_config() -> dict[str, str]:
    cfg = load_settings()
    url = cfg.supabase_url.rstrip("/")
    key = cfg.supabase_key
    if not url or not key:
        raise RuntimeError(
            "Supabase not configured. Set SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY."
        )
    return {"url": url, "key": key}


def _headers(key: str, *, prefer: str, extra: dict[str, str] | None = None) -> dict[str, str]:
    headers = {
        "apikey": key,
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
        "Prefer": prefer,
        "Accept": "application/json",
    }
    if extra:
        headers.update(extra)
    return headers


def request_on(
    method: str,
    path: str,
    *,
    url: str,
    key: str,
    body: Any = None,
    prefer: str = "return=minimal",
    extra_headers: dict[str, str] | None = None,
    query: dict[str, str] | None = None,
) -> tuple[int, str]:
    qs = f"?{parse.urlencode(query, safe='.,*')}" if query else ""
    endpoint = f"{url.rstrip('/')}/rest/v1/{path.lstrip('/')}{qs}"
    data = None if body is None else json.dumps(body, default=str).encode("utf-8")
    req = request.Request(
        endpoint,
        data=data,
        headers=_headers(key, prefer=prefer, extra=extra_headers),
        method=method,
    )
    try:
        with request.urlopen(req, timeout=120) as resp:
            return resp.status, resp.read().decode("utf-8", errors="replace")
    except error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(
            f"Supabase {method} {endpoint} failed ({exc.code}): {detail[:500]}"
        ) from exc


def _request(
    method: str,
    path: str,
    *,
    body: Any = None,
    prefer: str = "return=minimal",
    extra_headers: dict[str, str] | None = None,
    query: dict[str, str] | None = None,
) -> tuple[int, str]:
    cfg = supabase_config()
    return request_on(
        method,
        path,
        url=cfg["url"],
        key=cfg["key"],
        body=body,
        prefer=prefer,
        extra_headers=extra_headers,
        query=query,
    )


def rpc(name: str, body: dict[str, Any] | None = None) -> Any:
    _status, text = _request(
        "POST",
        f"rpc/{name}",
        body=body or {},
        prefer="return=representation",
    )
    if not text:
        return None
    try:
        return json.loads(text)
    except ValueError:
        return text


def rest_select(
    table: str,
    *,
    params: dict[str, str] | None = None,
    extra_headers: dict[str, str] | None = None,
) -> list[dict[str, Any]]:
    _status, text = _request(
        "GET",
        table,
        prefer="return=representation",
        extra_headers=extra_headers,
        query=params,
    )
    if not text:
        return []
    data = json.loads(text)
    return data if isinstance(data, list) else []


def rest_insert(table: str, rows: list[dict[str, Any]]) -> int:
    if not rows:
        return 0
    written = 0
    for i in range(0, len(rows), BATCH_SIZE):
        batch = rows[i : i + BATCH_SIZE]
        _request("POST", table, body=batch, prefer="return=minimal")
        written += len(batch)
    return written


def rest_upsert(
    table: str,
    rows: list[dict[str, Any]],
    *,
    on_conflict: str,
) -> int:
    if not rows:
        return 0
    written = 0
    for i in range(0, len(rows), BATCH_SIZE):
        batch = rows[i : i + BATCH_SIZE]
        _request(
            "POST",
            f"{table}?on_conflict={on_conflict}",
            body=batch,
            prefer="resolution=merge-duplicates,return=minimal",
        )
        written += len(batch)
    return written


def rest_patch(
    table: str,
    *,
    params: dict[str, str],
    body: dict[str, Any],
) -> None:
    _request("PATCH", table, body=body, query=params, prefer="return=minimal")


def load_private_api_keys() -> list[dict[str, str]]:
    """Read private.api_keys via security-definer RPC when present."""
    try:
        data = rpc("pw_read_api_keys", {})
    except RuntimeError:
        return []
    if isinstance(data, list):
        return [
            {"name": str(r.get("name") or ""), "value": str(r.get("value") or "")}
            for r in data
            if isinstance(r, dict)
        ]
    return []


def project_ref() -> str:
    cfg = load_settings()
    host = cfg.supabase_url.split("//", 1)[-1].split("/", 1)[0]
    return host.split(".")[0] or DEFAULT_SUPABASE_PROJECT
