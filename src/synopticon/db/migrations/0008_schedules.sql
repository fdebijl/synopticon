-- 0008: user-defined job schedules for the web GUI.
--
-- A schedule is a saved, consent-validated job submission plus a cron
-- expression. The scheduler thread in the web process fires them; nothing else
-- reads these tables, but they live in the pipeline DB like every other piece of
-- durable state so a container restart keeps them.
--
-- `confirm` is the stored answer to the job's plain confirmation gate. The
-- typed-phrase tier is deliberately not representable here: `web/schedules.py`
-- validates every schedule with `confirm_phrase=None`, so a job form that needs
-- a phrase (merge_named, dedupe --apply, reset --all) simply cannot be saved.

CREATE TABLE IF NOT EXISTS schedules (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    name         TEXT    NOT NULL,
    job          TEXT    NOT NULL,
    params_json  TEXT    NOT NULL DEFAULT '{}',
    confirm      INTEGER NOT NULL DEFAULT 0,
    cron         TEXT    NOT NULL,
    timezone     TEXT,
    enabled      INTEGER NOT NULL DEFAULT 1,
    created_at   INTEGER NOT NULL,
    updated_at   INTEGER NOT NULL,
    next_run_at  INTEGER,
    last_run_at  INTEGER,
    last_job_id  TEXT,
    last_status  TEXT
);

CREATE INDEX IF NOT EXISTS idx_schedules_due ON schedules(enabled, next_run_at);

-- One row per firing attempt, including the ones that did not start a job
-- (`skipped` when the same command is already in flight, `missed` for an
-- occurrence that fell while the server was down, `error` for a rejected
-- submission). Without these a schedule that never runs looks identical to one
-- that runs fine.
CREATE TABLE IF NOT EXISTS schedule_runs (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    schedule_id  INTEGER NOT NULL REFERENCES schedules(id) ON DELETE CASCADE,
    fired_at     INTEGER NOT NULL,
    job_id       TEXT,
    status       TEXT    NOT NULL,
    detail       TEXT
);

CREATE INDEX IF NOT EXISTS idx_schedule_runs ON schedule_runs(schedule_id, fired_at DESC);
