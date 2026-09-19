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
