#!/usr/bin/env python3
import base64
import json
import os
import re
import shutil
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
PAGES_DIRNAME = "pages"  # under .build/, so a compile retry cannot unlink it
PAGE_DPI = 150  # A4 at 150dpi is ~1240px wide: a 390pt iPhone at 3x
PDFTOPPM_TIMEOUT = 60
_page_locks: dict[str, threading.Lock] = {}
_page_locks_guard = threading.Lock()

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


_PDFINFO_PAGES_RE = re.compile(r"^Pages:\s+(\d+)$", re.M)
PDFINFO_TIMEOUT = 10
# project path -> (log mtime, pdf mtime, pages)
_page_counts: dict[str, tuple[float, float, int]] = {}


def _page_count(project_dir: Path) -> int:
    """How many pages main.pdf has, or 0 if that cannot be determined.

    pdflatex records the count in main.log, so the common case costs a file
    read. A PDF compiled outside this container has no matching log, so fall
    back to asking poppler.

    Memoized on both mtimes: /mtime/ is polled every 2s per connected client,
    and re-parsing the whole log on every poll would be wasteful.
    """
    key = str(project_dir)
    log_m = _mtime(project_dir / "main.log")
    pdf_m = _mtime(project_dir / "main.pdf")
    cached = _page_counts.get(key)
    if cached and cached[0] == log_m and cached[1] == pdf_m:
        return cached[2]

    count = _compute_page_count(project_dir)
    _page_counts[key] = (log_m, pdf_m, count)
    return count


def _compute_page_count(project_dir: Path) -> int:
    try:
        parsed = parse_log((project_dir / "main.log").read_text(errors="replace"))
    except OSError:
        parsed = None
    if parsed and parsed["output"]:
        return parsed["output"]["pages"]

    pdf = project_dir / "main.pdf"
    if not pdf.exists():
        return 0
    try:
        out = subprocess.run(
            ["pdfinfo", str(pdf)],
            capture_output=True, text=True, timeout=PDFINFO_TIMEOUT,
        )
    except (OSError, subprocess.SubprocessError):
        return 0
    m = _PDFINFO_PAGES_RE.search(out.stdout)
    return int(m.group(1)) if m else 0


def _page_lock(name: str) -> threading.Lock:
    with _page_locks_guard:
        return _page_locks.setdefault(name, threading.Lock())


def _render_page(project_dir: Path, n: int, mtime_ns: int) -> bytes | None:
    """PNG bytes for page `n`, rasterizing through poppler if not cached.

    Cached under .build/pages/<mtime_ns>-<n>.png. The nanosecond mtime in the
    name means a recompile invalidates every page without any explicit
    invalidation step, and two compiles landing in the same whole second
    still get distinct cache entries; pages from older mtimes are swept on
    the first request after the change.

    Returns None if poppler is missing or the render fails.
    """
    cache = project_dir / BUILD_DIRNAME / PAGES_DIRNAME
    stamp = str(mtime_ns)
    target = cache / f"{stamp}-{n}.png"

    # A finished page is published with os.replace(), so a reader sees either
    # the whole file or none of it. Serving it without taking the lock keeps a
    # cached page from waiting behind another page's render.
    try:
        return target.read_bytes()
    except OSError:
        pass

    # One render at a time per project: it stops two requests racing to render
    # the same page, and it stops one request's sweep from deleting another's
    # freshly written output for the same project.
    with _page_lock(project_dir.name):
        # Another thread may have rendered this page while we waited.
        try:
            return target.read_bytes()
        except OSError:
            pass

        try:
            cache.mkdir(parents=True, exist_ok=True)
            for old in cache.glob("*.png"):
                if not old.name.startswith(f"{stamp}-"):
                    old.unlink(missing_ok=True)
        except OSError:
            return None

        # pdftoppm zero-pads its output suffix based on the page count, so
        # render into a private directory and take whatever single file lands.
        scratch = cache / f"tmp-{n}"
        shutil.rmtree(scratch, ignore_errors=True)
        try:
            scratch.mkdir()
            subprocess.run(
                ["pdftoppm", "-png", "-r", str(PAGE_DPI), "-f", str(n), "-l", str(n),
                 str(project_dir / "main.pdf"), str(scratch / "p")],
                check=True, capture_output=True, timeout=PDFTOPPM_TIMEOUT,
            )
            produced = sorted(scratch.glob("*.png"))
            if not produced:
                return None
            data = produced[0].read_bytes()
            os.replace(produced[0], target)  # same filesystem, so atomic
            return data
        except FileNotFoundError:
            print(f"pdftoppm not found: cannot rasterize {project_dir.name}. "
                  "Is poppler-utils installed in the image?")
            return None
        except subprocess.TimeoutExpired:
            print(f"pdftoppm timed out after {PDFTOPPM_TIMEOUT}s "
                  f"on {project_dir.name} page {n}")
            return None
        except subprocess.CalledProcessError as exc:
            err = (exc.stderr or b"").decode(errors="replace").strip()
            print(f"pdftoppm failed on {project_dir.name} page {n}: {err}")
            return None
        except OSError as exc:
            print(f"Could not rasterize {project_dir.name} page {n}: {exc!r}")
            return None
        finally:
            shutil.rmtree(scratch, ignore_errors=True)


