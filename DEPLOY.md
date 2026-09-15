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

Published on host port **8585** (container port 8080), on `127.0.0.1` by
default. Both are configurable — see [Configuration](#configuration).

## Prerequisites

- Docker with Compose v2 — Docker Engine on Linux, or Docker Desktop on macOS /
  Windows. Works on x86-64 and ARM (Apple Silicon, Raspberry Pi 4/5).
- Host port **8585** free (or pick another in `.env`)
- ~3.3 GB of disk for the built image (TeX Live is the bulk of it)
- A Docker socket autoheal can mount — `/var/run/docker.sock` by default

No secrets. The service has **no login**, which is why it only listens on
localhost unless you say otherwise.

## Configuration

All settings are optional environment variables, read by Compose from a `.env`
file next to `docker-compose.yml` (gitignored). Start from the template:

```bash
cp .env.example .env
```

| Variable | Default | Purpose |
|----------|---------|---------|
| `LATEX_WORKSPACE_PORT` | `8585` | Host port for the viewer |
| `LATEX_WORKSPACE_BIND` | `127.0.0.1` | Host interface. `0.0.0.0` makes it reachable from other devices — only do this on a trusted network such as Tailscale or a home LAN |
| `DOCKER_SOCK` | `/var/run/docker.sock` | Docker socket for autoheal. Rootless Docker: `/run/user/<uid>/docker.sock`; Podman: `/run/user/<uid>/podman/podman.sock` |

After changing `.env`, apply it with `docker compose up -d`.

### File ownership

On Linux, `server.py` starts as root, then switches to the user and group that
own `documents/` before compiling anything. PDFs, logs and `.build/` therefore
belong to you, not root. Build files left as root by older versions are handed
over automatically at startup. On Docker Desktop (macOS/Windows) the mount
usually reports root and ownership is mapped by Docker itself, so no switch
happens and none is needed.

If `documents/` is itself owned by root (for example Docker created it because it
did not exist), fix that on the host: `sudo chown -R "$USER": documents`.

### Platform notes

- **Windows:** clone inside WSL2 (e.g. `~/latex-workspace`) rather than under
  `C:\` — file change polling and I/O are much faster there. `.gitattributes`
  keeps LF line endings on every checkout.
- **macOS:** no extra setup; Docker Desktop shares your home directory by default.
  If the repo lives elsewhere, add that path under Settings → Resources → File
  sharing.
- **Fedora / RHEL (SELinux):** if the container gets `Permission denied` on
  `/documents`, change the mount in `docker-compose.yml` to
  `./documents:/documents:z`.

## First Deploy

```bash
git clone git@github.com:aftahiArdi/latex-viewer.git latex-workspace
cd latex-workspace
cp .env.example .env    # optional, see Configuration
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
curl -s http://localhost:8585/log/<project>        # parsed errors/warnings from main.log
docker logs latex-workspace | head                 # "Running as uid=…" on Linux
```

(Use your `LATEX_WORKSPACE_PORT` if you changed it.)

On startup `server.py` compiles every project under `/documents` that has a
`main.tex`, then polls for changes every second.

## Redeploying After a Change

**Changed `server.py` only** — it is `COPY`'d into the image, so a rebuild is
still required:

```bash
cd latex-workspace
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
cd latex-workspace

docker compose up -d          # start
docker compose stop           # stop, keep containers
docker compose down           # stop and remove containers
docker compose restart        # restart in place
docker compose logs -f        # follow both containers
docker logs latex-workspace -f
```

Both containers use `restart: unless-stopped`, so they come back automatically
after a host reboot (provided Docker starts on boot — on Docker Desktop, enable
"Start Docker Desktop when you sign in").

## Data and Persistence

There are **no Docker volumes**. The single mount is:

```
./documents  →  /documents   (read-write bind mount)
```

Each subdirectory of `documents/` with a `main.tex` is a project. `server.py`
compiles into a hidden `.build/` folder inside the project, then moves the
finished `main.pdf` (only if the compile succeeded) and `main.log` next to your
sources. The PDF is swapped in with an atomic rename, so the viewer can never
fetch a half-written file; a failed compile leaves the last good PDF in place.
If a compile fails, it is retried once from an empty `.build/`, so a stale or
truncated `.aux` from an interrupted run cannot keep breaking later compiles.
Intermediate files (`.aux`, `.out`, …) stay in `.build/`, which is gitignored.

`pdflatex` runs with `max_print_line=1000` (plus wider `error_line` settings), so
`main.log` is not hard-wrapped at 79 columns and each message stays on one line.
The viewer's log panel reads that file through `/log/<project>`.

Backing up means backing up `documents/`. Nothing else on the host needs saving.
`documents/` is gitignored, so git is not a backup of your documents.

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

To have an external uptime monitor (Uptime Kuma, Glance, Healthchecks, …) watch
the service, point it at `http://<host>:8585/healthz` — it returns 200 when the
server and file watcher are working, and 503 otherwise.

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
| `GET /healthz` | `{"status": "ok"}`, or 503 if the file watcher has not run for 30s — used by the container healthcheck |
| `GET /projects` | JSON list of projects and whether each has a PDF |
| `GET /pdf/<project>` | Serves `main.pdf`, `Cache-Control: no-cache`; waits up to 5s if the file is mid-write, then 503 |
| `GET /mtime/<project>` | PDF mtime; the browser polls this every 2s to auto-reload |
| `GET /compile/<project>` | Triggers a compile (the toolbar **Compile** button) |

Project names are resolved against `/documents` and rejected if they escape it,
so path traversal via these routes returns 404.

## Troubleshooting

**Port 8585 already in use** — `docker compose up` fails to bind. Find the
holder (`ss -tlnp | grep 8585` on Linux, `lsof -iTCP:8585 -sTCP:LISTEN` on macOS,
`netstat -ano | findstr 8585` on Windows), or set `LATEX_WORKSPACE_PORT` in
`.env` and run `docker compose up -d`.

**Can't open it from another device** — the default bind is `127.0.0.1`. Set
`LATEX_WORKSPACE_BIND=0.0.0.0` in `.env`, run `docker compose up -d`, and make
sure the host firewall allows the port.

**Generated files owned by root / `Permission denied` in the log** — check
`docker logs latex-workspace | head` for the `Running as uid=…` line. If it is
missing on Linux, `documents/` is owned by root; run
`sudo chown -R "$USER": documents` and `docker compose restart latex-workspace`.

**autoheal exits with a Docker socket error** — your socket is not at
`/var/run/docker.sock` (rootless Docker, Podman, Colima). Set `DOCKER_SOCK` in
`.env`. autoheal is optional; `docker compose up -d latex-workspace` starts the
viewer alone.

**A project's PDF never appears** — the compile failed. `server.py` runs
`pdflatex -interaction=nonstopmode -halt-on-error -output-directory=.build main.tex` and discards its
output, so the error is not in `docker logs`. Read the TeX log on the host
instead:

```bash
tail -40 documents/<project>/main.log
```

Or reproduce the exact command inside the container:

```bash
docker exec -w /documents/<project> latex-workspace \
  pdflatex -interaction=nonstopmode -halt-on-error -output-directory=.build main.tex
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
cd latex-workspace
docker compose down                          # remove both containers
docker image rm latex-workspace-latex-workspace:latest   # reclaim ~3.3 GB
```

`documents/` is untouched by either command. If you added the service to an
uptime monitor, remove it there too.
