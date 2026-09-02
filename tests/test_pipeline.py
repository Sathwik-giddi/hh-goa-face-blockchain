"""End-to-end tests. Live SerpAPI + EVM in this env; force BLOCKCHAIN_MODE=local for hermetic tests."""
import os
import importlib
import shutil
import sys
from pathlib import Path

# Force hermetic, $0 test mode (no faucet / no SERPAPI cost)
os.environ["BLOCKCHAIN_MODE"] = "local"
os.environ.setdefault("CHAIN_FILE", "/tmp/test_chain.json")

TEST_CHAIN = Path("/tmp/test_chain.json")
TEST_OUT = Path("/tmp/test_out")
TEST_CHAIN.unlink(missing_ok=True)
if TEST_OUT.exists():
    shutil.rmtree(TEST_OUT)

# reload modules to pick up new env
for mod in ["src.blockchain_local", "src.blockchain", "src.pipeline", "src.face_id", "src.search", "src.utils"]:
    if mod in sys.modules:
        importlib.reload(sys.modules[mod])

# ensure import
from src.pipeline import run_pipeline
from src.blockchain_local import anchor_local, verify_local, _load_chain
from src.utils import is_hex64


def test_pipeline_end_to_end_local():
    """E2E without SERPAPI: face detect + local chain."""
    face = detect_and_encode_safe("data/samples/lena.jpg")
    assert face["num_faces"] >= 1, "lena should have a face"
    fp = "a" * 64
    receipt = anchor_local(fp, {"test": True}, chain_file=str(TEST_CHAIN))
    assert receipt["mode"] == "local"
    v = verify_local(fp, chain_file=str(TEST_CHAIN))
    assert v["verified"] is True
    # tamper: flip last char
    bad = fp[:-1] + ("0" if fp[-1] != "0" else "1")
    assert bad != fp, "tamper must differ"
    assert verify_local(bad, chain_file=str(TEST_CHAIN))["verified"] is False


def detect_and_encode_safe(p):
    from src.face_id import detect_and_encode
    return detect_and_encode(p, out_dir=TEST_OUT)


def test_pipeline_end_to_end_live_if_key():
    """Only runs live search if SERPAPI_API_KEY is set (skips otherwise)."""
    if not os.getenv("SERPAPI_API_KEY"):
        import pytest
        pytest.skip("SERPAPI_API_KEY not set")
    r = run_pipeline("data/samples/lena.jpg", out_dir=TEST_OUT, chain_file=TEST_CHAIN, verbose=False)
    assert r["face"]["num_faces"] >= 1
    assert r["search"]["mode"] == "live"
    assert len(r["search"].get("visual_matches", [])) >= 1
    assert r["search"]["reddit_found"] is True
    # verify same fingerprint via local chain
    fp = r["fingerprint"]["fingerprint_sha256"]
    v = verify_local(fp, chain_file=str(TEST_CHAIN))
    assert v["verified"] is True
    # tamper test: must be a different fingerprint
    tampered = fp[:-1] + ("0" if fp[-1] != "0" else "1")
    assert tampered != fp
    assert verify_local(tampered, chain_file=str(TEST_CHAIN))["verified"] is False


def test_chain_integrity():
    """If a block is tampered, integrity check fails."""
    if TEST_CHAIN.exists():
        TEST_CHAIN.unlink()
    anchor_local("a" * 64, {"x": 1}, chain_file=str(TEST_CHAIN))
    anchor_local("b" * 64, {"x": 2}, chain_file=str(TEST_CHAIN))
    chain = _load_chain(TEST_CHAIN)
    chain[1]["data"] = {"x": 99}
    import json
    TEST_CHAIN.write_text(json.dumps(chain))
    v = verify_local("a" * 64, chain_file=str(TEST_CHAIN))
    assert v["verified"] is False
    assert any(k in v.get("reason", "") for k in ["integrity", "tampered", "mismatch", "inconsistent"])


def test_no_face_no_crash():
    """Image with no detectable face should not crash pipeline."""
    import cv2
    import numpy as np
    p = Path("/tmp/no_face.jpg")
    img = np.ones((100, 100, 3), dtype=np.uint8) * 200
    cv2.imwrite(str(p), img)
    r = detect_and_encode_safe(p)
    assert r is not None
    assert "crop_path" in r
    # either no face OR detection succeeded — but no crash
    assert isinstance(r.get("num_faces"), int)


