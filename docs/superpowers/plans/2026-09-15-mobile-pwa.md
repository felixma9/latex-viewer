# Mobile PWA Shell Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Serve an installable, touch-friendly mobile shell at `/m` that renders the current PDF as page images, without changing the desktop viewer.

**Architecture:** `server.py` gains a second HTML constant (`MOBILE_HTML`) and four routes (`/m`, `/manifest.webmanifest`, the two icons, `/page/<project>/<n>.png`). Page images are rasterized lazily by `pdftoppm` on request and cached under `.build/pages/` keyed by PDF mtime. The mobile shell reuses the existing `/projects`, `/mtime/`, `/log/` and `/compile/` JSON endpoints; only `/mtime/` changes, gaining a `pages` field.

**Tech Stack:** Python 3.11 stdlib (`http.server`, no framework), poppler-utils (`pdftoppm`, `pdfinfo`), vanilla HTML/CSS/JS with no build step and no dependencies, pytest for tests.

**Spec:** `docs/superpowers/specs/2026-09-15-mobile-pwa-design.md`

## Global Constraints

- **`INDEX_HTML` must not change.** Not one byte. `/` keeps serving it. No mobile markup, manifest link, or `/page/` reference may be added to it.
- **No service worker.** No `sw.js` route, no `navigator.serviceWorker` call, no cache API usage.
- **No HTTPS, no networking changes.** Plain HTTP on 8585. Nothing touches `docker-compose.yml` port mappings or the host's Tailscale config.
- **No third-party runtime dependencies.** No CDN `<script>` or `<link>` tags, no vendored JS. The shell is self-contained in `server.py`.
- **Python stdlib only** in `server.py`. PIL is used once, on the host, to generate icon bytes; it is never imported by `server.py`.
- **Style:** match the existing file — 4-space indent, type hints on new functions, comments that explain *why* not *what*, Catppuccin CSS custom properties reused from `INDEX_HTML`.
- **Page cache lives in a subdirectory** `.build/pages/`, never as flat files in `.build/`. `_compile_once()` unlinks all *files* in `.build/` on a compile retry (`server.py:76-78`); a subdirectory survives, flat files would not.

---

### Task 1: Test harness and desktop regression guard

Sets up pytest against a live server instance and locks in the "desktop is untouched" constraint before any other code lands.

**Files:**
- Create: `tests/conftest.py`
- Create: `tests/test_desktop_unchanged.py`
- Modify: `.gitignore` (add `.pytest_cache/`)

**Interfaces:**
- Consumes: nothing
- Produces: pytest fixture `client(tmp_path)` yielding a `Client` with `.get(path) -> Response`, where `Response` has `.status: int`, `.body: bytes`, `.headers: email.message.Message`, and `.json() -> object`. Every later task's tests use this fixture. Also produces `make_project(root, name, *, pdf=None, log=None) -> Path`.

- [ ] **Step 1: Write the fixture**

Create `tests/conftest.py`:

```python
import json
import sys
import threading
from dataclasses import dataclass
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import urlopen

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import server  # noqa: E402


@dataclass
class Response:
    status: int
    body: bytes
    headers: object

    def json(self):
        return json.loads(self.body)


class Client:
    def __init__(self, base: str) -> None:
        self.base = base

    def get(self, path: str) -> Response:
        try:
            with urlopen(self.base + path, timeout=10) as r:
                return Response(r.status, r.read(), r.headers)
        except HTTPError as e:
            return Response(e.code, e.read(), e.headers)


@pytest.fixture(autouse=True)
def _clear_caches():
    """Module-level memos outlive a test; clear them so tests cannot leak state."""
    for name in ("_page_counts", "_page_locks"):
        getattr(server, name, {}).clear()
    yield


@pytest.fixture
def client(tmp_path, monkeypatch):
    """A live server whose /documents is an empty tmp_path."""
    monkeypatch.setattr(server, "DOCUMENTS_DIR", tmp_path.resolve())
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield Client(f"http://127.0.0.1:{httpd.server_port}")
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)


def make_project(root: Path, name: str, *, pdf: bytes | None = None,
                 log: str | None = None) -> Path:
    """Create a project directory under `root` the way the watcher expects."""
    d = root / name
    d.mkdir(parents=True)
    (d / "main.tex").write_text(r"\documentclass{article}\begin{document}x\end{document}")
    if pdf is not None:
        (d / "main.pdf").write_bytes(pdf)
    if log is not None:
        (d / "main.log").write_text(log)
    return d


# A byte string that passes server._is_complete_pdf().
FAKE_PDF = b"%PDF-1.4\n" + b"x" * 200 + b"\n%%EOF\n"

# A pdflatex log tail carrying the line server.parse_log() reads page counts from.
LOG_2_PAGES = "This is pdfTeX\nOutput written on main.pdf (2 pages, 12345 bytes).\n"
```

- [ ] **Step 2: Write the failing test**

Create `tests/test_desktop_unchanged.py`:

```python
import server


def test_root_serves_index_html_verbatim(client):
    r = client.get("/")
    assert r.status == 200
    assert r.body == server.INDEX_HTML.encode()
    assert r.headers["Content-Type"] == "text/html"


def test_index_html_has_no_mobile_bits():
    """The desktop page must not grow mobile markup. Guards the core constraint."""
    for marker in ("/page/", "manifest.webmanifest", "apple-mobile-web-app", "/m'", '/m"'):
        assert marker not in server.INDEX_HTML, f"{marker!r} leaked into the desktop page"


def test_desktop_still_uses_the_iframe_viewer():
    """Desktop PDF rendering is unchanged; only the mobile shell uses page images."""
    assert "createElement('iframe')" in server.INDEX_HTML
```

- [ ] **Step 3: Run tests to verify they pass**

These pass immediately — they are regression guards for code that already exists, not new behaviour. The point is that they fail later if someone edits the wrong constant.

