-- Baseline indexes for time-ordered queries on audit, grab, and search history.
CREATE INDEX IF NOT EXISTS ix_grab_history_created_at ON grab_history (created_at DESC);
CREATE INDEX IF NOT EXISTS ix_search_logs_created_at ON search_logs (created_at DESC);
CREATE INDEX IF NOT EXISTS ix_audit_logs_created_at ON audit_logs (created_at DESC);

