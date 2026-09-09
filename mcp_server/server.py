"""People Waterfall MCP — named-person resolver. Never finds an email."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Callable

from mcp.server import MCPServer
from mcp.types import ToolAnnotations

from mcp_server.playbook import INSTRUCTIONS, WHEN_TO_USE

ROOT = Path(__file__).resolve().parent.parent

mcp = MCPServer(
    name="people-waterfall",
    title="People Waterfall",
    description=(
        "Given a company, return named people whose title is one the client "
        "asked for. Never finds an email. Hands name + title + domain to the "
        "Email Finder Waterfall. ICP lives in public.wf_client_profiles."
    ),
    instructions=INSTRUCTIONS,
    version="0.1.0",
)


def _json(data: Any) -> str:
    return json.dumps(data, indent=2, default=str)


def _ensure_repo_cwd() -> None:
    os.chdir(ROOT)


def _http_mode() -> bool:
    return os.environ.get("MCP_TRANSPORT", "stdio").lower() in (
        "streamable-http",
        "http",
        "sse",
    )


def _reload_settings() -> None:
    from people_waterfall import config as cfg
    from people_waterfall import supabase_sync

    cfg.settings = cfg.load_settings()
    try:
        rows = supabase_sync.load_private_api_keys()
        if rows:
            cfg.settings = cfg.merge_private_keys(rows)
    except Exception:
        pass


@mcp.resource(
    "people-waterfall://playbook",
    name="playbook",
    description="When to use People Waterfall and how it writes people rows.",
    mime_type="text/markdown",
)
def playbook_resource() -> str:
    return INSTRUCTIONS


@mcp.prompt(
    name="when_to_use",
    description="Decide whether the People Waterfall MCP applies.",
)
def when_to_use_prompt() -> str:
    return WHEN_TO_USE


@mcp.tool(
    annotations=ToolAnnotations(
        title="Get client profile",
        readOnlyHint=True,
        openWorldHint=False,
    )
)
def get_profile(client_tag: str) -> str:
    """Read public.wf_client_profiles. Domain Waterfall ensure_profile creates it."""
    _ensure_repo_cwd()
    _reload_settings()
    from people_waterfall.profile import get_profile as _get

    return _json(_get(client_tag).to_public())


@mcp.tool(
    annotations=ToolAnnotations(
        title="Get job status",
        readOnlyHint=True,
        openWorldHint=False,
    )
)
def get_job_status(job_id: str) -> str:
    """Last known progress for a job. Never a bare error."""
    from mcp_server.jobs import get_job

    return _json(get_job(job_id).to_public())


@mcp.tool(
    annotations=ToolAnnotations(
        title="List jobs",
        readOnlyHint=True,
        openWorldHint=False,
    )
)
def list_jobs(limit: int = 20) -> str:
    """Recent people-waterfall jobs on this process."""
    from mcp_server.jobs import list_jobs as _list

    return _json([j.to_public() for j in _list(limit=limit)])


@mcp.tool(
    annotations=ToolAnnotations(
        title="Receipt test",
        readOnlyHint=False,
        openWorldHint=True,
        destructiveHint=False,
    )
)
def receipt_test(
    client_tag: str,
    n: int = 15,
    estimate_only: bool = True,
    approve_cost_usd: float = 3.0,
    background: bool = True,
) -> str:
    """Score every tier on ground truth. Default estimate_only=true.

    With-domain: n domains that already have 3+ title-matched contacts.
    Without-domain: n companies from ground_truth.companies_no_domain.
    Drops zero-yield tiers and writes tier_order with the live prices used.
    """
    _ensure_repo_cwd()
    _reload_settings()
    from people_waterfall.receipt import run_receipt

    def _run(progress: Callable[[dict[str, Any]], None] | None = None) -> dict[str, Any]:
        return run_receipt(
            client_tag,
            n=int(n or 15),
            approve_cost_usd=approve_cost_usd,
            estimate_only=bool(estimate_only),
            progress_callback=progress,
        )

    if estimate_only or not background:
        return _json(_run())
    from mcp_server.jobs import start_job, update_job_progress

    def worker(job: Any) -> dict[str, Any]:
        return _run(lambda snap: update_job_progress(job.id, snap))

    job = start_job(
        "receipt_test",
        worker,
        meta={"client_tag": client_tag, "n": n},
    )
    return _json(
        {
            "job_id": job.id,
            "status": job.status,
            "message": f"Poll get_job_status with job_id={job.id}.",
            "client_tag": client_tag,
        }
    )


@mcp.tool(
    annotations=ToolAnnotations(
        title="Resolve people",
        readOnlyHint=False,
        openWorldHint=True,
        destructiveHint=True,
    )
)
def resolve_people(
    source_table: str,
    client_tag: str,
    where: str = "",
    max_tier: str = "all",
    approve_cost_usd: float | None = None,
    estimate_only: bool = True,
    require_title_match: bool = True,
    background: bool = True,
) -> str:
    """Find named people for companies in source_table + where.

    No inline rows. Response is counts / job_id / cost only.
    estimate_only=true (default) returns rows per tier and live unit prices.
    approve_cost_usd is the paid-tier ceiling. Free tiers ignore it.
    A paid tier that would cross the ceiling stops cleanly and writes deferred.
    """
    _ensure_repo_cwd()
    _reload_settings()
    from people_waterfall.waterfall import resolve_people as _resolve

    def _run(progress: Callable[[dict[str, Any]], None] | None = None) -> dict[str, Any]:
        return _resolve(
            source_table=source_table,
            where=where,
            client_tag=client_tag,
            max_tier=max_tier,
            approve_cost_usd=approve_cost_usd,
            estimate_only=bool(estimate_only),
            require_title_match=bool(require_title_match),
            write_supabase=not estimate_only,
            progress_callback=progress,
        )

    if estimate_only:
        return _json(_run())
    if background and _http_mode():
        from mcp_server.jobs import start_job, update_job_progress

        def worker(job: Any) -> dict[str, Any]:
            return _run(lambda snap: update_job_progress(job.id, snap))

        job = start_job(
            "resolve_people",
            worker,
            meta={
                "client_tag": client_tag,
                "source_table": source_table,
                "where": where,
                "max_tier": max_tier,
            },
        )
        return _json(
            {
                "job_id": job.id,
                "status": job.status,
                "message": f"Poll get_job_status with job_id={job.id}.",
                "client_tag": client_tag,
            }
        )
    return _json(_run())


def _mount_http_routes() -> None:
    try:
        from starlette.requests import Request
        from starlette.responses import JSONResponse, PlainTextResponse
    except ImportError:
        return

    @mcp.custom_route("/", methods=["GET"])
    async def root_page(_request: Request) -> PlainTextResponse:
        return PlainTextResponse(
            "People Waterfall MCP\n"
            "Claude custom connector URL: /mcp\n"
            "Health: /health\n"
            "Job status: /job-status?job_id=\n"
            "Jobs: /jobs\n"
            "Auth: none\n"
        )

    @mcp.custom_route("/health", methods=["GET"])
    async def health_live(_request: Request) -> JSONResponse:
        return JSONResponse(
            {
                "ok": True,
                "service": "people-waterfall",
                "transport": "streamable-http",
                "mcp_path": "/mcp",
                "job_status_path": "/job-status",
                "jobs_path": "/jobs",
                "auth": "none",
            }
        )

    @mcp.custom_route("/job-status", methods=["GET"])
    async def job_status_http(request: Request) -> JSONResponse:
        from mcp_server.jobs import get_job

        job_id = (request.query_params.get("job_id") or "").strip()
        return JSONResponse(get_job(job_id).to_public())

    @mcp.custom_route("/jobs", methods=["GET"])
    async def jobs_http(request: Request) -> JSONResponse:
        from mcp_server.jobs import list_jobs as _list

        try:
            limit = int(request.query_params.get("limit") or 20)
        except ValueError:
            limit = 20
        return JSONResponse([j.to_public() for j in _list(limit=limit)])


_mount_http_routes()


def main() -> None:
    _ensure_repo_cwd()
    transport = os.environ.get("MCP_TRANSPORT", "stdio").lower()
    host = os.environ.get("HOST", "0.0.0.0")
    port = int(os.environ.get("PORT", "8000"))

    if transport in ("streamable-http", "http"):
        kwargs: dict[str, Any] = {
            "transport": "streamable-http",
            "host": host,
            "port": port,
        }
        try:
            from mcp.server.transport_security import TransportSecuritySettings

            kwargs.update(
                {
                    "streamable_http_path": "/mcp",
                    "stateless_http": True,
                    "transport_security": TransportSecuritySettings(
                        enable_dns_rebinding_protection=False
                    ),
                }
            )
        except Exception:
            kwargs["path"] = "/mcp"
        mcp.run(**kwargs)
        return

    if transport == "sse":
        mcp.run(transport="sse", host=host, port=port)
        return

    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