Run: `python3 -m pytest tests/ -v`
Expected: 3 passed

- [ ] **Step 4: Ignore the pytest cache**

Add to `.gitignore`, under the `__pycache__/` line:

```
.pytest_cache/
```

- [ ] **Step 5: Commit**

```bash
git add tests/conftest.py tests/test_desktop_unchanged.py .gitignore
git commit -m "test: add harness and desktop regression guard

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 2: Page count

The mobile shell needs to know how many `<img>` elements to create before any of them load. The count comes from the log the compiler already writes, with `pdfinfo` covering PDFs built outside the container.

**Files:**
- Modify: `server.py` (add `_page_count()` immediately after `parse_log()`, which ends around line 310; extend `_serve_mtime()` at `server.py:1031`)
- Create: `tests/test_page_count.py`

**Interfaces:**
- Consumes: `client`, `make_project`, `FAKE_PDF`, `LOG_2_PAGES` from Task 1
- Produces: `server._page_count(project_dir: Path) -> int` — page count, or `0` when unknown. `/mtime/<name>` gains `"pages": int`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_page_count.py`:

```python
import subprocess

import pytest

import server
from conftest import FAKE_PDF, LOG_2_PAGES, make_project


def test_counts_pages_from_the_log(tmp_path):
    d = make_project(tmp_path, "doc", pdf=FAKE_PDF, log=LOG_2_PAGES)
    assert server._page_count(d) == 2


def test_falls_back_to_pdfinfo_when_the_log_has_no_count(tmp_path, monkeypatch):
    d = make_project(tmp_path, "doc", pdf=FAKE_PDF, log="This is pdfTeX\nno count here\n")

    def fake_run(cmd, **kwargs):
        assert cmd[0] == "pdfinfo"
        return subprocess.CompletedProcess(cmd, 0, stdout="Title: x\nPages:          7\n", stderr="")

    monkeypatch.setattr(server.subprocess, "run", fake_run)
    assert server._page_count(d) == 7


def test_falls_back_to_pdfinfo_when_there_is_no_log(tmp_path, monkeypatch):
    d = make_project(tmp_path, "doc", pdf=FAKE_PDF)
    monkeypatch.setattr(
        server.subprocess, "run",
        lambda cmd, **kw: subprocess.CompletedProcess(cmd, 0, stdout="Pages: 3\n", stderr=""),
    )
    assert server._page_count(d) == 3


def test_zero_when_there_is_no_pdf(tmp_path, monkeypatch):
    d = make_project(tmp_path, "doc")

    def explode(*a, **kw):
        pytest.fail("pdfinfo must not run when there is no PDF")

    monkeypatch.setattr(server.subprocess, "run", explode)
    assert server._page_count(d) == 0


def test_zero_when_pdfinfo_is_missing(tmp_path, monkeypatch):
    d = make_project(tmp_path, "doc", pdf=FAKE_PDF)
    monkeypatch.setattr(
        server.subprocess, "run",
        lambda *a, **kw: (_ for _ in ()).throw(FileNotFoundError("pdfinfo")),
    )
    assert server._page_count(d) == 0


def test_repeat_calls_are_memoized(tmp_path, monkeypatch):
    """/mtime/ is polled every 2s per client; it must not re-parse the log each time."""
    d = make_project(tmp_path, "doc", pdf=FAKE_PDF, log=LOG_2_PAGES)
    calls = []
    real = server.parse_log
    monkeypatch.setattr(server, "parse_log", lambda t: calls.append(1) or real(t))

    assert server._page_count(d) == 2
    assert server._page_count(d) == 2
    assert len(calls) == 1

    # A recompile changes the log mtime and must invalidate the memo.
    import os
    os.utime(d / "main.log", (2_000_000_000, 2_000_000_000))
    assert server._page_count(d) == 2
    assert len(calls) == 2


def test_mtime_route_reports_pages_alongside_its_existing_fields(client, tmp_path):
    make_project(tmp_path, "doc", pdf=FAKE_PDF, log=LOG_2_PAGES)
    body = client.get("/mtime/doc").json()
    assert body["pages"] == 2
    # The desktop viewer reads these four; they must keep working.
    assert set(body) >= {"mtime", "log_mtime", "compiling", "compile_error"}
    assert body["mtime"] > 0


def test_mtime_route_reports_zero_pages_for_unknown_project(client):
    assert client.get("/mtime/nope").json()["pages"] == 0
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_page_count.py -v`
Expected: FAIL with `AttributeError: module 'server' has no attribute '_page_count'`

- [ ] **Step 3: Implement `_page_count()`**

In `server.py`, add immediately after `parse_log()` ends (around line 310). It must sit below `parse_log`, since it calls it.

```python
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
```

Note: `_page_count` must be defined *below* `parse_log`, which sits at `server.py:188`. Place it immediately after `parse_log`'s closing `return` block (after line ~310) rather than at line 143, so the name is bound before use at import time is irrelevant but reading order stays sensible.

- [ ] **Step 4: Extend `/mtime/`**

Replace the body of `_serve_mtime()` at `server.py:1031`:

```python
    def _serve_mtime(self, name: str) -> None:
        d = self._project_dir(name)
        self._json({
            "mtime": _mtime(d / "main.pdf") if d else 0,
            "log_mtime": _mtime(d / "main.log") if d else 0,
            "compiling": d is not None and d.name in _compiling,
            "compile_error": _compile_errors.get(d.name) if d else None,
            "pages": _page_count(d) if d else 0,
        })
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `python3 -m pytest tests/ -v`
Expected: 11 passed

- [ ] **Step 6: Commit**

```bash
git add server.py tests/test_page_count.py
git commit -m "feat: report PDF page count from /mtime

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 3: Page rasterization and the `/page/` route

The reason the mobile shell exists: iOS Safari will not scroll a PDF in an iframe, so pages are served as images.

