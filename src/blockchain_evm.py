"""Optional Polygon Amoy anchoring — $0 via free faucet. Requires web3 + private key."""
import os
from pathlib import Path

def anchor_evm(fingerprint: str, payload: dict):
    try:
        from web3 import Web3
        from eth_account import Account
    except ImportError:
        return {"mode": "evm", "error": "web3/eth-account not installed — pip install web3 eth-account", "verified": False}

    rpc = os.getenv("EVM_RPC_URL", "https://rpc-amoy.polygon.technology")
    pk = os.getenv("EVM_PRIVATE_KEY", "")
    chain_id = int(os.getenv("EVM_CHAIN_ID", "80002"))
    if not pk:
        return {"mode": "evm", "error": "EVM_PRIVATE_KEY empty — generate throwaway or set in .env", "verified": False}

    w3 = Web3(Web3.HTTPProvider(rpc))
    if not w3.is_connected():
        return {"mode": "evm", "error": f"RPC not reachable {rpc}", "verified": False}
    acct = Account.from_key(pk)
    # EVM mapping(bytes32=>uint64) would be contract call; $0 shortcut: anchor as tx input data to self
    data_hex = "0x" + fingerprint.replace("0x", "") if fingerprint.startswith("0x") else "0x" + fingerprint.encode().hex()[:64]
    # use keccak hash as bytes32 if fingerprint is hex sha256
    try:
        # if fingerprint is 64-char hex, treat as bytes32
        if len(fingerprint) == 64 and all(c in "0123456789abcdefABCDEF" for c in fingerprint):
            data_hex = "0x" + fingerprint
        else:
            # hash it
            import hashlib
            data_hex = "0x" + hashlib.sha256(fingerprint.encode()).hexdigest()
    except Exception:
        pass

    try:
        nonce = w3.eth.get_transaction_count(acct.address)
        tx = {"to": acct.address, "value": 0, "data": data_hex, "nonce": nonce, "gas": 50000, "chainId": chain_id, "maxFeePerGas": w3.to_wei("60", "gwei"), "maxPriorityFeePerGas": w3.to_wei("30", "gwei")}
        signed = acct.sign_transaction(tx)
        h = w3.eth.send_raw_transaction(signed.raw_transaction)
        receipt = w3.eth.wait_for_transaction_receipt(h, timeout=60)
        explorer = f"https://amoy.polygonscan.com/tx/{h.hex()}"
        return {"mode": "evm", "txHash": h.hex(), "blockNumber": receipt.blockNumber, "explorerUrl": explorer, "data_hex": data_hex, "from": acct.address}
    except Exception as e:
        return {"mode": "evm", "error": str(e), "verified": False}

def verify_evm(fingerprint: str):
    try:
        from web3 import Web3
    except ImportError:
        return {"verified": False, "reason": "web3 not installed"}
    rpc = os.getenv("EVM_RPC_URL", "https://rpc-amoy.polygon.technology")
    w3 = Web3(Web3.HTTPProvider(rpc))
    # For tx.data anchoring, verification needs txHash — here we just state how to verify
    return {"verified": None, "note": "For tx.data anchoring, verify via amoy.polygonscan.com/tx/<txHash> → Input Data == fingerprint. Contract mapping verify would be w3.eth.call."}
