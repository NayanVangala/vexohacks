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