**Files:**
- Modify: `server.py` (constants near line 26; `_render_page()` after `_page_count()`; route in `do_GET` at `server.py:953`; `_serve_page()` handler)
- Create: `tests/test_page_route.py`

**Interfaces:**
- Consumes: `_page_count()` from Task 2, `_project_dir()` and `_mtime()` which already exist
- Produces: `server._render_page(project_dir: Path, n: int, mtime: float) -> bytes | None` returning PNG bytes or `None` on render failure. Route `GET /page/<project>/<n>.png` returning `image/png`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_page_route.py`:

```python
import os
import subprocess
import threading
from pathlib import Path

import pytest

import server
from conftest import FAKE_PDF, LOG_2_PAGES, make_project

PNG = b"\x89PNG\r\n\x1a\n" + b"fake-png-bytes"


@pytest.fixture
def fake_pdftoppm(monkeypatch):
    """Stand in for poppler: write one PNG into the output directory."""
    calls = []
    real_run = subprocess.run

    def fake_run(cmd, **kwargs):
        if cmd[0] != "pdftoppm":
            return real_run(cmd, **kwargs)
        calls.append(cmd)
        Path(cmd[-1] + "-1.png").write_bytes(PNG)
        return subprocess.CompletedProcess(cmd, 0, stdout=b"", stderr=b"")

    monkeypatch.setattr(server.subprocess, "run", fake_run)
    return calls


@pytest.fixture
def no_subprocess(monkeypatch):
    """Fail loudly if anything shells out. Used by the rejection tests."""
    def explode(*a, **kw):
        pytest.fail(f"must not shell out: {a!r}")

    monkeypatch.setattr(server.subprocess, "run", explode)


def test_serves_a_rendered_page(client, tmp_path, fake_pdftoppm):
    make_project(tmp_path, "doc", pdf=FAKE_PDF, log=LOG_2_PAGES)
    r = client.get("/page/doc/1.png")
    assert r.status == 200
    assert r.body == PNG
    assert r.headers["Content-Type"] == "image/png"
    assert len(fake_pdftoppm) == 1


def test_renders_the_requested_page_at_the_configured_dpi(client, tmp_path, fake_pdftoppm):
    make_project(tmp_path, "doc", pdf=FAKE_PDF, log=LOG_2_PAGES)
    client.get("/page/doc/2.png")
    cmd = fake_pdftoppm[0]
    assert cmd[cmd.index("-f") + 1] == "2"
    assert cmd[cmd.index("-l") + 1] == "2"
    assert cmd[cmd.index("-r") + 1] == str(server.PAGE_DPI)


def test_second_request_is_served_from_cache(client, tmp_path, fake_pdftoppm):
    make_project(tmp_path, "doc", pdf=FAKE_PDF, log=LOG_2_PAGES)
    assert client.get("/page/doc/1.png").body == PNG
    assert client.get("/page/doc/1.png").body == PNG
    assert len(fake_pdftoppm) == 1, "cached page was re-rendered"


def test_cache_lives_in_a_subdirectory_of_build(client, tmp_path, fake_pdftoppm):
    d = make_project(tmp_path, "doc", pdf=FAKE_PDF, log=LOG_2_PAGES)
    client.get("/page/doc/1.png")
    cache = d / server.BUILD_DIRNAME / server.PAGES_DIRNAME
    assert cache.is_dir()
    assert list(cache.glob("*.png"))
    # A compile retry unlinks files directly in .build/ but not subdirectories.
    assert not list((d / server.BUILD_DIRNAME).glob("*.png"))


def test_a_new_pdf_mtime_re_renders_and_sweeps_the_old_pages(client, tmp_path, fake_pdftoppm):
    d = make_project(tmp_path, "doc", pdf=FAKE_PDF, log=LOG_2_PAGES)
    client.get("/page/doc/1.png")
    cache = d / server.BUILD_DIRNAME / server.PAGES_DIRNAME
    stale = sorted(p.name for p in cache.glob("*.png"))

    os.utime(d / "main.pdf", (2_000_000_000, 2_000_000_000))
    client.get("/page/doc/1.png")

    assert len(fake_pdftoppm) == 2
    fresh = sorted(p.name for p in cache.glob("*.png"))
    assert fresh != stale
    assert len(fresh) == 1, "stale page images were not swept"


def test_rejects_a_project_outside_documents(client, tmp_path, no_subprocess):
    make_project(tmp_path, "doc", pdf=FAKE_PDF, log=LOG_2_PAGES)
    assert client.get("/page/..%2F..%2Fetc/1.png").status == 404


def test_rejects_an_unknown_project(client, no_subprocess):
    assert client.get("/page/nope/1.png").status == 404


def test_rejects_a_non_numeric_page(client, tmp_path, no_subprocess):
    make_project(tmp_path, "doc", pdf=FAKE_PDF, log=LOG_2_PAGES)
    assert client.get("/page/doc/x.png").status == 404


def test_rejects_a_page_beyond_the_document(client, tmp_path, no_subprocess):
    make_project(tmp_path, "doc", pdf=FAKE_PDF, log=LOG_2_PAGES)
    assert client.get("/page/doc/3.png").status == 404
    assert client.get("/page/doc/0.png").status == 404
    assert client.get("/page/doc/-1.png").status == 404


def test_returns_502_when_the_renderer_fails(client, tmp_path, monkeypatch):
    make_project(tmp_path, "doc", pdf=FAKE_PDF, log=LOG_2_PAGES)
    monkeypatch.setattr(
        server.subprocess, "run",
        lambda *a, **kw: (_ for _ in ()).throw(FileNotFoundError("pdftoppm")),
    )
    assert client.get("/page/doc/1.png").status == 502


