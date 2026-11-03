CREATE INDEX IF NOT EXISTS scheduled_jobs_active_rendered_watch_mode_idx
    ON scheduled_jobs ((payload->'mode'))
    WHERE kind='watch' AND status='active';
