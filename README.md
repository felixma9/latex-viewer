# LaTeX Workspace

A self-hosted LaTeX editor and live preview server, running as a Docker container. Replaces Overleaf for local document editing.

For deployment, rebuilds, health monitoring and troubleshooting, see [DEPLOY.md](DEPLOY.md).

## How It Works

```
You edit .tex in Neovim
       ↓
Docker container detects the save (polls every 1s)
       ↓
latexmk recompiles main.pdf
       ↓
Browser polls /mtime every 2s, detects change, reloads PDF
```

The `documents/` folder is bind-mounted into the container. Everything you edit on the host is immediately visible to the compiler inside Docker.

## Accessing the Preview

Open in any browser on your Tailscale network:

```
http://ardi.tail351339.ts.net:8585
```

Select a project from the left sidebar to view its PDF. The status badge in the toolbar shows when a recompile is in progress or the PDF has updated.

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

To add a new project: create the folder and `main.tex`. It appears in the browser sidebar within 5 seconds. The container compiles it on first detection.

To remove a project from the UI: delete or rename the folder.

## Editing Workflow

Open a file in Neovim:

```bash
nvim ~/Projects/latex-workspace/documents/cv-main/main.tex
```

Save with `:w` — the container recompiles and the browser updates automatically within a few seconds.

### Working with Claude

`claudecode.nvim` is already installed in your Neovim config:

| Key | Action |
|-----|--------|
| `<leader>ac` | Toggle Claude panel |
| `<leader>af` | Focus Claude panel |
| `<leader>as` | Send visual selection to Claude |
| `<leader>at` | Add current file to Claude context |

Typical flow: open the `.tex` file, hit `<leader>ac`, describe what you want ("move Education before Experience", "add a Skills section for cloud tools", "make my name larger"), Claude edits the file directly, the preview updates.

### VimTeX Keybindings (available in .tex files)

| Key | Action |
|-----|--------|
| `]]` / `[[` | Jump to next/previous section |
| `cse` | Change surrounding environment |
| `dse` | Delete surrounding environment |
| `tse` | Toggle starred environment |
| `<leader>ll` | (disabled — Docker handles compilation) |

### Manual Compile

If you want to force a recompile without saving, click the **Compile** button in the browser toolbar, or rename and re-save the file.

## Starting and Stopping

```bash
cd ~/Projects/latex-workspace

docker compose up -d      # start
docker compose down       # stop
docker compose logs -f    # follow logs
```

The container restarts automatically on server reboot (`restart: unless-stopped`).

## Optional: LaTeX LSP in Neovim

For completions and inline diagnostics, install `texlab` via Mason:

```
:MasonInstall texlab
```

This is a standalone binary — no local LaTeX installation needed.

## Project Structure

```
latex-workspace/
├── Dockerfile            # debian-slim + texlive-latex-extra + latexmk + python3
├── docker-compose.yml    # port 8585, bind-mounts documents/
├── server.py             # HTTP server + file watcher + latexmk runner
└── documents/            # your .tex projects live here
    └── cv-main/
        └── main.tex
```

## Adding Packages

If a package is missing, add it to the `apt-get install` line in the `Dockerfile`, then rebuild:

```bash
docker compose build
docker compose up -d
```

Currently installed: `texlive-latex-extra`, `texlive-fonts-recommended`, `texlive-fonts-extra`. These cover most CV and report packages including `moderncv`, `geometry`, `hyperref`, `xcolor`, and `fontawesome`.
