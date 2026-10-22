-- extract_remaining stops counting sites site-people gave up on (3 failed
-- attempts), so pollers reach 0 instead of re-calling a stuck batch.
-- Needs topup migration 20261022230000_site_extractions_attempts.sql applied first
-- (site_extractions.attempts / gave_up_at). Do not apply until Josh approves.

CREATE OR REPLACE FUNCTION public.pw_site_people_remaining(
  p_batch text
) RETURNS jsonb
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path TO 'public', 'topup'
AS $function$
#variable_conflict use_variable
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
        AND (x.error IS NULL OR x.gave_up_at IS NOT NULL OR x.attempts >= 3)
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
