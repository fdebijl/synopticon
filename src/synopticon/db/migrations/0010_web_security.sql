-- Migration 10: web security hardening.
--
-- One migration for five features that all land on the web auth surface:
--   * two-step sign-in            -> web_totp, web_recovery_codes, web_login_challenges
--   * sign-in log                 -> web_auth_log
--   * session pinning             -> web_users.session_pin_mode,
--                                    web_sessions.pin_mode / pin_hash
--   * network allowlist           -> no schema (config only, see [security] allow_from)
--   * login throttling            -> no schema; the throttle is in-memory by
--                                    design and dies with the process, because a
--                                    container restart must stay a valid way out
--                                    of a self-inflicted lockout. Only the
--                                    *record* persists, in web_auth_log.
--
-- Every timestamp is unix seconds (store.now()), matching every other table.
-- Every INTEGER here becomes BIGINT on PostgreSQL through translate_ddl, on a
-- fresh install and an upgrading one alike, so nothing in this migration needs
-- a companion entry in 0009_widen_integers.pg.sql -- and could not have one
-- anyway, since 0009 runs first and would ALTER tables that do not exist yet.

-- Two-step sign-in -----------------------------------------------------------
--
-- One row per user. It exists *unconfirmed* for the length of enrolment (the
-- secret has to be bound before the user has proved their authenticator works)
-- and goes live when confirmed_at is stamped. last_step is the replay guard: a
-- code is accepted only for a time step strictly greater than the last one
-- accepted, so the same six digits cannot be replayed inside their own 30-second
-- window, nor from the skew window either side of it. The guard is enforced in
-- the UPDATE's own WHERE clause, never by a read-then-write in Python -- see
-- twofactor.verify_totp.
--
-- `secret` is plaintext base32, which is what an authenticator app needs and
-- what no amount of hashing can avoid. That makes this table the only directly
-- usable bearer credential in the database, which is why db/snapshot.py's
-- SNAPSHOT_EXCLUDE leaves it out of every backup download.
--
-- created_at is read: twofactor.totp_status compares it against ENROLMENT_TTL to
-- decide whether a pending enrolment is still live, which is what routes 10 and
-- 11 report as `pending_expires_in`.

CREATE TABLE IF NOT EXISTS web_totp (
    user_id      INTEGER PRIMARY KEY REFERENCES web_users(id) ON DELETE CASCADE,
    secret       TEXT    NOT NULL,
    created_at   INTEGER NOT NULL,
    confirmed_at INTEGER,
    last_step    INTEGER
);

-- One row per single-use backup code, stored sha256-hashed like an API key.
-- These are 64-bit CSPRNG strings (`secrets.token_hex(8)`), not human-chosen
-- passwords, so a fast hash is the correct one: it keeps verification a single
-- indexed lookup instead of ten scrypt derivations. The width is load-bearing
-- for that argument and is fixed in twofactor.generate_recovery_codes.
--
-- created_at is read by totp_status, which reports the newest value as
-- `recovery_generated_at` so TwoStepCard can say when this set was issued -- the
-- fact that decides whether a user still has the paper they printed.

CREATE TABLE IF NOT EXISTS web_recovery_codes (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id    INTEGER NOT NULL REFERENCES web_users(id) ON DELETE CASCADE,
    code_hash  TEXT    NOT NULL UNIQUE,
    created_at INTEGER NOT NULL,
    used_at    INTEGER
);

CREATE INDEX IF NOT EXISTS idx_web_recovery_user ON web_recovery_codes (user_id, used_at);

-- A half-finished sign-in: the password step is done, the code step is not.
-- user_id is nullable on purpose -- a *wrong* password on an instance that has
-- a second factor also gets a challenge row, with NULL, so step one's reply is
-- byte-identical whether or not the password was right. username is stored so
-- the throttle keeps its (ip, username) key across both steps, and it is stored
-- TRUNCATED to clientip.USERNAME_MAX by start_login_challenge -- it is unbounded
-- request input and this table has a row cap, not a byte cap.
--
-- There is deliberately no created_at: expires_at is the only time this table's
-- readers ask about, and a column nothing reads is a column the next
-- implementer either deletes or misuses. The row cap in start_login_challenge
-- orders by expires_at, which is monotonic in insertion order for a fixed TTL,
-- and it is spelled out as a dialect-safe statement there (F5) -- the obvious
-- `DELETE ... ORDER BY ... LIMIT` form is SQLite-only and would fail on
-- PostgreSQL under exactly the flood the cap exists for.

