from fastapi.testclient import TestClient
from paperforge_texd.main import app

client = TestClient(app)


def test_rejects_traversing_file_path():
    response = client.post(
        "/compile",
        json={"files": {"../escape.tex": "bad"}, "entrypoint": "../escape.tex"},
    )
    assert response.status_code == 200
    assert response.json()["ok"] is False
    assert "unsafe path" in response.json()["log"]


def test_rejects_traversing_entrypoint_even_when_files_are_safe():
    response = client.post(
        "/compile",
        json={"files": {"main.tex": "ok"}, "entrypoint": "../../etc/passwd"},
    )
    assert response.status_code == 200
    assert response.json()["ok"] is False
    assert "unsafe path" in response.json()["log"]


def test_rejects_entrypoint_not_present_in_request():
    response = client.post(
        "/compile",
        json={"files": {"main.tex": "ok"}, "entrypoint": "other.tex"},
    )
    assert response.json() == {
        "ok": False,
        "log": "entrypoint not supplied: other.tex",
        "pdf_base64": None,
    }
