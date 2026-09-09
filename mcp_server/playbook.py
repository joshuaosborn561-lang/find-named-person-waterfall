INSTRUCTIONS = """# People Waterfall MCP

Given a company, return named people whose title is one the client asked for.
This service never finds an email. Title-matched rows are handed to the existing
Email Finder Waterfall in `name_company` mode.

Nothing industry-specific lives in code. Titles, synonyms, geography, company
size bands, ground truth, and cache tables come from `public.wf_client_profiles`,
the same document Domain Waterfall reads. `get_profile` is read-only; Domain
Waterfall `ensure_profile` creates the row.

## Tools

- `resolve_people(source_table, where, client_tag, max_tier, approve_cost_usd, estimate_only, require_title_match)`
  Source is `source_table` + `where`, paged 500 server side. No inline rows.
  `estimate_only=true` first on any paid run. Free tiers ignore the cost ceiling.
- `receipt_test(client_tag, n)` — phase-zero ground-truth score. Prints live
  prices, drops zero-yield tiers, writes `tier_order`.
- `get_profile(client_tag)`
- `get_job_status(job_id)` — last known progress, never a bare error.
- `list_jobs(limit)`

## Ordering

Cheapest to most expensive, always. Free first, then paid sorted by live unit
price per person. Free-on-miss sorts on price × measured hit rate (default 0.5).
A receipt does not reorder that rule; it only drops a zero-yield tier.

## Write rules

`public.<client_tag>_wf_contacts` only: first_name, last_name, job_title,
title_match, title_rank, linkedin_url, domain, company_name, source_tier,
source_confidence, phone, person_city, person_state.

Writeback on the source row: wf_people_count, wf_people_source, wf_people_status
in resolved | partial | deferred | people_unresolved.

Never touch dl_status, sg_exclude, or skip_*.

Wrong titles go to public.name_bank. Every accepted person passes the company
match (first ten characters or equal domain) and the title audit.
"""

WHEN_TO_USE = """Use People Waterfall when the task is "who works here in this title?"
Do not use it to find emails, crawl websites, or scrape Google Maps.
Use Email Finder Waterfall after title_match rows exist.
Use Domain Waterfall when the company has no domain yet.
"""
