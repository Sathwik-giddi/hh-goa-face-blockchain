import os
from pathlib import Path
from .blockchain_local import anchor_local, verify_local
from .utils import is_hex64


def anchor(fingerprint: str, payload: dict, chain_file=None):
    """Dispatcher: anchor to local hash-chain or Polygon Amoy (EVM).

    chain_file defaults to $CHAIN_FILE env or './chain.json'.
    """
    if chain_file is None:
        chain_file = os.getenv("CHAIN_FILE", "./chain.json")
    if not is_hex64(fingerprint or ""):
        raise ValueError("fingerprint must be 64-char hex sha256")
    mode = os.getenv("BLOCKCHAIN_MODE", "local").strip().lower()
    if mode == "evm":
        from .blockchain_evm import anchor_evm
        r = anchor_evm(fingerprint, payload, chain_file=chain_file)
        if r.get("error"):
            raise RuntimeError(f"EVM anchor failed (LIVE mode, no fallback): {r['error']}")
        return r
    return anchor_local(fingerprint, payload, chain_file=chain_file)


def verify(fingerprint: str, chain_file=None):
    if chain_file is None:
        chain_file = os.getenv("CHAIN_FILE", "./chain.json")
    if not is_hex64(fingerprint or ""):
        return {"verified": False, "reason": "fingerprint must be 64-char hex sha256"}
    mode = os.getenv("BLOCKCHAIN_MODE", "local").strip().lower()
    if mode == "evm":
        from .blockchain_evm import verify_evm
        return verify_evm(fingerprint, chain_file=chain_file)
    return verify_local(fingerprint, chain_file=chain_file)
