-- Migration 6: web GUI authentication (users, sessions, API keys).
--
-- Backs the web GUI's mandatory login and named revocable API keys. All secrets
-- are stored hashed, never in plaintext: passwords via scrypt with a per-user
-- salt (web_users.password_scrypt / salt), session tokens and API keys as their
-- sha256 hash (token_hash / key_hash). web_api_keys.key_prefix keeps a short
-- non-secret identifier ("syn_" + first 8 chars) so keys are distinguishable in
-- the UI without ever revealing the full key. Sessions cascade-delete with their
-- owning user. These tables are web-only and independent of the pipeline schema.

CREATE TABLE web_users (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    username        TEXT    UNIQUE NOT NULL,
    password_scrypt BLOB    NOT NULL,
    salt            BLOB    NOT NULL,
    created_at      INTEGER NOT NULL
);

CREATE TABLE web_sessions (
    token_hash   TEXT    PRIMARY KEY,
    user_id      INTEGER NOT NULL REFERENCES web_users(id) ON DELETE CASCADE,
    created_at   INTEGER NOT NULL,
    expires_at   INTEGER NOT NULL,
    last_seen_at INTEGER
);

CREATE INDEX idx_web_sessions_user ON web_sessions (user_id);

CREATE TABLE web_api_keys (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    name         TEXT    NOT NULL,
    key_hash     TEXT    UNIQUE NOT NULL,
    key_prefix   TEXT    NOT NULL,
    created_at   INTEGER NOT NULL,
    last_used_at INTEGER,
    revoked      INTEGER NOT NULL DEFAULT 0
);
