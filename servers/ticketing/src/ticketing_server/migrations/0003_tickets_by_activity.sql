-- list_tickets orders by recent activity: updated_at, then id, newest first.
CREATE INDEX tickets_by_activity ON tickets (updated_at DESC, id DESC);
