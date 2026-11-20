-- One row per project that every workflow transaction locks first: shared to authorise or
-- record work, exclusive to pause, resume or cancel. It is not the project's own row,
-- because the ledger writes to that one and would turn shared holders into exclusive.
CREATE TABLE project_gates (
    project_id text PRIMARY KEY REFERENCES projects
);