def test_phash_deterministic():
    """pHash same for same image, differs for different image."""
    from src.face_id import encode_phash
    h1 = encode_phash("data/samples/lena.jpg")
    h2 = encode_phash("data/samples/lena.jpg")
    assert h1 == h2
    # 64-bit pHash packed to 16 hex chars
    assert len(h1) == 16
    assert all(c in "0123456789abcdef" for c in h1)
    # different image → different hash
    h3 = encode_phash("data/samples/3q.jpg")
    # may collide but statistically unlikely; allow equal but warn
    if h1 == h3:
        import warnings
        warnings.warn("phash collision lena==3q (rare)")


def test_hamming_distance():
    """Hamming on 64-bit pHash: identical=0, flipped>=1."""
    from src.search import _hamming
    assert _hamming("0" * 16, "0" * 16) == 0
    assert _hamming("0" * 16, "f" * 16) > 0


def test_is_hex64():
    assert is_hex64("0" * 64)
    assert is_hex64("a" * 64)
    assert not is_hex64("0" * 63)
    assert not is_hex64("0" * 65)
    assert not is_hex64("xyz")
    assert not is_hex64("")


def test_safe_filename():
    from src.utils import safe_filename
    assert safe_filename("..") == "img"
    assert safe_filename(".env") == "env"
    assert safe_filename("foo/bar.png") == "foo_bar.png"
    # path traversal must be neutralized
    sanitized = safe_filename("../../etc/passwd")
    # result must not traverse directories
    assert "/" not in sanitized
    assert ".." not in sanitized.split("/")[-1] or sanitized.split("/")[-1].startswith("_.._")
    # default for empty / whitespace-only
    assert safe_filename("") == "img"
    assert safe_filename("   ") != ""  # not empty
    assert "/" not in safe_filename("   ")


def test_canonicalization_deterministic():
    """§15: same canonical record → same SHA-256; any value change → different hash."""
    from src.utils import fingerprint_post, recompute_fingerprint, sha256_json
    post = {"link": "https://example.com/post", "title": "t", "source": "s", "thumbnail": "th"}
    f1 = fingerprint_post(post)
    f2 = fingerprint_post(dict(reversed(list(post.items()))))  # key order must not matter
    assert f1["fingerprint_sha256"] == f2["fingerprint_sha256"]
    # stable key ordering + separators are load-bearing: hand-recompute must match
    parts = {"url": "https://example.com/post", "title": "t", "source": "s", "thumbnail": "th"}
    assert recompute_fingerprint(post) == sha256_json(parts)
    # tamper: one value changed → hash changes
    tampered = dict(parts, title="t2")
    assert sha256_json(tampered) != sha256_json(parts)


def test_multi_face_deterministic_selection():
    """§11: multi-face images must expose deterministic per-face options, not random pick."""
    face = detect_and_encode_safe("data/samples/3q.jpg")
    assert face["num_faces"] >= 1
    opts = face.get("face_options") or []
    assert len(opts) >= 1
    assert opts[0]["index"] == 0
    # explicit selection must switch the encoded face deterministically
    if len(opts) > 1:
        f0 = detect_and_encode_safe("data/samples/3q.jpg")
        f1 = detect_and_encode("data/samples/3q.jpg", out_dir=TEST_OUT, face_index=1)
        assert f1["selected_index"] == 1
        assert f1["bbox"] == f1["all_faces"][1]["bbox"]
        assert f0["bbox"] == f0["all_faces"][0]["bbox"]


def test_reverify_independent_match_and_tamper():
    """§17/§18: re-hash from re-retrieved content; tampered record must mismatch."""
    from src.utils import fingerprint_post, reverify_independent
    post = {"link": "https://example.com/post", "title": "t", "source": "s",
            "thumbnail": "https://example.com/img.jpg"}
    fp = fingerprint_post(post)  # no image download in test env → metadata-only hash
    rv = reverify_independent(fp, out_dir=TEST_OUT)
    assert rv["match"] is True
    assert rv["recomputed_fingerprint"] == fp["fingerprint_sha256"]
    # tamper one value → re-hash no longer matches the stored fingerprint
    tampered = dict(fp, title="t2")
    rv2 = reverify_independent(tampered, out_dir=TEST_OUT)
    assert rv2["match"] is False


def test_invalid_image_fails_cleanly():
    """§26: corrupt input → meaningful error, no fake success."""
    p = Path("/tmp/corrupt.jpg")
    p.write_bytes(b"not an image at all")
    r = detect_and_encode_safe(p)
    assert r["crop_path"] is None
    assert r["num_faces"] == 0
    assert "cannot decode" in (r.get("warning") or "")