def test_concurrent_requests_render_once(client, tmp_path, fake_pdftoppm):
    make_project(tmp_path, "doc", pdf=FAKE_PDF, log=LOG_2_PAGES)
    bodies = []
    threads = [threading.Thread(target=lambda: bodies.append(client.get("/page/doc/1.png").body))
               for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=15)
    assert bodies == [PNG] * 4
    assert len(fake_pdftoppm) == 1, "the per-project lock did not serialise rendering"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_page_route.py -v`
Expected: FAIL with `AttributeError: module 'server' has no attribute 'PAGE_DPI'`

- [ ] **Step 3: Add the module constants**

In `server.py`, add `import shutil` to the import block (alphabetically, after `import re`), and add near the other tunables around line 26:

```python
PAGES_DIRNAME = "pages"  # under .build/, so a compile retry cannot unlink it
PAGE_DPI = 150  # A4 at 150dpi is ~1240px wide: a 390pt iPhone at 3x
PDFTOPPM_TIMEOUT = 60
_page_locks: dict[str, threading.Lock] = {}
_page_locks_guard = threading.Lock()
```

- [ ] **Step 4: Implement `_render_page()`**

In `server.py`, add after `_page_count()`:

```python
def _page_lock(name: str) -> threading.Lock:
    with _page_locks_guard:
        return _page_locks.setdefault(name, threading.Lock())


def _render_page(project_dir: Path, n: int, mtime: float) -> bytes | None:
    """PNG bytes for page `n`, rasterizing through poppler if not cached.

    Cached under .build/pages/<mtime>-<n>.png. The mtime in the name means a
    recompile invalidates every page without any explicit invalidation step;
    pages from older mtimes are swept on the first request after the change.

    Returns None if poppler is missing or the render fails.
    """
    cache = project_dir / BUILD_DIRNAME / PAGES_DIRNAME
    stamp = f"{mtime:.0f}"
    target = cache / f"{stamp}-{n}.png"

    # One render at a time per project, or concurrent requests for the same
    # page race each other writing the same file.
    with _page_lock(project_dir.name):
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
        except (OSError, subprocess.SubprocessError):
            return None
        finally:
            shutil.rmtree(scratch, ignore_errors=True)
```

- [ ] **Step 5: Implement the route**

In `do_GET` (`server.py:953`), add a branch after the `/pdf/` one:

```python
            elif p.startswith("/page/"):
                self._serve_page(p[6:])
```

And add the handler next to `_serve_pdf()`:

```python
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
        mtime = _mtime(d / "main.pdf")
        if not mtime:
            self.send_error(404)
            return
        png = _render_page(d, n, mtime)
        if png is None:
            self.send_error(502, "Could not rasterize page")
            return
        # The mtime is in the query string, so a cached page is never stale.
        self._send(200, "image/png", png,
                   {"Cache-Control": "public, max-age=31536000, immutable"})
```

- [ ] **Step 6: Run tests to verify they pass**

Run: `python3 -m pytest tests/ -v`
Expected: 22 passed

- [ ] **Step 7: Smoke-test against real poppler**

The host has `pdftoppm`. Confirm the real binary agrees with the stub:

```bash
python3 - <<'EOF'
import sys, threading; sys.path.insert(0, '.')
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.request import urlopen
import server
server.DOCUMENTS_DIR = Path('documents').resolve()
httpd = ThreadingHTTPServer(('127.0.0.1', 0), server.Handler)
threading.Thread(target=httpd.serve_forever, daemon=True).start()
base = f'http://127.0.0.1:{httpd.server_port}'
print(urlopen(base + '/mtime/cv-main').read().decode())
png = urlopen(base + '/page/cv-main/1.png').read()
print('png bytes:', len(png), 'magic ok:', png[:8] == b'\x89PNG\r\n\x1a\n')
EOF
```
Expected: a `pages` count above 0, and tens of kilobytes of valid PNG.

- [ ] **Step 8: Commit**

```bash
git add server.py tests/test_page_route.py
git commit -m "feat: rasterize PDF pages on demand for mobile clients

iOS Safari will not scroll a PDF in an iframe, so the mobile shell needs
page images. Rendering is lazy and cached by PDF mtime, so desktop-only
sessions never invoke poppler.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 4: Icons and web manifest

**Files:**
- Create: `tools/make_icons.py` (host-only generator, never imported by `server.py`)
- Modify: `server.py` (icon constants, manifest constant, three routes)
- Create: `tests/test_manifest.py`

**Interfaces:**
- Consumes: `client` from Task 1
- Produces: routes `GET /manifest.webmanifest`, `GET /icon-180.png`, `GET /icon-512.png`. Constants `server.ICON_180_B64`, `server.ICON_512_B64`, `server.MANIFEST`.

- [ ] **Step 1: Write the icon generator**

Create `tools/make_icons.py`. This runs once on the host, where PIL is installed; its output is pasted into `server.py`.

```python
#!/usr/bin/env python3
"""Generate the PWA icons and print them as base64 constants for server.py.

Run on the host (needs Pillow), not in the container:
    python3 tools/make_icons.py >> /tmp/icons.txt

Kept as a script so the icons can be regenerated, but server.py carries the
bytes inline to preserve its single-file, stdlib-only property.
"""
import base64
import io
import textwrap

from PIL import Image, ImageDraw

BASE = "#1e1e2e"    # Catppuccin base, matches the app background
MAUVE = "#cba6f7"   # the accent INDEX_HTML uses for its title


def render(size: int, safe: float) -> bytes:
    """A rounded-square mark. `safe` is the fraction of the canvas kept clear
    so Android's maskable crop cannot clip the glyph."""
    scale = 4  # supersample, then downscale, for clean edges without antialias flags
    px = size * scale
    img = Image.new("RGBA", (px, px), BASE)
    d = ImageDraw.Draw(img)

    inset = px * safe
    box = (inset, inset, px - inset, px - inset)
    d.rounded_rectangle(box, radius=px * 0.16, fill=MAUVE)

    # A serif "T" with a descender bar, reading as TeX without needing a font file.
    w = px - 2 * inset
    stem = w * 0.12
    cx = px / 2
    top = inset + w * 0.24
    d.rectangle((cx - w * 0.28, top, cx + w * 0.28, top + stem), fill=BASE)
    d.rectangle((cx - stem / 2, top, cx + stem / 2, inset + w * 0.76), fill=BASE)
    d.rectangle((cx - stem / 2, inset + w * 0.64, cx + w * 0.30, inset + w * 0.76), fill=BASE)

    buf = io.BytesIO()
    img.resize((size, size), Image.LANCZOS).convert("RGB").save(buf, "PNG", optimize=True)
    return buf.getvalue()


for name, size, safe in (("ICON_180_B64", 180, 0.02), ("ICON_512_B64", 512, 0.10)):
    b64 = base64.b64encode(render(size, safe)).decode()
    print(f'{name} = (\n' + "\n".join(f'    "{c}"' for c in textwrap.wrap(b64, 76)) + "\n)\n")
```

