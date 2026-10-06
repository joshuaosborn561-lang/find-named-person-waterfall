-- Preflight columns, source filters, single-name bank rows, and client-schema contacts.

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

CREATE OR REPLACE FUNCTION public.ew_read_source(
  p_schema text,
  p_table text,
  p_filters jsonb DEFAULT '[]'::jsonb,
  p_columns text[] DEFAULT NULL,
  p_key_column text DEFAULT 'id',
  p_after text DEFAULT NULL,
  p_limit integer DEFAULT 500
) RETURNS jsonb
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path TO 'public'
AS $function$
DECLARE
  sch text;
  tbl text;
  keycol text;
  cols text[];
  filt jsonb;
  clause text := ' WHERE true';
  col text;
  op text;
  val text;
  sql text;
  result jsonb;
  lim int;
  key_type text;
  numeric_key boolean;
  order_sql text;
BEGIN
  sch := lower(regexp_replace(coalesce(p_schema, 'public'), '[^a-z0-9_]', '', 'g'));
  tbl := lower(regexp_replace(coalesce(p_table, ''), '[^a-z0-9_]', '', 'g'));
  keycol := lower(regexp_replace(coalesce(p_key_column, 'id'), '[^a-z0-9_]', '', 'g'));
  lim := least(greatest(coalesce(p_limit, 500), 1), 500);
  IF sch = '' OR tbl = '' OR to_regclass(format('%I.%I', sch, tbl)) IS NULL THEN
    RAISE EXCEPTION 'unknown source table %.%', sch, tbl;
  END IF;

  IF p_columns IS NULL OR coalesce(array_length(p_columns, 1), 0) = 0 THEN
    cols := ARRAY[keycol];
  ELSE
    SELECT array_agg(lower(regexp_replace(c, '[^a-z0-9_]', '', 'g')))
      INTO cols
    FROM unnest(p_columns) AS c
    WHERE c IS NOT NULL AND btrim(c) <> '';
    IF cols IS NULL THEN
      cols := ARRAY[keycol];
    ELSIF NOT keycol = ANY (cols) THEN
      cols := cols || keycol;
    END IF;
  END IF;

  FOR filt IN SELECT * FROM jsonb_array_elements(coalesce(p_filters, '[]'::jsonb))
  LOOP
    col := lower(regexp_replace(coalesce(filt->>'col', ''), '[^a-z0-9_]', '', 'g'));
    op := lower(coalesce(filt->>'op', ''));
    val := filt->>'value';
    IF col = '' THEN
      CONTINUE;
    END IF;
    IF op IN ('is.null', 'is null') THEN
      clause := clause || format(' AND %I IS NULL', col);
    ELSIF op IN ('not.is.null', 'is not null') THEN
      clause := clause || format(' AND %I IS NOT NULL', col);
    ELSIF op IN ('is.not.false', 'is not false') THEN
      clause := clause || format(' AND (%I IS NOT FALSE)', col);
    ELSIF op IN ('not.blank', 'not blank') THEN
      clause := clause || format(' AND coalesce(%I::text, '''') <> ''''', col);
    ELSIF op IN ('eq', '=') THEN
      clause := clause || format(' AND %I = %L', col, val);
    ELSIF op IN ('neq', '!=', '<>') THEN
      clause := clause || format(' AND %I <> %L', col, val);
    ELSE
      RAISE EXCEPTION 'unsupported filter op: %', op;
    END IF;
  END LOOP;

  SELECT c.data_type
    INTO key_type
  FROM information_schema.columns c
  WHERE c.table_schema = sch
    AND c.table_name = tbl
    AND c.column_name = keycol;
  numeric_key := key_type IN (
    'smallint', 'integer', 'bigint', 'numeric', 'real', 'double precision'
  );

  IF numeric_key THEN
    order_sql := format('%I ASC', keycol);
    IF p_after IS NOT NULL AND btrim(p_after) <> '' THEN
      clause := clause || format(' AND %I > %L::numeric', keycol, p_after);
    END IF;
  ELSE
    order_sql := format('%I::text ASC', keycol);
    IF p_after IS NOT NULL AND btrim(p_after) <> '' THEN
      clause := clause || format(' AND %I::text > %L', keycol, p_after);
    END IF;
  END IF;

  sql := format(
    'SELECT coalesce(jsonb_agg(to_jsonb(t)), ''[]''::jsonb) FROM (SELECT %s FROM %I.%I%s ORDER BY %s LIMIT %s) t',
    (SELECT string_agg(format('%I', c), ', ') FROM unnest(cols) AS c),
    sch,
    tbl,
    clause,
    order_sql,
    lim
  );
  EXECUTE sql INTO result;
  RETURN coalesce(result, '[]'::jsonb);
END;
$function$;

CREATE TABLE IF NOT EXISTS public.wf_people_status (
  client_tag text NOT NULL,
  source_table text NOT NULL,
  source_key text NOT NULL,
  status text,
  reason text,
  updated_at timestamptz,
  PRIMARY KEY (client_tag, source_table, source_key)
);

ALTER TABLE public.name_bank
  DROP CONSTRAINT IF EXISTS name_bank_rejection_reason_check;

