-- When a turn's call can no longer be running: its own deadline, set when it is recorded
-- from the limit its own host gave the call. A turn still open after that was left by a
-- host that died, and nobody knows how its call ended. Read from here by whoever asks,
-- whenever they ask: recovery does not wait for somebody to open another session.
-- Null for a turn recorded before this was kept.
ALTER TABLE conversation_turns ADD COLUMN gives_up_at timestamptz;
