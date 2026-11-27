-- Taken by the confirmation that is about to carry it out: once, and only while it is
-- neither ended nor expired. From then on the code is that confirmation's, to be decided
-- under its request and answered by that request's receipt: it no longer expires.
ALTER TABLE prepared_actions ADD COLUMN consumed_by text;

-- Which passphrase this is: raised each time it is set or changed. A decision confirmed
-- by a passphrase says which one, and is refused where it is decided if the passphrase
-- has been changed since it was checked.
ALTER TABLE installation ADD COLUMN passphrase_revision integer NOT NULL DEFAULT 0;

-- An installation whose passphrase was set by an earlier build has had one passphrase.
UPDATE installation SET passphrase_revision = 1 WHERE passphrase IS NOT NULL;

-- How the person who made a decision was known to be the Product Owner: for a decision
-- made through the command line, that the passphrase was read at the controlling
-- terminal, and which passphrase. Null for a decision made by a caller of the service
-- that established its principal some other way.
ALTER TABLE human_actions ADD COLUMN confirmed jsonb;
