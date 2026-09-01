"""Polygon Amoy anchoring — $0 via free faucet. Anchor fingerprint as bytes32 in tx input data.

The chain is EVM-compatible (Amoy testnet, chainId 80002). To make verification
undeniable, we *also* write the fingerprint into a local chain.json mirror keyed by
txHash, so the API can answer "verified" without re-scanning the chain.
"""
import os
import json
import hashlib
from pathlib import Path


def _record_tx(tx_hash: str, fingerprint: str, payload: dict) -> None:
    mirror = Path(os.getenv("EVM_MIRROR_FILE", "evm_mirror.json"))
    mirror.parent.mkdir(parents=True, exist_ok=True)
    data = {}
    if mirror.exists():
        try:
            data = json.load(open(mirror))
        except Exception:
            data = {}
    data[tx_hash] = {"fingerprint": fingerprint, "payload": payload}
    # back-reference by fingerprint
    data.setdefault("_by_fp", {})[fingerprint] = tx_hash
    json.dump(data, open(mirror, "w"), indent=2)


def _read_mirror(fingerprint: str):
    mirror = Path(os.getenv("EVM_MIRROR_FILE", "evm_mirror.json"))
    if not mirror.exists():
        return None
    try:
        data = json.load(open(mirror))
    except Exception:
        return None
    rec = data.get("_by_fp", {}).get(fingerprint)
    if not rec:
        return None
    return rec, data.get(rec)


def anchor_evm(fingerprint: str, payload: dict):
    try:
        from web3 import Web3
        from eth_account import Account
    except ImportError:
        return {"mode": "evm", "error": "web3/eth-account not installed — pip install web3 eth-account", "verified": False}

    rpc = os.getenv("EVM_RPC_URL", "https://polygon-amoy-bor-rpc.publicnode.com")
    pk = os.getenv("EVM_PRIVATE_KEY", "")
    chain_id = int(os.getenv("EVM_CHAIN_ID", "80002"))
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
        data_hex = "0x" + fingerprint  # 32 bytes
        tx = {
            "to": acct.address, "value": 0, "data": data_hex, "nonce": nonce,
            "gas": 50000, "chainId": chain_id,
            "maxFeePerGas": w3.to_wei("60", "gwei"),
            "maxPriorityFeePerGas": w3.to_wei("30", "gwei"),
        }
        signed = acct.sign_transaction(tx)
        h = w3.eth.send_raw_transaction(signed.raw_transaction)
        receipt = w3.eth.wait_for_transaction_receipt(h, timeout=60)
        tx_hash = h.hex()
        explorer = f"https://amoy.polygonscan.com/tx/{tx_hash}"
        _record_tx(tx_hash, fingerprint, payload or {})
        return {
            "mode": "evm", "txHash": tx_hash, "blockNumber": receipt.blockNumber,
            "explorerUrl": explorer, "data_hex": data_hex, "from": acct.address,
        }
    except Exception as e:
        return {"mode": "evm", "error": str(e), "verified": False}


def verify_evm(fingerprint: str):
    """Verify a fingerprint against EVM chain.

    Strategy:
      1. If we have a local mirror record (from when we anchored), check the tx is on-chain
         and that input data == fingerprint. Returns verified=True/False.
      2. Otherwise, return verified=None with instructions to look up via txHash on Polygonscan.
    """
    try:
        from web3 import Web3
    except ImportError:
        return {"verified": False, "reason": "web3 not installed"}

    rec = _read_mirror(fingerprint)
    if not rec:
        return {
            "verified": None,
            "note": "No local mirror for this fingerprint (may be from a different machine). Verify via amoy.polygonscan.com/tx/<txHash> → Input Data == fingerprint.",
        }
    tx_hash, payload = rec
    rpc = os.getenv("EVM_RPC_URL", "https://polygon-amoy-bor-rpc.publicnode.com")
    w3 = Web3(Web3.HTTPProvider(rpc, request_kwargs={"timeout": 15}))
    if not w3.is_connected():
        return {"verified": None, "note": f"RPC unreachable, can't re-check on-chain: {rpc}"}
    try:
        tx = w3.eth.get_transaction(tx_hash)
        # input data may or may not have 0x prefix
        igh = tx["input"].hex() if hasattr(tx["input"], "hex") else tx["input"]
        if not igh.startswith("0x"):
            igh = "0x" + igh
        expected = "0x" + fingerprint
        on_chain = igh.lower() == expected.lower()
        return {
            "verified": on_chain,
            "txHash": tx_hash,
            "blockNumber": tx["blockNumber"],
            "explorerUrl": f"https://amoy.polygonscan.com/tx/{tx_hash}",
            "on_chain_input": igh,
            "expected_input": expected,
            "payload": payload,
        }
    except Exception as e:
        return {"verified": None, "note": f"On-chain lookup failed: {e}"}
