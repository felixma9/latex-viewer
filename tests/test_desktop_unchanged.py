import hashlib

import server

# INDEX_HTML as it stood on master, before this project began. The desktop
# viewer must not change, so this hash must not change either. A failure here
# means an edit leaked into the desktop page: revert the edit rather than
# repinning the hash.
INDEX_HTML_SHA256 = "7202c777b47b2ad40471545086d649d03e678898a96695503f39a641e720ee0d"


def test_root_serves_index_html_verbatim(client):
    r = client.get("/")
    assert r.status == 200
    assert r.body == server.INDEX_HTML.encode()
    assert r.headers["Content-Type"] == "text/html"


def test_index_html_is_byte_for_byte_unchanged():
    actual = hashlib.sha256(server.INDEX_HTML.encode()).hexdigest()
    assert actual == INDEX_HTML_SHA256, (
        "INDEX_HTML changed. The desktop viewer must stay byte-for-byte "
        "identical for this project; revert the edit rather than repinning."
    )


def test_index_html_has_no_mobile_bits():
    """The desktop page must not grow mobile markup. Guards the core constraint."""
    for marker in ("/page/", "manifest.webmanifest", "apple-mobile-web-app", "serviceWorker", "/m'", '/m"'):
        assert marker not in server.INDEX_HTML, f"{marker!r} leaked into the desktop page"


def test_desktop_still_uses_the_iframe_viewer():
    """Desktop PDF rendering is unchanged; only the mobile shell uses page images."""
    assert "createElement('iframe')" in server.INDEX_HTML
