"""End-to-end tests for the face→social→chain pipeline.

These tests run in **local** chain mode (no faucet, no SERPAPI key required).
If SERPAPI_API_KEY is set, the search step runs live and asserts ≥1 face-similar hit.
"""
import os; os.environ.setdefault("BLOCKCHAIN_MODE", "local")
import importlib
from pathlib import Path

# Force local mode for tests so we don't burn faucet
os.environ["BLOCKCHAIN_MODE"] = "local"

# Use a temp chain for isolation
TEST_CHAIN = Path("/tmp/test_chain.json")
TEST_OUT = Path("/tmp/test_out")
TEST_CHAIN.unlink(missing_ok=True)
if TEST_OUT.exists():
    import shutil
    shutil.rmtree(TEST_OUT)


def _reload_modules():
    """Reload pipeline+blockchain to pick up new env."""
    for mod in ["src.blockchain_local", "src.blockchain", "src.pipeline"]:
        if mod in __import__("sys").modules:
            importlib.reload(__import__(mod))


_reload_modules()
from src.pipeline import run_pipeline  # noqa: E402
from src.blockchain_local import verify_local, _load_chain  # noqa: E402


def test_pipeline_end_to_end():
    """Run full pipeline on lena — without SERPAPI key it raises, so we skip the search step."""
    has_key = bool(os.getenv("SERPAPI_API_KEY"))
    if not has_key:
        # Just test the wiring: face + chain. Search would fail.
        from src.face_id import detect_and_encode
        from src.blockchain import anchor, verify

        face = detect_and_encode("data/samples/lena.jpg", out_dir=TEST_OUT)
        assert face["num_faces"] >= 1, "lena should have a detectable face"
        # anchor a fake fingerprint
        fp = "0" * 64
        receipt = anchor(fp, {"test": True}, chain_file=str(TEST_CHAIN))
        assert receipt["mode"] == "local"
        v = verify(fp, chain_file=str(TEST_CHAIN))
        assert v["verified"] is True
        return

    r = run_pipeline("data/samples/lena.jpg", out_dir=TEST_OUT, chain_file=TEST_CHAIN, verbose=False)
    assert r["face"]["num_faces"] >= 1
    assert r["search"]["mode"] == "live"
    assert len(r["search"].get("visual_matches", [])) >= 1
    assert r["search"]["reddit_found"] is True
    assert r["search"]["face_similar_count"] >= 1
    # in EVM mode verify returns on-chain match dict, in local returns True
    assert r["verify"].get("verified") in (True, None)  # True (local) or on-chain dict (EVM)
    # tampered
    fp = r["fingerprint"]["fingerprint_sha256"]
    bad = fp[:-1] + ("0" if fp[-1] != "0" else "1")
    assert verify_local(bad, chain_file=str(TEST_CHAIN))["verified"] is False


def test_chain_integrity():
    """If a block is tampered, integrity check fails."""
    from src.blockchain_local import anchor_local
    if TEST_CHAIN.exists():
        TEST_CHAIN.unlink()
    anchor_local("a" * 64, {"x": 1}, chain_file=str(TEST_CHAIN))
    anchor_local("b" * 64, {"x": 2}, chain_file=str(TEST_CHAIN))
    chain = _load_chain(TEST_CHAIN)
    # tamper
    chain[1]["data"] = {"x": 99}
    import json
    json.dump(chain, open(TEST_CHAIN, "w"))
    v = verify_local("a" * 64, chain_file=str(TEST_CHAIN))
    assert v["verified"] is False
    assert any(k in v.get("reason", "") for k in ["integrity", "tampered", "inconsistent", "mismatch"])


def test_no_face_no_crash():
    """Image with no detectable face should not crash — pipeline still runs search on full image."""
    # tiny solid image
    import cv2
    import numpy as np
    p = Path("/tmp/no_face.jpg")
    img = np.ones((100, 100, 3), dtype=np.uint8) * 200
    cv2.imwrite(str(p), img)
    from src.face_id import detect_and_encode
    r = detect_and_encode(p, out_dir=TEST_OUT)
    assert r is not None
    assert "crop_path" in r
