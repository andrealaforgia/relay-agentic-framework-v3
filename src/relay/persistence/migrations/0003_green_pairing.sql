-- A green counts only when paired with the red that preceded it, so the behaviour
-- remembers which run that was and which candidate was last judged.
ALTER TABLE behaviours ADD COLUMN red_execution_id uuid REFERENCES executions;
ALTER TABLE behaviours ADD COLUMN candidate_revision text
    CHECK (candidate_revision ~ '^[0-9a-f]{40}$');