iOS ignores the manifest icons for the home screen and uses `apple-touch-icon`, so the 180px file is the one that matters visually; it gets almost no safe-zone inset because iOS applies its own mask. The 512px one is inset 10% so Android's maskable crop cannot clip it.

- [ ] **Step 2: Generate the icons and paste them in**

```bash
python3 tools/make_icons.py > /tmp/icons.txt
head -3 /tmp/icons.txt
```

Paste both constants into `server.py` immediately above `INDEX_HTML` (around line 330). Then add, below them:

```python
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
```

Add `import base64` to the import block, first alphabetically.

- [ ] **Step 3: Write the failing test**

Create `tests/test_manifest.py`:

```python
import json

import server


def test_manifest_is_served_with_the_right_type(client):
    r = client.get("/manifest.webmanifest")
    assert r.status == 200
    assert r.headers["Content-Type"] == "application/manifest+json"


def test_manifest_opens_the_mobile_shell_standalone(client):
    m = json.loads(client.get("/manifest.webmanifest").body)
    assert m["start_url"] == "/m"
    assert m["display"] == "standalone"
    assert m["name"] == "LaTeX Workspace"


def test_manifest_icons_resolve(client):
    m = json.loads(client.get("/manifest.webmanifest").body)
    for icon in m["icons"]:
        r = client.get(icon["src"])
        assert r.status == 200, icon["src"]
        assert r.headers["Content-Type"] == "image/png"
        assert r.body[:8] == b"\x89PNG\r\n\x1a\n"


def test_icons_are_the_sizes_they_claim():
    # PNG dimensions live in the IHDR chunk: bytes 16-24.
    import struct
    for data, expected in ((server.ICON_180, 180), (server.ICON_512, 512)):
        w, h = struct.unpack(">II", data[16:24])
        assert (w, h) == (expected, expected)


def test_no_service_worker_route(client):
    """Offline support is explicitly out of scope."""
    assert client.get("/sw.js").status == 404
    assert "serviceWorker" not in server.INDEX_HTML
```

- [ ] **Step 4: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_manifest.py -v`
Expected: FAIL — `/manifest.webmanifest` returns 404

- [ ] **Step 5: Add the routes**

In `do_GET`, after the `/healthz` branch:

```python
            elif p == "/manifest.webmanifest":
                self._send(200, "application/manifest+json", MANIFEST)
            elif p == "/icon-180.png":
                self._send(200, "image/png", ICON_180,
                           {"Cache-Control": "public, max-age=86400"})
            elif p == "/icon-512.png":
                self._send(200, "image/png", ICON_512,
                           {"Cache-Control": "public, max-age=86400"})
```

- [ ] **Step 6: Run tests to verify they pass**

Run: `python3 -m pytest tests/ -v`
Expected: 27 passed

- [ ] **Step 7: Eyeball the icon**

```bash
python3 -c "
import sys; sys.path.insert(0,'.')
import server; open('/tmp/icon.png','wb').write(server.ICON_180)"
```
Open `/tmp/icon.png`. If it looks wrong, adjust `tools/make_icons.py` and regenerate — do not hand-edit the base64.

- [ ] **Step 8: Commit**

```bash
git add tools/make_icons.py server.py tests/test_manifest.py
git commit -m "feat: add PWA manifest and home-screen icons

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 5: Mobile shell — structure, drawer, project switching

The first half of `MOBILE_HTML`: document head, header, project drawer, and project loading. Page rendering and the error strip arrive in Tasks 6 and 7.

**Files:**
- Modify: `server.py` (add `MOBILE_HTML` after `INDEX_HTML`; add the `/m` route)
- Create: `tests/test_mobile_shell.py`

**Interfaces:**
- Consumes: `/projects` (unchanged), manifest route from Task 4
- Produces: `server.MOBILE_HTML`, route `GET /m`. JS globals later tasks extend: `current` (project name or `null`), `$()`, `store`, and functions `selectProject(name)`, `renderStatus()`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_mobile_shell.py`:

```python
import server


def test_m_route_serves_the_mobile_shell(client):
    r = client.get("/m")
    assert r.status == 200
    assert r.headers["Content-Type"] == "text/html"
    assert r.body == server.MOBILE_HTML.encode()


def test_shell_is_configured_as_an_ios_web_app():
    html = server.MOBILE_HTML
    assert 'rel="manifest" href="/manifest.webmanifest"' in html
    assert 'rel="apple-touch-icon" href="/icon-180.png"' in html
    assert 'name="apple-mobile-web-app-capable" content="yes"' in html
    assert 'name="theme-color"' in html


def test_shell_covers_the_notch_and_allows_pinch_zoom():
    html = server.MOBILE_HTML
    assert "viewport-fit=cover" in html
    # Locking zoom would make a full-page CV unreadable.
    assert "maximum-scale" not in html
    assert "user-scalable=no" not in html
    assert "env(safe-area-inset-" in html


