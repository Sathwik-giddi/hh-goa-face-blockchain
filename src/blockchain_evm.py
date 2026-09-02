"""Polygon Amoy anchoring — $0 via free faucet. Atomic, dedupe-safe, EVM-only.

Anchors a fingerprint to Polygon Amoy as a 0-value self-tx with `data=0x+fingerprint`
(32 bytes). Also writes a local hash-chain block (chain.json) for unified audit.

Note: contracts/Anchor.sol is a reference; this code uses self-send because it
is gas-cheap, no contract deployment required, and judges can verify on Polygonscan
input data directly.
"""
import json
import os
import tempfile
from pathlib import Path

from filelock import FileLock


def _atomic_write_json(path: Path, data) -> None:
    """Atomic JSON write: temp + replace + fsync."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=path.name + ".", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _atomic_read_json(path: Path):
    if not path.exists():
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return None


def _normalize_hex(s: str) -> str:
    s = (s or "").lower()
    if s.startswith("0x"):
        s = s[2:]
    return "0x" + s


def _record_tx(tx_hash: str, fingerprint: str, payload: dict) -> None:
    """Write to mirror, keeping history of all tx for a fingerprint (not just last)."""
    mirror = Path(os.getenv("EVM_MIRROR_FILE", "evm_mirror.json"))
    lock = FileLock(str(mirror) + ".lock", timeout=10)
    with lock:
        data = _atomic_read_json(mirror) or {}
        data[tx_hash] = {"fingerprint": fingerprint, "payload": payload or {}}
        # keep list of all tx for a fingerprint (not just last)
        by_fp = data.setdefault("_by_fp", {})
        if fingerprint not in by_fp:
            by_fp[fingerprint] = []
        if isinstance(by_fp[fingerprint], str):
            by_fp[fingerprint] = [by_fp[fingerprint]]
        if tx_hash not in by_fp[fingerprint]:
            by_fp[fingerprint].append(tx_hash)
        _atomic_write_json(mirror, data)


def _read_mirror(fingerprint: str):
    mirror = Path(os.getenv("EVM_MIRROR_FILE", "evm_mirror.json"))
    data = _atomic_read_json(mirror) or {}
    by_fp = data.get("_by_fp", {})
    entries = by_fp.get(fingerprint)
    if not entries:
        return None
    if isinstance(entries, str):
        entries = [entries]
    out = []
    for h in entries:
        rec = data.get(h)
        if rec:
            out.append((h, rec))
    return out


def anchor_evm(fingerprint: str, payload: dict, chain_file=None):
    """Anchor fingerprint to Polygon Amoy. Also writes a local chain.json block
    if chain_file is provided. Atomic, dedup-safe, raises on real failure.
    """
    try:
        from web3 import Web3
        from eth_account import Account
    except ImportError:
        return {"mode": "evm", "error": "web3/eth-account not installed — pip install web3 eth-account", "verified": False}

    rpc = os.getenv("EVM_RPC_URL", "https://polygon-amoy-bor-rpc.publicnode.com")
    pk = os.getenv("EVM_PRIVATE_KEY", "").strip()
    chain_id_str = os.getenv("EVM_CHAIN_ID", "80002")
    try:
        chain_id = int(chain_id_str)
    except ValueError:
        return {"mode": "evm", "error": f"EVM_CHAIN_ID not int: {chain_id_str!r}", "verified": False}
    if not pk:
        return {"mode": "evm", "error": "EVM_PRIVATE_KEY empty — set in .env", "verified": False}
    if not (len(fingerprint) == 64 and all(c in "0123456789abcdefABCDEF" for c in fingerprint)):
        return {"mode": "evm", "error": "fingerprint must be 64-char hex sha256", "verified": False}

    w3 = Web3(Web3.HTTPProvider(rpc, request_kwargs={"timeout": 15}))
    if not w3.is_connected():
        return {"mode": "evm", "error": f"RPC not reachable: {rpc}", "verified": False}

    try:
        acct = Account.from_key(pk)
        nonce = w3.eth.get_transaction_count(acct.address)
        data_hex = "0x" + fingerprint
        # Use current base_fee * 2 + priority for safety on Amoy
        try:
            base_fee = w3.eth.get_block("latest").get("baseFeePerGas", 0)
        except Exception:
            base_fee = 0
        max_fee = max(w3.to_wei(60, "gwei"), int(base_fee * 2) + w3.to_wei(30, "gwei"))
        max_priority = w3.to_wei(30, "gwei")
        tx = {
            "to": acct.address, "value": 0, "data": data_hex, "nonce": nonce,
            "gas": 50000, "chainId": chain_id,
            "maxFeePerGas": max_fee,
            "maxPriorityFeePerGas": max_priority,
        }
        signed = acct.sign_transaction(tx)
        # web3.py 6.x: signed.raw_transaction. 5.x: signed.rawTransaction.
        if hasattr(signed, "raw_transaction"):
            raw = signed.raw_transaction
        elif hasattr(signed, "rawTransaction"):
            raw = signed.rawTransaction
        else:
            return {"mode": "evm", "error": "signed tx missing raw bytes (web3.py version?)", "verified": False}
        h = w3.eth.send_raw_transaction(raw)
        receipt = w3.eth.wait_for_transaction_receipt(h, timeout=60)
        tx_hash_raw = h.hex()
        if not tx_hash_raw.startswith("0x"):
            tx_hash_raw = "0x" + tx_hash_raw
        explorer = f"https://amoy.polygonscan.com/tx/{tx_hash_raw}"
        _record_tx(tx_hash_raw, fingerprint, payload or {})

        result = {
            "mode": "evm", "txHash": tx_hash_raw, "blockNumber": receipt.blockNumber,
            "explorerUrl": explorer, "data_hex": data_hex, "from": acct.address,
        }

        if chain_file is not None:
            try:
                from .blockchain_local import anchor_local
                mirror_payload = {
                    "mode": "evm",
                    "txHash": tx_hash_raw,
                    "blockNumber": receipt.blockNumber,
                    "explorerUrl": explorer,
                    "from": acct.address,
                }
                # add payload keys (except fingerprint) safely
                for k, v in (payload or {}).items():
                    if k != "fingerprint" and k not in mirror_payload:
                        mirror_payload[k] = v
                local_receipt = anchor_local(fingerprint, mirror_payload, chain_file=chain_file)
                result["local_block"] = local_receipt.get("block_index")
                result["local_block_hash"] = local_receipt.get("block_hash")
                if local_receipt.get("deduplicated"):
                    result["deduplicated"] = True
            except Exception as e:
                print(f"[blockchain_evm] local mirror failed (non-fatal): {e}")
        return result
    except Exception as e:
        return {"mode": "evm", "error": str(e)[:200], "verified": False}


def verify_evm(fingerprint: str, chain_file=None):
    """Verify a fingerprint against EVM chain + local mirror.

    Always runs local hash-chain check first (cheap), then on-chain input data
    verification for any tx we have in the mirror.
    """
    try:
        from web3 import Web3
    except ImportError:
        return {"verified": False, "reason": "web3 not installed"}

    # Step 1: local hash-chain check
    local_block = None
    local_reason = None
    if chain_file:
        try:
            from .blockchain_local import verify_local
            v = verify_local(fingerprint, chain_file=chain_file)
            if v.get("verified"):
                local_block = {
                    "block_index": v.get("block_index"),
                    "block_hash": v.get("block_hash"),
                    "timestamp": v.get("timestamp"),
                }
            else:
                local_reason = v.get("reason")
        except Exception as e:
            local_reason = f"local check error: {e}"

    # Step 2: EVM mirror
    entries = _read_mirror(fingerprint)
    if not entries:
        if local_block:
            return {
                "verified": None,
                "note": "Local chain has the block; no EVM mirror (different machine). Verify via amoy.polygonscan.com/tx/<txHash>.",
                "local_block": local_block,
            }
        return {
            "verified": False,
            "reason": local_reason or "fingerprint not anchored on this machine",
            "local_block": local_block,
        }

    # Step 3: on-chain verify each tx in mirror
    rpc = os.getenv("EVM_RPC_URL", "https://polygon-amoy-bor-rpc.publicnode.com")
    w3 = Web3(Web3.HTTPProvider(rpc, request_kwargs={"timeout": 15}))
    if not w3.is_connected():
        return {
            "verified": None,
            "note": f"RPC unreachable, can't re-check on-chain: {rpc}",
            "local_block": local_block,
        }
    expected = "0x" + fingerprint.lower()
    results = []
    for tx_hash, payload in entries:
        try:
            tx = w3.eth.get_transaction(tx_hash)
            igh = tx["input"].hex() if hasattr(tx["input"], "hex") else tx["input"]
            if not igh.startswith("0x"):
                igh = "0x" + igh
            ok = igh.lower() == expected
            results.append({
                "verified": ok,
                "txHash": tx_hash,
                "blockNumber": tx.get("blockNumber"),
                "explorerUrl": f"https://amoy.polygonscan.com/tx/{tx_hash}",
                "on_chain_input": igh,
                "expected_input": expected,
            })
        except Exception as e:
            results.append({"verified": None, "txHash": tx_hash, "error": str(e)[:100]})
    # Aggregate: True if any is True
    any_true = any(r.get("verified") is True for r in results)
    if any_true:
        return {
            "verified": True,
            "txs": results,
            "local_block": local_block,
        }
    if all(r.get("verified") is False for r in results):
        return {
            "verified": False,
            "reason": "on-chain input data does not match fingerprint",
            "txs": results,
            "local_block": local_block,
        }
    return {
        "verified": None,
        "note": "could not confirm any tx on-chain (lookups failed)",
        "txs": results,
        "local_block": local_block,
    }
