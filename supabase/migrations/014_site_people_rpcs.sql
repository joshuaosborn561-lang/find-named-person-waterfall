-- Site-people load / remaining / roster / default picks.
-- topup is not always on pgrst.db_schemas, so these security-definer RPCs
-- live in public and read/write topup.* with the service role.

CREATE OR REPLACE FUNCTION public.pw_site_people_load(
  p_domains text[],
  p_batch text,
  p_reuse_days integer DEFAULT 30
) RETURNS jsonb
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path TO 'public', 'topup'
AS $function$
DECLARE
  batch text;
  reuse interval;
  host text;
  loaded int := 0;
  reused int := 0;
  reset_n int := 0;
  inserted int := 0;
BEGIN
  batch := lower(regexp_replace(coalesce(p_batch, ''), '[^a-z0-9_]', '', 'g'));
  IF batch = '' THEN
    RAISE EXCEPTION 'batch is required';
  END IF;
  reuse := make_interval(days => greatest(coalesce(p_reuse_days, 30), 1));

  FOREACH host IN ARRAY coalesce(p_domains, ARRAY[]::text[])
  LOOP
    host := lower(btrim(host));
    host := regexp_replace(host, '^https?://', '');
    host := split_part(host, '/', 1);
    host := split_part(host, ':', 1);
    IF host LIKE 'www.%' THEN
      host := substring(host from 5);
    END IF;
    IF host = '' THEN
      CONTINUE;
    END IF;
    loaded := loaded + 1;
    IF EXISTS (SELECT 1 FROM topup.site_people_text t WHERE t.domain = host) THEN
      IF EXISTS (
        SELECT 1
        FROM topup.site_people_text t
        WHERE t.domain = host
          AND t.http_status BETWEEN 200 AND 399
          AND t.fetched_at IS NOT NULL
          AND t.fetched_at >= now() - reuse
      ) THEN
        UPDATE topup.site_people_text
        SET batch = batch
        WHERE domain = host;
        reused := reused + 1;
      ELSE
        UPDATE topup.site_people_text
        SET batch = batch,
            http_status = NULL
        WHERE domain = host;
        reset_n := reset_n + 1;
      END IF;
    ELSE
      INSERT INTO topup.site_people_text (domain, batch)
      VALUES (host, batch)
      ON CONFLICT (domain) DO UPDATE
        SET batch = excluded.batch;
      inserted := inserted + 1;
    END IF;
  END LOOP;

  RETURN jsonb_build_object(
    'loaded', loaded,
    'reused', reused,
    'reset', reset_n,
    'inserted', inserted,
    'batch', batch
  );
END;
$function$;


CREATE OR REPLACE FUNCTION public.pw_site_people_remaining(
  p_batch text
) RETURNS jsonb
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path TO 'public', 'topup'
AS $function$
DECLARE
  batch text;
  fetch_left int;
  extract_left int;
  fetched_ok int;
  extracted_ok int;
  dollars numeric;
BEGIN
  batch := lower(regexp_replace(coalesce(p_batch, ''), '[^a-z0-9_]', '', 'g'));
  SELECT count(*) INTO fetch_left
  FROM topup.site_people_text t
  WHERE t.batch = batch
    AND (t.http_status IS NULL OR t.http_status = -1);

  SELECT count(*) INTO extract_left
  FROM topup.site_people_text t
  WHERE t.batch = batch
    AND t.http_status BETWEEN 200 AND 399
    AND coalesce(t.chars, 0) > 300
    AND NOT EXISTS (
      SELECT 1
      FROM topup.site_extractions x
      WHERE x.domain = t.domain
        AND x.error IS NULL
    );

  SELECT count(*) INTO fetched_ok
  FROM topup.site_people_text t
  WHERE t.batch = batch
    AND t.http_status BETWEEN 200 AND 399;

  SELECT count(*) INTO extracted_ok
  FROM topup.site_extractions x
  WHERE x.error IS NULL
    AND EXISTS (
      SELECT 1 FROM topup.site_people_text t
      WHERE t.domain = x.domain AND t.batch = batch
    );

  SELECT coalesce(sum(x.cost), 0) INTO dollars
  FROM topup.site_extractions x
  WHERE EXISTS (
    SELECT 1 FROM topup.site_people_text t
    WHERE t.domain = x.domain AND t.batch = batch
  );

  RETURN jsonb_build_object(
    'fetch_remaining', fetch_left,
    'extract_remaining', extract_left,
    'fetched_ok', fetched_ok,
    'extracted_ok', extracted_ok,
    'dollars', dollars
  );
END;
$function$;


CREATE OR REPLACE FUNCTION public.pw_site_people_roster(
  p_batch text
) RETURNS jsonb
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path TO 'public', 'topup'
AS $function$
DECLARE
  batch text;
  result jsonb;
BEGIN
  batch := lower(regexp_replace(coalesce(p_batch, ''), '[^a-z0-9_]', '', 'g'));
  SELECT coalesce(jsonb_agg(to_jsonb(r)), '[]'::jsonb)
    INTO result
  FROM (
    SELECT
      t.domain,
      p.person_key,
      p.full_name,
      p.title,
      p.page_url,
      x.org_name,
      x.org_type,
      (t.http_status BETWEEN 200 AND 399) AS fetched,
      (x.error IS NULL AND x.domain IS NOT NULL) AS extracted,
      coalesce(x.cost, 0) AS dollars
    FROM topup.site_people_text t
    LEFT JOIN LATERAL (
      SELECT e.org_name, e.org_type, e.error, e.domain, e.cost
      FROM topup.site_extractions e
      WHERE e.domain = t.domain
      ORDER BY e.extracted_at DESC
      LIMIT 1
    ) x ON true
    LEFT JOIN topup.site_people p ON p.domain = t.domain
    WHERE t.batch = batch
  ) r;
  RETURN coalesce(result, '[]'::jsonb);
END;
$function$;


CREATE OR REPLACE FUNCTION public.pw_site_people_picks(
  p_batch text
) RETURNS jsonb
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path TO 'public', 'topup'
AS $function$
DECLARE
  batch text;
  result jsonb;
BEGIN
  batch := lower(regexp_replace(coalesce(p_batch, ''), '[^a-z0-9_]', '', 'g'));
  SELECT coalesce(jsonb_agg(to_jsonb(p)), '[]'::jsonb)
    INTO result
  FROM topup.site_dm_pick p
  WHERE EXISTS (
    SELECT 1
    FROM topup.site_people_text t
    WHERE t.domain = p.domain
      AND t.batch = batch
  );
  RETURN coalesce(result, '[]'::jsonb);
END;
$function$;