def test_shell_has_no_service_worker_and_no_external_assets():
    html = server.MOBILE_HTML
    assert "serviceWorker" not in html
    for marker in ("http://", "https://", "cdn.", "//unpkg"):
        assert marker not in html, f"external reference {marker!r} in the shell"


def test_shell_does_not_use_an_iframe():
    """The whole reason this shell exists."""
    assert "iframe" not in server.MOBILE_HTML


def test_projects_endpoint_still_works_for_the_shell(client, tmp_path):
    from conftest import FAKE_PDF, make_project
    make_project(tmp_path, "alpha", pdf=FAKE_PDF)
    make_project(tmp_path, "beta")
    names = [p["name"] for p in client.get("/projects").json()]
    assert names == ["alpha", "beta"]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_mobile_shell.py -v`
Expected: FAIL with `AttributeError: module 'server' has no attribute 'MOBILE_HTML'`

- [ ] **Step 3: Write the shell head, styles and markup**

In `server.py`, after `INDEX_HTML`'s closing `"""`, add:

```python
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
  .burger { font-size: 20px; line-height: 1; padding: 6px 8px; color: var(--sub); }
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

  .empty { padding: 25vh 24px; text-align: center; color: var(--o0); line-height: 1.6; }
  .empty span { display: block; font-size: 34px; margin-bottom: 10px; }
</style>
</head>
<body>
<header>
  <button class="burger" id="burger" aria-label="Projects">&#9776;</button>
  <span class="title" id="title">LaTeX Workspace</span>
  <span class="dot" id="dot"></span>
  <span class="state" id="state"></span>
</header>

<main id="main">
  <div class="empty"><span>&#128196;</span>Choose a project</div>
</main>

<div class="scrim" id="scrim"></div>
<nav class="drawer" id="drawer">
  <h2>Projects</h2>
  <div id="projects"></div>
  <div class="fill"></div>
  <button class="compile" id="compile">Compile</button>
</nav>

<script>
const $ = id => document.getElementById(id);
const store = {
  get(k, d) { try { const v = localStorage.getItem('lwm.' + k); return v === null ? d : JSON.parse(v); } catch { return d; } },
  set(k, v) { try { localStorage.setItem('lwm.' + k, JSON.stringify(v)); } catch {} },
};
function esc(s) {
  return String(s).replace(/[&<>"]/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
}

let current = null, projects = [];
let pages = 0, pdfMtime = 0, logMtime = -1, compileError = null, logData = null;
let compiling = false, online = true, flashUntil = 0;

/* ---------- drawer ---------- */

function setDrawer(open) {
  $('drawer').classList.toggle('open', open);
  $('scrim').classList.toggle('open', open);
}
$('burger').addEventListener('click', () => setDrawer(!$('drawer').classList.contains('open')));
$('scrim').addEventListener('click', () => setDrawer(false));

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
  current = name;
  store.set('project', name);
  pages = 0; pdfMtime = 0; logMtime = -1; compileError = null; logData = null;
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
  if (!online) return setDot('offline', 'offline');
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

/* ---------- compile ---------- */

$('compile').addEventListener('click', compileNow);

async function compileNow() {
  if (!current) return;
  compiling = true;
  renderStatus();
  setDrawer(false);
  try { await fetch('/compile/' + encodeURIComponent(current)); } catch {}
  setTimeout(poll, 800);
}

/* poll() is defined in the polling section below. */
refreshProjects();
setInterval(refreshProjects, 5000);
</script>
</body>
</html>
"""
```

- [ ] **Step 4: Add the route**

In `do_GET`, change the first branch so `/m` is served alongside `/` without disturbing it:

```python
            if p in ("/", "/index.html"):
                self._send(200, "text/html", INDEX_HTML.encode())
            elif p == "/m":
                self._send(200, "text/html", MOBILE_HTML.encode())
```

- [ ] **Step 5: Run tests to verify they pass**

The shell references `poll()`, which lands in Task 6. Tests here only assert the served document, so they pass; the page is not yet functional in a browser.

Run: `python3 -m pytest tests/ -v`
Expected: 33 passed

- [ ] **Step 6: Commit**

```bash
git add server.py tests/test_mobile_shell.py
git commit -m "feat: add mobile shell with project drawer at /m

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 6: Page rendering, polling and pull-to-refresh

Makes the shell live: it shows pages, follows recompiles, and stops hammering the server when backgrounded or offline.

**Files:**
- Modify: `server.py` (extend `MOBILE_HTML` styles and script)
- Modify: `tests/test_mobile_shell.py`

**Interfaces:**
- Consumes: `current`, `$()`, `renderStatus()`, `store` from Task 5; `/mtime/` `pages` field from Task 2; `/page/` route from Task 3
- Produces: JS functions `poll()`, `showPages()`, `schedule()`; the `loadLog()` stub that Task 7 fills in

- [ ] **Step 1: Write the failing test**

Append to `tests/test_mobile_shell.py`:

```python
def test_pages_are_requested_with_a_cache_busting_mtime():
    assert "'/page/' + encodeURIComponent(current)" in server.MOBILE_HTML
    assert "'?t=' + pdfMtime" in server.MOBILE_HTML


def test_page_images_are_lazy_and_reserve_their_height():
    html = server.MOBILE_HTML
    assert "loading = 'lazy'" in html
    # Reserving an A4 ratio stops the scroll jumping as images arrive.
    assert "aspectRatio" in html


def test_polling_pauses_when_the_app_is_backgrounded():
    html = server.MOBILE_HTML
    assert "visibilitychange" in html
    assert "document.hidden" in html


def test_polling_backs_off_on_failure():
    html = server.MOBILE_HTML
    assert "BACKOFF = [2000, 5000, 15000, 30000]" in html
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_mobile_shell.py -v`
Expected: 4 FAIL, the Task 5 tests still passing

- [ ] **Step 3: Add the page styles**

