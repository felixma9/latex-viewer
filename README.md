# LaTeX Workspace

A self-hosted LaTeX live preview server that runs in Docker. Edit `.tex` files in
any editor; the PDF recompiles and refreshes in your browser. No local TeX
installation needed. Works on Linux, macOS and Windows (Docker Desktop or WSL2).

For deployment options, rebuilds, health monitoring and troubleshooting, see
[DEPLOY.md](DEPLOY.md).

## Quick Start

Requirements: Docker with Compose v2 (`docker compose version`).

```bash
git clone git@github.com:aftahiArdi/latex-viewer.git latex-workspace
cd latex-workspace
docker compose up -d --build     # first build takes several minutes (TeX Live)
```

Then open <http://localhost:8585>.

By default the viewer only listens on this computer. To use a different port,
or to reach it from other devices, copy `.env.example` to `.env` and edit it
(see [DEPLOY.md](DEPLOY.md#configuration)).

## How It Works

```
You save a .tex file in your editor
       ↓
Container notices main.tex changed (polls every 1s)
       ↓
pdflatex builds into .build/, finished main.pdf is swapped in
       ↓
Browser polls /mtime every 2s, detects change, reloads PDF
```

The `documents/` folder is bind-mounted into the container, so anything you edit
on the host is immediately visible to the compiler. On Linux the compiler runs as
the user that owns `documents/`, so generated files stay editable by you.

## Managing Projects

Each project is a folder inside `documents/` containing a `main.tex`:

```
documents/
├── cv-main/
│   └── main.tex       ← entry point, must be named main.tex
├── cover-letter/
│   └── main.tex
└── my-report/
    └── main.tex
```

`documents/` is gitignored, so your CVs and letters stay on your machine and are
never committed. A fresh clone starts with an empty `documents/`.

To add a project: create the folder and its `main.tex`. It appears in the sidebar
within 5 seconds and is compiled straight away.

To remove a project from the UI: delete or rename the folder.

## Editing Workflow

Open `documents/<project>/main.tex` in any editor (VS Code, Neovim, Emacs, …) and
save. The container recompiles and the browser updates within a few seconds.

If a save doesn't produce a new PDF, the compile probably failed: the last good
PDF stays on screen, the status badge turns red and the log panel opens.

### Compile log

The counters in the toolbar (errors · warnings · bad boxes) open a log panel under
the PDF; press `L` to toggle it. Drag its top edge to resize.

- **Problems** lists what `pdflatex` reported in `main.log`, errors first. Each
  entry shows where TeX stopped and the surrounding lines of `main.tex`. Filter by
  kind with the chips, or by text with the search box (`/`).
- **main.log** is the full log with line numbers and highlighting. Click `log:N`
  on a problem to jump to it; **Hide noise** drops package and font loading lines.
- **Copy** puts the whole log on your clipboard; **Open ↗** shows the raw file.

A project can be linked directly as `http://localhost:8585/#<project>`.

To force a recompile without saving, click **Compile** in the browser toolbar.

### Optional: Neovim

- [VimTeX](https://github.com/lervag/vimtex) for motions and text objects
  (`]]`/`[[` sections, `cse`/`dse` change/delete environment). Disable its
  compiler, since the container compiles for you.
- `texlab` for completions and diagnostics: `:MasonInstall texlab`.

## Starting and Stopping

```bash
docker compose up -d      # start
docker compose down       # stop
docker compose logs -f    # follow logs
```

The containers restart automatically after a reboot (`restart: unless-stopped`)
as long as Docker itself starts on boot.

## Project Structure

```
latex-workspace/
├── Dockerfile            # debian-slim + TeX Live + python3
├── docker-compose.yml    # port mapping, documents/ bind mount, autoheal
├── .env.example          # optional port / bind address / Docker socket settings
├── server.py             # HTTP server, file watcher, pdflatex runner, log parser
└── documents/            # your .tex projects live here (gitignored)
    └── cv-main/
        └── main.tex
```

## Adding Packages

If a package is missing, add it to the `apt-get install` line in the `Dockerfile`,
then rebuild:

```bash
docker compose build
docker compose up -d
```

Currently installed: `texlive-latex-extra`, `texlive-fonts-recommended`,
`texlive-fonts-extra`. These cover most CV and report packages including
`moderncv`, `geometry`, `hyperref`, `xcolor`, and `fontawesome`.
