-- Keyset pages compared id::text, so a cursor of "9999" never reached id 10000
-- ("10000" < "9999" lexicographically) and iter_source stopped on the short page.

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
