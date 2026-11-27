-- The installation: one row, made with the database. Everything that is the same for
-- every project and every process is here, and nowhere a process could find a
-- different answer by being started somewhere else.
CREATE TABLE installation (
    singleton       boolean PRIMARY KEY DEFAULT true CHECK (singleton),
    -- What transport and refs are named under: two installations that share a Redis or
    -- a repository do not read each other's deliveries or take each other's attempts.
    installation_id uuid NOT NULL,
    -- The control plane's term. Each `relay up` that takes the lock opens a new one, and
    -- every transaction of a control plane that changes workflow state is of its term
    -- or is refused: holding a lock says who may start, and this stops one that lost it.
    term            bigint NOT NULL DEFAULT 0,
    -- Where workspaces and mirrors are kept, where agent definitions are looked for,
    -- each role's profile, the oracle's image.
    settings        jsonb NOT NULL DEFAULT '{}',
    -- The Product Owner's passphrase, as a salted scrypt hash and nothing else.
    passphrase      text,
    -- The operator's statement of the policy this host is given from elsewhere (D-10):
    -- what it says, who stated it, when, and which revision of the statement it is.
    policy          jsonb
);
INSERT INTO installation (installation_id) VALUES (gen_random_uuid());

-- The term of the control plane that started an effect; null for a worker's. An effect
-- started under an earlier term than the one there is now is nobody's: whoever started
-- it can no longer say it is done.
ALTER TABLE invocation_effects ADD COLUMN term bigint;

-- Each send of an intent, recorded before it leaves. What Git said to it is recorded
-- here once, by whoever hears it, whatever term it is by then: that a send was refused
-- is a fact and not a decision. What follows from it is decided by the control plane of
-- the day, which counts each answer once.
CREATE TABLE integration_sends (
    send_id    uuid PRIMARY KEY,
    intent_id  uuid NOT NULL REFERENCES integration_intents,
    heard      text CHECK (heard IN ('refused', 'never_left')),
    reason     text,
    counted    boolean NOT NULL DEFAULT false,
    started_at timestamptz NOT NULL DEFAULT now(),
    CHECK ((heard IS NULL) = (reason IS NULL)),
    CHECK (NOT counted OR heard IS NOT NULL)
);

CREATE INDEX integration_sends_of_intent ON integration_sends (intent_id);
