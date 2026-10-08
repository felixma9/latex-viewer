import json


def test_app_json_names_the_app_for_the_dashboard(client):
    r = client.get("/api/app.json")
    assert r.status == 200
    info = json.loads(r.body)
    assert info["id"] == "latex-viewer" and info["calls"] == []
