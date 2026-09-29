-- DiscoLike writes the company email pattern and an unresolved reason.

CREATE OR REPLACE FUNCTION public.pw_ensure_people_writeback(
  p_schema text,
  p_table text
) RETURNS void
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path TO 'public'
AS $function$
DECLARE
  sch text;
  tbl text;
BEGIN
  sch := lower(regexp_replace(coalesce(p_schema, 'public'), '[^a-z0-9_]', '', 'g'));
  tbl := lower(regexp_replace(coalesce(p_table, ''), '[^a-z0-9_]', '', 'g'));
  IF sch = '' OR tbl = '' OR to_regclass(format('%I.%I', sch, tbl)) IS NULL THEN
    RAISE EXCEPTION 'unknown source table %.%', sch, tbl;
  END IF;
  EXECUTE format(
    'ALTER TABLE %I.%I ADD COLUMN IF NOT EXISTS wf_people_count integer',
    sch, tbl
  );
  EXECUTE format(
    'ALTER TABLE %I.%I ADD COLUMN IF NOT EXISTS wf_people_source text',
    sch, tbl
  );
  EXECUTE format(
    'ALTER TABLE %I.%I ADD COLUMN IF NOT EXISTS wf_people_status text',
    sch, tbl
  );
  EXECUTE format(
    'ALTER TABLE %I.%I ADD COLUMN IF NOT EXISTS wf_people_reason text',
    sch, tbl
  );
  EXECUTE format(
    'ALTER TABLE %I.%I ADD COLUMN IF NOT EXISTS wf_email_pattern text',
    sch, tbl
  );
  EXECUTE format(
    'ALTER TABLE %I.%I ADD COLUMN IF NOT EXISTS wf_email_pattern_conf double precision',
    sch, tbl
  );
END;
$function$;