ICON_180_B64 = (
    "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"
)

ICON_512_B64 = (
    "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"
    "h8JaEXiBUk6eRluZmbTZbErZ8TMVpcbrY424zm+WAODETC2NMS85bakUNvBKxnb4HNMqqXfs3TxW"
    "O6q1Y3NwNKSUWZZWyxOWL1hobCZER0dASdmM66efttQYI4Xk90sA8ELHlziN3/WaDyqml0IYa0u+"
    "fP/n33nfxjVlp5qTAERJc/mMhf/zP361Hln+TK0/U5xEkkNBAHBCRHHTdvz+shDCWCNEKYf7YMaY"
    "emTqUZ3t79ayldGfAOBETjNZUB/fZFC5/WCtf/I3QmF+TRwCACAAAAACAAAgAAAAAgAAIAAAAAIA"
    "ACAAAAACAAAgAAAAAgAAIAAAAAIAACAAAAACAAAgAAAAAgAAIAAAAAIAACAAAAACAAAgAAAAAgAA"
    "IAAAQAAAAAQAAEAAAAAEAABAAAAABAAAQAAAAAQAAEAAAAAEAABAAAAABAAAQAAAAAQAAEAAAAAE"
    "AABAAAAABAAAQAAAAAQAAEAAAAAEAABAAACAAAAACAAAgAAAAAgAAIAAAAAIAACAAAAACAAAgAAA"
    "AAgAAIAAAAAIAACAAAAACAAAgAAAAAgAAIAAAAAIAACAAAAACAAAgAAAAAgAAIAAAAAIAAAQAAAA"
    "AQAAEAAAAAEAABAAAAABAAAQAAAAAQAAEAAAAAEAABAAAAABAAAQAAAAAQAAEAAAAAEAABAAAAAB"
    "AAAQAAAAAQAAEAAAAAEAABAAACAAAAACAAAgAAAAAgAAIAAAAAIAACAAAAACAAAgAAAAAgAAIAAA"
    "AAIAACAAAAACAAAgAACAk8fhEAAnhLHGWMNx+P9MOSWTTgIAtNm4plTJV0KUlZQcjWdihYjiSAjL"
    "oSAAQFsMatb6brBjcPP7P/dOYzMhCMDvIaXIsqwcVv7TGz9eCiqZySSlJABAGwRAKWesdvTejWsY"
    "0545ADLN0q7yxNSkDP0EAGirCmjlVEpdbG48ewDKYUWyQiIAQLsVQFhrMo7DswTAGGMMJ8nzhTPy"
    "AEAAAAAEAABAAAAABAAAQAAAAAQAAEAAAAAEAABAAAAABAAAQAAAAAQAAEAAAAAEAABAAAAABAAA"
    "QAAAAAQAAEAAAAAEAABAAAAABAAAQAAAgAAAAAgAAIAAAAAIAACAAAAACAAAgAAAAAgAAIAAAAAI"
    "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"
)

