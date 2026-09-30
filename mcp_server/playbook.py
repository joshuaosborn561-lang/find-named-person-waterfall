INSTRUCTIONS = """# People Waterfall MCP

Given a company, return named people whose title is one the client asked for.
This service does not buy emails. Title-matched rows are handed to the
existing Email Finder Waterfall in `name_company` mode.

Nothing industry-specific lives in code. Titles, synonyms, geography, company
size bands, ground truth, and cache tables come from `public.wf_client_profiles`,
the same document Domain Waterfall reads. `get_profile` is read-only; Domain
Waterfall `ensure_profile` creates the row.

## Tools

- `resolve_people(source_table, where, client_tag, max_tier, min_tier, skip_tiers, approve_cost_usd, estimate_only, require_title_match)`
  This is the job runner. Source is `source_table` + `where`, paged 500
  server side. No inline rows. `estimate_only=true` first on any paid run.
  Free tiers ignore the cost ceiling. `min_tier`/`max_tier` window the
  people-tier order. `skip_tiers` is a comma list.
  DiscoLike-only: `min_tier=discolike` `max_tier=discolike`.
  Zero-pass companies are written wf_people_status=people_unresolved.
  counter.done counts companies that finished every selected tier.
  Contacts, name_bank, and wf_people_status are written after each tier
  batch, in that order, in chunks of 500 with three retries. Contacts
  upsert on (client_tag, domain, lower first name, lower last name).
  A write error fails the job before the next tier spends.
  A job that spent money and wrote 0 contacts fails instead of completing.
- `receipt_test(client_tag, n)` — phase-zero ground-truth score. Prints live
  prices, drops zero-yield people tiers, writes `people_tier_order`. Never
  writes the domain resolver's shared `tier_order`.
- `get_profile(client_tag)`
- `get_job_status(job_id)` — last known progress, never a bare error.
  Always includes `counter`: done / total / remaining / pct plus a
  `message` like "running: 12/100 companies (12.0%)". HTTP: `/job-status?job_id=`.
- `list_jobs(limit)` — same counter on every row. HTTP: `/jobs`.

## Ordering

Default people_tier_order is cache → discolike → leadmagic_employee.
Those are the only people tiers. A receipt may drop a zero-yield
default tier; it does not cheapest-sort the declared order.

DiscoLike is the primary discovery tier. It needs a domain. One sequential
task for the full domain list (cap 5,000; more domains run as later
tasks, never concurrent). search_context_size=low (2 queries),
max_contacts_per_domain=3, find_emails=false. integration_id is
"native" (Groove, no LLM key). search_provider_id is the account's
Serper provider from GET /v1/search-providers.
  A selected paid tier that makes zero calls fails the job.
  The generate task_id is stored on the job the moment start_generate
  returns, before polling. resume_discolike_task(task_id, client_tag,
  source_table, where) re-reads GET /discogen/status/{task_id} for free
  and runs the same gates and writes. Duplicate source domains are gated
  once; sibling rows are writeback copies and are not banked again.
icp_text comes from profile.discolike_icp_text (generated from
target_titles + vertical on first run). Cost is
SERPER_USD_PER_QUERY × 2 + DISCOLIKE_USD_PER_COMPANY (defaults
0.001 and 0.0035). estimate_only prices rows_with_domain × unit.
No-domain rows skip this tier and are marked people_unresolved
with reason no_domain. Email pattern lands on the source row as
wf_email_pattern / wf_email_pattern_conf. After a job, title-matched
rows are handed to Email Finder Waterfall; the handoff calls
ensure_client first so public.{tag}_wf_contacts exists.

## Write rules

`public.<client_tag>_wf_contacts` only: first_name, last_name, job_title,
title_match, title_rank, linkedin_url, domain, company_name, source_tier,
source_confidence, phone, person_city, person_state.

Writeback on the source row: wf_people_count, wf_people_source, wf_people_status
in resolved | partial | deferred | people_unresolved.

Never touch dl_status, sg_exclude, or skip_*.

Wrong titles go to public.name_bank with rejection_reason
title, seniority, geo, or company. A title that is literally in
target_titles is not rejected by seniority_floor. A contact with no
city and no state passes the geo gate. regate_name_bank(client_tag)
re-applies the current profile to banked rows and promotes passes
into the contacts table. It does not call a vendor.
Every accepted person passes the company match (first ten characters
or equal domain) and the title audit.
"""

WHEN_TO_USE = """Use People Waterfall when the task is "who works here in this title?"
Do not use it to find emails, crawl websites, or scrape Google Maps.
Use Email Finder Waterfall after title_match rows exist.
Use Domain Waterfall when the company has no domain yet.
"""
