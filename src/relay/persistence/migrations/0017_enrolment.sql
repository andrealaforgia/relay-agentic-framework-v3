-- A request for runs of the boundary suite, on record before anything is launched and
-- before anything is known to be needed: which installation it was made of, whether it
-- retries an attempt, and the whole set of boundaries it asked to have tried, which may
-- be none. Sent again, a request asks for what it asked for then, and for nothing that
-- has come to be owed since.
CREATE TABLE boundary_requests (
    request_id   text PRIMARY KEY,
    installation uuid NOT NULL,
    retries      uuid,
    -- [{"role": ..., "digest": ...}], in the order they are tried.
    asked        jsonb NOT NULL,
    created_at   timestamptz NOT NULL DEFAULT now()
);

-- A run of the boundary suite is paid for. Each is on record before anything is
-- launched, under the request that asked for it, so that asking again buys nothing
-- more. Its verdict is the row of `boundary_passes` with the same identity.
--
-- What became of the paid call is kept beside the verdict, since they are not the same
-- thing: a call that was launched and ended with no result has no verdict worth having
-- and may have been paid for; a run that never reached the provider cost nothing.
--   not_launched: known never to have started. Nothing was spent.
--   call = 'uncertain': launched, and nobody can say how it ended.
-- With neither and no verdict, the attempt is running while the process that launched it
-- lives (that process holds a session lock named by the attempt), and uncertain once it
-- does not. An uncertain attempt is never repeated by itself. A retry is an attempt of
-- its own, naming the one it retries, and that one is retried once.
CREATE TABLE boundary_runs (
    attempt_id   uuid PRIMARY KEY,
    request_id   text NOT NULL REFERENCES boundary_requests,
    role         text NOT NULL,
    digest       text NOT NULL,
    runtime      jsonb NOT NULL,
    retries      uuid UNIQUE REFERENCES boundary_runs,
    not_launched text,
    call         text,
    started_at   timestamptz NOT NULL DEFAULT now(),
    UNIQUE (request_id, role)
);

CREATE INDEX boundary_runs_by_digest ON boundary_runs (digest, started_at DESC);

-- What a command that changed the installation did, kept with the change: the passphrase
-- changed, a statement of policy made. Sent again under its identity, the request is
-- answered from here and changes nothing more. Nothing secret is kept: of a passphrase,
-- only that it was changed.
CREATE TABLE installation_receipts (
    request_id   text PRIMARY KEY,
    installation uuid NOT NULL,
    action       text NOT NULL,
    -- A digest of what was asked, so that an identity used again for something else is
    -- refused and not answered with another request's outcome.
    asked        text NOT NULL,
    outcome      jsonb NOT NULL,
    at           timestamptz NOT NULL DEFAULT now()
);

-- A handle names one request: for one action, asking one thing. It is taken here, once,
-- before the command that uses it does anything or asks anybody anything: whichever
-- command takes a handle first has it, and another command given the same handle, on
-- this machine or any other, is refused before it acts.
CREATE TABLE request_handles (
    request_id text PRIMARY KEY,
    action     text NOT NULL,
    -- A digest of what was asked: the statement of policy, the attempt retried.
    asked      text NOT NULL,
    at         timestamptz NOT NULL DEFAULT now()
);
