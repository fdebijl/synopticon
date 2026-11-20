# ADR 23 — An MCP endpoint for agents, behind scoped tokens

**Status:** Accepted
**Applies to:** `web/mcp/` (`registry.py`, `protocol.py`, `routes.py`, `tools_core.py`, `tools_diag.py`), `web/auth/mcp_tokens.py`, migration `0014_mcp_tokens.sql`, `config.py::McpConfig`, `web/app.py`'s `/mcp` branch in `_AuthMiddleware`, `runlog.origin_from_env`

## Context

Most of what goes wrong in a Synopticon install is a tuning problem: a detection threshold that
misses small faces, a grouping threshold that splits one person over a dozen groups or fuses two
siblings into one, a Synology library whose labels disagree with ours. Diagnosing one means reading
several tables side by side, changing a setting, re-running Group faces and comparing. That loop is
tedious for a person and well suited to an AI agent, provided the agent can see the data and run the
steps, and cannot do anything the operator did not mean to allow.

The web GUI already has a REST API and API keys, but an API key is all-or-nothing: it reaches every
route a signed-in user reaches except the human-only ones. Handing that to an agent would grant
Apply, and the REST shapes were built for the SPA, not for a model reading tool descriptions.

## Decision

`synopticon web` serves a Model Context Protocol endpoint at `/mcp`, **off by default**
(`[mcp] enabled = false`). Turning it on is a human act: the setting is read once at start-up, the
config write route is human-only, and the MCP itself can never write `[mcp]` or `[security]`.

### Transport and protocol

Streamable HTTP on the existing web process, hand-rolled: stateless JSON-RPC 2.0 over `POST`,
answered with `application/json`, no SSE stream, no session ids. Only `initialize`, `ping`,
`tools/list` and `tools/call` are implemented. Synopticon is usually a Docker container on a NAS
or a homelab box, so an agent is rarely on the same filesystem; stdio would also have had no
credential to carry scopes. Writing the protocol by hand keeps the dependency list and the
middleware stack unchanged: the official SDK brings its own Starlette app, session manager lifespan
and SSE plumbing, all of which would have to be re-audited against ADR 07.

The endpoint sits after the network allowlist in `_AuthMiddleware` and before any cookie handling.
It authenticates itself. Tool handlers run in the threadpool, so ADR 07's invariants hold
unchanged; `get_environment` and `check_nas` shell out to `synopticon hwinfo` / `check` rather than
import `diagnostics.py`, which would load onnxruntime into the web process.

### Credentials: a separate token kind with scopes

`web_mcp_tokens` is its own table, not a column on `web_api_keys`. An MCP token (`synm_…`) is
accepted only on `/mcp`; REST API keys and session cookies are refused there, and an MCP token is
unknown to every `/api/*` route because `validate_api_key` never reads its table. Each credential
keeps exactly the reach it was minted for, and existing API keys keep their behaviour.

Scopes (`registry.SCOPES`):

| Scope | Grants |
|---|---|
| `read` | always; status, config, job logs, review queue, diagnostics, crops, photo previews |
| `config` | write `[inference]`, `[detection]`, `[restoration]`, `[clustering]`, `[crossref]` |
| `jobs` | NAS-read-only jobs, plus dry-run Apply and Dedupe |
| `review` | approve / reject / hide / reset review items (no NAS) |
| `maintenance` | the confirm-level jobs: clear-queue, clear-applies, prune-queue, delete-crops, reset |
| `apply` | `apply --apply` (assign, low_confidence, new_person) |
| `apply:reassign` | adds `--apply-reassigns`; requires `apply` |
| `apply:merge` | adds `--apply-merges`; requires `apply` |

`run_job` decides the scope from the **resolved argv**, after `validate_consent` has run, not from
the job name: an apply whose argv carries `--apply-merges` needs `apply:merge` however the request
spelled it. The MCP submits with `confirm=True` (holding the scope is the confirmation) and
`confirm_phrase=None`, always. **No scope reaches a typed-phrase gate** — merging two named people,
deleting duplicates, `reset --all` — for the same reason schedules cannot (ADR 12): the phrase
exists to make a human stop and read, and a token minted last month is not that.

### Tuning safety

`update_config` refuses while a job is queued or running (as `PUT /api/config` does), and refuses a
change that alters `pipeline_version` unless the call passes `confirm_redetect`: such a change makes
the next Detect faces run redo every photo and orphans the review items proposed before it
(ADR 15). Jobs are subprocesses that load config at start, so a tuning change applies to the very
next job without a restart; `get_config` reloads from disk for the same reason.

### Diagnostics

The diagnostics tools match our faces to Synology's boxes with the same IoU rule crossref uses, so
a "missed" or "extra" verdict agrees with what clustering sees. Unlisted Synology persons are
reported but flagged, never treated as labels (ADR 19, ADR 20). Whole-library computations are
cached behind a fingerprint, like the Stats page's.

## Alternatives considered

- **Scopes on every API key.** Would have needed a migration that guesses a scope set for existing
  keys, and a scope check on every REST route. A separate token kind needed neither.
- **A stdio server (`synopticon mcp`).** No credential, so scopes would have come from a CLI flag
  the agent's own config controls. Rejected with the user.
- **A grantable scope for typed-phrase operations.** Rejected with the user: see above.
- **The official `mcp` SDK.** See *Transport and protocol*.

## Consequences

- A new auth surface. It inherits the network allowlist and the sign-in log's failed-credential
  recording, but not login throttling: like an API key, a token is 128 random bits.
- MCP-started jobs record `origin = "mcp"` in `command_runs` and the job list.
- Anything new an agent should be able to do is a new tool in `tools_core.py` or `tools_diag.py`
  with an explicit scope. A tool that writes to the NAS outside `run_job` would bypass the consent
  model and must not be added.
