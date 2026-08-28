![Synopticon logo](https://raw.githubusercontent.com/fdebijl/synopticon/main/assets/Synopticon%20Hero.png)

**Synopticon supplements Synology Photos' face recognition.** It syncs your photo library from the NAS, runs an ensemble face pipeline over it, groups the faces, cross-references the groups against the people Synology already knows, and — only after you approve each suggestion — writes the corrections back through Synology Photos' Person API. It also finds and removes duplicate photos.

Everything before the apply step is **read-only toward your NAS**, and writes are dry-run by default.

📖 **Full documentation, CLI reference and configuration:** [github.com/fdebijl/synopticon](https://github.com/fdebijl/synopticon)

## Tags

| Tag | Variant | Notes |
|---|---|---|
| `latest`, `cpu` | CPU | Runs anywhere. `latest` is deliberately the CPU build. |
| `gpu` | CUDA 12 | NVIDIA hosts. Needs the [NVIDIA Container Toolkit](https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/latest/install-guide.html). Much faster face detection. |
| `0.12.2`, `0.12.2-cpu`, `0.12.2-gpu` | both | Pin to an exact release. |
| `0.12`, `0.12-cpu`, `0.12-gpu` | both | Track patch releases only. |
| `sha-<short>-cpu`, `sha-<short>-gpu` | both | One exact commit. |

`linux/amd64` only — NVIDIA ships no arm64 CUDA wheels.

## Quick start

The entrypoint is `synopticon`, so anything after the image name is a subcommand. Start with the web GUI: it walks you through NAS credentials, downloads the models, and runs every job with live progress.

```bash
mkdir -p data models

docker run -d --name synopticon \
  -p 127.0.0.1:8686:8686 \
  -v "$PWD/data:/data" \
  -v "$PWD/models:/models" \
  -e SYNOPTICON_CONFIG=/data/config.toml \
  fdebijl/synopticon:cpu web --host 0.0.0.0
```

Open <http://127.0.0.1:8686> and follow the setup wizard.

⚠️ **`--host 0.0.0.0` is required inside a container** (the default bind is `127.0.0.1`, unreachable from outside). Keep the access control on the host side by publishing to `127.0.0.1:8686` as above — the GUI is unauthenticated until you finish the wizard.

### docker compose

```yaml
services:
  synopticon:
    image: fdebijl/synopticon:cpu       # :gpu on an NVIDIA host
    command: ["web", "--host", "0.0.0.0"]
    ports:
      - "127.0.0.1:8686:8686"
    environment:
      # Keeps the wizard-written config on the volume
      - SYNOPTICON_CONFIG=/data/config.toml
      # Optional — the wizard can store these instead:
      # - SYNOPTICON_NAS__URL=https://your-nas.example.com
      # - SYNOPTICON_NAS__ACCOUNT=photos-bot
      # - SYNOPTICON_NAS__PASSWORD=...
      # - SYNOPTICON_NAS__OTP_CODE=...       # first login only, if 2FA is on
    volumes:
      - ./data:/data
      - ./models:/models
    restart: unless-stopped
```

Run `docker compose up -d`, then open <http://127.0.0.1:8686>.

## Volumes

| Path | Holds |
|---|---|
| `/data` | database, `config.toml`, face crops, reports, originals cache |
| `/models` | ONNX model weights (a few GB, downloaded on first use) |

Mount both. They are the entire state of a Synopticon install — portable between hosts, and between Docker and a bare-metal run.

## Configuration

Every setting is editable in the GUI, or via `config.toml` on `/data`, or per-key with environment variables shaped `SYNOPTICON_<SECTION>__<KEY>` (note the double underscore):

| Variable | Meaning |
|---|---|
| `SYNOPTICON_CONFIG` | Config file path. Set to `/data/config.toml` so it survives a container rebuild. |
| `SYNOPTICON_NAS__URL` | NAS base URL, e.g. `https://nas.example.com` |
| `SYNOPTICON_NAS__ACCOUNT` / `SYNOPTICON_NAS__PASSWORD` | NAS credentials. Prefer env over the config file. |
| `SYNOPTICON_NAS__OTP_CODE` | One-time code for the first login when the account has 2FA; the device token is then stored. |
| `SYNOPTICON_NAS__REQUESTS_PER_SECOND` | Default 4. The NAS also serves your family — be gentle. |
| `SYNOPTICON_INFERENCE__DEVICE` | `auto`, `cpu`, or `cuda`. Defaults to `auto` in the CPU image and `cuda` in the GPU one; both fall back to CPU if no GPU is usable. |

Full list: [Configuration in the README](https://github.com/fdebijl/synopticon#configuration).

## Running jobs without the GUI

The same image runs one-shot commands. Against an already-configured `/data`:

```bash
alias syn='docker run --rm -v "$PWD/data:/data" -v "$PWD/models:/models" \
  -e SYNOPTICON_CONFIG=/data/config.toml fdebijl/synopticon:cpu'

syn check                # verify NAS connectivity (read-only, fast)
syn models download      # fetch model weights
syn sync                 # pull photos, people and existing labels
syn extract              # detect faces — the long pass, resumable
syn cluster              # group faces and cross-reference them
syn apply                # dry-run; add --apply to write back
```

Deduplication is a shorter, separate flow off the same sync:

```bash
syn sync --hash          # one-time: hash every original (slow, resumable)
syn dedupe --exact       # dry-run; add --apply to delete
```

`extract` and `sync` commit per photo, so you can interrupt them and re-run at any time. Every command is documented in the [CLI reference](https://github.com/fdebijl/synopticon#cli-reference).

If you'd rather not run jobs by hand at all, the GUI's **Schedules** page puts `sync`, `extract`, `cluster` and friends on a cron timer inside the long-running web container — no cron daemon needed.

## GPU

Use the `gpu` tag on a host with a recent NVIDIA driver and the NVIDIA Container Toolkit. No system CUDA toolkit is needed — the CUDA and cuDNN runtimes ship in the image.

```bash
docker run --rm --gpus all \
  -v "$PWD/data:/data" -v "$PWD/models:/models" \
  -e SYNOPTICON_CONFIG=/data/config.toml \
  fdebijl/synopticon:gpu extract
```

Only face detection benefits; everything else is unaffected. A GPU run and a CPU run share the same `/data`, so a common pattern is to do the initial catch-up on a GPU box and copy `data/` to a long-lived CPU deployment.

## Notes for long-lived deployments

- **Keep it to one replica.** The default SQLite backend is single-writer. (PostgreSQL is supported, but it doesn't make the app multi-instance.)
- **Pin the container to one node** on Swarm/Kubernetes so it keeps seeing the same `/data` and `/models`.
- **No `HEALTHCHECK` is baked in**, because the same entrypoint serves one-shot commands. Add one at the service level if you want it, pointed at `GET /api/health`:
  ```yaml
  healthcheck:
    test: ["CMD", "python", "-c", "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8686/api/health').status==200 else 1)"]
    interval: 30s
    timeout: 10s
    retries: 5
    start_period: 60s
  ```
  Long jobs can make a strict healthcheck kill the container — loosen it if that happens.
- **TLS is not terminated in the container.** Publish to loopback and put nginx, Caddy or Traefik in front. Synopticon honours `X-Forwarded-Proto` itself, and only from an address listed in `[security] trusted_proxies` — uvicorn's own proxy-header handling is switched off. Until the proxy is listed, the session cookie's `Secure` flag follows only a direct https connection, not the proxy's.

  A same-host nginx container on the compose network sees Synopticon at its **container address**, not `127.0.0.1` — list that address, and make nginx overwrite the visitor's address header rather than pass it through:

  ```toml
  [security]
  trusted_proxies = ["172.20.0.0/16"]   # the compose network's subnet, not 127.0.0.1
  ```

  ```nginx
  location / {
      proxy_pass http://synopticon:8686;
      proxy_set_header X-Forwarded-For $remote_addr;   # overwrite, never pass through
      proxy_set_header X-Forwarded-Proto $scheme;
      proxy_set_header Host $host;
  }
  ```

  `$remote_addr` overwrites whatever the visitor sent; nginx forwards client headers by default, so without that line a visitor's own `X-Forwarded-For` reaches Synopticon and they can claim to be any address — switching off `[security] allow_from` and the per-address sign-in limits for whoever asks. Full explanation and the Caddy/Traefik comparison: [Behind a reverse proxy in the README](https://github.com/fdebijl/synopticon#behind-a-reverse-proxy). **`FORWARDED_ALLOW_IPS` is ignored under `synopticon web`** — uvicorn's own proxy trust is switched off explicitly, so only `[security] trusted_proxies` governs this.
- **Locked yourself out from a container?** The recovery commands run against the same volume, from the shell, with no login needed:
  ```bash
  docker exec -it synopticon synopticon web-access --clear    # locked out by the address list
  docker exec -it synopticon synopticon disable-2fa            # lost the authenticator device
  docker exec -it synopticon synopticon session-pin off        # stuck in a pin loop
  docker compose restart synopticon                            # only web-access needs this
  ```
  `GET /api/health` stays reachable throughout — the network allowlist and every auth check sit
  behind it, never in front, so a healthcheck pointed at it (see above) keeps passing while you fix
  the rest.
- **Model weights are not bundled** (mixed upstream licenses). `models download` fetches the two auto-downloadable detectors and ArcFace; two optional embedders need a manual export, described in the repo.

## Compatibility

Any NAS running Synology Photos on DSM 7.x. API versions are discovered at runtime rather than hardcoded, so firmware differences are tolerated; if a write endpoint is missing on an older DSM it fails loudly instead of corrupting anything.

## Links

- **Source, docs and issues:** [github.com/fdebijl/synopticon](https://github.com/fdebijl/synopticon)
- **Safety model** (what can write, and what gates it): [README](https://github.com/fdebijl/synopticon#safety-model)
- **License:** MIT

Parts of this codebase were created by a large language model, in particular models provided by Anthropic.
