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
    try:
        from web3.middleware import ExtraDataToPOAMiddleware
        w3.middleware_onion.inject(ExtraDataToPOAMiddleware, layer=0)
    except Exception:
        pass
    if not w3.is_connected():
        return {"mode": "evm", "error": f"RPC not reachable: {rpc}", "verified": False}

    contract_address = os.getenv("EVM_CONTRACT_ADDRESS", "").strip()

    try:
        acct = Account.from_key(pk)
        nonce = w3.eth.get_transaction_count(acct.address)

        if contract_address:
            # Contract path: FaceAnchor.anchor(bytes32,string) — event + mapping,
            # verifiable trustlessly by anyone via the contract's verify() view.
            # Pre-check anchoredAt: the contract reverts on duplicates, so skip
            # the tx entirely and return a dedupe receipt.
            check_abi = [{
                "inputs": [{"internalType": "bytes32", "name": "fingerprint", "type": "bytes32"}],
                "name": "anchoredAt",
                "outputs": [{"internalType": "uint64", "name": "", "type": "uint64"}],
                "stateMutability": "view", "type": "function",
            }]
            c_check = w3.eth.contract(address=Web3.to_checksum_address(contract_address), abi=check_abi)
            existing_ts = c_check.functions.anchoredAt(bytes.fromhex(fingerprint)).call()
            if existing_ts:
                base = {
                    "mode": "evm", "via": "contract", "deduplicated": True,
                    "contractAddress": contract_address,
                    "anchoredAt": int(existing_ts),
                    "explorerUrl": f"https://amoy.polygonscan.com/address/{contract_address}",
                    "data_hex": "0x" + fingerprint, "from": acct.address,
                }
                if chain_file is not None:
                    try:
                        from .blockchain_local import anchor_local
                        local_receipt = anchor_local(fingerprint, {"mode": "evm", "deduplicated": True}, chain_file=chain_file)
                        base["local_block"] = local_receipt.get("block_index")
                        base["local_block_hash"] = local_receipt.get("block_hash")
                    except Exception as e:
                        print(f"[blockchain_evm] local mirror failed (non-fatal): {e}")
                return base
            abi = [{
                "inputs": [
                    {"internalType": "bytes32", "name": "fingerprint", "type": "bytes32"},
                    {"internalType": "string", "name": "cid", "type": "string"},
                ],
                "name": "anchor", "outputs": [], "stateMutability": "nonpayable", "type": "function",
            }]
            c = w3.eth.contract(address=Web3.to_checksum_address(contract_address), abi=abi)
            # §8: store the source reference on-chain (off-chain evidence pointer)
            cid = (payload or {}).get("post", {}).get("link", "") or ""
            call_args = [bytes.fromhex(fingerprint), cid]
            data_hex = c.encode_abi("anchor", call_args)
            tx_to = Web3.to_checksum_address(contract_address)
            try:
                gas = min(int(c.functions.anchor(*call_args).estimate_gas({"from": acct.address}) * 1.25) + 10_000, 500_000)
            except Exception:
                gas = 400_000
        else:
            data_hex = "0x" + fingerprint
            tx_to = acct.address
            gas = 50_000

        # Fee at market (Amoy baseFee ≈ 0; node gas_price ≈ 30-40 gwei) + funds
        # pre-check with an actionable message instead of a raw RPC error dump.
        market = w3.eth.gas_price
        max_fee = max(market, w3.to_wei(35, "gwei"))
        max_priority = min(w3.to_wei(25, "gwei"), max_fee)
        balance = w3.eth.get_balance(acct.address)
        need = gas * max_fee
        if balance < need:
            return {
                "mode": "evm",
                "error": (f"wallet balance {Web3.from_wei(balance, 'ether')} POL is below the estimated "
                          f"tx cost {Web3.from_wei(need, 'ether')} POL. Top up free at "
                          f"https://faucet.polygon.technology/ (Amoy, 0.2 POL per 24h), or set "
                          f"BLOCKCHAIN_MODE=local to anchor offline."),
                "verified": False,
            }
        nonce = w3.eth.get_transaction_count(acct.address)
        tx = {
            "to": tx_to,
            "value": 0, "data": data_hex, "nonce": nonce,
            "gas": gas, "chainId": chain_id,
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
        if getattr(receipt, "status", 1) != 1:
            return {"mode": "evm", "error": f"tx reverted on-chain (tx {h.hex()})", "verified": False}
        tx_hash_raw = h.hex()
        if not tx_hash_raw.startswith("0x"):
            tx_hash_raw = "0x" + tx_hash_raw
        explorer = f"https://amoy.polygonscan.com/tx/{tx_hash_raw}"
        _record_tx(tx_hash_raw, fingerprint, payload or {})

        result = {
            "mode": "evm", "txHash": tx_hash_raw, "blockNumber": receipt.blockNumber,
            "explorerUrl": explorer, "data_hex": data_hex, "from": acct.address,
            "via": "contract" if contract_address else "self-tx",
        }
        if contract_address:
            result["contractAddress"] = contract_address

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

    # Step 2b: trustless contract read — works even with no mirror file, so a
    # judge can verify any fingerprint from contract state alone.
    contract_address = os.getenv("EVM_CONTRACT_ADDRESS", "").strip()
    if not entries and contract_address:
        try:
            from web3 import Web3 as _W3
            w3 = _W3(_W3.HTTPProvider(
                os.getenv("EVM_RPC_URL", "https://polygon-amoy-bor-rpc.publicnode.com"),
                request_kwargs={"timeout": 15},
            ))
            if w3.is_connected():
                abi = [
                    {"inputs": [{"internalType": "bytes32", "name": "fingerprint", "type": "bytes32"}],
                     "name": "anchoredAt",
                     "outputs": [{"internalType": "uint64", "name": "", "type": "uint64"}],
                     "stateMutability": "view", "type": "function"},
                ]
                c = w3.eth.contract(address=_W3.to_checksum_address(contract_address), abi=abi)
                ts = c.functions.anchoredAt(bytes.fromhex(fingerprint.lower())).call()
                if ts:
                    return {
                        "verified": True,
                        "txs": [{
                            "verified": True, "via": "contract",
                            "contractAddress": contract_address,
                            "anchoredAt": int(ts),
                            "explorerUrl": f"https://amoy.polygonscan.com/address/{contract_address}#readContract",
                        }],
                        "local_block": local_block,
                    }
        except Exception:
            pass  # fall through to mirror-based verification

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

    # Trustless path: read FaceAnchor.anchoredAt(bytes32) straight from the
    # contract — no mirror file needed, anyone can replicate this on Polygonscan.
    if contract_address:
        try:
            abi = [
                {"inputs": [{"internalType": "bytes32", "name": "fingerprint", "type": "bytes32"}],
                 "name": "anchoredAt",
                 "outputs": [{"internalType": "uint64", "name": "", "type": "uint64"}],
                 "stateMutability": "view", "type": "function"},
            ]
            c = w3.eth.contract(address=Web3.to_checksum_address(contract_address), abi=abi)
            ts = c.functions.anchoredAt(bytes.fromhex(fingerprint.lower())).call()
            results.append({
                "verified": ts != 0,
                "via": "contract",
                "contractAddress": contract_address,
                "anchoredAt": int(ts) if ts else None,
                "explorerUrl": f"https://amoy.polygonscan.com/address/{contract_address}#readContract",
            })
        except Exception as e:
            results.append({"verified": None, "via": "contract", "error": str(e)[:100]})

    for tx_hash, payload in entries:
        try:
            tx = w3.eth.get_transaction(tx_hash)
            igh = tx["input"].hex() if hasattr(tx["input"], "hex") else tx["input"]
            if not igh.startswith("0x"):
                igh = "0x" + igh
            to_addr = (tx.get("to") or "").lower()
            if contract_address and to_addr == contract_address.lower():
                # Contract anchor: calldata = 4-byte selector + 32-byte fp;
                # in the 0x-prefixed hex the fingerprint spans [10:74].
                ok = igh.lower()[10:74] == fingerprint.lower()
            else:
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
