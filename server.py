#!/usr/bin/env python3
import json
import os
import re
import socket
import subprocess
import threading
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlparse

DOCUMENTS_DIR = Path("/documents")
BUILD_DIRNAME = ".build"
PORT = 8080

_tex_mtimes: dict[str, float] = {}
_compiling: set[str] = set()
# Projects whose source changed while a compile was already running.
_pending: set[str] = set()
_lock = threading.Lock()
_watch_heartbeat = time.monotonic()
WATCH_STALE_AFTER = 30  # seconds without a watcher pass before /healthz fails
PDF_WAIT_SECONDS = 5  # how long /pdf waits for an in-progress write to finish
PDFLATEX_TIMEOUT = 120
# Problems from each project's most recent compile that never reached main.log
# (a timeout, or pdflatex missing), so the log viewer can still explain them.
_compile_errors: dict[str, str] = {}

# TeX hard-wraps its log at 79 columns and truncates error context, which
# splits messages mid-word. Wide limits keep one message per line.
PDFLATEX_ENV = {
    **os.environ,
    "max_print_line": "1000",
    "error_line": "254",
    "half_error_line": "238",
}


def _pdflatex(project_dir: Path) -> bool:
    result = subprocess.run(
        [
            "pdflatex",
            "-interaction=nonstopmode",
            "-halt-on-error",
            f"-output-directory={BUILD_DIRNAME}",
            "main.tex",
        ],
        cwd=project_dir,
        env=PDFLATEX_ENV,
        capture_output=True,
        timeout=PDFLATEX_TIMEOUT,
    )
    return result.returncode == 0


def _compile_once(project_dir: Path) -> None:
    _compile_errors.pop(project_dir.name, None)
    try:
        # pdflatex truncates and rewrites its output in place, fonts last, so
        # serving that file mid-compile hands out a PDF with missing glyphs.
        # Build in a private directory and swap the finished PDF in atomically.
        build_dir = project_dir / BUILD_DIRNAME
        build_dir.mkdir(exist_ok=True)
        built_pdf = build_dir / "main.pdf"
        built_log = build_dir / "main.log"
        # A failed earlier run can leave a partial PDF here; never promote it.
        built_pdf.unlink(missing_ok=True)
        ok = _pdflatex(project_dir)
        if not ok:
            # A killed or crashed run can leave a truncated .aux that breaks
            # every later compile. Retry once from a clean build directory so
            # only genuine errors in the document keep failing.
            for leftover in build_dir.iterdir():
                if leftover.is_file():
                    leftover.unlink()
            ok = _pdflatex(project_dir)
        if built_log.exists():
            os.replace(built_log, project_dir / "main.log")
        # On failure keep serving the last good PDF rather than a partial one.
        if ok and _is_complete_pdf_file(built_pdf):
            os.replace(built_pdf, project_dir / "main.pdf")
    except subprocess.TimeoutExpired:
        _compile_errors[project_dir.name] = (
            f"pdflatex did not finish within {PDFLATEX_TIMEOUT}s and was stopped. "
            "Look for an infinite loop or a command waiting for input."
        )
    except Exception as exc:
        # A hung or failing pdflatex must never take down the worker thread.
        _compile_errors[project_dir.name] = f"Compile crashed: {exc!r}"
        traceback.print_exc()


def _run_pdflatex(project_dir: Path) -> None:
    name = project_dir.name
    with _lock:
        if name in _compiling:
            # The running worker recompiles once its current pass finishes, so
            # a save that lands mid-compile is never lost.
            _pending.add(name)
            return
        _compiling.add(name)
    try:
        while True:
            with _lock:
                _pending.discard(name)
            _compile_once(project_dir)
            with _lock:
                # Check and release under one lock hold, so a request arriving
                # now either sees _compiling and queues, or starts a new worker.
                if name not in _pending:
                    _compiling.discard(name)
                    return
    except BaseException:
        with _lock:
            _compiling.discard(name)
            _pending.discard(name)
        raise


def _compile(project_dir: Path) -> None:
    threading.Thread(target=_run_pdflatex, args=(project_dir,), daemon=True).start()


def _mtime(path: Path) -> float:
    try:
        return path.stat().st_mtime
    except OSError:
        return 0


def _is_complete_pdf(data: bytes) -> bool:
    # pdfTeX writes the %%EOF marker last; a truncated file never ends with it.
    return data.startswith(b"%PDF-") and b"%%EOF" in data[-1024:]


def _is_complete_pdf_file(path: Path) -> bool:
    try:
        return _is_complete_pdf(path.read_bytes())
    except OSError:
        return False


def _watch() -> None:
    global _watch_heartbeat
    while True:
        try:
            projects = [d for d in DOCUMENTS_DIR.iterdir() if d.is_dir()]
        except Exception:
            projects = []
        for d in projects:
            # One unreadable project must not stop the others being watched.
            try:
                tex = d / "main.tex"
                if not tex.exists():
                    continue
                mtime = tex.stat().st_mtime
                # First sight (server start, or a project created while running)
                # compiles too, so a new project gets a PDF without a second save.
                if _tex_mtimes.get(d.name) != mtime:
                    _tex_mtimes[d.name] = mtime
                    _compile(d)
            except Exception:
                traceback.print_exc()
        _watch_heartbeat = time.monotonic()
        time.sleep(1)


_WARNING_RE = re.compile(r"^((?:\S+ ){0,2}?\S+) [Ww]arning(?: \([^)]*\))?: ?(.*)$")
_BADBOX_RE = re.compile(r"^(Overfull|Underfull) \\[hv]box \(([^)]*)\) (.*)$")
_CONTEXT_LINE_RE = re.compile(r"^l\.(\d+) ?(.*)$")
_INPUT_LINE_RE = re.compile(r"(?:on input line|at lines?) (\d+)")
_OUTPUT_RE = re.compile(r"^Output written on .*\((\d+) pages?, (\d+) bytes\)")
# Interactive prompts TeX prints after an error; noise in nonstopmode.
_ERROR_BOILERPLATE = (
    "See the ",
    "Type  H <return>",
    "Type X to quit",
    "or enter new name.",
    "Enter file name:",
    "<read ",
    "...",
)
TEX_LOG_WIDTH = 79  # logs written before PDFLATEX_ENV are wrapped here