In `MOBILE_HTML`'s `<style>`, before the `.empty` rule:

```css
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
```

Then replace the whole `<main>` block from Task 5 with this one, which adds the
pull-to-refresh indicator and moves the placeholder into a `#pages` container:

```html
<main id="main">
  <div class="ptr" id="ptr">Pull to recompile</div>
  <div id="pages"><div class="empty"><span>&#128196;</span>Choose a project</div></div>
</main>
```

- [ ] **Step 4: Add page rendering**

Insert into the script, after the status section:

```js
/* ---------- pages ---------- */

function showPages() {
  const area = $('pages');
  if (!current) {
    area.innerHTML = '<div class="empty"><span>&#128196;</span>Choose a project</div>';
    return;
  }
  if (!pdfMtime || !pages) {
    const why = logData && !logData.ok ? 'the last compile failed' : 'nothing built yet';
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
    img.src = '/page/' + encodeURIComponent(current) + '/' + n + '.png?t=' + pdfMtime;
    frag.appendChild(img);
  }
  area.replaceChildren(frag);
}
```

- [ ] **Step 5: Add polling with backoff and visibility handling**

Replace the `/* poll() is defined in the polling section below. */` comment with:

```js
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
    logMtime = s.log_mtime;
    compileError = s.compile_error;
    await loadLog(name);
  }
  renderStatus();
  schedule();
}

async function loadLog(name) {
  try {
    const data = await (await fetch('/log/' + encodeURIComponent(name))).json();
    if (name !== current) return;
    logData = data;
  } catch { return; }
  if (!pdfMtime) showPages();
}

document.addEventListener('visibilitychange', () => {
  if (document.hidden) { clearTimeout(timer); return; }
  failures = 0;
  poll();
});
```

`loadLog()` gains its error-strip rendering in Task 7.

- [ ] **Step 6: Add pull-to-refresh**

Append to the script:

```js
/* ---------- pull to recompile ---------- */

(function () {
  const THRESHOLD = 70, MAX = 90;
  const ptr = $('ptr');
  let startY = null, armed = false;

  addEventListener('touchstart', e => {
    // Only arm at the very top, or this fights the normal scroll.
    startY = (scrollY <= 0 && e.touches.length === 1) ? e.touches[0].clientY : null;
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
```

- [ ] **Step 7: Run tests to verify they pass**

Run: `python3 -m pytest tests/ -v`
Expected: 37 passed

- [ ] **Step 8: Check it in a desktop browser first**

Run the server against the real documents directory and open `http://127.0.0.1:8586/m` with the browser's device toolbar set to iPhone:

```bash
python3 - <<'EOF'
import sys, threading; sys.path.insert(0, '.')
from http.server import ThreadingHTTPServer
from pathlib import Path
import server
server.DOCUMENTS_DIR = Path('documents').resolve()
print('http://127.0.0.1:8586/m')
ThreadingHTTPServer(('127.0.0.1', 8586), server.Handler).serve_forever()
EOF
```
Expected: the project list opens from the burger, pages render and scroll, the console is clean.

- [ ] **Step 9: Commit**

```bash
git add server.py tests/test_mobile_shell.py
git commit -m "feat: render pages and poll for changes in the mobile shell

Polling pauses while backgrounded and backs off on failure, so an
installed PWA does not run a 2s request loop all day.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 7: Error strip and problems sheet

**Files:**
- Modify: `server.py` (extend `MOBILE_HTML`)
- Create: `tests/test_mobile_errors.py`

**Interfaces:**
- Consumes: `logData`, `loadLog()`, `$()`, `esc()` from Tasks 5-6; `/log/` (unchanged)
- Produces: JS `renderIssues()`, `setSheet(open)`

- [ ] **Step 1: Write the failing test**

Create `tests/test_mobile_errors.py`:

```python
import server


def test_strip_shows_counts_from_the_parsed_log():
    html = server.MOBILE_HTML
    assert "renderIssues" in html
    assert "counts" in html


def test_strip_clears_the_home_indicator():
    """A fixed bottom bar must not sit under the iPhone home indicator."""
    strip = server.MOBILE_HTML[server.MOBILE_HTML.index(".strip {"):]
    assert "var(--bot)" in strip[:400]


def test_sheet_lists_problems_but_not_the_raw_log():
    html = server.MOBILE_HTML
    assert "problems" in html
    # The raw main.log viewer and its filter chips stay desktop-only.
    assert "rawlog" not in html
    assert "Hide noise" not in html


