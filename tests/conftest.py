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