CREATE TABLE IF NOT EXISTS web_login_challenges (
    token_hash TEXT    PRIMARY KEY,
    username   TEXT    NOT NULL,
    user_id    INTEGER REFERENCES web_users(id) ON DELETE CASCADE,
    expires_at INTEGER NOT NULL,
    attempts   INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_web_challenges_expiry ON web_login_challenges (expires_at);

-- Sign-in log ----------------------------------------------------------------
--
-- What was *attempted*, never a credential: no password (not even its length),
-- no session token or token hash, no API key, not even a key prefix -- a
-- presented-but-wrong key is a near miss of a real secret.
--
-- Deliberately no foreign key to web_users: an attempt names a string somebody
-- typed, which usually matches no row, and the record must outlive any account
-- it refers to. user_id is a convenience back-reference, set only when the
-- attempt resolved to a real account.
--
-- `event` carries authlog.AUTH_EVENTS. There is no `scope` column: the
-- throttle's scopes are a different vocabulary with a different lifetime, and
-- authlog's mapping table (section 4.6) is where the two are related.
--
-- Nothing reads this table to make a decision. It is evidence, never a gate --
-- and it is best-effort evidence: record_attempt never raises, so under write
-- contention an entry can be dropped rather than failing the request.

CREATE TABLE IF NOT EXISTS web_auth_log (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    ts         INTEGER NOT NULL,
    event      TEXT    NOT NULL,
    outcome    TEXT    NOT NULL,
    reason     TEXT,
    username   TEXT,
    user_id    INTEGER,
    ip         TEXT,
    user_agent TEXT
);

CREATE INDEX IF NOT EXISTS idx_web_auth_log_ts ON web_auth_log (ts DESC);
CREATE INDEX IF NOT EXISTS idx_web_auth_log_outcome ON web_auth_log (outcome, id DESC);
-- Read by authlog.auth_log's `ip` filter, which route 18 exposes as `&ip=` --
-- "what else came from this address" is the first question anyone asks of a
-- suspicious row, and without the index it is a full scan of up to 5000 rows on
-- the event loop's threadpool.
CREATE INDEX IF NOT EXISTS idx_web_auth_log_ip ON web_auth_log (ip, ts DESC);

-- Session pinning ------------------------------------------------------------
--
-- web_users.session_pin_mode is the account's *setting* -- what a new session
-- gets pinned to. web_sessions.pin_mode / pin_hash are the enforcement datum,
-- denormalised onto the session row on purpose: validate_session has to decide
-- with the single lookup it already does, on the hot path of every request,
-- without a join to web_users. (That denormalisation is exactly why the
-- `synopticon session-pin` CLI command has to clear BOTH -- see section 7.3.)
--
-- NULL pin_mode means an unpinned session (created while the setting was 'off',
-- or before this migration). pin_hash is a sha256 of the client facts; the facts
-- themselves are never stored -- a database copy must not hand someone the
-- string they need to forge a pin. (It is also why a pin violation cannot report
-- what it expected, only what it saw.)
--
-- All three columns are TEXT because that is what they hold: two short mode
-- names and a hex digest. (An earlier draft claimed the TEXT choice was
-- load-bearing for test_db_dialect's widening invariant. It is not: section 1.3
-- scopes _declared_integer_columns to the migrations *before* 0009, so 0010 is
-- excluded whatever its column types. Do not re-derive a rule from this
-- comment.)

ALTER TABLE web_users ADD COLUMN session_pin_mode TEXT NOT NULL DEFAULT 'off';

ALTER TABLE web_sessions ADD COLUMN pin_mode TEXT;

ALTER TABLE web_sessions ADD COLUMN pin_hash TEXT;
