-- People Waterfall keeps its own order. Domain Waterfall owns tier_order /
-- dropped_tiers / measured_rates on the same wf_client_profiles row.

ALTER TABLE public.wf_client_profiles
  ADD COLUMN IF NOT EXISTS people_tier_order jsonb,
  ADD COLUMN IF NOT EXISTS people_dropped_tiers jsonb,
  ADD COLUMN IF NOT EXISTS people_measured_rates jsonb;

-- Copy people-shaped measured_rates (getleads / serp / …) into the people
-- keys. Do not copy domain dropped_tiers (cache/aiark/leadmagic) or string
-- domain orders (maps, discolike).
UPDATE public.wf_client_profiles
SET
  people_measured_rates = coalesce(
    people_measured_rates,
    (
      SELECT coalesce(jsonb_object_agg(e.key, e.value), '{}'::jsonb)
      FROM jsonb_each(coalesce(profile->'measured_rates', '{}'::jsonb)) AS e
      WHERE e.key IN (
        'cache',
        'getleads',
        'smartlead',
        'leadmagic_employee',
        'aiark',
        'serp',
        'prospeo',
        'leadmagic_role',
        'leadmagic_search_free'
      )
    )
  ),
  people_dropped_tiers = coalesce(people_dropped_tiers, '[]'::jsonb),
  profile = coalesce(profile, '{}'::jsonb)
    || jsonb_build_object(
      'people_measured_rates',
      coalesce(
        people_measured_rates,
        profile->'people_measured_rates',
        (
          SELECT coalesce(jsonb_object_agg(e.key, e.value), '{}'::jsonb)
          FROM jsonb_each(coalesce(profile->'measured_rates', '{}'::jsonb)) AS e
          WHERE e.key IN (
            'cache',
            'getleads',
            'smartlead',
            'leadmagic_employee',
            'aiark',
            'serp',
            'prospeo',
            'leadmagic_role',
            'leadmagic_search_free'
          )
        )
      ),
      'people_dropped_tiers',
      coalesce(people_dropped_tiers, profile->'people_dropped_tiers', '[]'::jsonb)
    )
WHERE people_measured_rates IS NULL
   OR people_dropped_tiers IS NULL
   OR NOT (profile ? 'people_measured_rates');

-- Copy an already-people-shaped object array into people_tier_order.
UPDATE public.wf_client_profiles
SET
  people_tier_order = coalesce(people_tier_order, profile->'tier_order'),
  profile = profile || jsonb_build_object(
    'people_tier_order',
    coalesce(people_tier_order, profile->'tier_order')
  )
WHERE people_tier_order IS NULL
  AND jsonb_typeof(profile->'tier_order') = 'array'
  AND jsonb_typeof(profile->'tier_order'->0) = 'object'
  AND coalesce(profile->'tier_order'->0->>'tier', '') IN (
    'cache',
    'getleads',
    'smartlead',
    'leadmagic_employee',
    'aiark',
    'serp',
    'prospeo',
    'leadmagic_role'
  );
