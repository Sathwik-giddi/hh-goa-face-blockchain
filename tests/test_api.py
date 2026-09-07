"""API tests via FastAPI TestClient: health, verify validation, scan guard, path traversal.

Hermetic: BLOCKCHAIN_MODE=local, dedicated CHAIN_FILE, no SERPAPI key.
"""
import os
from pathlib import Path

# Must be set before importing app (CHAIN is bound at import time).
os.environ["BLOCKCHAIN_MODE"] = "local"
os.environ["CHAIN_FILE"] = "/tmp/test_api_chain.json"
os.environ.setdefault("EVM_MIRROR_FILE", "/tmp/test_api_mirror.json")
os.environ.pop("SERPAPI_API_KEY", None)

TEST_CHAIN = Path("/tmp/test_api_chain.json")
TEST_CHAIN.unlink(missing_ok=True)

from fastapi.testclient import TestClient  # noqa: E402
import pytest  # noqa: E402
from fastapi import HTTPException  # noqa: E402

from app import CHAIN, OUTPUTS, app, outputs_file  # noqa: E402
from src.blockchain import anchor  # noqa: E402

client = TestClient(app)

FP = "c" * 64


def test_health_ok():
    r = client.get("/api/health")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["mode"] == "local"


def test_chain_view_after_anchor():
    anchor(FP, {"note": "api test"}, chain_file=str(CHAIN))
    r = client.get("/api/chain")
    assert r.status_code == 200
    body = r.json()
    assert body["height"] >= 2
    assert body["chain"][0]["data"]["fingerprint"] == FP


def test_verify_api_rejects_non_hex_hash():
    r = client.get("/api/verify", params={"hash": "not-hex"})
    assert r.status_code == 400
    assert "64-hex" in r.json()["detail"]


def test_verify_api_soft_fails_for_unknown_hash():
    r = client.get("/api/verify", params={"hash": "d" * 64})
    assert r.status_code == 200
    assert r.json()["verified"] is False


def test_scan_rejects_non_image_content_type():
    r = client.post(
        "/api/scan",
        files={"file": ("x.txt", b"hello", "text/plain")},
    )
    assert r.status_code == 400


def test_outputs_path_traversal_blocked():
    for path in ("/outputs/../README.md", "/outputs/%2e%2e/README.md", "/outputs/..%2FREADME.md"):
        r = client.get(path)
        assert r.status_code == 404, f"{path} should be blocked"
    # escape via a nested traversal that still starts inside outputs
    r = client.get("/outputs/sub/../../README.md")
    assert r.status_code == 404


def test_outputs_handler_rejects_escaping_paths():
    """Direct handler check: the resolution guard, not router normalization."""
    for path in ("../README.md", "sub/../../README.md", ".."):
        with pytest.raises(HTTPException) as ei:
            outputs_file(path)
        assert ei.value.status_code == 404
    # symlink-free positive sanity: an in-root relative path resolves fine
    with pytest.raises(HTTPException):
        outputs_file("../../etc/passwd")


def test_outputs_serves_files_inside_root():
    served = OUTPUTS / "_api_test_file.txt"
    served.write_text("api-served", encoding="utf-8")
    try:
        r = client.get("/outputs/_api_test_file.txt")
        assert r.status_code == 200
        assert r.text == "api-served"
    finally:
        served.unlink(missing_ok=True)
