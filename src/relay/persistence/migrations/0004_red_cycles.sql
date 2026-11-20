-- Each time a red is reopened a new cycle begins. A run that started in an earlier
-- cycle is stale when it returns and must not fill or answer the current one.
ALTER TABLE behaviours ADD COLUMN red_cycle integer NOT NULL DEFAULT 0;
