-- Migration 12: the run ledger — one row per CLI command invocation.
--
-- GUI jobs are CLI subprocesses, so recording at the CLI counts terminal,
-- GUI and scheduled runs alike. Nothing prunes this table and `reset` leaves
-- it alone: it is the lifetime half of the Stats page. A row whose
-- `finished_at` is NULL never finished (still running, or killed outright).

CREATE TABLE IF NOT EXISTS command_runs (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    command      TEXT    NOT NULL,              -- root subcommand: sync, extract, cluster, models, ...
    origin       TEXT    NOT NULL,              -- 'cli' | 'gui' | 'schedule'
    job_id       TEXT,                          -- the GUI job id when origin != 'cli'
    started_at   INTEGER NOT NULL,              -- epoch seconds
    finished_at  INTEGER,
    status       TEXT    NOT NULL,              -- running | ok | failed | cancelled
    stats_json   TEXT                           -- the command's last emitter.result(stats=...)
);

CREATE INDEX IF NOT EXISTS idx_command_runs_command ON command_runs (command, started_at);
CREATE INDEX IF NOT EXISTS idx_command_runs_job ON command_runs (job_id);
