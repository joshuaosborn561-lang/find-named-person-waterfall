-- Seed lives in the shared profile JSON. Domain Waterfall owns ensure_profile;
-- this insert is the Peterson / Goliath pilot documents so People Waterfall
-- can run before that service writes them.

INSERT INTO public.wf_client_profiles (client_tag, display_name, profile)
VALUES (
  'peterson_roof',
  'Peterson roofing GCs',
  '{
    "client_tag": "peterson_roof",
    "target_titles": [
      "Owner", "President", "Principal", "Partner", "Vice President",
      "Project Manager", "Senior Project Manager", "Project Executive",
      "Estimator", "Chief Estimator", "Senior Estimator", "Superintendent",
      "General Superintendent", "Preconstruction Manager",
      "Director of Preconstruction", "Purchasing Manager", "Operations Manager"
    ],
    "title_synonyms": {"PM": "Project Manager", "Super": "Superintendent", "Precon": "Preconstruction"},
    "geo": {"states": ["TX"], "person_geo_mode": "cap"},
    "ground_truth": {
      "contacts_table": "gc.contacts",
      "companies_no_domain": {
        "schema": "client_peterson",
        "table": "leads",
        "where": "domain is null"
      }
    },
    "cache_tables": ["gc.contacts", "public.peterson_contacts", "public.peterson_roof_wf_contacts"]
  }'::jsonb
),
(
  'goliath',
  'Goliath IT DMs',
  '{
    "client_tag": "goliath",
    "target_titles": [
      "IT Director", "Director of IT", "Director of Information Technology",
      "Director of Technology", "VP of IT", "VP of Information Technology",
      "IT Manager", "Head of IT", "Head of Information Technology",
      "System Administrator", "Sysadmin", "Systems Administrator",
      "Network Administrator", "IT Administrator", "IT Admin",
      "Infrastructure Manager", "Security Manager", "Help Desk Manager"
    ],
    "title_exclude_regex": "CEO|CFO|COO|President",
    "geo": {"person_geo_mode": "ignore"},
    "ground_truth": {
      "contacts_table": "public.goliath_wf_contacts",
      "companies_no_domain": {
        "schema": "public",
        "table": "goliath_wf_companies",
        "where": "domain is null"
      }
    },
    "cache_tables": ["public.goliath_wf_contacts", "client_goliath.contacts"]
  }'::jsonb
)
ON CONFLICT (client_tag) DO NOTHING;