ICON_180 = base64.b64decode(ICON_180_B64)
ICON_512 = base64.b64decode(ICON_512_B64)

MANIFEST = json.dumps({
    "name": "LaTeX Workspace",
    "short_name": "LaTeX",
    "start_url": "/m",
    "scope": "/",
    "display": "standalone",
    "background_color": "#1e1e2e",
    "theme_color": "#1e1e2e",
    "icons": [
        {"src": "/icon-180.png", "sizes": "180x180", "type": "image/png"},
        {"src": "/icon-512.png", "sizes": "512x512", "type": "image/png",
         "purpose": "any maskable"},
    ],
}).encode()

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

MOBILE_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="theme-color" content="#1e1e2e">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
<meta name="apple-mobile-web-app-title" content="LaTeX">
<link rel="manifest" href="/manifest.webmanifest">
<link rel="apple-touch-icon" href="/icon-180.png">
<title>LaTeX Workspace</title>
<style>
  *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
  :root {
    --base: #1e1e2e; --mantle: #181825; --crust: #11111b;
    --s0: #313244; --s1: #45475a; --o0: #6c7086; --sub: #a6adc8; --text: #cdd6f4;
    --red: #f38ba8; --yellow: #f9e2af; --peach: #fab387; --green: #a6e3a1;
    --blue: #89b4fa; --mauve: #cba6f7;
    --top: env(safe-area-inset-top); --bot: env(safe-area-inset-bottom);
  }
  html { background: var(--base); }
  body {
    font: 15px -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
    background: var(--base); color: var(--text);
    min-height: 100vh; -webkit-text-size-adjust: 100%; overscroll-behavior-y: contain;
  }
  button { font: inherit; color: inherit; background: none; border: none; }
  [hidden] { display: none !important; }

  header {
    position: sticky; top: 0; z-index: 30;
    padding: calc(var(--top) + 8px) 12px 8px;
    background: #181825f2; -webkit-backdrop-filter: blur(12px); backdrop-filter: blur(12px);
    border-bottom: 1px solid var(--s0);
    display: flex; align-items: center; gap: 10px;
  }
  .burger { font-size: 20px; line-height: 1; padding: 6px 8px; color: var(--sub);
            min-width: 44px; min-height: 44px; }
  .title { flex: 1; min-width: 0; font-size: 15px; font-weight: 600;
           overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .dot { width: 9px; height: 9px; border-radius: 50%; background: var(--s1); flex-shrink: 0; }
  .dot.compiling { background: var(--yellow); animation: pulse 1s ease-in-out infinite; }
  .dot.ready { background: var(--green); }
  .dot.updated { background: var(--blue); }
  .dot.error { background: var(--red); }
  .dot.offline { background: var(--o0); }
  @keyframes pulse { 50% { opacity: .3; } }
  .state { font-size: 12px; color: var(--o0); }

  .scrim { position: fixed; inset: 0; z-index: 40; background: #11111baa;
           opacity: 0; pointer-events: none; transition: opacity .2s; }
  .scrim.open { opacity: 1; pointer-events: auto; }
  .drawer {
    position: fixed; z-index: 41; top: 0; bottom: 0; left: 0; width: min(78vw, 300px);
    background: var(--mantle); border-right: 1px solid var(--s0);
    padding: calc(var(--top) + 16px) 10px calc(var(--bot) + 16px);
    transform: translateX(-100%); transition: transform .22s ease;
    display: flex; flex-direction: column; gap: 4px; overflow-y: auto;
  }
  .drawer.open { transform: none; }
  .drawer h2 { font-size: 11px; text-transform: uppercase; letter-spacing: .08em;
               color: var(--o0); padding: 0 10px 10px; }
  .proj { display: flex; align-items: center; gap: 9px; padding: 13px 12px;
          border-radius: 8px; font-size: 15px; color: #bac2de; text-align: left; width: 100%; }
  .proj.active { background: var(--s1); color: var(--text); font-weight: 600; }
  .proj .pn { flex: 1; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .drawer .fill { flex: 1; }
  .compile { margin: 0 4px; padding: 14px; border-radius: 10px;
             background: var(--mauve); color: var(--base); font-weight: 700; }
  .compile:disabled { opacity: .5; }

  /* Bottom padding clears the fixed problem strip added in Task 7. */
  #main { padding: 10px 10px calc(var(--bot) + 64px); }
  #pages { display: flex; flex-direction: column; gap: 10px; }
  .page { display: block; width: 100%; height: auto; border-radius: 6px;
          background: #fff; box-shadow: 0 1px 6px #11111b80; }
  .page.failed { aspect-ratio: 1 / 1.414; display: flex; align-items: center;
                 justify-content: center; background: var(--mantle);
                 color: var(--o0); font-size: 13px; border: 1px dashed var(--s0); }

  .ptr { height: 0; overflow: hidden; display: flex; align-items: center;
         justify-content: center; color: var(--o0); font-size: 13px;
         transition: height .16s; }
  .ptr.armed { color: var(--mauve); }

  .strip {
    position: fixed; left: 0; right: 0; bottom: 0; z-index: 22;
    padding: 11px 16px calc(var(--bot) + 11px);
    background: #181825f2; -webkit-backdrop-filter: blur(12px); backdrop-filter: blur(12px);
    border-top: 1px solid var(--s0);
    display: flex; align-items: center; gap: 10px; font-size: 13px; color: var(--sub);
  }
  .strip .caret { margin-left: auto; color: var(--o0); transition: transform .2s; }
  .strip.open .caret { transform: rotate(180deg); }
  .sev { width: 8px; height: 8px; border-radius: 50%; flex-shrink: 0; }
  .sev.error { background: var(--red); }
  .sev.warning { background: var(--yellow); }
  .sev.badbox { background: var(--peach); border-radius: 2px; }
  .n { display: inline-flex; align-items: center; gap: 6px; font-variant-numeric: tabular-nums; }
  .n.zero { opacity: .4; }

  .sheet {
    position: fixed; left: 0; right: 0; bottom: 0; z-index: 21;
    max-height: 62vh; overflow-y: auto; -webkit-overflow-scrolling: touch;
    background: var(--crust); border-top: 1px solid var(--s0);
    border-radius: 14px 14px 0 0;
    padding: 8px 0 calc(var(--bot) + 64px);
    transform: translateY(100%); transition: transform .22s ease;
  }
  .sheet.open { transform: none; }
  .prob { display: flex; gap: 10px; align-items: baseline;
          padding: 11px 16px; border-bottom: 1px solid #1e1e2e; }
  .prob .msg { flex: 1; min-width: 0; font: 12.5px/1.5 ui-monospace, Menlo, monospace;
               overflow-wrap: anywhere; }
  .prob.error .msg { color: #f5c2d0; }
  .prob .at { color: var(--o0); font-size: 11px; white-space: nowrap; }
  .sheet .none { padding: 26px 16px; text-align: center; color: var(--o0); font-size: 13px; }

  .empty { padding: 25vh 24px; text-align: center; color: var(--o0); line-height: 1.6; }
  .empty span { display: block; font-size: 34px; margin-bottom: 10px; }
</style>
</head>
<body>
<header>
  <button class="burger" id="burger" aria-label="Projects" aria-expanded="false">&#9776;</button>
  <span class="title" id="title">LaTeX Workspace</span>
  <span class="dot" id="dot"></span>
  <span class="state" id="state"></span>
</header>

<main id="main">
  <div class="ptr" id="ptr">Pull to recompile</div>
  <div id="pages"><div class="empty"><span>&#128196;</span>Choose a project</div></div>
</main>

<div class="scrim" id="scrim"></div>
<nav class="drawer" id="drawer" aria-label="Projects">
  <h2>Projects</h2>
  <div id="projects"></div>
  <div class="fill"></div>
  <button class="compile" id="compile">Compile</button>
</nav>

<div class="sheet" id="sheet"></div>
<div class="strip" id="strip" role="button" tabindex="0" aria-controls="sheet" aria-expanded="false" hidden></div>

<script>
const $ = id => document.getElementById(id);
const store = {
  get(k, d) { try { const v = localStorage.getItem('lwm.' + k); return v === null ? d : JSON.parse(v); } catch { return d; } },
  set(k, v) { try { localStorage.setItem('lwm.' + k, JSON.stringify(v)); } catch {} },
};
function esc(s) {
  return String(s).replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}

let current = null, projects = [];
let pages = 0, pdfMtime = 0, logMtime = -1, compileError = null, logData = null;
let compiling = false, online = true, flashUntil = 0;

/* ---------- drawer ---------- */

function setDrawer(open) {
  $('drawer').classList.toggle('open', open);
  $('scrim').classList.toggle('open', open);
  $('burger').setAttribute('aria-expanded', open);
}
$('burger').addEventListener('click', () => setDrawer(!$('drawer').classList.contains('open')));
$('scrim').addEventListener('click', () => setDrawer(false));
addEventListener('keydown', e => { if (e.key === 'Escape') setDrawer(false); });

/* ---------- projects ---------- */

async function refreshProjects() {
  if (document.hidden) return;  // backgrounded PWAs stay quiet
  let list;
  try {
    list = await (await fetch('/projects')).json();
  } catch { return; }
  projects = list;
  $('projects').innerHTML = list.map(p =>
    `<button class="proj${p.name === current ? ' active' : ''}" data-name="${esc(p.name)}">` +
    `<span class="pn">${esc(p.name)}</span></button>`
  ).join('') || '<div class="empty" style="padding:24px 12px">No projects</div>';

  if (!current) {
    const remembered = store.get('project', null);
    const pick = list.find(p => p.name === remembered) || list[0];
    if (pick) selectProject(pick.name);
  }
}

$('projects').addEventListener('click', e => {
  const btn = e.target.closest('.proj');
  if (btn) { selectProject(btn.dataset.name); setDrawer(false); }
});

function selectProject(name) {
  if (name === current) return;
  clearTimeout(timer);
  current = name;
  store.set('project', name);
  pages = 0; pdfMtime = 0; logMtime = -1; compileError = null; logData = null;
  renderIssues();
  showPages();
  $('title').textContent = name;
  document.title = name + ' · LaTeX';
  refreshProjects();
  renderStatus();
  poll();
}

/* ---------- status ---------- */

function setDot(cls, text) {
  $('dot').className = 'dot ' + cls;
  $('state').textContent = text;
}

function renderStatus() {
  if (!online) return setDot('offline', "can't reach server");
  if (!current) return setDot('', '');
  if (compiling) return setDot('compiling', '');
  if (Date.now() < flashUntil) {
    setTimeout(renderStatus, flashUntil - Date.now() + 20);
    return setDot('updated', 'updated');
  }
  if (!logData) return setDot('', '');
  if (logData.compile_error || (logData.exists && !logData.ok)) return setDot('error', 'failed');
  setDot('ready', '');
}

/* ---------- pages ---------- */

function showPages() {
  const area = $('pages');
  if (!current) {
    area.innerHTML = '<div class="empty"><span>&#128196;</span>Choose a project</div>';
    return;
  }
  if (!pdfMtime || !pages) {
    const why = logData && logData.exists && !logData.ok ? 'the last compile failed' : 'nothing built yet';
    area.innerHTML = '<div class="empty"><span>&#128196;</span>No PDF &mdash; ' + why + '</div>';
    return;
  }
  const frag = document.createDocumentFragment();
  for (let n = 1; n <= pages; n++) {
    const img = document.createElement('img');
    img.className = 'page';
    img.loading = 'lazy';
    img.decoding = 'async';
    img.alt = 'Page ' + n;
    // Hold an A4 slot until the real dimensions arrive, so lazy loads further
    // down the document do not yank the scroll position around.
    img.style.aspectRatio = '1 / 1.414';
    img.addEventListener('load', () => { img.style.aspectRatio = 'auto'; }, { once: true });
    img.addEventListener('error', () => {
      img.replaceWith(Object.assign(document.createElement('div'),
        { className: 'page failed', textContent: 'Page ' + n + ' failed to render' }));
    }, { once: true });
    img.src = '/page/' + encodeURIComponent(current) + '/' + n + '.png' + '?t=' + pdfMtime;
    frag.appendChild(img);
  }
  area.replaceChildren(frag);
}

/* ---------- compile ---------- */

$('compile').addEventListener('click', compileNow);

async function compileNow() {
  if (!current) return;
  clearTimeout(timer);
  compiling = true;
  renderStatus();
  setDrawer(false);
  try { await fetch('/compile/' + encodeURIComponent(current)); } catch {}
  setTimeout(poll, 800);
}

/* ---------- polling ---------- */

const BACKOFF = [2000, 5000, 15000, 30000];
let failures = 0, timer = null;

function schedule() {
  clearTimeout(timer);
  // A backgrounded PWA must not keep a 2s request loop running in a pocket.
  if (document.hidden || !current) return;
  timer = setTimeout(poll, BACKOFF[Math.min(failures, BACKOFF.length - 1)]);
}

async function poll() {
  if (!current) return;
  const name = current;
  let s;
  try {
    s = await (await fetch('/mtime/' + encodeURIComponent(name))).json();
  } catch {
    if (name !== current) return;
    failures++;
    online = false;
    renderStatus();
    schedule();
    return;
  }
  if (name !== current) return;
  failures = 0;
  online = true;

  compiling = s.compiling;
  if (s.mtime !== pdfMtime || s.pages !== pages) {
    const first = pdfMtime === 0;
    pdfMtime = s.mtime;
    pages = s.pages;
    showPages();
    if (!first && pdfMtime) flashUntil = Date.now() + 2000;
  }
  if (s.log_mtime !== logMtime || s.compile_error !== compileError) {
    if (await loadLog(name)) {
      logMtime = s.log_mtime;
      compileError = s.compile_error;
    }
  }
  renderStatus();
  schedule();
}

async function loadLog(name) {
  let data;
  try {
    data = await (await fetch('/log/' + encodeURIComponent(name))).json();
  } catch { return false; }
  if (name !== current) return false;
  logData = data;
  if (!pdfMtime) showPages();
  renderIssues();
  return true;
}

document.addEventListener('visibilitychange', () => {
  if (document.hidden) { clearTimeout(timer); return; }
  failures = 0;
  if (document.querySelector('.page.failed')) showPages();
  poll();
});

refreshProjects();
setInterval(refreshProjects, 5000);

/* ---------- pull to recompile ---------- */

(function () {
  const THRESHOLD = 70, MAX = 90;
  const ptr = $('ptr');
  let startY = null, armed = false;

  addEventListener('touchstart', e => {
    // Only arm at the very top, or this fights the normal scroll. Never arm
    // inside the problems sheet or the project drawer (both their own
    // scrollers), or while the page is pinch-zoomed.
    const inOverlay = e.target.closest('#sheet, #drawer')
      || $('drawer').classList.contains('open')
      || $('sheet').classList.contains('open');
    const zoomed = window.visualViewport && visualViewport.scale > 1;
    startY = (scrollY <= 0 && e.touches.length === 1 && !inOverlay && !zoomed)
      ? e.touches[0].clientY : null;
    armed = false;
  }, { passive: true });

  addEventListener('touchmove', e => {
    if (startY === null) return;
    const dy = e.touches[0].clientY - startY;
    if (dy <= 0) { ptr.style.height = '0px'; return; }
    const h = Math.min(dy * 0.5, MAX);
    ptr.style.height = h + 'px';
    armed = h >= THRESHOLD * 0.5;
    ptr.classList.toggle('armed', armed);
    ptr.textContent = armed ? 'Release to recompile' : 'Pull to recompile';
  }, { passive: true });

  addEventListener('touchend', () => {
    if (startY !== null && armed) compileNow();
    startY = null; armed = false;
    ptr.style.height = '0px';
    ptr.classList.remove('armed');
  }, { passive: true });
})();

/* ---------- problems ---------- */

const KIND_ORDER = { error: 0, warning: 1, badbox: 2 };
const KIND_LABEL = { error: 'error', warning: 'warning', badbox: 'bad box' };
const brokeSeen = {};

function setSheet(open) {
  $('sheet').classList.toggle('open', open);
  $('strip').classList.toggle('open', open);
  $('strip').setAttribute('aria-expanded', open);
}
$('strip').addEventListener('click', () => setSheet(!$('sheet').classList.contains('open')));
$('strip').addEventListener('keydown', e => {
  if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); $('strip').click(); }
});

function renderIssues() {
  const strip = $('strip');
  if (!logData) { strip.hidden = true; setSheet(false); return; }
  const c = logData.counts || { error: 0, warning: 0, badbox: 0 };
  const total = c.error + c.warning + c.badbox;
  const broke = !!(logData.compile_error || (logData.exists && !logData.ok));
  if (!total && !broke) { strip.hidden = true; setSheet(false); return; }

  strip.hidden = false;
  strip.innerHTML =
    ['error', 'warning', 'badbox'].map(k =>
      `<span class="n${c[k] ? '' : ' zero'}"><i class="sev ${k}"></i>${c[k]}</span>`
    ).join('') + '<span class="caret">&#9652;</span>';

  const problems = (logData.problems || []).slice().sort(
    (a, b) => KIND_ORDER[a.kind] - KIND_ORDER[b.kind]
  );
  const rows = problems.map(p =>
    `<div class="prob ${p.kind}"><i class="sev ${p.kind}"></i>` +
    `<span class="msg">${esc(p.message)}</span>` +
    `<span class="at">${p.line ? 'main.tex:' + p.line : KIND_LABEL[p.kind]}</span></div>`
  ).join('');

  $('sheet').innerHTML =
    (logData.compile_error
      ? `<div class="prob error"><i class="sev error"></i>` +
        `<span class="msg">${esc(logData.compile_error)}</span></div>`
      : '') +
    (rows || (logData.compile_error ? '' : '<div class="none">Nothing to report</div>'));

  // Surface a newly broken build without the user having to go looking.
  if (broke && !brokeSeen[current]) setSheet(true);
  brokeSeen[current] = broke;
}
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
            elif p == "/m":
                self._send(200, "text/html", MOBILE_HTML.encode())
            elif p == "/healthz":
                self._serve_health()
            elif p == "/manifest.webmanifest":
                self._send(200, "application/manifest+json", MANIFEST)
            elif p == "/icon-180.png":
                self._send(200, "image/png", ICON_180,
                           {"Cache-Control": "public, max-age=86400"})
            elif p == "/icon-512.png":
                self._send(200, "image/png", ICON_512,
                           {"Cache-Control": "public, max-age=86400"})
            elif p == "/projects":
                self._json(self._list_projects())
            elif p.startswith("/pdf/"):
                self._serve_pdf(p[5:])
            elif p.startswith("/page/"):
                self._serve_page(p[6:])
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

    def _serve_page(self, rest: str) -> None:
        """GET /page/<project>/<n>.png — one rasterized page for the mobile shell."""
        name, _, leaf = rest.rpartition("/")
        if not name or not leaf.endswith(".png"):
            self.send_error(404)
            return
        d = self._project_dir(name)
        if d is None:
            self.send_error(404)
            return
        try:
            n = int(leaf[:-4])
        except ValueError:
            self.send_error(404)
            return
        # Bound the page number before shelling out, so a bad request can
        # never reach poppler.
        if not 1 <= n <= _page_count(d):
            self.send_error(404)
            return
        try:
            mtime_ns = (d / "main.pdf").stat().st_mtime_ns
        except OSError:
            mtime_ns = 0
        if not mtime_ns:
            self.send_error(404)
            return
        png = _render_page(d, n, mtime_ns)
        if png is None:
            self.send_error(502, "Could not rasterize page")
            return
        # Safe to cache forever: a recompile changes the PDF's mtime, and
        # therefore the URL the client requests, so a stale cached response
        # is never reused for the current PDF.
        self._send(200, "image/png", png,
                   {"Cache-Control": "public, max-age=31536000, immutable"})

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
            "pages": _page_count(d) if d else 0,
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
