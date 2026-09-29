INSTRUCTIONS = """# People Waterfall MCP

Given a company, return named people whose title is one the client asked for.
This service does not buy emails. Style D may copy a published company
email pattern from an aggregator snippet onto the contact row.
Title-matched rows are handed to the existing Email Finder Waterfall
in `name_company` mode.

Nothing industry-specific lives in code. Titles, synonyms, geography, company
size bands, ground truth, and cache tables come from `public.wf_client_profiles`,
the same document Domain Waterfall reads. `get_profile` is read-only; Domain
Waterfall `ensure_profile` creates the row.

## Tools

- `resolve_people(source_table, where, client_tag, max_tier, min_tier, skip_tiers, approve_cost_usd, estimate_only, require_title_match)`
  This is the job runner. Source is `source_table` + `where`, paged 500
  server side. No inline rows. `estimate_only=true` first on any paid run.
  Free tiers ignore the cost ceiling. `min_tier`/`max_tier` window the
  people-tier order. `skip_tiers` is a comma list. SERP-only:
  `min_tier=serp` `max_tier=serp`. SERP walks styles A→B→C→D per
  company and stops once a title-matched person is found.
  A: `site:linkedin.com/in "{company}" ("Owner" OR ...target_titles)`.
  B: `site:{domain} ("staff" OR "our team" OR "leadership" OR "about us")`
  when the row has a domain; names come from the organic title + snippet.
  C: `site:facebook.com "{company}" "{city}"`; parse the about snippet.
  D: `"{company}" "{city}"` plus the profile target_titles OR-list;
  keep zoominfo / rocketreach / signalhire / church and school
  directory hosts and copy a published email onto the contact when
  present. Queries use the conversational company name, never the
  legal name. Profile `serp_styles` defaults to ["a","b","c","d"].
  Batches are 90–110 queries per Apify run; every batch is started
  with waitForFinish=0 and polled together. Cost is $0.0045 per
  query and counts against approve_cost_usd. estimate_only prices
  enabled styles × rows. The target_titles A query is sent first.
  fallback_titles A is queued only when that target query returned
  no company-name match. per_tier records serp_a…serp_d so a
  receipt can drop a zero-yield style without dropping serp.
  Zero-pass companies are written wf_people_status=people_unresolved.
  counter.done advances when a company is processed and written, not
  when its query is submitted.
- `receipt_test(client_tag, n)` — phase-zero ground-truth score. Prints live
  prices, drops zero-yield people tiers and zero-yield SERP styles,
  writes `people_tier_order`. Never writes the domain resolver's
  shared `tier_order`.
- `get_profile(client_tag)`
- `get_job_status(job_id)` — last known progress, never a bare error.
  Always includes `counter`: done / total / remaining / pct plus a
  `message` like "running: 12/100 companies (12.0%)". HTTP: `/job-status?job_id=`.
- `list_jobs(limit)` — same counter on every row. HTTP: `/jobs`.

## Ordering

Default people_tier_order is cache → discolike → leadmagic_employee.
Nothing else runs unless the profile's people_tier_order names it, or the
job windows it in with min_tier / max_tier / skip_tiers. getleads,
smartlead, aiark, serp, prospeo, and leadmagic_role stay implemented
behind those gates. A receipt may drop a zero-yield default tier; it
does not cheapest-sort the declared order.

DiscoLike is the primary discovery tier. It needs a domain. One sequential
task for the full domain list (cap 5,000; more domains run as later
tasks, never concurrent). search_context_size=low (2 queries), 
max_contacts_per_domain=3, find_emails=false, integration_id=native.
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
source_confidence, phone, person_city, person_state, email (style D
published pattern only).

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
