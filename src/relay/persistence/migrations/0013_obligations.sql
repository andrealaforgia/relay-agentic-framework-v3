-- Everything a candidate must still satisfy, each with an identity: one per required
-- project check, one per behaviour on trunk. A candidate's regression set is the active
-- ones, less any it is exempt from by identity, and only then one check per digest: two
-- obligations that ask for the same observation are still two obligations.
CREATE TABLE obligations (
    obligation_id uuid PRIMARY KEY,
    project_id    text NOT NULL REFERENCES projects,
    source        text NOT NULL CHECK (source IN ('check', 'behaviour')),
    name          text NOT NULL,
    behaviour_id  text,
    contract      jsonb NOT NULL,
    digest        text NOT NULL,
    state         text NOT NULL DEFAULT 'active' CHECK (state IN ('active', 'retired')),
    -- The supersession that retired it, when its replacement reached trunk.
    retired_by    uuid,
    created_at    timestamptz NOT NULL DEFAULT clock_timestamp(),
    CHECK ((source = 'behaviour') = (behaviour_id IS NOT NULL)),
    FOREIGN KEY (project_id, behaviour_id) REFERENCES behaviours
);

-- The proposal, and then the authority, to replace one obligation by one behaviour.
-- proposed: waiting at its gate. standing: approved, and usable by a candidate of the
-- replacing behaviour. reserved: bound to an integration that was authorised with it,
-- until that integration settles. used: the replacement reached trunk and the
-- obligation is retired. void, sent_back, withdrawn: ended without replacing anything.
CREATE TABLE supersessions (
    supersession_id uuid PRIMARY KEY,
    project_id      text NOT NULL REFERENCES projects,
    obligation_id   uuid NOT NULL REFERENCES obligations,
    behaviour_id    text NOT NULL,
    reason          text NOT NULL,
    proposed_by     text NOT NULL,
    -- Exactly what the Product Owner is asked to approve. If any of it changes, the
    -- proposal or the authority no longer stands.
    basis           jsonb NOT NULL,
    state           text NOT NULL DEFAULT 'proposed' CHECK (state IN
        ('proposed', 'standing', 'reserved', 'used', 'void', 'sent_back', 'withdrawn')),
    gate_id         uuid REFERENCES gates,
    intent_id       uuid,
    ended_because   text,
    proposed_at     timestamptz NOT NULL DEFAULT clock_timestamp(),
    FOREIGN KEY (project_id, behaviour_id) REFERENCES behaviours
);

-- One replacement per obligation, and one obligation per replacing behaviour, among
-- those that are still live.
CREATE UNIQUE INDEX one_replacement_per_obligation ON supersessions (obligation_id)
    WHERE state IN ('proposed', 'standing', 'reserved');
CREATE UNIQUE INDEX one_obligation_per_replacement ON supersessions (project_id, behaviour_id)
    WHERE state IN ('proposed', 'standing', 'reserved');

-- What replaced a behaviour on trunk, once that has happened. The behaviour stays in its
-- story's list as history.
ALTER TABLE behaviours ADD COLUMN superseded_by text;

-- The exemption a batch of checks was started with, and an integration authorised with.
ALTER TABLE check_batches ADD COLUMN supersession_id uuid REFERENCES supersessions;
ALTER TABLE integration_intents ADD COLUMN supersession_id uuid REFERENCES supersessions;
