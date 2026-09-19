import re

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


def _z(html, selector):
    rule = html[html.index(selector + " {"):]
    return int(re.search(r"z-index:\s*(\d+)", rule[:rule.index("}")]).group(1))


def test_the_open_sheet_never_covers_its_toggle():
    html = server.MOBILE_HTML
    assert _z(html, ".strip") > _z(html, ".sheet")


def test_auto_open_is_tracked_per_project():
    html = server.MOBILE_HTML
    assert "dataset.broke" not in html
    assert "brokeSeen[current]" in html


def test_switching_projects_clears_the_previous_problems():
    html = server.MOBILE_HTML
    body = html[html.index("function selectProject("):]
    body = body[:body.index("\n}\n")]
    assert body.index("renderIssues()") > body.index("logData = null")


def test_strip_is_exposed_as_a_button():
    html = server.MOBILE_HTML
    assert 'role="button"' in html and 'aria-controls="sheet"' in html
    assert "$('strip').setAttribute('aria-expanded', open)" in html