def test_log_endpoint_feeds_the_shell(client, tmp_path):
    from conftest import FAKE_PDF, make_project
    log = (
        "This is pdfTeX\n"
        "! Undefined control sequence.\n"
        "l.7 \\notacommand\n"
        "Output written on main.pdf (2 pages, 100 bytes).\n"
    )
    make_project(tmp_path, "doc", pdf=FAKE_PDF, log=log)
    body = client.get("/log/doc").json()
    assert body["counts"]["error"] >= 1
    assert body["problems"][0]["kind"] == "error"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_mobile_errors.py -v`
Expected: FAIL — `renderIssues` is not in the shell, and `.strip {` raises `ValueError` from `index()`

- [ ] **Step 3: Add the styles**

In `MOBILE_HTML`'s `<style>`:

```css
  .strip {
    position: fixed; left: 0; right: 0; bottom: 0; z-index: 20;
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
```

Task 6 already gave `#main` its `calc(var(--bot) + 64px)` bottom padding, so the
strip does not cover the last page. Do not add a second `#main` rule.

- [ ] **Step 4: Add the markup**

Before `</body>`, after the drawer:

```html
<div class="sheet" id="sheet"></div>
<div class="strip" id="strip" hidden></div>
```

- [ ] **Step 5: Add the script**

Append to the script, and add a `renderIssues();` call at the end of `loadLog()`:

```js
/* ---------- problems ---------- */

const KIND_ORDER = { error: 0, warning: 1, badbox: 2 };
const KIND_LABEL = { error: 'error', warning: 'warning', badbox: 'bad box' };

function setSheet(open) {
  $('sheet').classList.toggle('open', open);
  $('strip').classList.toggle('open', open);
}
$('strip').addEventListener('click', () => setSheet(!$('sheet').classList.contains('open')));

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
  if (broke && !strip.dataset.broke) setSheet(true);
  strip.dataset.broke = broke ? '1' : '';
}
```

- [ ] **Step 6: Run tests to verify they pass**

Run: `python3 -m pytest tests/ -v`
Expected: 41 passed

- [ ] **Step 7: Commit**

```bash
git add server.py tests/test_mobile_errors.py
git commit -m "feat: show compile problems in the mobile shell

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 8: Container dependency, docs, and device verification

Ships it. Without `poppler-utils` in the image, `/page/` returns 502 for every request.

**Files:**
- Modify: `Dockerfile:3-9`
- Modify: `README.md`
- Modify: `DEPLOY.md`
- Create: `tests/test_docker_deps.py`

**Interfaces:**
- Consumes: everything above
- Produces: a working deployment

- [ ] **Step 1: Write the failing test**

Create `tests/test_docker_deps.py`:

```python
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def test_dockerfile_installs_poppler():
    """/page/ shells out to pdftoppm and pdfinfo; both ship in poppler-utils."""
    assert "poppler-utils" in (ROOT / "Dockerfile").read_text()


def test_readme_documents_the_mobile_route():
    readme = (ROOT / "README.md").read_text()
    assert "/m" in readme
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_docker_deps.py -v`
Expected: 2 FAIL

- [ ] **Step 3: Add poppler-utils**

In `Dockerfile`, add to the `apt-get install` list, after `latexmk`:

```
    poppler-utils \
```

- [ ] **Step 4: Document it**

In `README.md`, add a section after "Editing Workflow":

```markdown
## On a phone

`http://<host>:8585/m` is a touch-friendly version of the viewer: project
switcher, the PDF as scrollable page images, Compile, and a summary of compile
problems. On iOS, open it in Safari and use **Share → Add to Home Screen** to
install it as an app; it then opens fullscreen with its own icon.

Pages are rasterized by `pdftoppm` on request and cached under `.build/pages/`,
so the desktop viewer is unaffected and nothing is rendered until a phone asks
for it. The mobile shell needs the server reachable to work — there is no
offline mode.

The desktop viewer at `/` is unchanged.
```

In `DEPLOY.md`, add to the configuration notes:

```markdown
### Mobile shell

The mobile shell at `/m` needs `poppler-utils` in the image (it is in the
Dockerfile). After pulling a version that adds it, rebuild rather than just
restarting, or `/page/` will return 502:

    docker compose build && docker compose up -d

Rendering resolution is `PAGE_DPI` in `server.py`, 150 by default. Raising it
gives crisper pinch-zoom at the cost of larger images and slower first paint.

Adding to an iPhone home screen works over plain HTTP. There is no service
worker and no offline support; that would require serving over HTTPS.
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `python3 -m pytest tests/ -v`
Expected: 43 passed

- [ ] **Step 6: Rebuild and deploy**

```bash
cd /home/ardi/Projects/latex-workspace
docker compose build
docker compose up -d
docker compose ps
curl -sS -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8585/m
curl -sS http://127.0.0.1:8585/mtime/cv-main
curl -sS -o /tmp/p1.png -w '%{http_code} %{size_download}\n' http://127.0.0.1:8585/page/cv-main/1.png
file /tmp/p1.png
```
Expected: `200` for `/m`, a non-zero `pages` in the JSON, `200` with tens of kilobytes for the page, and `file` reporting PNG image data.

- [ ] **Step 7: Verify on the iPhone**

This is the acceptance gate — none of the tests above prove the thing works on the device it was built for. On the phone, with Tailscale connected, open `http://ardi.tail351339.ts.net:8585/m` and confirm each:

- [ ] Pages render and scroll smoothly through a multi-page document
- [ ] Pinch-zoom works and text is sharp at 2-3x
- [ ] Share → Add to Home Screen installs it with the right icon and name
- [ ] Launching from the home screen is fullscreen, with no Safari chrome
- [ ] Nothing is clipped by the notch or the home indicator
- [ ] The drawer opens, switches projects, and closes on the scrim
- [ ] Editing `main.tex` on the host updates the phone within a few seconds
- [ ] Pull-to-refresh triggers a recompile and does not fight the scroll
- [ ] Introducing a deliberate `\notacommand` pops the error sheet with a `main.tex:N` line
- [ ] Backgrounding the app stops the polling (`docker compose logs` goes quiet)
- [ ] Turning Tailscale off shows the offline dot rather than a broken page
- [ ] Opening `/` on the desktop is exactly as it was

- [ ] **Step 8: Commit**

```bash
git add Dockerfile README.md DEPLOY.md tests/test_docker_deps.py
git commit -m "feat: install poppler and document the mobile shell

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

## Notes for the implementer

- **`MOBILE_HTML` is an `r"""` string inside `server.py`.** Any literal backslash in the CSS or JS is fine, but a `"""` sequence would end it. Use HTML entities for glyphs (`&#9776;`) rather than pasting emoji, matching how `INDEX_HTML` does it.
- **Never edit `INDEX_HTML`.** `tests/test_desktop_unchanged.py` fails if mobile markup leaks into it, and that is the point.
- **Tests stub poppler.** Only the Task 3 Step 7 smoke test and the Task 8 deploy check use the real binary. A test that shells out to `pdftoppm` is a test that fails on a machine without poppler.
- **Page numbers are validated before any subprocess call.** Keep it that way — `_page_count()` bounds `n` so that a crafted URL cannot reach poppler with arbitrary arguments.
