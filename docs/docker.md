# Running Synopticon with Docker

You can run Synopticon under Docker either locally, or on a separate box that hosts Synopticon as a long-lived process. The former is perfect for intermittent use, but if you want to keep grooming the faces in Synopticon on a regular basis, the latter is a better fit.

**Local workflow**
```bash
export SYNOPTICON_NAS__URL=https://your-nas.example.com
export SYNOPTICON_NAS__ACCOUNT=photos-bot
export SYNOPTICON_NAS__PASSWORD=...

docker compose build                           # or skip: see Docker images below to pull instead
docker compose run synopticon models download
docker compose run synopticon sync
docker compose run synopticon extract          # the long pass; resumable, re-runnable (see GPU acceleration below)
docker compose run synopticon cluster
docker compose run synopticon report           # static HTML in ./data/report/<run>/
docker compose run --service-ports synopticon web --host 0.0.0.0   # GUI at http://127.0.0.1:8686 (review lives at /review)
docker compose run synopticon apply            # dry-run; add --apply to write
```

Deduplication is a separate, shorter flow off the same `sync`:

```bash
docker compose run synopticon sync --hash        # one-time: hash every original (slow, resumable)
docker compose run synopticon dedupe --exact     # dry-run; add --apply to delete
```

All data lives under the repo root, whether you're running locally or with Docker: `data/` has the SQLite db, face crops, reports, originals cache and `models/` hosts the ONNX weights. Inside the container those directories appear as `/data` and `/models` (the compose file mounts them), but on disk it's the same `./data` and `./models` either way, so you can freely mix Docker and bare-metal runs against the same state if you so desire.

**Remote workflow**
Running the Docker image persistently has a few gotcha's. See the sample Portainer stack below for what to consider when rolling out Synopticon on a Docker Swarm or Kubernetes cluster.

```yml
services:
  synopticon:
    image: fdebijl/synopticon:cpu          # swap to :gpu for CUDA, see the note below
    command: ["web", "--host", "0.0.0.0"]  # entrypoint is `synopticon`; this is the long-running GUI
    volumes:
      - /home/willem/synopticon/data:/data
      - /home/willem/synopticon/models:/models
    networks:
      - traefik-public-v2
    environment:
      # Persist the wizard-written config onto the /data volume:
      - SYNOPTICON_CONFIG=/data/config.toml
      # NAS creds are OPTIONAL here — the first-run web wizard can enter + store
      # them into /data/config.toml instead. Uncomment to inject here instead:
      # - SYNOPTICON_NAS__URL=https://your-nas.example.com
      # - SYNOPTICON_NAS__ACCOUNT=photos-bot
      # - SYNOPTICON_NAS__PASSWORD=...
      # - SYNOPTICON_NAS__OTP_CODE=...
    # Healthchecks for Synopticon can be tricky - disable it or increase the leniency if your jobs get killed constantly
    healthcheck:
      test: ["CMD", "python", "-c", "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8686/api/health').status==200 else 1)"]
      interval: 30s
      timeout: 10s
      retries: 5
      start_period: 60s
    deploy:
      mode: replicated
      replicas: 1                          # MUST stay 1 when using SQLite
      placement:
        constraints:
          - node.role != manager
          - node.hostname == foobar        # pin to one node so the data and model dirs stay consistent
      labels:
        - "deploy.labels=here"

# Change the image to fdebijl/synopticon:gpu on a node with an NVIDIA GPU + the
# NVIDIA Container Toolkit. Swarm GPU scheduling is fiddly — the compose-v3
# `deploy.resources.reservations.devices` NVIDIA syntax used in the repo's
# docker-compose.yml is a non-Swarm feature and Swarm ignores it. For a
# single-GPU homelab it's usually simplest to run this one service outside Swarm
# (plain `docker compose` / `docker run --gpus all`) and keep the rest in
# Portainer. Only `extract` benefits from the GPU; everything else is unaffected.
```

## Docker images

