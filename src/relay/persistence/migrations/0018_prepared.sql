-- An action someone proposes that the Product Owner take: a model in the live session,
-- usually. It is nothing until the person confirms it at a terminal of their own, with
-- the passphrase. What was prepared is kept exactly: the project, the action, what it
-- is about, what would be said, and a digest of the record as the preparer saw it. If
-- that record has moved by the time it is confirmed, or its item is no longer waiting,
-- the action is stale and nothing is done. A code is confirmed once; unused, it expires.
CREATE TABLE prepared_actions (
    code        text PRIMARY KEY,
    project_id  text NOT NULL REFERENCES projects,
    verb        text NOT NULL,
    target      text NOT NULL,
    said        text,
    reason      text,
    -- Why the preparer proposes it, in the preparer's words: shown to the person.
    why         text NOT NULL,
    -- What the preparer was shown of the record, and its digest.
    shown       text NOT NULL,
    basis       text NOT NULL,
    prepared_at timestamptz NOT NULL DEFAULT now(),
    expires_at  timestamptz NOT NULL,
    -- Found stale or expired when it came to be confirmed: said once, and final.
    ended       text
);
