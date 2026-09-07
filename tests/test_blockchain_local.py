"""Focused tests for the local hash-chain: corruption recovery, dedupe, integrity layers."""
import json

import pytest

from src.blockchain_local import (
    _hash_block,
    _load_chain,
    anchor_local,
    verify_local,
)

FP = "a" * 64


def _write_chain(path, blocks):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(blocks), encoding="utf-8")


def test_corrupt_chain_file_recovers_to_genesis(tmp_path):
    """Truncated/corrupt chain file must not crash: backup + start fresh genesis."""
    ch = tmp_path / "chain.json"
    ch.write_text("{ not valid json", encoding="utf-8")
    receipt = anchor_local(FP, {"note": "after corruption"}, chain_file=str(ch))
    assert receipt["block_index"] == 1
    backup = tmp_path / "chain.corrupt.json"
    assert backup.exists(), "corrupt file should be backed up, not silently deleted"
    # recovered chain is valid end to end
    v = verify_local(FP, chain_file=str(ch))
    assert v["verified"] is True


def test_non_list_chain_recovers_to_genesis(tmp_path):
    """A JSON file that is not a list falls back to genesis."""
    ch = tmp_path / "chain.json"
    _write_chain(ch, {"not": "a list"})
    receipt = anchor_local(FP, {}, chain_file=str(ch))
    assert receipt["block_index"] == 1
    v = verify_local(FP, chain_file=str(ch))
    assert v["verified"] is True


def test_anchor_deduplicates_fingerprint(tmp_path):
    """Re-anchoring the same fingerprint returns the existing block, no new block."""
    ch = tmp_path / "chain.json"
    r1 = anchor_local(FP, {"note": 1}, chain_file=str(ch))
    r2 = anchor_local(FP, {"note": 2}, chain_file=str(ch))
    assert r2["deduplicated"] is True
    assert r2["block_index"] == r1["block_index"]
    assert r2["block_hash"] == r1["block_hash"]
    chain = _load_chain(ch)
    assert len(chain) == 2, "genesis + one block, not a duplicate block"
    # original payload preserved, second payload ignored
    assert chain[1]["data"]["note"] == 1


def test_payload_fingerprint_key_is_stripped(tmp_path):
    """A payload smuggling its own `fingerprint` key must not override the anchor."""
    ch = tmp_path / "chain.json"
    anchor_local(FP, {"fingerprint": "e" * 64, "note": "x"}, chain_file=str(ch))
    chain = _load_chain(ch)
    assert chain[1]["data"]["fingerprint"] == FP
    assert "e" * 64 not in json.dumps(chain)
    assert verify_local(FP, chain_file=str(ch))["verified"] is True


def test_layer3_data_hash_inconsistency_detected(tmp_path):
    """Tamper data_hash (with block hash recomputed) must fail layer-3 verification."""
    ch = tmp_path / "chain.json"
    anchor_local(FP, {}, chain_file=str(ch))
    chain = _load_chain(ch)
    # change data_hash of the last block only, then re-hash it so layers 1-2 pass
    chain[1]["data_hash"] = "f" * 64
    chain[1]["hash"] = _hash_block({k: v for k, v in chain[1].items() if k != "hash"})
    _write_chain(ch, chain)
    v = verify_local(FP, chain_file=str(ch))
    assert v["verified"] is False
    assert "data_hash" in v.get("reason", "")


def test_verify_local_soft_failures(tmp_path):
    """Bad fingerprints and missing chains fail soft (dict), never raise."""
    assert verify_local("", chain_file=str(tmp_path / "x.json")) == {
        "verified": False,
        "reason": "fingerprint must be a non-empty string",
    }
    assert verify_local(None, chain_file=str(tmp_path / "x.json"))["verified"] is False
    assert verify_local("z" * 64, chain_file=str(tmp_path / "missing.json"))["verified"] is False
    assert verify_local("z" * 64, chain_file=str(tmp_path / "missing.json"))["reason"] == "no chain file"


def test_anchor_local_rejects_bad_fingerprint(tmp_path):
    ch = tmp_path / "chain.json"
    with pytest.raises(ValueError):
        anchor_local("", {}, chain_file=str(ch))
    with pytest.raises(ValueError):
        anchor_local(1234, {}, chain_file=str(ch))
    with pytest.raises(ValueError):
        anchor_local(None, {}, chain_file=str(ch))


def test_chain_blocks_are_linked_and_hash_consistent(tmp_path):
    ch = tmp_path / "chain.json"
    r0 = anchor_local(FP, {}, chain_file=str(ch))
    r1 = anchor_local("b" * 64, {}, chain_file=str(ch))
    chain = _load_chain(ch)
    assert r0["block_index"] == 1 and r1["block_index"] == 2
    for i in range(1, len(chain)):
        assert chain[i]["prev_hash"] == chain[i - 1]["hash"]
    for b in chain:
        expected = _hash_block({k: v for k, v in b.items() if k != "hash"})
        assert b["hash"] == expected
