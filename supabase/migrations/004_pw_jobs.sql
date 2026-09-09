-- Job snapshots for get_job_status. Read/write from the service role.
-- Does not affect waterfall write tables.

CREATE TABLE IF NOT EXISTS public.pw_jobs (
  id text PRIMARY KEY,
  kind text NOT NULL DEFAULT 'unknown',
  status text NOT NULL DEFAULT 'unknown',
  payload jsonb NOT NULL DEFAULT '{}'::jsonb,
  updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS pw_jobs_updated_at_idx ON public.pw_jobs (updated_at DESC);

ALTER TABLE public.pw_jobs ENABLE ROW LEVEL SECURITY;
