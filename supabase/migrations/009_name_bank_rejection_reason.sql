-- Why a name_bank row failed the profile gate.
-- title | seniority | geo | company. Null on rows written before this column.

ALTER TABLE public.name_bank
  ADD COLUMN IF NOT EXISTS rejection_reason text;

ALTER TABLE public.name_bank
  DROP CONSTRAINT IF EXISTS name_bank_rejection_reason_check;

ALTER TABLE public.name_bank
  ADD CONSTRAINT name_bank_rejection_reason_check
  CHECK (
    rejection_reason IS NULL
    OR rejection_reason IN ('title', 'seniority', 'geo', 'company')
  );
