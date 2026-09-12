# Deploying LaTeX Workspace

Deployment and operations guide for the `latex-workspace` service. For day-to-day
authoring (Neovim keybindings, adding projects, editing workflow) see
[README.md](README.md).

## What Gets Deployed

Two containers, defined in `docker-compose.yml`:

| Container | Image | Role |
|-----------|-------|------|
| `latex-workspace` | built locally from `Dockerfile` | TeX Live + `server.py` — HTTP server, file watcher, PDF compiler |
| `latex-workspace-autoheal` | `willfarrell/autoheal:latest` | Restarts `latex-workspace` if its healthcheck fails |

Published on host port **8585** (container port 8080), reachable over Tailscale at
`http://ardi.tail351339.ts.net:8585`.

## Prerequisites

- Docker + Compose v2 (host currently runs Docker 29.3.0 / Compose v5.1.0)
- Host port **8585** free
- ~3.3 GB of disk for the built image (TeX Live is the bulk of it)
- `/var/run/docker.sock` readable by the Docker daemon's user — autoheal mounts
  it read-write so it can issue restarts

No `.env` file and no secrets. The service has no auth of its own; access
control is Tailscale.

## First Deploy

```bash
cd ~/Projects/latex-workspace
docker compose up -d --build
```

The first build downloads and installs `texlive-latex-extra`,
`texlive-fonts-recommended`, `texlive-fonts-extra` and `latexmk` — expect
several minutes and a few GB of traffic. Subsequent builds hit the layer cache
unless the `apt-get` line changes.

Verify:

```bash
docker compose ps                                  # both containers Up
curl -s http://localhost:8585/healthz              # {"status": "ok"}
curl -s http://localhost:8585/projects             # lists documents/ projects
```

On startup `server.py` compiles every project under `/documents` that has a
`main.tex`, then polls for changes every second.

## Redeploying After a Change

**Changed `server.py` only** — it is `COPY`'d into the image, so a rebuild is
still required:

```bash
cd ~/Projects/latex-workspace
docker compose up -d --build
```

**Changed the `Dockerfile`** (e.g. adding a TeX package):

```bash
docker compose build          # reinstalls the apt layer
docker compose up -d
```

**Changed `docker-compose.yml`:**

```bash
docker compose up -d          # recreates only what changed
```

A redeploy is safe at any time: all state lives in the bind-mounted
`documents/` directory on the host, so recreating or rebuilding the container
loses nothing. In-flight compiles are killed and re-run on startup.

## Stop, Start, Logs

```bash
cd ~/Projects/latex-workspace

docker compose up -d          # start
docker compose stop           # stop, keep containers
docker compose down           # stop and remove containers
docker compose restart        # restart in place
docker compose logs -f        # follow both containers
docker logs latex-workspace -f
```

Both containers use `restart: unless-stopped`, so they come back automatically
after a host reboot. Logs are also available in Dozzle at
`http://ardi.tail351339.ts.net:8888`.

## Data and Persistence

There are **no Docker volumes**. The single mount is:

```
./documents  →  /documents   (read-write bind mount)
```

Each subdirectory of `documents/` with a `main.tex` is a project. `server.py`
writes build output (`main.pdf`, `.aux`, `.log`, `.fls`, `.fdb_latexmk`,
`.out`) back into that same host directory, so the compiler's artifacts land
next to your sources.

Backing up means backing up `documents/` (currently ~516 KB). Nothing else on
the host needs saving.

## Health Monitoring

The container healthcheck requests `/healthz` from inside the container every
30s (10s timeout, 3 retries, 20s start period). `latex-workspace` carries the
label `autoheal: "true"`; the autoheal container watches for that label and
restarts any container whose healthcheck goes `unhealthy`, checking every 15s
with a 30s grace period. Nothing else on the host is labelled, so autoheal
touches only this service.

Check health directly:

