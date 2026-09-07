"""Dispatcher contract (src/blockchain) + EVM validation and mirror-logic tests.

All tests are hermetic: dispatcher routes to the local chain or to monkeypatched
EVM functions; EVM tests stop before any RPC call.
"""
import json

import pytest

import src.blockchain as dispatcher
from src.blockchain import anchor, verify
from src.blockchain_evm import (
    _normalize_hex,
    _read_mirror,
    _record_tx,
    anchor_evm,
    verify_evm,
)
from src.blockchain_local import anchor_local

FP = "a" * 64


def test_anchor_invalid_fingerprint_raises():
    with pytest.raises(ValueError, match="64-char hex"):
        anchor("abc", {})


def test_verify_invalid_fingerprint_is_soft_failure():
    # Production contract: anchor raises, verify returns a False reason dict.
    r = verify("abc")
    assert r["verified"] is False
    assert "64-char hex" in r["reason"]


def test_dispatcher_local_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setenv("BLOCKCHAIN_MODE", "local")
    monkeypatch.delenv("SERPAPI_API_KEY", raising=False)
    ch = tmp_path / "chain.json"
    receipt = anchor(FP, {"k": "v"}, chain_file=str(ch))
    assert receipt["mode"] == "local"
    assert verify(FP, chain_file=str(ch))["verified"] is True


def test_dispatcher_evm_mode_routes_to_anchor_evm(tmp_path, monkeypatch):
    monkeypatch.setenv("BLOCKCHAIN_MODE", "evm")
    fake = {"mode": "evm", "txHash": "0xabc", "verified": True}
    monkeypatch.setattr("src.blockchain_evm.anchor_evm", lambda *a, **k: fake)
    assert anchor(FP, {}, chain_file=str(tmp_path / "c.json")) == fake


def test_dispatcher_evm_mode_raises_on_anchor_error(monkeypatch):
    monkeypatch.setenv("BLOCKCHAIN_MODE", "evm")
    monkeypatch.setattr(
        "src.blockchain_evm.anchor_evm",
        lambda *a, **k: {"mode": "evm", "error": "RPC not reachable: x"},
    )
    with pytest.raises(RuntimeError, match="EVM anchor failed"):
        anchor(FP, {})


def test_dispatcher_evm_mode_routes_to_verify_evm(monkeypatch):
    monkeypatch.setenv("BLOCKCHAIN_MODE", "evm")
    fake = {"verified": True, "txs": []}
    monkeypatch.setattr("src.blockchain_evm.verify_evm", lambda *a, **k: fake)
    assert verify(FP) == fake


# --- EVM pure helpers ---

def test_normalize_hex():
    assert _normalize_hex("0xAbC") == "0xabc"
    assert _normalize_hex("AbC") == "0xabc"
    assert _normalize_hex("") == "0x"
    assert _normalize_hex(None) == "0x"


def test_mirror_record_and_read(tmp_path, monkeypatch):
    monkeypatch.setenv("EVM_MIRROR_FILE", str(tmp_path / "mirror.json"))
    _record_tx("0xh1", FP, {"a": 1})
    entries = _read_mirror(FP)
    assert entries == [("0xh1", {"fingerprint": FP, "payload": {"a": 1}})]
    # duplicate record does not duplicate history
    _record_tx("0xh1", FP, {"a": 2})
    assert len(_read_mirror(FP)) == 1
    # two different tx hashes for the same fingerprint both retained
    _record_tx("0xh2", FP, {"a": 3})
    assert [h for h, _ in _read_mirror(FP)] == ["0xh1", "0xh2"]
    assert _read_mirror("f" * 64) is None


def test_mirror_read_migrates_legacy_string_entry(tmp_path, monkeypatch):
    mirror = tmp_path / "mirror.json"
    monkeypatch.setenv("EVM_MIRROR_FILE", str(mirror))
    mirror.write_text(json.dumps({"_by_fp": {FP: "0xlegacy"}, "0xlegacy": {"fingerprint": FP}}))
    assert _read_mirror(FP) == [("0xlegacy", {"fingerprint": FP})]


def test_anchor_evm_validates_before_network(tmp_path, monkeypatch):
    """Pre-flight validation errors must be returned without touching RPC."""
    monkeypatch.setenv("EVM_MIRROR_FILE", str(tmp_path / "m.json"))
    monkeypatch.delenv("EVM_PRIVATE_KEY", raising=False)
    r = anchor_evm(FP, {})
    assert r["mode"] == "evm" and r.get("error") and "EVM_PRIVATE_KEY" in r["error"]
    assert r.get("verified") is False

    monkeypatch.setenv("EVM_PRIVATE_KEY", "0x" + "11" * 32)
    monkeypatch.setenv("EVM_CHAIN_ID", "not-an-int")
    r = anchor_evm(FP, {})
    assert "not int" in r["error"]

    monkeypatch.setenv("EVM_CHAIN_ID", "80002")
    r = anchor_evm("short", {})
    assert "64-char hex" in r["error"]


def test_verify_evm_no_mirror_no_chain(tmp_path, monkeypatch):
    monkeypatch.setenv("EVM_MIRROR_FILE", str(tmp_path / "missing_mirror.json"))
    r = verify_evm(FP, chain_file=None)
    assert r["verified"] is False
    assert r["reason"] == "fingerprint not anchored on this machine"


def test_verify_evm_local_only_returns_note(tmp_path, monkeypatch):
    """Local block anchored but no EVM mirror -> explicit 'no EVM mirror' note."""
    ch = tmp_path / "chain.json"
    mirror = tmp_path / "empty_mirror.json"
    monkeypatch.setenv("EVM_MIRROR_FILE", str(mirror))
    anchor_local(FP, {"note": "local only"}, chain_file=str(ch))
    r = verify_evm(FP, chain_file=str(ch))
    assert r["verified"] is None
    assert "no EVM mirror" in r["note"]
    assert r["local_block"]["block_index"] == 1


def test_anchor_evm_missing_web3_reports_error(tmp_path, monkeypatch):
    """If web3 is unavailable the function reports it instead of raising."""
    import builtins
    real_import = builtins.__import__

    def fake_import(name, *a, **k):
        if name == "web3":
            raise ImportError("no web3")
        return real_import(name, *a, **k)

    monkeypatch.delenv("EVM_PRIVATE_KEY", raising=False)
    monkeypatch.setattr(builtins, "__import__", fake_import)
    r = anchor_evm(FP, {})
    assert "web3/eth-account not installed" in r["error"]