def parse_log(text: str) -> dict:
    """Pull errors, warnings and bad boxes out of a pdflatex log.

    Each problem records `log_line` (1-based, into the log) so the viewer can
    jump to it, and `line` (into the .tex source) when TeX reported one.
    """
    lines = text.splitlines()
    problems: list[dict] = []
    fatal = False
    output = None
    i = 0
    while i < len(lines):
        line = lines[i]

        if line.startswith("!"):
            if line.startswith("!  ==>"):
                fatal = True
                i += 1
                continue
            message = line[1:].strip()
            prev = problems[-1] if problems else None
            # A missing file is reported twice: the real error, then an
            # "Emergency stop" carrying only the location. Show it once.
            merge = (
                message == "Emergency stop."
                and prev is not None
                and prev["kind"] == "error"
                and prev["line"] is None
            )
            item = prev if merge else {
                "kind": "error",
                "source": "TeX",
                "message": message,
                "line": None,
                "log_line": i + 1,
                "context": [],
                "code": None,
                "help": [],
            }
            if not merge:
                m = re.match(r"^((?:LaTeX|Package \S+|Class \S+)) Error: (.*)$", message)
                if m:
                    item["source"], item["message"] = m.group(1), m.group(2)
            j = i + 1
            while j < len(lines) and j < i + 40:
                cur = lines[j]
                if cur.startswith("!") or cur.startswith("Here is how much"):
                    break
                m = _CONTEXT_LINE_RE.match(cur)
                if m:
                    after = lines[j + 1].strip() if j + 1 < len(lines) else ""
                    item["line"] = int(m.group(1))
                    item["code"] = {
                        "before": m.group(2).replace("^^M", "").rstrip(),
                        "after": after.replace("^^M", ""),
                    }
                    j += 2
                    # Explanation TeX prints after the location (not shown
                    # with -halt-on-error, but present in other logs).
                    while j < len(lines) and lines[j].strip():
                        if lines[j].startswith(("!", "Here is how much")):
                            break
                        item["help"].append(lines[j].strip())
                        j += 1
                    break
                stripped = cur.strip()
                if stripped and not stripped.startswith(_ERROR_BOILERPLATE):
                    item["context"].append(cur.rstrip())
                j += 1
            if not merge:
                problems.append(item)
            i = max(j, i + 1)
            continue

        m = _WARNING_RE.match(line)
        if m or line.startswith("Missing character:"):
            source = m.group(1) if m else "Font"
            parts = [(m.group(2) if m else line).strip()]
            tag = f"({source.split()[-1]})"
            j = i + 1
            while j < len(lines) and lines[j].strip():
                cur = lines[j]
                if cur.startswith(tag):
                    parts.append(cur[len(tag):].strip())
                elif len(lines[j - 1]) == TEX_LOG_WIDTH:
                    parts[-1] += cur  # hard-wrapped by an old 79-column log
                else:
                    break
                j += 1
            message = " ".join(p for p in parts if p)
            ln = _INPUT_LINE_RE.search(message)
            problems.append({
                "kind": "warning",
                "source": source,
                "message": message,
                "line": int(ln.group(1)) if ln else None,
                "log_line": i + 1,
                "context": [],
                "code": None,
                "help": [],
            })
            i = j
            continue

        m = _BADBOX_RE.match(line)
        if m:
            context = []
            j = i + 1
            while j < len(lines) and lines[j].strip() and j < i + 6:
                if lines[j].strip() != "[]":
                    context.append(lines[j].rstrip())
                j += 1
            ln = _INPUT_LINE_RE.search(m.group(3))
            problems.append({
                "kind": "badbox",
                "source": m.group(1),
                "message": line.strip(),
                "line": int(ln.group(1)) if ln else None,
                "log_line": i + 1,
                "context": context,
                "code": None,
                "help": [],
            })
            i = j
            continue

        m = _OUTPUT_RE.match(line)
        if m:
            output = {"pages": int(m.group(1)), "bytes": int(m.group(2))}
        i += 1

    counts = {"error": 0, "warning": 0, "badbox": 0}
    for p in problems:
        counts[p["kind"]] += 1
    return {
        "problems": problems,
        "counts": counts,
        "fatal": fatal,
        "output": output,
        "ok": not fatal and counts["error"] == 0 and output is not None,
    }