ALTER TABLE public.name_bank
  ADD CONSTRAINT name_bank_rejection_reason_check
  CHECK (
    rejection_reason IS NULL
    OR rejection_reason IN ('title', 'seniority', 'geo', 'company', 'single_name')
  );

CREATE OR REPLACE FUNCTION public.pw_insert_contacts(
  p_schema text,
  p_table text,
  p_rows jsonb
) RETURNS integer
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path TO 'public'
AS $function$
DECLARE
  sch text;
  tbl text;
  rec jsonb;
  existing text[];
  wanted text[] := ARRAY[
    'client_tag', 'domain', 'first_name', 'last_name', 'job_title', 'email',
    'email_type', 'page_url', 'title_match', 'title_rank', 'company_name',
    'source', 'source_url', 'source_tier', 'source_confidence', 'linkedin_url',
    'phone', 'person_city', 'person_state', 'cellphone', 'contact_city',
    'contact_state', 'confidence', 'source_tool', 'updated_at'
  ];
  use_cols text[];
  col text;
  col_sql text;
  val_sql text;
  inserted integer := 0;
  found integer;
  tag text;
  dom text;
  fname text;
  lname text;
  raw text;
BEGIN
  sch := lower(regexp_replace(coalesce(p_schema, 'public'), '[^a-z0-9_]', '', 'g'));
  tbl := lower(regexp_replace(coalesce(p_table, ''), '[^a-z0-9_]', '', 'g'));
  IF sch = '' OR tbl = '' OR to_regclass(format('%I.%I', sch, tbl)) IS NULL THEN
    RAISE EXCEPTION 'unknown contacts table %.%', sch, tbl;
  END IF;

  EXECUTE format('ALTER TABLE %I.%I ADD COLUMN IF NOT EXISTS email_type text', sch, tbl);
  EXECUTE format('ALTER TABLE %I.%I ADD COLUMN IF NOT EXISTS page_url text', sch, tbl);
  EXECUTE format('ALTER TABLE %I.%I ADD COLUMN IF NOT EXISTS title_match boolean', sch, tbl);
  EXECUTE format('ALTER TABLE %I.%I ADD COLUMN IF NOT EXISTS title_rank integer', sch, tbl);
  EXECUTE format('ALTER TABLE %I.%I ADD COLUMN IF NOT EXISTS company_name text', sch, tbl);
  EXECUTE format('ALTER TABLE %I.%I ADD COLUMN IF NOT EXISTS source text', sch, tbl);
  EXECUTE format('ALTER TABLE %I.%I ADD COLUMN IF NOT EXISTS source_url text', sch, tbl);

  SELECT array_agg(c.column_name)
    INTO existing
  FROM information_schema.columns c
  WHERE c.table_schema = sch AND c.table_name = tbl;

  use_cols := ARRAY(
    SELECT u
    FROM unnest(wanted) AS u
    WHERE u = ANY (existing)
  );

  FOR rec IN SELECT * FROM jsonb_array_elements(coalesce(p_rows, '[]'::jsonb))
  LOOP
    tag := coalesce(rec->>'client_tag', '');
    dom := lower(coalesce(rec->>'domain', ''));
    fname := lower(coalesce(rec->>'first_name', ''));
    lname := lower(coalesce(rec->>'last_name', ''));
    IF tag = '' OR dom = '' OR fname = '' THEN
      CONTINUE;
    END IF;
    EXECUTE format(
      'SELECT 1 FROM %I.%I WHERE client_tag = $1 AND lower(coalesce(domain, '''')) = $2 AND lower(coalesce(first_name, '''')) = $3 AND lower(coalesce(last_name, '''')) = $4 LIMIT 1',
      sch, tbl
    ) INTO found USING tag, dom, fname, lname;
    IF found = 1 THEN
      CONTINUE;
    END IF;

    col_sql := '';
    val_sql := '';
    FOREACH col IN ARRAY use_cols
    LOOP
      raw := rec->>col;
      IF col_sql <> '' THEN
        col_sql := col_sql || ', ';
        val_sql := val_sql || ', ';
      END IF;
      col_sql := col_sql || format('%I', col);
      IF col = 'updated_at' THEN
        val_sql := val_sql || 'now()';
      ELSIF raw IS NULL OR raw = '' THEN
        val_sql := val_sql || 'NULL';
      ELSIF col = 'title_match' THEN
        val_sql := val_sql || format('%L::boolean', raw);
      ELSIF col = 'title_rank' THEN
        val_sql := val_sql || format('%L::integer', raw);
      ELSIF col IN ('source_confidence', 'confidence') THEN
        val_sql := val_sql || format('%L::double precision', raw);
      ELSE
        val_sql := val_sql || format('%L', raw);
      END IF;
    END LOOP;
    IF col_sql = '' THEN
      CONTINUE;
    END IF;
    EXECUTE format('INSERT INTO %I.%I (%s) VALUES (%s)', sch, tbl, col_sql, val_sql);
    inserted := inserted + 1;
  END LOOP;
  RETURN inserted;
END;
$function$;
