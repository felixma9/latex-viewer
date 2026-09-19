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