INDEX_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>LaTeX Workspace</title>
<style>
  *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
  :root {
    --base: #1e1e2e; --mantle: #181825; --crust: #11111b;
    --s0: #313244; --s1: #45475a; --o0: #6c7086; --sub: #a6adc8; --text: #cdd6f4;
    --red: #f38ba8; --yellow: #f9e2af; --peach: #fab387; --green: #a6e3a1; --blue: #89b4fa; --mauve: #cba6f7;
    --mono: 'JetBrains Mono', ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
  }
  body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; background: var(--base); color: var(--text); height: 100vh; display: flex; flex-direction: column; }
  button { font: inherit; color: inherit; }
  [hidden] { display: none !important; }
  header { background: var(--mantle); border-bottom: 1px solid var(--s0); padding: 10px 16px; display: flex; align-items: center; gap: 12px; flex-shrink: 0; }
  header h1 { font-size: 15px; font-weight: 600; color: var(--mauve); letter-spacing: .02em; }
  .layout { display: flex; flex: 1; overflow: hidden; }
  .sidebar { width: 220px; background: var(--mantle); border-right: 1px solid var(--s0); padding: 12px 8px; overflow-y: auto; flex-shrink: 0; transition: width 0.2s, padding 0.2s; }
  .sidebar.collapsed { width: 0; padding: 0; overflow: hidden; border-right: none; }
  .sidebar h2 { font-size: 11px; text-transform: uppercase; letter-spacing: .08em; color: var(--o0); padding: 0 8px 10px; }
  .project { padding: 8px 10px; border-radius: 6px; cursor: pointer; font-size: 13px; color: #bac2de; display: flex; align-items: center; gap: 8px; }
  .project:hover { background: var(--s0); }
  .project.active { background: var(--s1); color: var(--text); font-weight: 500; }
  .project .pname { flex: 1; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .toggle-btn { background: none; border: none; color: var(--o0); cursor: pointer; font-size: 16px; padding: 2px 4px; line-height: 1; }
  .toggle-btn:hover { color: var(--text); }
  .viewer { flex: 1; display: flex; flex-direction: column; min-width: 0; }
  .toolbar { background: var(--mantle); border-bottom: 1px solid var(--s0); padding: 8px 14px; display: flex; align-items: center; gap: 10px; flex-shrink: 0; font-size: 13px; flex-wrap: wrap; }
  .toolbar .name { font-weight: 500; color: var(--text); }
  .spacer { flex: 1; }
  .badge { font-size: 11px; padding: 2px 8px; border-radius: 10px; background: var(--s0); color: var(--o0); }
  .badge.compiling { background: #f9e2af20; color: var(--yellow); }
  .badge.ready { background: #a6e3a120; color: var(--green); }
  .badge.updated { background: #89b4fa20; color: var(--blue); }
  .badge.error { background: #f38ba820; color: var(--red); }
  .btn { padding: 4px 14px; background: var(--mauve); color: var(--base); border: none; border-radius: 5px; font-size: 12px; font-weight: 600; cursor: pointer; }
  .btn:hover { background: #d4b3f8; }

  /* Problem counts in the toolbar double as the log panel toggle. */
  .issues { display: flex; align-items: center; gap: 10px; background: var(--base); border: 1px solid var(--s0); border-radius: 5px; padding: 3px 10px; cursor: pointer; font-size: 12px; color: var(--sub); }
  .issues:hover, .issues.on { border-color: var(--s1); background: var(--s0); color: var(--text); }
  .issues .n { display: inline-flex; align-items: center; gap: 5px; font-variant-numeric: tabular-nums; }
  .issues .n.zero { opacity: .45; }
  .sev { display: inline-block; width: 8px; height: 8px; border-radius: 50%; flex-shrink: 0; }
  .sev.error { background: var(--red); }
  .sev.warning { background: var(--yellow); }
  .sev.badbox { background: var(--peach); border-radius: 2px; }
  .sev.clean { background: var(--green); }

  .stage { flex: 1; display: flex; flex-direction: column; min-height: 0; position: relative; }
  .pdf { flex: 1; display: flex; min-height: 0; position: relative; }
  .stage.dragging iframe { pointer-events: none; }
  .stage.dragging { cursor: row-resize; user-select: none; }
  iframe { flex: 1; border: none; background: white; }
  .empty { flex: 1; display: flex; align-items: center; justify-content: center; color: var(--s1); font-size: 14px; flex-direction: column; gap: 8px; text-align: center; padding: 16px; }
  .empty span { font-size: 32px; }

  .resizer { height: 5px; flex-shrink: 0; cursor: row-resize; background: var(--s0); position: relative; }
  .resizer::after { content: ''; position: absolute; left: 50%; top: 1px; width: 36px; height: 3px; margin-left: -18px; border-radius: 2px; background: var(--s1); }
  .resizer:hover, .stage.dragging .resizer { background: var(--mauve); }
  .panel { height: 320px; flex-shrink: 0; display: flex; flex-direction: column; background: var(--crust); min-height: 0; }
  .panel-head { display: flex; align-items: center; gap: 8px; padding: 6px 10px; background: var(--mantle); border-bottom: 1px solid var(--s0); flex-wrap: wrap; font-size: 12px; }
  .tabs { display: flex; gap: 2px; }
  .tab { background: none; border: none; padding: 5px 10px; border-radius: 5px; cursor: pointer; color: var(--o0); font-weight: 500; }
  .tab:hover { color: var(--text); }
  .tab.active { background: var(--s0); color: var(--text); }
  .tab .tc { margin-left: 4px; color: var(--o0); font-variant-numeric: tabular-nums; }
  .chips { display: flex; gap: 4px; }
  .chip { display: inline-flex; align-items: center; gap: 6px; background: none; border: 1px solid var(--s0); border-radius: 12px; padding: 2px 9px; cursor: pointer; color: var(--o0); }
  .chip.on { color: var(--text); background: var(--s0); border-color: var(--s1); }
  .chip b { font-weight: 600; font-variant-numeric: tabular-nums; }
  .chip:not(.on) .sev { background: var(--s1); }
  .search { background: var(--base); border: 1px solid var(--s0); border-radius: 5px; color: var(--text); padding: 4px 8px; font: 12px var(--mono); width: 180px; outline: none; }
  .search:focus { border-color: var(--mauve); }
  .meta { color: var(--o0); font-variant-numeric: tabular-nums; white-space: nowrap; }
  .icon { background: none; border: none; color: var(--o0); cursor: pointer; padding: 3px 7px; border-radius: 4px; font-size: 12px; text-decoration: none; }
  .icon:hover { background: var(--s0); color: var(--text); }
  .panel-body { flex: 1; overflow: auto; min-height: 0; }

  .banner { margin: 10px 12px 0; padding: 9px 12px; border-radius: 6px; font-size: 13px; line-height: 1.45; border-left: 3px solid; }
  .banner.error { background: #f38ba814; border-color: var(--red); color: #f5c2d0; }
  .banner.ok { background: #a6e3a110; border-color: var(--green); color: var(--green); }
  .banner strong { color: var(--text); }
  .note { padding: 28px 16px; text-align: center; color: var(--o0); font-size: 13px; }

  .probs { padding: 8px 0 14px; }
  .prob { border-bottom: 1px solid #1e1e2e; }
  .prob-head { display: flex; align-items: baseline; gap: 10px; padding: 7px 12px; cursor: default; font-size: 13px; }
  .prob.expandable .prob-head { cursor: pointer; }
  .prob-head:hover { background: #181825; }
  .prob-head .sev { position: relative; top: -1px; }
  .chev { width: 10px; color: var(--o0); font-size: 10px; flex-shrink: 0; transition: transform .12s; }
  .prob.open .chev { transform: rotate(90deg); }
  .msg { flex: 1; min-width: 0; color: var(--text); font-family: var(--mono); font-size: 12.5px; line-height: 1.5; overflow-wrap: anywhere; }
  .prob.error .msg { color: #f5c2d0; }
  .src { color: var(--o0); font-size: 11px; white-space: nowrap; }
  .loc { font: 11px var(--mono); color: var(--blue); white-space: nowrap; }
  .jump { background: none; border: 1px solid transparent; border-radius: 4px; color: var(--o0); font: 11px var(--mono); padding: 0 5px; cursor: pointer; white-space: nowrap; }
  .jump:hover { border-color: var(--s1); color: var(--text); }
  .prob-body { display: none; padding: 0 12px 12px 40px; }
  .prob.open .prob-body { display: block; }
  .label { font-size: 10px; text-transform: uppercase; letter-spacing: .08em; color: var(--o0); margin: 8px 0 4px; }
  pre.code { font: 12px/1.55 var(--mono); background: var(--base); border: 1px solid var(--s0); border-radius: 6px; padding: 7px 0; overflow-x: auto; }
  pre.code .row { display: flex; padding: 0 10px; }
  pre.code .row.hit { background: #f38ba818; box-shadow: inset 2px 0 var(--red); }
  .prob.warning pre.code .row.hit { background: #f9e2af12; box-shadow: inset 2px 0 var(--yellow); }
  .prob.badbox pre.code .row.hit { background: #fab38712; box-shadow: inset 2px 0 var(--peach); }
  pre.code .gut { width: 44px; flex-shrink: 0; color: var(--s1); text-align: right; padding-right: 12px; user-select: none; }
  pre.code .row.hit .gut { color: var(--sub); }
  pre.code .txt { white-space: pre; color: var(--sub); }
  .stop-before { color: var(--text); }
  .stop-mark { display: inline-block; width: 2px; height: 1.1em; vertical-align: text-bottom; background: var(--red); margin: 0 3px; animation: blink 1.1s steps(1) infinite; }
  .stop-after { color: var(--o0); }
  @keyframes blink { 50% { opacity: .25; } }
  .help { color: var(--sub); font-size: 12.5px; line-height: 1.55; max-width: 72ch; }

  .raw { font: 12px/1.6 var(--mono); padding: 6px 0 14px; min-width: max-content; }
  .rl { display: flex; padding-right: 16px; }
  .rl .ln { width: 58px; flex-shrink: 0; text-align: right; padding-right: 14px; color: var(--s1); user-select: none; }
  .rl .tx { white-space: pre; color: var(--sub); }
  .rl.dim .tx { color: #585b70; }
  .rl.error { background: #f38ba816; } .rl.error .tx { color: var(--red); } .rl.error .ln { color: var(--red); }
  .rl.errctx .tx { color: #f5c2d0; }
  .rl.warning .tx { color: var(--yellow); } .rl.warning .ln { color: #f9e2af90; }
  .rl.badbox .tx { color: var(--peach); }
  .rl.good .tx { color: var(--green); }
  .rl.flash { animation: flash 1.6s ease-out; }
  @keyframes flash { 0%, 30% { background: #cba6f740; } 100% { background: transparent; } }
  mark { background: #cba6f755; color: var(--text); border-radius: 2px; }

  @media (max-width: 700px) {
    .sidebar { width: 160px; }
    .toolbar { padding: 8px 12px; }
    .panel-head { gap: 6px; padding: 6px 8px; }
    .chip .lbl, .src, .meta, .jump, .issues .log-word { display: none; }
    .search { width: 0; flex: 1; min-width: 90px; }
    .prob-head { padding: 7px 8px; gap: 8px; }
    .prob-body { padding: 0 8px 10px; }
  }
</style>
</head>
<body>
<header>
  <button class="toggle-btn" onclick="toggleSidebar()" title="Toggle sidebar">☰</button>
  <h1>LaTeX Workspace</h1>
</header>
<div class="layout">
  <nav class="sidebar" id="sidebar">
    <h2>Projects</h2>
    <div id="project-list"></div>
  </nav>
  <div class="viewer">
    <div class="toolbar">
      <span class="name" id="proj-name">No project selected</span>
      <span class="badge" id="status">—</span>
      <div class="spacer"></div>
      <button class="issues" id="issues-btn" onclick="togglePanel()" title="Show compile log (L)" hidden></button>
      <button class="btn" onclick="compileNow()">Compile</button>
    </div>
    <div class="stage" id="stage">
      <div class="pdf" id="pdf"><div class="empty"><span>📄</span>Select a project to view its PDF</div></div>
      <div class="resizer" id="resizer" title="Drag to resize" hidden></div>
      <section class="panel" id="panel" hidden>
        <div class="panel-head">
          <div class="tabs">
            <button class="tab active" data-tab="problems">Problems<span class="tc" id="tc-problems"></span></button>
            <button class="tab" data-tab="raw">main.log<span class="tc" id="tc-raw"></span></button>
          </div>
          <div class="chips" id="kind-chips">
            <button class="chip on" data-kind="error"><i class="sev error"></i><span class="lbl">Errors</span> <b data-count="error">0</b></button>
            <button class="chip on" data-kind="warning"><i class="sev warning"></i><span class="lbl">Warnings</span> <b data-count="warning">0</b></button>
            <button class="chip on" data-kind="badbox"><i class="sev badbox"></i><span class="lbl">Bad boxes</span> <b data-count="badbox">0</b></button>
          </div>
          <div class="chips" id="raw-chips" hidden>
            <button class="chip" id="noise-chip" title="Hide file loading and font info lines">Hide noise</button>
          </div>
          <input class="search" id="search" type="search" placeholder="Filter…  ( / )" autocomplete="off" spellcheck="false">
          <div class="spacer"></div>
          <span class="meta" id="log-meta"></span>
          <button class="icon" onclick="copyLog(this)" title="Copy main.log">Copy</button>
          <a class="icon" id="raw-link" target="_blank" rel="noopener" title="Open main.log in a new tab">Open ↗</a>
          <button class="icon" onclick="togglePanel(false)" title="Close (Esc)">✕</button>
        </div>
        <div class="panel-body" id="panel-body">
          <div id="problems-view"></div>
          <div id="raw-view" hidden></div>
        </div>
      </section>
    </div>
  </div>
</div>
<script>
const store = {
  get(k, d) { try { const v = localStorage.getItem('lw.' + k); return v === null ? d : JSON.parse(v); } catch { return d; } },
  set(k, v) { try { localStorage.setItem('lw.' + k, JSON.stringify(v)); } catch {} },
};
const $ = id => document.getElementById(id);
const KIND_ORDER = { error: 0, warning: 1, badbox: 2 };
const KIND_LABEL = { error: 'Error', warning: 'Warning', badbox: 'Bad box' };

let current = null, pollTimer = null;
let pdfMtime = 0, logMtime = -1, compiling = false, compileError = null;
let logData = null, lastFailed = {}, flashUntil = 0;
let tab = store.get('tab', 'problems'), kinds = store.get('kinds', { error: true, warning: true, badbox: true });
let hideNoise = store.get('hideNoise', false), query = '';
const openProblems = new Set();

function esc(s) {
  return String(s).replace(/[&<>"]/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
}
function highlight(text) {
  if (!query) return esc(text);
  const lower = text.toLowerCase(), q = query.toLowerCase();
  let out = '', i = 0, j;
  while ((j = lower.indexOf(q, i)) !== -1) {
    out += esc(text.slice(i, j)) + '<mark>' + esc(text.slice(j, j + q.length)) + '</mark>';
    i = j + q.length;
  }
  return out + esc(text.slice(i));
}

/* ---------- projects & PDF ---------- */

async function refreshProjects() {
  const res = await fetch('/projects').catch(() => null);
  if (!res) return;
  const projects = await res.json();
  const list = $('project-list');
  list.innerHTML = '';
  projects.forEach(p => {
    const el = document.createElement('div');
    el.className = 'project' + (current === p.name ? ' active' : '');
    el.dataset.name = p.name;
    const failed = lastFailed[p.name];
    el.innerHTML = '<span class="pname"></span>' + (failed ? '<i class="sev error" title="Last compile failed"></i>' : '');
    el.querySelector('.pname').textContent = p.name;
    el.onclick = () => select(p.name);
    list.appendChild(el);
  });
}

function select(name) {
  current = name;
  // -1 = not checked yet, so the first poll always renders the PDF area.
  pdfMtime = -1; logMtime = -1; logData = null; compileError = null;
  openProblems.clear();
  $('proj-name').textContent = name;
  // On a phone the sidebar would take a third of the screen.
  if (window.innerWidth < 700) $('sidebar').classList.add('collapsed');
  history.replaceState(null, '', '#' + encodeURIComponent(name));
  document.querySelectorAll('.project').forEach(el => el.classList.toggle('active', el.dataset.name === name));
  $('raw-link').href = '/rawlog/' + encodeURIComponent(name);
  $('issues-btn').hidden = false;
  $('pdf').innerHTML = '<div class="empty"><span>⏳</span>Loading…</div>';
  setStatus('compiling', 'Loading…');
  if (store.get('panelOpen', false)) togglePanel(true);
  renderIssues(); renderPanel();
  if (pollTimer) clearInterval(pollTimer);
  pollTimer = setInterval(poll, 2000);
  poll();
}

function showPdf() {
  const area = $('pdf');
  if (pdfMtime <= 0) {
    area.innerHTML = '<div class="empty"><span>📄</span>No PDF yet' +
      (logData && !logData.ok ? ' — the compile failed, see the log below' : '') + '</div>';
    return;
  }
  let frame = area.querySelector('iframe');
  if (!frame) { area.innerHTML = ''; frame = document.createElement('iframe'); area.appendChild(frame); }
  frame.src = '/pdf/' + encodeURIComponent(current) + '?t=' + pdfMtime + '#pagemode=none';
}

async function poll() {
  const name = current;
  if (!name) return;
  const res = await fetch('/mtime/' + encodeURIComponent(name)).catch(() => null);
  if (!res || name !== current) return;
  const s = await res.json();
  compiling = s.compiling;
  if (s.mtime !== pdfMtime) {
    const first = pdfMtime === -1;
    pdfMtime = s.mtime;
    showPdf();
    if (!first && pdfMtime) flashUntil = Date.now() + 2000;
  }
  if (s.log_mtime !== logMtime || s.compile_error !== compileError) {
    logMtime = s.log_mtime;
    compileError = s.compile_error;
    await loadLog(name);
  }
  renderStatus();
}

async function loadLog(name) {
  const res = await fetch('/log/' + encodeURIComponent(name)).catch(() => null);
  if (!res || name !== current) return;
  logData = await res.json();
  const failed = !!(logData.compile_error || (logData.exists && !logData.ok));
  // Pop the log open when a project starts failing, not on every failing save.
  if (failed && !lastFailed[name]) togglePanel(true);
  if (failed !== !!lastFailed[name]) { lastFailed[name] = failed; refreshProjects(); }
  if (pdfMtime === 0) showPdf();
  renderIssues(); renderPanel();
}

async function compileNow() {
  if (!current) return;
  compiling = true;
  renderStatus();
  await fetch('/compile/' + encodeURIComponent(current)).catch(() => null);
  setTimeout(poll, 800);
}

function setStatus(cls, text) {
  const el = $('status');
  el.className = 'badge ' + cls;
  el.textContent = text;
}

function renderStatus() {
  if (!current) return;
  if (compiling) return setStatus('compiling', 'Compiling…');
  if (Date.now() < flashUntil) { setTimeout(renderStatus, flashUntil - Date.now() + 20); return setStatus('updated', 'Updated!'); }
  if (!logData) return setStatus('', '—');
  if (logData.compile_error || (logData.exists && !logData.ok)) return setStatus('error', 'Compile failed');
  setStatus('ready', 'Ready');
}

function toggleSidebar() { $('sidebar').classList.toggle('collapsed'); }

/* ---------- log panel ---------- */

function renderIssues() {
  const btn = $('issues-btn');
  const c = logData ? logData.counts : { error: 0, warning: 0, badbox: 0 };
  btn.classList.toggle('on', !$('panel').hidden);
  if (logData && logData.exists && !logData.compile_error && c.error + c.warning + c.badbox === 0) {
    btn.innerHTML = '<span class="n"><i class="sev clean"></i>No issues</span>';
    return;
  }
  btn.innerHTML = ['error', 'warning', 'badbox'].map(k =>
    `<span class="n${c[k] ? '' : ' zero'}" title="${KIND_LABEL[k]}s"><i class="sev ${k}"></i>${c[k]}</span>`).join('') +
    '<span class="log-word" style="color:var(--o0)">Log</span>';
}

function togglePanel(show) {
  const panel = $('panel');
  const open = show === undefined ? panel.hidden : show;
  if (open && !current) return;
  panel.hidden = !open;
  $('resizer').hidden = !open;
  store.set('panelOpen', open);
  renderIssues();
  if (open) renderPanel();
}

function setTab(t) {
  tab = t;
  store.set('tab', t);
  $('panel-body').scrollTop = 0;
  renderPanel();
}

function renderPanel() {
  if ($('panel').hidden) return;
  document.querySelectorAll('.tab').forEach(b => b.classList.toggle('active', b.dataset.tab === tab));
  $('kind-chips').hidden = tab !== 'problems';
  $('raw-chips').hidden = tab !== 'raw';
  $('problems-view').hidden = tab !== 'problems';
  $('raw-view').hidden = tab !== 'raw';
  $('noise-chip').classList.toggle('on', hideNoise);
  document.querySelectorAll('#kind-chips .chip').forEach(b => b.classList.toggle('on', kinds[b.dataset.kind]));
  const d = logData;
  const c = d ? d.counts : { error: 0, warning: 0, badbox: 0 };
  document.querySelectorAll('[data-count]').forEach(b => b.textContent = c[b.dataset.count]);
  $('tc-problems').textContent = d ? c.error + c.warning + c.badbox : '';
  $('tc-raw').textContent = '';
  let meta = '';
  if (d && d.exists) {
    meta = 'Built ' + new Date(d.mtime * 1000).toLocaleTimeString();
    if (d.output) meta += ` · ${d.output.pages} page${d.output.pages === 1 ? '' : 's'} · ${Math.round(d.output.bytes / 1024)} KB`;
  }
  $('log-meta').textContent = meta;
  if (tab === 'problems') renderProblems(); else renderRaw();
}

function renderProblems() {
  const view = $('problems-view');
  const d = logData;
  if (!d) { view.innerHTML = '<div class="note">Loading log…</div>'; return; }
  let html = '';
  if (d.compile_error) html += `<div class="banner error"><strong>Compile did not finish.</strong> ${esc(d.compile_error)}</div>`;
  if (!d.exists) {
    view.innerHTML = html + '<div class="note">No main.log yet. It appears after the first compile.</div>';
    return;
  }
  if (!d.ok) {
    html += '<div class="banner error"><strong>Compile failed.</strong> ' +
      (pdfMtime > 0 ? 'The PDF shown is from the last successful build.' : 'No PDF has been produced yet.') + '</div>';
  }
  const all = d.problems.map((p, idx) => ({ ...p, idx }))
    .sort((a, b) => KIND_ORDER[a.kind] - KIND_ORDER[b.kind] || a.log_line - b.log_line);
  if (!all.length) {
    view.innerHTML = html + (d.ok ? '<div class="banner ok">Clean build: no errors, warnings or bad boxes.</div>' : '');
    return;
  }
  const q = query.toLowerCase();
  const shown = all.filter(p => kinds[p.kind] && (!q ||
    [p.message, p.source, ...p.context, ...p.help, p.code ? p.code.before + p.code.after : ''].join('\n').toLowerCase().includes(q)));
  if (!shown.length) html += '<div class="note">No problems match the current filters.</div>';
  html += '<div class="probs">' + shown.map(p => renderProblem(p, d)).join('') + '</div>';
  view.innerHTML = html;
}

function renderProblem(p, d) {
  const key = p.kind + ':' + p.log_line;
  let body = '';
  if (p.code && (p.code.before || p.code.after)) {
    body += '<div class="label">Where TeX stopped</div><pre class="code"><div class="row"><span class="gut">l.' + p.line +
      '</span><span class="txt"><span class="stop-before">' + esc(p.code.before) + '</span><span class="stop-mark"></span><span class="stop-after">' +
      esc(p.code.after) + '</span></span></div></pre>';
  }
  if (p.context.length) {
    body += '<div class="label">' + (p.kind === 'badbox' ? 'Offending text' : 'Context') + '</div><pre class="code">' +
      p.context.map(l => '<div class="row"><span class="txt">' + esc(l) + '</span></div>').join('') + '</pre>';
  }
  if (p.line != null && d.source[p.line] !== undefined) {
    const rows = [];
    for (let n = p.line - 2; n <= p.line + 2; n++) {
      if (d.source[n] === undefined) continue;
      rows.push(`<div class="row${n === p.line ? ' hit' : ''}"><span class="gut">${n}</span><span class="txt">${esc(d.source[n]) || ' '}</span></div>`);
    }
    body += '<div class="label">main.tex</div><pre class="code">' + rows.join('') + '</pre>';
  }
  if (p.help.length) body += '<div class="label">TeX says</div><div class="help">' + esc(p.help.join(' ')) + '</div>';
  const isOpen = openProblems.has(key) || (p.kind === 'error' && !openProblems.has('!' + key));
  return `<div class="prob ${p.kind}${body ? ' expandable' : ''}${body && isOpen ? ' open' : ''}" data-key="${esc(key)}">
    <div class="prob-head">
      <span class="chev">${body ? '▶' : ''}</span>
      <i class="sev ${p.kind}" title="${KIND_LABEL[p.kind]}"></i>
      <span class="msg">${highlight(p.message)}</span>
      <span class="src">${esc(p.source)}</span>
      ${p.line != null ? `<span class="loc">line ${p.line}</span>` : ''}
      <button class="jump" data-log="${p.log_line}" title="Show in main.log">log:${p.log_line}</button>
    </div>
    ${body ? '<div class="prob-body">' + body + '</div>' : ''}
  </div>`;
}

const NOISE = /^\s*[()<>{}\[\]]*\s*(\/usr\/|\.\/|\.build\/)|^(File|Package|Document Class|Language|\\[A-Za-z@]+=|LaTeX Font Info|LaTeX Info|Package \S+ Info|\(Font\)|\s*\[\]|luaotfload|\*geometry\*|For additional information)/;
const WARN_RE = /^((\S+ ){0,2}?\S+) [Ww]arning\b|^Missing character:/;

function classify(lines) {
  const out = new Array(lines.length);
  let inError = false, warnTag = null;
  for (let i = 0; i < lines.length; i++) {
    const l = lines[i];
    let k = '';
    if (l.startsWith('!')) { k = 'error'; inError = !l.startsWith('!  ==>'); warnTag = null; }
    else if (inError) {
      if (!l.trim() || l.startsWith('Here is how much')) inError = false; else k = 'errctx';
      if (/^l\.\d+/.test(l)) k = 'errctx';
    }
    if (!k) {
      const m = l.match(WARN_RE);
      if (m) { k = 'warning'; warnTag = m[1] ? '(' + m[1].split(' ').pop() + ')' : null; }
      else if (warnTag && l.startsWith(warnTag)) k = 'warning';
      else if (/^(Overfull|Underfull) \\[hv]box/.test(l)) k = 'badbox';
      else if (/^Output written on/.test(l)) k = 'good';
      else if (NOISE.test(l)) k = 'dim';
      if (!l.trim()) warnTag = null;
    }
    out[i] = k;
  }
  return out;
}

let rawCache = { mtime: null, lines: [], kinds: [] };
function renderRaw() {
  const view = $('raw-view');
  const d = logData;
  if (!d) { view.innerHTML = '<div class="note">Loading log…</div>'; return; }
  if (!d.exists) { view.innerHTML = '<div class="note">No main.log yet.</div>'; return; }
  if (rawCache.mtime !== d.mtime || rawCache.log !== d.log) {
    const lines = d.log.replace(/\n$/, '').split(/\r?\n/);
    rawCache = { mtime: d.mtime, log: d.log, lines, kinds: classify(lines) };
  }
  const { lines, kinds: ks } = rawCache;
  const q = query.toLowerCase();
  const parts = [];
  let matches = 0;
  for (let i = 0; i < lines.length; i++) {
    const k = ks[i];
    if (q) {
      if (!lines[i].toLowerCase().includes(q)) continue;
      matches++;
    } else if (hideNoise && (k === 'dim' || !lines[i].trim())) continue;
    parts.push(`<div class="rl ${k}" id="L${i + 1}"><span class="ln">${i + 1}</span><span class="tx">${highlight(lines[i]) || ' '}</span></div>`);
  }
  $('tc-raw').textContent = q ? matches : lines.length;
  view.innerHTML = parts.length ? '<div class="raw">' + parts.join('') + '</div>' : '<div class="note">No lines match.</div>';
}

function jumpToLog(n) {
  query = ''; $('search').value = '';
  hideNoise = false; store.set('hideNoise', false);
  setTab('raw');
  const row = $('L' + n);
  if (!row) return;
  row.scrollIntoView({ block: 'center' });
  row.classList.remove('flash'); void row.offsetWidth; row.classList.add('flash');
}

async function copyLog(btn) {
  if (!logData) return;
  const text = logData.log;
  try { await navigator.clipboard.writeText(text); }
  catch {
    // Clipboard API needs HTTPS or localhost; fall back for plain-HTTP access.
    const ta = document.createElement('textarea');
    ta.value = text; ta.style.position = 'fixed'; ta.style.opacity = '0';
    document.body.appendChild(ta); ta.select(); document.execCommand('copy'); ta.remove();
  }
  btn.textContent = 'Copied';
  setTimeout(() => btn.textContent = 'Copy', 1200);
}

document.querySelector('.tabs').addEventListener('click', e => {
  const b = e.target.closest('.tab'); if (b) setTab(b.dataset.tab);
});
$('kind-chips').addEventListener('click', e => {
  const b = e.target.closest('.chip'); if (!b) return;
  kinds[b.dataset.kind] = !kinds[b.dataset.kind];
  store.set('kinds', kinds); renderPanel();
});
$('noise-chip').onclick = () => { hideNoise = !hideNoise; store.set('hideNoise', hideNoise); renderPanel(); };
$('search').addEventListener('input', e => { query = e.target.value; renderPanel(); });
$('problems-view').addEventListener('click', e => {
  const jump = e.target.closest('.jump');
  if (jump) return jumpToLog(+jump.dataset.log);
  const head = e.target.closest('.prob-head');
  const prob = head && head.parentElement;
  if (!prob || !prob.classList.contains('expandable') || e.target.closest('.prob-body')) return;
  const key = prob.dataset.key, open = !prob.classList.contains('open');
  prob.classList.toggle('open', open);
  openProblems.delete(key); openProblems.delete('!' + key);
  if (open) openProblems.add(key); else if (prob.classList.contains('error')) openProblems.add('!' + key);
});

// Drag the bar between PDF and log to resize the panel.
(function () {
  const panel = $('panel'), stage = $('stage'), bar = $('resizer');
  const h = store.get('panelHeight', 320);
  panel.style.height = h + 'px';
  bar.addEventListener('pointerdown', e => {
    e.preventDefault();
    bar.setPointerCapture(e.pointerId);
    stage.classList.add('dragging');
    const move = ev => {
      const r = stage.getBoundingClientRect();
      const height = Math.max(120, Math.min(r.bottom - ev.clientY, r.height - 60));
      panel.style.height = height + 'px';
    };
    const up = () => {
      stage.classList.remove('dragging');
      bar.removeEventListener('pointermove', move);
      bar.removeEventListener('pointerup', up);
      store.set('panelHeight', parseInt(panel.style.height, 10));
    };
    bar.addEventListener('pointermove', move);
    bar.addEventListener('pointerup', up);
  });
})();

document.addEventListener('keydown', e => {
  const typing = e.target.matches('input, textarea');
  if (e.key === 'Escape') {
    if (typing && e.target.value) { e.target.value = ''; query = ''; renderPanel(); }
    else if (!$('panel').hidden) togglePanel(false);
    if (typing) e.target.blur();
    return;
  }
  if (typing || e.metaKey || e.ctrlKey || e.altKey) return;
  if (e.key === 'l' || e.key === 'L') { togglePanel(); e.preventDefault(); }
  else if (e.key === '/' && !$('panel').hidden) { $('search').focus(); e.preventDefault(); }
});

refreshProjects();
setInterval(refreshProjects, 5000);
if (location.hash.length > 1) select(decodeURIComponent(location.hash.slice(1)));
</script>
</body>
</html>
"""


class Handler(BaseHTTPRequestHandler):
    timeout = 30

    def handle_one_request(self) -> None:
        # A client that disconnects mid-response raises here; that is normal and
        # must never surface as a traceback or take the connection thread down.
        try:
            super().handle_one_request()
        except (BrokenPipeError, ConnectionResetError, TimeoutError, socket.timeout):
            self.close_connection = True

    def do_GET(self) -> None:
        p = urlparse(self.path).path.rstrip("/") or "/"

        try:
            if p in ("/", "/index.html"):
                self._send(200, "text/html", INDEX_HTML.encode())
            elif p == "/healthz":
                self._serve_health()
            elif p == "/projects":
                self._json(self._list_projects())
            elif p.startswith("/pdf/"):
                self._serve_pdf(p[5:])
            elif p.startswith("/log/"):
                self._serve_log(p[5:])
            elif p.startswith("/rawlog/"):
                self._serve_raw_log(p[8:])
            elif p.startswith("/mtime/"):
                self._serve_mtime(p[7:])
            elif p.startswith("/compile/"):
                self._trigger_compile(p[9:])
            else:
                self.send_error(404)
        except (BrokenPipeError, ConnectionResetError, TimeoutError, socket.timeout):
            self.close_connection = True
        except Exception:
            # Any unexpected failure must return an error, not kill the handler.
            traceback.print_exc()
            try:
                self.send_error(500)
            except Exception:
                self.close_connection = True

    def _project_dir(self, name: str) -> Path | None:
        """Resolve a project name to a directory, refusing anything outside /documents."""
        candidate = (DOCUMENTS_DIR / unquote(name)).resolve()
        if candidate.parent != DOCUMENTS_DIR.resolve() or not candidate.is_dir():
            return None
        return candidate

    def _list_projects(self) -> list:
        out = []
        if DOCUMENTS_DIR.exists():
            for d in sorted(DOCUMENTS_DIR.iterdir()):
                if d.is_dir() and (d / "main.tex").exists():
                    out.append({"name": d.name, "has_pdf": (d / "main.pdf").exists()})
        return out

    def _serve_pdf(self, name: str) -> None:
        d = self._project_dir(name)
        if d is None:
            self.send_error(404)
            return
        pdf = d / "main.pdf"
        # Our own compiles swap the PDF in atomically, but a pdflatex run
        # outside this server (e.g. on the host) still writes it in place.
        # Wait for it to finish instead of sending a truncated file.
        deadline = time.monotonic() + PDF_WAIT_SECONDS
        while True:
            try:
                data = pdf.read_bytes()
            except OSError:
                data = None
            if data is not None and _is_complete_pdf(data):
                break
            if time.monotonic() >= deadline:
                if data is None:
                    self.send_error(404)
                else:
                    self._send(
                        503,
                        "text/plain",
                        b"PDF is being written, retry shortly\n",
                        {"Retry-After": "2", "Cache-Control": "no-store"},
                    )
                return
            time.sleep(0.25)
        self._send(200, "application/pdf", data, {"Cache-Control": "no-cache"})

    def _serve_health(self) -> None:
        stale = time.monotonic() - _watch_heartbeat
        if stale > WATCH_STALE_AFTER:
            # A dead watcher means edits silently stop compiling; report
            # unhealthy so autoheal restarts the container.
            body = json.dumps({"status": "watcher stalled", "seconds": round(stale)})
            self._send(503, "application/json", body.encode())
            return
        self._json({"status": "ok"})

    def _serve_mtime(self, name: str) -> None:
        d = self._project_dir(name)
        self._json({
            "mtime": _mtime(d / "main.pdf") if d else 0,
            "log_mtime": _mtime(d / "main.log") if d else 0,
            "compiling": d is not None and d.name in _compiling,
            "compile_error": _compile_errors.get(d.name) if d else None,
        })

    def _serve_log(self, name: str) -> None:
        d = self._project_dir(name)
        if d is None:
            self.send_error(404)
            return
        log = d / "main.log"
        try:
            text = log.read_bytes().decode("utf-8", errors="replace")
            mtime = log.stat().st_mtime
        except OSError:
            text, mtime = None, 0
        result = parse_log(text) if text is not None else {
            "problems": [], "counts": {"error": 0, "warning": 0, "badbox": 0},
            "fatal": False, "output": None, "ok": False,
        }
        # Source lines around each reported line, so problems can show the
        # offending code. Only for single-file projects: TeX's line numbers
        # belong to whichever file was being read, and with \input that may
        # not be main.tex.
        source = {}
        tex_files = [f for f in d.rglob("*.tex") if BUILD_DIRNAME not in f.parts]
        if len(tex_files) == 1:
            try:
                tex_lines = (d / "main.tex").read_text(errors="replace").splitlines()
            except OSError:
                tex_lines = []
            for item in result["problems"]:
                if item["line"] is None:
                    continue
                for n in range(item["line"] - 2, item["line"] + 3):
                    if 1 <= n <= len(tex_lines):
                        source[n] = tex_lines[n - 1]
        self._json({
            **result,
            "exists": text is not None,
            "mtime": mtime,
            "compiling": d.name in _compiling,
            "compile_error": _compile_errors.get(d.name),
            "source": source,
            "log": text or "",
        })

    def _serve_raw_log(self, name: str) -> None:
        d = self._project_dir(name)
        try:
            data = (d / "main.log").read_bytes() if d else None
        except OSError:
            data = None
        if data is None:
            self.send_error(404)
            return
        self._send(200, "text/plain; charset=utf-8", data, {"Cache-Control": "no-cache"})

    def _trigger_compile(self, name: str) -> None:
        d = self._project_dir(name)
        if d and (d / "main.tex").exists():
            _compile(d)
        self._json({"status": "compiling"})

    def _json(self, obj: object) -> None:
        self._send(200, "application/json", json.dumps(obj).encode())

    def _send(self, code: int, ct: str, body: bytes, extra: dict | None = None) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ct)
        self.send_header("Content-Length", str(len(body)))
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_) -> None:
        pass


BUILD_OUTPUTS = ("main.pdf", "main.log", "main.aux", "main.out", "main.fls", "main.fdb_latexmk")


def _drop_privileges() -> None:
    """Run as whoever owns /documents instead of root.

    Otherwise, on Linux hosts, every PDF and log the compiler writes into the
    bind mount is owned by root and the user cannot edit or delete it. Docker
    Desktop (macOS/Windows) mounts often report root, in which case staying
    root is harmless because the host side maps ownership itself.
    """
    if os.getuid() != 0 or not DOCUMENTS_DIR.exists():
        return
    st = DOCUMENTS_DIR.stat()
    uid, gid = st.st_uid, st.st_gid
    if uid == 0:
        return
    # Hand back root-owned output left by earlier versions that ran as root,
    # or pdflatex could not overwrite it after the switch.
    for d in DOCUMENTS_DIR.iterdir():
        if not d.is_dir():
            continue
        paths = [d / name for name in BUILD_OUTPUTS]
        build_dir = d / BUILD_DIRNAME
        if build_dir.is_dir():
            paths.append(build_dir)
            paths.extend(build_dir.iterdir())
        for path in paths:
            try:
                if path.lstat().st_uid == 0:
                    os.lchown(path, uid, gid)
            except OSError:
                pass
    os.setgroups([])
    os.setgid(gid)
    os.setuid(uid)
    # root's HOME is unwritable now; TeX writes caches under $HOME.
    os.environ["HOME"] = "/tmp"
    print(f"Running as uid={uid} gid={gid} (owner of {DOCUMENTS_DIR})")


if __name__ == "__main__":
    _drop_privileges()
    # The watcher's first pass compiles every project.
    threading.Thread(target=_watch, daemon=True).start()

    print(f"LaTeX Workspace running on http://0.0.0.0:{PORT}")
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
