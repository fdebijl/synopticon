# MCP server (agent access)

Synopticon can expose a [Model Context Protocol](https://modelcontextprotocol.io) endpoint so an AI agent, such as Claude Code, can work on your setup with you. It is meant for:

- **Tuning settings.** Read the current configuration with its documentation, change detection and grouping thresholds, re-run a step and look at the effect.
- **Debugging.** Find out why faces were missed or mis-detected during Detect faces, and why people were split or mixed during Group faces, with face crops and annotated photo previews.
- **Running pipeline steps** (Sync, Detect faces, Group faces, reports) and following their logs.
- **Deciding review items and applying them**, if you grant a token those powers.

The endpoint is off by default, and every token only carries the scopes you pick.

## Enable it

Turn on **Settings → MCP → Enabled** and restart Synopticon, or set `[mcp] enabled = true` in `config.toml` (`SYNOPTICON_MCP__ENABLED=true` in the environment) and restart. While it is off, `/mcp` answers 404 to everything.

## Create a token

Open **Settings → Access → MCP tokens**, give the token a name, tick the scopes it needs and create it. The secret (`synm_…`) is shown once, together with ready-to-copy connection snippets. You can create tokens while MCP is still off, and revoke them at any time.

## Connect a client

Claude Code:

```bash
claude mcp add --transport http synopticon https://synopticon.example.com/mcp \
  --header "Authorization: Bearer synm_..."
```

Any other client that speaks streamable HTTP takes the same URL and header:

```json
{
  "mcpServers": {
    "synopticon": {
      "type": "http",
      "url": "https://synopticon.example.com/mcp",
      "headers": { "Authorization": "Bearer synm_..." }
    }
  }
}
```

The server is stateless: plain JSON responses over POST, no sessions and no event stream.

## Scopes

`read` is on every token. A tool is only listed to a token that can use it.

| Scope | Danger | What it allows |
|---|---|---|
| `read` | safe | Status, settings, job logs, the review queue, and face detection and grouping diagnostics, including face crops and photo previews. |
| `config` | local | Change the Inference, Face detection, Restoration, Face grouping and Crossref settings. Never NAS, Storage, Database, Security or MCP settings. |
| `jobs` | local | Start and cancel Sync, Detect faces, Group faces, reports, crop regeneration, benchmarks and model downloads, plus dry-run previews of Apply and Dedupe. None of these write to the NAS. |
| `review` | local | Approve, reject, hide or reset suggestions in the review queue. Nothing reaches the NAS until an Apply runs. |
| `maintenance` | local | Clear the review queue or apply history, prune orphaned suggestions, delete crops, and reset derived data (never a full reset). |
| `apply` | nas | Write approved assign, low-confidence and new-person suggestions to Synology Photos. |
| `apply:reassign` | nas | Also move faces Synology already tagged to a different person. Needs `apply`. |
| `apply:merge` | nas | Also merge an unnamed person into another. Needs `apply`. Merging two named people is never available to an agent. |

## Tools

**Status**
- `get_overview`: version, this token's scopes, library statistics, review queue counts, whether detection is up to date, the latest run of each command and active jobs.
- `get_environment`: hardware and runtime diagnostics. `check_nas`: the read-only NAS connectivity probe.

**Settings**
- `get_config`: current values with defaults, descriptions, allowed ranges and which changes force re-detection.
- `update_config` (`config`): change settings in the tunable sections. Refused while a job is queued or running. A change that would re-detect every photo is refused unless the agent confirms it explicitly.

**Jobs**
- `list_jobs`, `get_job` (progress and log events, with a cursor), `list_runs` (the run history).
- `run_job` (`jobs`, `maintenance` or `apply`, depending on the job and its flags) and `cancel_job` (`jobs`).

**Review**
- `list_review_items`, `get_review_counts`.
- `decide_review_items` (`review`): approve, reject, hide, or put items back to pending.

**Diagnostics**
- Detection: `detection_summary`, `find_detection_gaps` (faces Synology sees that we missed, detections Synology does not have, boxes that only line up after correcting a photo's rotation, low scores, tiny faces, skipped photos), `list_extract_skips`, `get_face_crops`, `inspect_photo`, `render_photo` (a preview with our boxes and Synology's drawn on it).
- Grouping: `grouping_summary`, `find_grouping_issues` (people split over many groups, groups that mix people, large unmatched groups, weak matches), `get_cluster`, `get_person`, `explain_face` (a face's nearest neighbours and how they compare with the grouping threshold).

## Safety

- It is off by default, and a human has to enable it and restart.
- MCP tokens work on `/mcp` only. REST API keys and browser sessions are refused there, and MCP tokens are refused on `/api/*`.
- The [network allowlist](configuration.md) (`[security] allow_from`) applies to `/mcp` like to everything else, and failed tokens are written to the sign-in log. Sign-in throttling does not: like an API key, a token is 128 random bits and cannot be guessed.
- Anything behind a typed confirmation phrase (merging two named people, deleting duplicates, a full reset) cannot be done by an agent, whatever its scopes. Neither can the apply tiers a token was not granted. The usual dry-run, approval and audit rules in the [safety model](safety-model.md) still hold, and agent actions are recorded as `mcp` in the run history.
- `update_config` cannot touch NAS, Storage, Database, Security or MCP settings, and refuses while a job is running. A change that would make the next Detect faces run redo every photo is refused unless the agent passes `confirm_redetect`; review suggestions made before such a change lose their faces, so expect the agent to ask you first.
- When Synopticon is reachable beyond your own network, put it behind HTTPS with a reverse proxy; a token is a bearer credential.

## Example session

> **You:** Faces in my backlit photos keep getting missed. Can you look into it?
>
> The agent calls `get_overview` and `detection_summary`, sees many photos with zero faces, then `find_detection_gaps` with kind `missed` and `render_photo` on a few of them. It reads the `[detection]` settings with `get_config`, proposes lowering the detection score threshold, and, because that changes what Detect faces produces for every photo, asks you before calling `update_config` with `confirm_redetect`. It starts Detect faces with `run_job`, follows it with `get_job`, then re-runs `find_detection_gaps` to check that the gaps shrank and `find_detection_gaps` with kind `extra` to check it has not added false positives.
>
> Grouping works the same way: `grouping_summary`, `find_grouping_issues`, `explain_face` on a pair that should not have been joined, an `update_config` on the grouping threshold, and a Group faces run.
