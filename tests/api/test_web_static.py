"""前端构建产物与接口同源提供：页面路径回退到 index.html，接口路径不被页面吞掉。"""

from fastapi.testclient import TestClient

from server.main import create_app


def build_dist(root):
    (root / "assets").mkdir(parents=True)
    (root / "index.html").write_text("<!doctype html><title>Pebble</title>", encoding="utf-8")
    (root / "assets" / "app.js").write_text("console.log(1)", encoding="utf-8")
    return root


def test_serves_built_pages_and_falls_back_for_client_routes(settings, tmp_path):
    app = create_app(web_dist=build_dist(tmp_path / "dist"))

    with TestClient(app) as client:
        root = client.get("/")
        route = client.get("/tasks/abc")
        asset = client.get("/assets/app.js")
        api = client.get("/api/tasks")
        unknown_api = client.get("/api/not-a-route")

    assert "Pebble" in root.text
    assert "Pebble" in route.text
    assert asset.text == "console.log(1)"
    assert api.status_code == 200
    assert unknown_api.status_code == 404
    assert "Pebble" not in unknown_api.text


def test_without_a_build_only_the_api_is_served(settings, tmp_path):
    app = create_app(web_dist=tmp_path / "missing")

    with TestClient(app) as client:
        assert client.get("/tasks").status_code == 404
        assert client.get("/api/tasks").status_code == 200
