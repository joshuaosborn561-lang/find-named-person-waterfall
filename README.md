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
| `resolve_people` | Page `source_table` + `where` (500/server-side). Estimate first. `min_tier` / `skip_tiers` window the cache → site_people → getleads → discolike → prospeo_search → aiark_people order. |
| `receipt_test` | Score every people tier on ground truth. Write `people_tier_order`. |
| `get_profile` | Read-only. Profiles are created by Domain Waterfall `ensure_profile`. |
| `get_job_status` | Last known progress plus `counter` (`done`/`total`/`pct`). Never a bare error. |
| `list_jobs` | Recent jobs on this process. Same `counter` on every row. |

## Ordering

Default people-tier order is cache → site_people → getleads →
discolike → prospeo_search → aiark_people. `site_people` is force-
included unless a receipt dropped it. `site_staff` stays published
for one release (`max_tier=site_staff` runs that tier alone). A
receipt may drop a zero-yield people tier; it does not reorder the
declared list. Legacy `leadmagic_*` names are accepted as no-ops
with a warning. Unrecognized names in a custom `people_tier_order`
are warned, not dropped silently. People Waterfall writes
`people_tier_order` / `people_dropped_tiers` / `people_measured_rates`.
It never reads or writes the domain resolver's shared `tier_order`.

`SITE_PEOPLE_KEY` is required whenever `site_people` is selected.
A missing or empty key fails the job before any fetch. Do not store
the key on `Settings` or print it. Set it on the Railway service
before deploy, and do not deploy while a `resolve_people` job is
running. Fetch/extract edge calls run in a pool sized by
`SITE_PEOPLE_CONCURRENCY` (default 6, max 12). Connection-slot,
429, and 5xx responses back off with jitter and shrink the pool.

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
