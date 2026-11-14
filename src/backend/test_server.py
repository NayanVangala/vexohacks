"""One check for the receiver: real-shaped uploads are saved and counted, junk is not.

Run: .venv/bin/python -m pytest -q
"""

import os
import struct
import tempfile

os.environ.setdefault("SCENE_DATA_DIR", tempfile.mkdtemp())

from fastapi.testclient import TestClient  # noqa: E402

import server  # noqa: E402

JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 100 + b"\xff\xd9"
WAV = b"RIFF" + struct.pack("<I", 36) + b"WAVE" + b"\x00" * 32


def test_capture_activate_stats():
    c = TestClient(server.app)

    assert c.post("/capture", files={"photo": ("1.jpg", JPEG, "image/jpeg")}).status_code == 200
    assert c.post("/capture", files={"photo": ("2.jpg", JPEG, "image/jpeg")},
                  data={"source": "activation"}).status_code == 200
    assert c.post("/capture", files={"audio": ("1.wav", WAV, "audio/wav")}).status_code == 200
    assert c.post("/activate").status_code == 200

    # Rejections: wrong magic bytes, both parts, neither part.
    assert c.post("/capture", files={"photo": ("x.jpg", b"nope", "image/jpeg")}).status_code == 415
    assert c.post("/capture", files={"photo": ("a.jpg", JPEG, "image/jpeg"),
                                     "audio": ("a.wav", WAV, "audio/wav")}).status_code == 422
    assert c.post("/capture").status_code == 422

    s = c.get("/stats").json()
    assert (s["frames"], s["activation_photos"], s["audio_chunks"], s["activations"], s["rejected"]) == (1, 1, 1, 1, 1)

    d = server.DATA_DIR
    assert len(os.listdir(f"{d}/frames")) == 1
    assert len(os.listdir(f"{d}/activation")) == 1
    assert len(os.listdir(f"{d}/audio")) == 1


def test_followthrough_proxy_reports_when_down(monkeypatch):
    # Nothing listens on this port: the glasses get a clear 502 with how to start it, not a hang.
    monkeypatch.setattr(server, "FOLLOWTHROUGH_URL", "http://127.0.0.1:9")
    r = TestClient(server.app).get("/ft/api/state")
    assert r.status_code == 502 and "src/followthrough" in r.json()["detail"]


def test_output_mode_setting():
    c = TestClient(server.app)
    import voice
    voice._settings_path().unlink(missing_ok=True)
    assert c.get("/settings").json()["output"] is None          # not chosen yet: the glasses ask
    assert voice.speaks()                                       # default until chosen: both
    assert c.post("/settings", data={"output": "cards"}).json() == {"output": "cards"}
    assert c.get("/settings").json()["output"] == "cards" and not voice.speaks()
    assert c.post("/settings", data={"output": "shout"}).status_code == 422
    c.post("/settings", data={"output": "both"})
