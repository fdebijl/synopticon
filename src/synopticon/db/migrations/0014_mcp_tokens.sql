-- Migration 14: MCP access tokens.
--
-- Mirrors web_api_keys, plus the scopes a token may use. They authenticate only
-- the /mcp endpoint and are never accepted on /api/*. Only the sha256 hash and a
-- short non-secret prefix are stored, so the table is safe to keep in backups.

CREATE TABLE IF NOT EXISTS web_mcp_tokens (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    name         TEXT    NOT NULL,
    token_hash   TEXT    NOT NULL UNIQUE,
    token_prefix TEXT    NOT NULL,
    scopes_json  TEXT    NOT NULL,
    created_at   INTEGER NOT NULL,
    last_used_at INTEGER,
    revoked      INTEGER NOT NULL DEFAULT 0
);
