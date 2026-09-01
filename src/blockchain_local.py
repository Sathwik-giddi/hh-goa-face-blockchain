"""$0 local hash-chain — spec allows local/simulated. Tamper-evident, instant, no faucet."""
import json
import hashlib
from pathlib import Path
from datetime import datetime, timezone


def _hash_block(block: dict) -> str:
    canon = json.dumps(block, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canon.encode()).hexdigest()


def _load_chain(chain_file: "str | Path") -> list:
    chain_file = Path(chain_file)
    if not chain_file.exists():
        genesis = {
            "index": 0,
            "timestamp": "2026-01-01T00:00:00+00:00",
            "prev_hash": "0" * 64,
            "data_hash": hashlib.sha256(b"genesis").hexdigest(),
            "data": {"note": "genesis"},
            "hash": "",
        }
        genesis["hash"] = _hash_block({k: v for k, v in genesis.items() if k != "hash"})
        return [genesis]
    with open(chain_file) as f:
        return json.load(f)


def _save_chain(chain_file: "str | Path", chain: list) -> None:
    chain_file = Path(chain_file)
    chain_file.parent.mkdir(parents=True, exist_ok=True)
    with open(chain_file, "w") as f:
        json.dump(chain, f, indent=2)


def anchor_local(fingerprint: str, payload: dict, chain_file: "str | Path" = "chain.json") -> dict:
    chain_file = Path(chain_file)
    chain = _load_chain(chain_file)
    prev = chain[-1]
    data_hash = hashlib.sha256(fingerprint.encode()).hexdigest()
    block = {
        "index": len(chain),
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "prev_hash": prev["hash"],
        "data_hash": data_hash,
        "data": {"fingerprint": fingerprint, **(payload or {})},
    }
    block["hash"] = _hash_block(block)
    chain.append(block)
    _save_chain(chain_file, chain)
    return {
        "mode": "local",
        "block_index": block["index"],
        "block_hash": block["hash"],
        "prev_hash": block["prev_hash"],
        "data_hash": data_hash,
        "chain_file": str(chain_file),
        "explorerUrl": f"file://{chain_file.resolve()}#block-{block['index']}",
    }


def verify_local(fingerprint: str, chain_file: "str | Path" = "chain.json") -> dict:
    chain_file = Path(chain_file)
    if not chain_file.exists():
        return {"verified": False, "reason": "no chain file"}
    chain = _load_chain(chain_file)
    # integrity check 1: every block's prev_hash == previous block's hash
    for i in range(1, len(chain)):
        if chain[i]["prev_hash"] != chain[i - 1]["hash"]:
            return {"verified": False, "reason": f"chain integrity broken at block {i} (prev_hash mismatch)"}
    # integrity check 2: each block's hash matches its content (data_hash = sha256(fingerprint), block.hash = sha256({index,timestamp,prev_hash,data_hash,data}))
    for b in chain:
        expected_block_hash = _hash_block({k: v for k, v in b.items() if k != "hash"})
        if b.get("hash") != expected_block_hash:
            return {"verified": False, "reason": f"block {b['index']} content tampered (hash mismatch)"}
        # only check data_hash consistency if fingerprint is in data (skip genesis)
        fp = b["data"].get("fingerprint")
        if fp is not None:
            expected_data_hash = hashlib.sha256(fp.encode()).hexdigest()
            if b.get("data_hash") != expected_data_hash:
                return {"verified": False, "reason": f"block {b['index']} data_hash inconsistent with fingerprint"}
    # find fingerprint
    target = hashlib.sha256(fingerprint.encode()).hexdigest()
    for b in chain:
        if b["data_hash"] == target:
            return {
                "verified": True,
                "block_index": b["index"],
                "block_hash": b["hash"],
                "timestamp": b["timestamp"],
                "data": b["data"],
            }
    return {"verified": False, "reason": "fingerprint not anchored in this chain"}
