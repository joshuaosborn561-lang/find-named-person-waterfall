# People Waterfall

Railway MCP service. Given a company, return named people whose title is one
the client asked for. It never finds an email. Title-matched rows are handed to
the existing Email Finder Waterfall in `name_company` mode.

Nothing industry-specific lives in code. Titles, synonyms, geography, company
size bands, ground truth, and cache tables come from
`public.wf_client_profiles` — the same document Domain Waterfall reads.

## Tools

| Tool | Purpose |
| --- | --- |
| `resolve_people` | Page `source_table` + `where` (500/server-side). Estimate first. |
| `receipt_test` | Score every tier on ground truth. Write `tier_order`. |
| `get_profile` | Read-only. Profiles are created by Domain Waterfall `ensure_profile`. |
| `get_job_status` | Last known progress plus `counter` (`done`/`total`/`pct`). Never a bare error. |
| `list_jobs` | Recent jobs on this process. Same `counter` on every row. |

## Ordering

Cheapest to most expensive, always. Free tiers first. Paid tiers sort on the
account's live unit price at job start. Free-on-miss sorts on price × measured
hit rate (default half until a receipt measures it). A receipt may drop a
zero-yield tier; it does not change the sort rule.

## Writes

`public.<client_tag>_wf_contacts` only:

`first_name`, `last_name`, `job_title`, `title_match`, `title_rank`,
`linkedin_url`, `domain`, `company_name`, `source_tier`, `source_confidence`,
`phone`, `person_city`, `person_state`.

Source writeback: `wf_people_count`, `wf_people_source`, `wf_people_status`
(`resolved` \| `partial` \| `deferred` \| `people_unresolved`).

Never touches `dl_status`, `sg_exclude`, or `skip_*`. Wrong titles go to
`public.name_bank`.

## Run

```bash
pip install -r requirements.txt
cp .env.example .env
python -m mcp_server          # stdio
MCP_TRANSPORT=http python -m mcp_server
pytest
```

Railway: Dockerfile + `railway.json`. Health check is `/health`.

## Claude custom connector

HTTPS MCP URL (streamable HTTP, no auth):

`https://people-waterfall-production.up.railway.app/mcp`

Health: `https://people-waterfall-production.up.railway.app/health`

Poll a run without touching it. Response always includes `counter`:

`https://people-waterfall-production.up.railway.app/job-status?job_id=<id>`

Example: `"counter": {"done": 12, "total": 100, "remaining": 88, "pct": 12.0, "message": "running: 12/100 companies (12.0%)"}`

Recent jobs: `https://people-waterfall-production.up.railway.app/jobs`
