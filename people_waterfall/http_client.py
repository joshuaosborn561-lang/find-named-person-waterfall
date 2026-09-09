"""Shared gated HTTP helpers for vendor clients."""

from __future__ import annotations

import requests

from .concurrency import request_with_retry


def post(
    tier: str,
    url: str,
    *,
    json: object | None = None,
    headers: dict[str, str] | None = None,
    timeout: int = 45,
    data: object | None = None,
    files: object | None = None,
) -> requests.Response | None:
    kwargs: dict[str, object] = {"headers": headers or {}, "timeout": timeout}
    if json is not None:
        kwargs["json"] = json
    if data is not None:
        kwargs["data"] = data
    if files is not None:
        kwargs["files"] = files
    return request_with_retry(tier, "POST", url, **kwargs)


def get(
    tier: str,
    url: str,
    *,
    headers: dict[str, str] | None = None,
    timeout: int = 45,
    params: object | None = None,
) -> requests.Response | None:
    return request_with_retry(
        tier,
        "GET",
        url,
        headers=headers,
        timeout=timeout,
        params=params,
    )
