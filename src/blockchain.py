import os
from pathlib import Path
from .blockchain_local import anchor_local, verify_local

def anchor(fingerprint: str, payload: dict, chain_file="chain.json"):
    mode = os.getenv("BLOCKCHAIN_MODE", "local").lower()
    if mode == "evm":
        from .blockchain_evm import anchor_evm
        # Pass chain_file so EVM anchors also write a local hash-chain block
        r = anchor_evm(fingerprint, payload, chain_file=chain_file)
        if r.get("error"):
            raise RuntimeError(f"EVM anchor failed (LIVE mode, no fallback): {r['error']}")
        return r
    return anchor_local(fingerprint, payload, chain_file)

def verify(fingerprint: str, chain_file="chain.json"):
    mode = os.getenv("BLOCKCHAIN_MODE", "local").lower()
    if mode == "evm":
        # Verify against the local mirror block AND the on-chain tx input data
        from .blockchain_evm import verify_evm
        return verify_evm(fingerprint, chain_file=chain_file)
    return verify_local(fingerprint, chain_file)
