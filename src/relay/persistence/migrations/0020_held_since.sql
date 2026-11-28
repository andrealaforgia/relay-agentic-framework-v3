-- Since when a piece of work has been held, without a break, under the grant it has now.
-- Kept by the table itself, so that every route that grants (a worker's claim, the
-- control plane taking up a red, a grant given back when a call reports late) is
-- covered alike, and a hold that was interrupted starts again when it is resumed.
-- Null whenever the work is not held; and null for work that was already held when this
-- was added, whose hold nobody timed.
ALTER TABLE assignments ADD COLUMN held_since timestamptz;

CREATE FUNCTION assignments_time_the_hold() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.grant_id IS NULL OR NEW.state <> 'granted' THEN
        NEW.held_since := NULL;
    ELSIF TG_OP = 'INSERT'
       OR OLD.grant_id IS DISTINCT FROM NEW.grant_id
       OR OLD.state <> 'granted' THEN
        NEW.held_since := clock_timestamp();
    ELSE
        NEW.held_since := OLD.held_since;
    END IF;
    RETURN NEW;
END
$$;

CREATE TRIGGER assignments_time_the_hold
    BEFORE INSERT OR UPDATE ON assignments
    FOR EACH ROW EXECUTE FUNCTION assignments_time_the_hold();