```bash
docker inspect --format '{{.State.Health.Status}}' latex-workspace
docker inspect --format '{{json .State.Health}}' latex-workspace | python3 -m json.tool
```

The service is listed on the Glance dashboard — `glance/config/home.yml`, the
**Apps** monitor, entry "LaTeX Workspace". It has no `check-url` override
because `/` returns 200. If you ever want Glance to probe the health endpoint
instead, add:

```yaml
            - title: LaTeX Workspace
              url: http://ardi.tail351339.ts.net:8585
              check-url: http://ardi.tail351339.ts.net:8585/healthz
              icon: si:latex
```

Glance hot-reloads `config/`; no restart needed.

## Adding LaTeX Packages

Packages must be baked into the image — the container has no network-install
step. Edit the `apt-get install` list in `Dockerfile`, then rebuild:

```bash
docker compose build
docker compose up -d
```

Already installed: `texlive-latex-extra`, `texlive-fonts-recommended`,
`texlive-fonts-extra`, `latexmk`. These cover `moderncv`, `geometry`,
`hyperref`, `xcolor`, `fontawesome` and most CV/report packages.

## HTTP Endpoints

| Endpoint | Purpose |
|----------|---------|
| `GET /` | Single-page UI (HTML is embedded in `server.py`) |
| `GET /healthz` | `{"status": "ok"}` — used by the container healthcheck |
| `GET /projects` | JSON list of projects and whether each has a PDF |
| `GET /pdf/<project>` | Serves `main.pdf`, `Cache-Control: no-cache` |
| `GET /mtime/<project>` | PDF mtime; the browser polls this every 2s to auto-reload |
| `GET /compile/<project>` | Triggers a compile (the toolbar **Compile** button) |

Project names are resolved against `/documents` and rejected if they escape it,
so path traversal via these routes returns 404.

## Troubleshooting

**Port 8585 already in use** — `docker compose up` fails to bind. Find the
holder with `ss -tlnp | grep 8585`, or change the host side of the mapping in
`docker-compose.yml` (`"8585:8080"` → `"<new>:8080"`) and update the Glance
entry to match.

**A project's PDF never appears** — the compile failed. `server.py` runs
`pdflatex -interaction=nonstopmode -halt-on-error main.tex` and discards its
output, so the error is not in `docker logs`. Read the TeX log on the host
instead:

```bash
tail -40 documents/<project>/main.log
```

Or reproduce the exact command inside the container:

```bash
docker exec -w /documents/<project> latex-workspace \
  pdflatex -interaction=nonstopmode -halt-on-error main.tex
```

**A project doesn't show in the sidebar** — the directory must be a direct child
of `documents/` and contain a file named exactly `main.tex`. The UI re-fetches
`/projects` every 5s.

**Saves don't trigger a recompile** — the watcher compares `main.tex` mtime
every second. Editors that write via a temp file and rename still change the
mtime, so this normally works; confirm the host path you are editing really is
under `documents/` and that `docker inspect latex-workspace` shows the bind
mount. Forcing a recompile is always possible with the **Compile** button or
`curl http://localhost:8585/compile/<project>`.

**Container restart-looping** — autoheal is acting on a failing healthcheck.
Check why the HTTP server is not answering:

```bash
docker logs latex-workspace --tail 50
docker exec latex-workspace python3 -c \
  "import urllib.request; print(urllib.request.urlopen('http://127.0.0.1:8080/healthz').read())"
```

To stop the restarts while you investigate, `docker compose stop autoheal`.

**Build fails fetching packages** — a stale Debian package index. Force a fresh
`apt-get update` layer with `docker compose build --no-cache`.

## Full Teardown

```bash
cd ~/Projects/latex-workspace
docker compose down                          # remove both containers
docker image rm latex-workspace-latex-workspace:latest   # reclaim ~3.3 GB
```

`documents/` is untouched by either command. Also remove the "LaTeX Workspace"
entry from `glance/config/home.yml` so the dashboard stops reporting it down.
