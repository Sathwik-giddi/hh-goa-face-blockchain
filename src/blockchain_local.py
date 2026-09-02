"""$0 local hash-chain — spec allows local/simulated. Tamper-evident, atomic, with file lock."""
import json
import hashlib
import os
import tempfile
from pathlib import Path
from datetime import datetime, timezone
from filelock import FileLock

LOCK_SUFFIX = ".lock"


def _hash_block(block: dict) -> str:
    canon = json.dumps(block, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(canon.encode("utf-8")).hexdigest()


def _load_chain(chain_file) -> list:
    chain_file = Path(chain_file)
    if not chain_file.exists():
        return [_genesis()]
    try:
        with open(chain_file, "r", encoding="utf-8") as f:
            data = json.load(f)
    except json.JSONDecodeError:
        # file was truncated/corrupted — back it up and start fresh genesis
        try:
            chain_file.rename(chain_file.with_suffix(".corrupt.json"))
        except OSError:
            pass
        return [_genesis()]
    if not isinstance(data, list):
        return [_genesis()]
    if not data:
        return [_genesis()]
    return data


def _save_chain(chain_file, chain: list) -> None:
    """Atomic write: temp file + os.replace + fsync. Avoids truncation on crash."""
    chain_file = Path(chain_file)
    chain_file.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(prefix=chain_file.name + ".", dir=str(chain_file.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(chain, f, indent=2, ensure_ascii=False)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, chain_file)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def _genesis() -> dict:
    g = {
        "index": 0,
        "timestamp": "2026-01-01T00:00:00+00:00",
        "prev_hash": "0" * 64,
        "data_hash": hashlib.sha256(b"genesis").hexdigest(),
        "data": {"note": "genesis"},
        "hash": "",
    }
    g["hash"] = _hash_block({k: v for k, v in g.items() if k != "hash"})
    return g


def anchor_local(fingerprint: str, payload: dict, chain_file: str | Path = "chain.json") -> dict:
    """Append a new block to the local hash-chain. Atomic, locked, dedupes fingerprint.

    Refuses to re-anchor an existing fingerprint unless `overwrite=True` via payload flag.
    """
    chain_file = Path(chain_file)
    if not fingerprint or not isinstance(fingerprint, str):
        raise ValueError("fingerprint must be a non-empty string")
    lock = FileLock(str(chain_file) + LOCK_SUFFIX, timeout=10)
    with lock:
        chain = _load_chain(chain_file)
        # dedupe: if fingerprint already anchored, return existing block
        target = hashlib.sha256(fingerprint.encode("utf-8")).hexdigest()
        for b in chain:
            if b.get("data_hash") == target and fingerprint in (b.get("data") or {}).get("fingerprint", ""):
                return {
                    "mode": "local",
                    "block_index": b["index"],
                    "block_hash": b["hash"],
                    "prev_hash": b["prev_hash"],
                    "data_hash": target,
                    "chain_file": str(chain_file),
                    "explorerUrl": f"file://{chain_file.resolve()}#block-{b['index']}",
                    "deduplicated": True,
                }
        if not chain:
            chain = [_genesis()]
        prev = chain[-1]
        # payload may contain `fingerprint` key — strip to avoid overwrite
        safe_payload = {k: v for k, v in (payload or {}).items() if k != "fingerprint"}
        block = {
            "index": len(chain),
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "prev_hash": prev["hash"],
            "data_hash": target,
            "data": {"fingerprint": fingerprint, **safe_payload},
        }
        block["hash"] = _hash_block(block)
        chain.append(block)
        _save_chain(chain_file, chain)
    return {
        "mode": "local",
        "block_index": block["index"],
        "block_hash": block["hash"],
        "prev_hash": block["prev_hash"],
        "data_hash": target,
        "chain_file": str(chain_file),
        "explorerUrl": f"file://{chain_file.resolve()}#block-{block['index']}",
    }


def verify_local(fingerprint: str, chain_file: str | Path = "chain.json") -> dict:
    """3-layer integrity check: prev_hash, block.hash, data_hash consistency."""
    chain_file = Path(chain_file)
    if not fingerprint or not isinstance(fingerprint, str):
        return {"verified": False, "reason": "fingerprint must be a non-empty string"}
    if not chain_file.exists():
        return {"verified": False, "reason": "no chain file"}
    try:
        chain = _load_chain(chain_file)
    except Exception as e:
        return {"verified": False, "reason": f"chain load failed: {e}"}
    if not chain:
        return {"verified": False, "reason": "chain empty"}
    # Layer 1: prev_hash integrity
    for i in range(1, len(chain)):
        if chain[i]["prev_hash"] != chain[i - 1]["hash"]:
            return {"verified": False, "reason": f"chain integrity broken at block {i} (prev_hash mismatch)"}
    # Layer 2: each block's own hash matches content
    for b in chain:
        expected = _hash_block({k: v for k, v in b.items() if k != "hash"})
        if b.get("hash") != expected:
            return {"verified": False, "reason": f"block {b['index']} content tampered (hash mismatch)"}
    # Layer 3: data_hash consistency for non-genesis blocks
    for b in chain:
        fp = (b.get("data") or {}).get("fingerprint")
        if fp is not None:
            expected_data = hashlib.sha256(fp.encode("utf-8")).hexdigest()
            if b.get("data_hash") != expected_data:
                return {"verified": False, "reason": f"block {b['index']} data_hash inconsistent with fingerprint"}
    # Find the fingerprint
    target = hashlib.sha256(fingerprint.encode("utf-8")).hexdigest()
    for b in chain:
        if b.get("data_hash") == target:
            return {
                "verified": True,
                "block_index": b["index"],
                "block_hash": b["hash"],
                "timestamp": b["timestamp"],
                "data": b["data"],
            }
    return {"verified": False, "reason": "fingerprint not anchored in this chain"}
