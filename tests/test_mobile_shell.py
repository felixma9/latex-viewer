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


def test_drawer_is_dismissable_and_announced():
    html = server.MOBILE_HTML
    assert 'aria-expanded' in html
    assert "e.key === 'Escape'" in html
    assert "'&#39;'" in html  # esc() covers single quotes


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


def test_pull_to_recompile_ignores_the_sheet_drawer_and_zoom():
    html = server.MOBILE_HTML
    ptr = html[html.index("pull to recompile"):]
    start = ptr[ptr.index("'touchstart'"):]
    start = start[:start.index("}, { passive: true });")]
    for guard in ("closest('#sheet, #drawer')", "classList.contains('open')", "visualViewport"):
        assert guard in start, guard


def test_small_state_fixes_are_in_place():
    html = server.MOBILE_HTML
    sel = html[html.index("function selectProject("):]
    sel = sel[:sel.index("\n}\n")]
    assert "showPages()" in sel                                   # M-7
    assert "logData.exists && !logData.ok" in html                # M-4
    assert "can't reach server" in html                           # M-6
    vis = html[html.index("'visibilitychange'"):]
    assert ".page.failed" in vis[:vis.index("});")]              # M-1


def test_stale_poll_failures_and_restarts_cannot_fork_the_timer():
    html = server.MOBILE_HTML
    catch = html[html.index("async function poll()"):]
    catch = catch[catch.index("} catch {"):]
    assert catch.split("\n")[1].strip() == "if (name !== current) return;"
    for fn in ("function selectProject(", "async function compileNow("):
        body = html[html.index(fn):]
        body = body[:body.index("\n}\n")]
        assert "clearTimeout(timer)" in body, fn