Prebuilt images are published to Docker Hub at [`fdebijl/synopticon`](https://hub.docker.com/r/fdebijl/synopticon), so you can skip `docker compose build` entirely. You can pick from two tags: `cpu` and `gpu`. If you have an NVIDIA GPU, it's highly recommend to use the `gpu` image to drastically speed up extraction. See [GPU acceleration](configuration.md#gpu-acceleration) for further details.

| Tag | Variant | Moves? | Use it for |
|---|---|---|---|
| `latest` | CPU | on each release | Trying it out. `latest` is the CPU build deliberately — it's the variant that runs anywhere. |
| `cpu` | CPU | on each release | Same image as `latest`, but explicit. Prefer this in a compose file so the variant is legible. |
| `gpu` | CUDA | on each release | NVIDIA hosts. Needs the [NVIDIA Container Toolkit](https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/latest/install-guide.html). |
| `0.1.0`, `0.1.0-cpu` | CPU | never | Pinning to an exact release. |
| `0.1.0-gpu` | CUDA | never | Pinning to an exact release. |
| `0.1`, `0.1-cpu`, `0.1-gpu` | both | tracks patches | Auto-picking up `0.1.x` fixes without jumping a minor. |
| `sha-<short>-cpu`, `sha-<short>-gpu` | both | never | Reproducing one exact commit; also what unreleased manual builds publish. |

```bash
docker pull fdebijl/synopticon:cpu

docker run --rm \
  -v "$PWD/data:/data" -v "$PWD/models:/models" \
  -e SYNOPTICON_NAS__URL=https://your-nas.example.com \
  -e SYNOPTICON_NAS__ACCOUNT=photos-bot \
  -e SYNOPTICON_NAS__PASSWORD=... \
  fdebijl/synopticon:cpu sync
```

The GUI additionally needs a published port and `--host 0.0.0.0`:

```bash
docker run --rm -p 127.0.0.1:8686:8686 \
  -v "$PWD/data:/data" -v "$PWD/models:/models" \
  fdebijl/synopticon:cpu web --host 0.0.0.0
```

On a GPU host, swap the tag and add `--gpus all`:

```bash
docker run --rm --gpus all \
  -v "$PWD/data:/data" -v "$PWD/models:/models" \
  fdebijl/synopticon:gpu extract
```

## Schedules

Schedules puts recurring work on a cron timer without a cron daemon in the image. This is the intended setup for a container where `synopticon web` is the only long-running process. Pick a job, fill in the same parameters the Pipeline/Utilities/Apply pages expose, give it a 5-field cron expression (or one of the presets), and optionally a timezone.

- Schedulable: `sync`, `extract`, `cluster`, `recluster`, `regen-crops`, `report`, `apply`, `dedupe` (dry run), `clear-queue`, `clear-applies`, `delete-crops`.
- Not schedulable: anything behind a typed phrase. Named↔named merges, `dedupe --apply` and `reset --all` require a human to type the phrase at that moment, which a timer cannot do — the server refuses to save such a schedule at all. `reset` is left off the list entirely, as is `prune-queue` — repairing items a pipeline upgrade orphaned is a one-off, not recurring work.
- A firing is skipped when the same command is still in flight (a long `extract` never stacks) and missed — recorded, not backfilled — for occurrences that fell while the server was down.
- Each schedule keeps a short history of its firings, linking to the job log of each run. Run now fires one immediately without disturbing the next scheduled time.

Running the GUI bare-metal rather than in a container? Plain `cron` calling the CLI works just as well and survives the server being stopped.

## Stats

Stats, below Schedules in the sidebar, has two halves:

- **What Synopticon has done** — how often each command ran and how it ended, whether you started it from the terminal, the GUI or a schedule, how long it all took, how many faces it has ever found, and every correction it wrote to your NAS. Every command records itself, terminal runs included, so a host cron counts too. Jobs the GUI ran before Stats existed are imported once from their job logs; terminal runs from before then can't be recovered, so the page says when tracking started. `reset` never clears this history.
- **Your library** — photos (by year, hour and weekday, busiest day, duplicates), faces (Synology's tags against Synopticon's own detections, how many faces a photo usually has), and how faces spread across the people you've named. Only people Synology lists by name are shown; everyone else's faces are counted together.

The page refreshes itself when a job finishes while it's open.

## Backups

Utilities has two download buttons, both plain reads that touch nothing:

- **Back up settings** hands you your `config.toml` verbatim — comments and all — so restoring is a file copy. Passwords are blanked out by default; tick *include credentials* if you want a copy that logs straight back into your NAS, and treat the file the way you'd treat the passwords themselves. Either way the download is recorded in the audit log.
- **Download database** builds a compacted SQLite snapshot of everything Synopticon knows: photos, faces, embeddings, face groups, review decisions and your account. Restore it by dropping it in as `data/synopticon.db`. On PostgreSQL you get the same SQLite file (exported table by table), which you can load back into a server with [`synopticon db-migrate --from`](cli-reference.md#db-migrate).

The snapshot is built on the fly, so a large library takes a while before the download starts, and needs room for a second copy of the database on the data volume while it runs.

## Authentication

- Admin account — one username/password, created in the wizard, stored scrypt-hashed. Sessions are HttpOnly, SameSite=Lax cookies (30-day expiry, surviving restarts). Change the password from Settings → Access, or — if you've locked yourself out — from the shell with [`synopticon reset-password`](cli-reference.md#reset-password).
- API keys — create named, revocable keys under Settings → Access (shown once, then stored hashed). Send them as `Authorization: Bearer syn_...` on `/api/*` requests — the intended path for automation and planned sidecars (e.g. a browser extension). Cookie endpoints are CSRF-hardened (JSON-only + SameSite); the bearer path is immune by construction.

```bash
curl -H "Authorization: Bearer syn_xxxxxxxxxxxxxxxx" http://127.0.0.1:8686/api/stats
```

**Two-step sign-in** is optional, turned on per-account from Settings → Access. The setup screen shows both a QR code and the typed key underneath it, so it works whether or not your phone's camera can see the screen. Turning it on issues a set of backup codes shown exactly once — save them somewhere other than this computer, since any of them can be typed anywhere a code is asked for, including when you turn two-step sign-in back off. Backup codes and the authenticator secret are left out of database backups (Settings → Utilities), on purpose — a backup you might hand to someone else should never carry a working credential. API keys are unaffected either way: a key is a machine credential and never goes through the two-step prompt.

## Reverse proxy / TLS

Synopticon does not terminate TLS itself. Bind it to loopback (the default `127.0.0.1`) and put nginx, Caddy, or Traefik in front for HTTPS. Synopticon honours `X-Forwarded-Proto` itself, and only from an address listed in `[security] trusted_proxies` — uvicorn's own proxy-header handling is switched off. Until you list your proxy there, the session cookie's `Secure` flag follows only a direct https connection, not the proxy's.

### Behind a reverse proxy

```toml
[security]
trusted_proxies = ["127.0.0.1"]
```

```nginx
location / {
    proxy_pass http://127.0.0.1:8686;
    proxy_set_header X-Forwarded-For $remote_addr;   # overwrite, never pass through
    proxy_set_header X-Forwarded-Proto $scheme;
    proxy_set_header Host $host;
}
```

`$remote_addr` overwrites whatever the visitor sent; nginx forwards client headers by default, so without this line a visitor's own `X-Forwarded-For` reaches Synopticon and they can claim to be any address — which switches off the address list and the per-address sign-in limits. Caddy and Traefik set this correctly by default, so the plain example below needs no equivalent line:

```
photos.example.com {
    reverse_proxy 127.0.0.1:8686
}
```

Leaving `trusted_proxies` unset is safe — the socket peer is used and no header is ever believed — but it makes every visitor behind that proxy share one address for the sign-in limits, and makes an address allowlist unenforceable (every visitor looks like the proxy). And listing `127.0.0.1` without the `proxy_set_header X-Forwarded-For` line above is strictly worse than leaving `trusted_proxies` unset: it starts believing a header nothing is overwriting, so any visitor can claim to be any address.

### Restricting who can reach the GUI

`[security] allow_from` (Settings → Access, or `config.toml` directly) refuses every address not on the list, checked on every request — not only at sign-in. Your own machine is always allowed regardless of what's listed, so a terminal on the server or an SSH tunnel can never be locked out. If you do lock out the browser you're using — a typo in the list, or a proxy you haven't listed yet — the way back in is from the server itself:

```bash
synopticon web-access --clear
docker compose restart synopticon
```

`config.toml` is read once at start-up, so the restart is what makes the cleared list take effect.
