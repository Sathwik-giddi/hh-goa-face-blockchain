"""Utils tests: fingerprint determinism, hashing errors, image IO guard rails."""
import cv2
import numpy as np
import pytest

from src.utils import (
    download_image,
    ensure_image,
    fingerprint_post,
    sha256_file,
    sha256_json,
)


class _FakeResponse:
    def __init__(self, content_type, chunks, status=200):
        self.headers = {"content-type": content_type}
        self._chunks = chunks
        self.status_code = status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def iter_content(self, size):
        yield from self._chunks


def test_fingerprint_post_deterministic_except_timestamp():
    post = {"link": "http://x", "title": "hi", "source": "reddit", "thumbnail": "http://t"}
    a, b = fingerprint_post(post), fingerprint_post(dict(post))
    assert a["fingerprint_sha256"] == b["fingerprint_sha256"]
    assert a["fingerprint_sha256"] == sha256_json(
        {k: v for k, v in a.items() if k not in ("fingerprint_sha256", "created_at")}
    )
    # unknown post keys must not enter the fingerprint
    c = fingerprint_post({**post, "secret_internal": "x"})
    assert c["fingerprint_sha256"] == a["fingerprint_sha256"]
    assert "secret_internal" not in c


def test_fingerprint_post_missing_fields_default_to_empty():
    fp = fingerprint_post({})
    assert fp["url"] == "" and fp["title"] == "" and fp["source"] == ""
    assert fp["fingerprint_sha256"]


def test_fingerprint_post_image_sha_only_when_file_exists(tmp_path):
    img = tmp_path / "face.jpg"
    cv2.imwrite(str(img), np.zeros((40, 40, 3), dtype=np.uint8))
    fp = fingerprint_post({"link": "http://x"}, image_path=str(img))
    assert len(fp["image_sha256"]) == 64
    # missing image path -> key absent, fingerprint stable
    fp2 = fingerprint_post({"link": "http://x"}, image_path=str(tmp_path / "nope.jpg"))
    assert "image_sha256" not in fp2
    assert fp2["fingerprint_sha256"] != fp["fingerprint_sha256"]


def test_sha256_file_missing_raises():
    with pytest.raises(FileNotFoundError):
        sha256_file("/tmp/surely_missing_sha_file.bin")


def test_sha256_file_streams_and_matches(tmp_path):
    f = tmp_path / "data.bin"
    f.write_bytes(b"hello world")
    h1 = sha256_file(f)
    h2 = sha256_file(f)
    assert h1 == h2 and len(h1) == 64


def test_ensure_image_missing_raises():
    with pytest.raises(FileNotFoundError):
        ensure_image("/tmp/surely_missing_image.jpg")


def test_ensure_image_directory_raises(tmp_path):
    with pytest.raises(ValueError, match="directory"):
        ensure_image(tmp_path)


def test_ensure_image_corrupt_raises(tmp_path):
    p = tmp_path / "bad.jpg"
    p.write_bytes(b"garbage bytes")
    with pytest.raises(ValueError, match="Cannot decode"):
        ensure_image(p)


def test_download_image_rejects_non_image_content_type(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "requests.get",
        lambda *a, **k: _FakeResponse("text/html", [b"<html>"]),
    )
    assert download_image("http://x", tmp_path / "o" / "img.jpg") is None


def test_download_image_enforces_size_cap(tmp_path, monkeypatch):
    chunk = b"x" * 8192
    monkeypatch.setattr(
        "requests.get",
        lambda *a, **k: _FakeResponse("image/jpeg", [chunk] * 2000),  # ~16MB
    )
    assert download_image("http://x", tmp_path / "o" / "img.jpg") is None


def test_download_image_happy_path(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "requests.get",
        lambda *a, **k: _FakeResponse("image/jpeg", [b"\xff\xd8\xff", b"rest"]),
    )
    dest = download_image("http://x", tmp_path / "o" / "img.jpg")
    assert dest is not None and dest.exists()
    assert dest.read_bytes() == b"\xff\xd8\xffrest"
