#!/usr/bin/env python3
"""Pre-flight doctor: verify every link in the chain before a demo.

Usage: python3 scripts/doctor.py
Exits 0 only when the pipeline is ready end to end. Never mutates anything.
"""
import os
import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv

load_dotenv(ROOT / ".env")

OK, WARN, BAD = "✓", "!", "✗"
results = []


def check(name, fn):
    try:
        note = fn()
        results.append((OK, name, note or ""))
    except Exception as e:
        results.append((BAD, name, str(e)[:110]))


def warn_check(name, fn):
    try:
        note = fn()
        results.append((OK, name, note or ""))
    except Exception as e:
        results.append((WARN, name, str(e)[:110]))


# 1. Python + deps — importlib = pure availability check
def c_deps():
    import importlib
    import cv2
    for m in ("web3", "fastapi", "onnxruntime", "filelock", "solcx"):
        importlib.import_module(m)
    return f"opencv {cv2.__version__}, onnxruntime ok"
check("python dependencies", c_deps)


def c_heif():
    import importlib
    importlib.import_module("pillow_heif")
    return "HEIC support present"
check("pillow-heif (iPhone photos)", c_heif)

# 2. Face models
def c_yunet():
    p = ROOT / "models" / "face_detection_yunet_2023mar.onnx"
    assert p.exists() and p.stat().st_size > 100_000, "will auto-download on first scan"
    return f"{p.stat().st_size // 1024} KB"
check("YuNet detector", c_yunet)


def c_arc():
    p = ROOT / "models" / "w600k_r50.onnx"
    assert p.exists() and p.stat().st_size > 100_000_000, "will auto-download (~166MB) on first scan"
    return f"{p.stat().st_size // (1024 * 1024)} MB"
check("ArcFace recognizer", c_arc)

# 3. Live end-to-end face embedding
def c_embed():
    from src.face_id import face_embedding
    f, m = face_embedding(str(ROOT / "data" / "samples" / "lena.jpg"))
    assert f is not None, "embedding failed"
    return f"{m}, {len(f)}-D"
check("face embedding", c_embed)

# 4. Keys
def c_serp():
    k = os.getenv("SERPAPI_API_KEY", "")
    assert k, "missing — free at serpapi.com/users/sign_up"
    return f"***{k[-4:]}"
check("SERPAPI_API_KEY", c_serp)


def c_pk():
    pk = os.getenv("EVM_PRIVATE_KEY", "")
    assert pk, "missing — generate a throwaway wallet"
    from eth_account import Account
    return Account.from_key(pk).address
check("EVM_PRIVATE_KEY", c_pk)

# 5. Chain reachability + balance
def c_rpc():
    from web3 import Web3
    from web3.middleware import ExtraDataToPOAMiddleware
    w3 = Web3(Web3.HTTPProvider(os.getenv("EVM_RPC_URL", "https://polygon-amoy-bor-rpc.publicnode.com"),
                                request_kwargs={"timeout": 10}))
    w3.middleware_onion.inject(ExtraDataToPOAMiddleware, layer=0)
    assert w3.is_connected(), "RPC unreachable"
    n = w3.eth.block_number
    return f"block {n}"
check("Polygon Amoy RPC", c_rpc)


def c_bal():
    from web3 import Web3
    from web3.middleware import ExtraDataToPOAMiddleware
    from eth_account import Account
    w3 = Web3(Web3.HTTPProvider(os.getenv("EVM_RPC_URL", "https://polygon-amoy-bor-rpc.publicnode.com"),
                                request_kwargs={"timeout": 10}))
    w3.middleware_onion.inject(ExtraDataToPOAMiddleware, layer=0)
    a = Account.from_key(os.getenv("EVM_PRIVATE_KEY", "").strip())
    bal = w3.from_wei(w3.eth.get_balance(a.address), "ether")
    anchors_left = int(float(bal) / 0.005)
    assert bal > 0, "empty — faucet.polygon.technology (Amoy)"
    return f"{bal:.4f} POL (~{anchors_left} anchors)"
check("wallet balance", c_bal)


def c_contract():
    addr = os.getenv("EVM_CONTRACT_ADDRESS", "")
    assert addr, "not deployed — python3 scripts/deploy_contract.py"
    from web3 import Web3
    from web3.middleware import ExtraDataToPOAMiddleware
    w3 = Web3(Web3.HTTPProvider(os.getenv("EVM_RPC_URL", "https://polygon-amoy-bor-rpc.publicnode.com"),
                                request_kwargs={"timeout": 10}))
    w3.middleware_onion.inject(ExtraDataToPOAMiddleware, layer=0)
    abi = [{"inputs": [], "name": "totalAnchored",
            "outputs": [{"name": "", "type": "uint64"}], "stateMutability": "view", "type": "function"}]
    c = w3.eth.contract(address=Web3.to_checksum_address(addr), abi=abi)
    return f"{addr[:10]}… totalAnchored={c.functions.totalAnchored().call()}"
check("FaceAnchor contract", c_contract)

# 6. Audit trail integrity — full hash walk, same algorithm as blockchain_local
def c_chain():
    from src.blockchain_local import _load_chain, _hash_block
    chain = _load_chain(ROOT / "chain.json")
    assert len(chain) >= 2, "chain too short"
    prev = "0" * 64
    for b in chain:
        assert b["prev_hash"] == prev, f"broken prev_hash linkage at block {b['index']}"
        calc = _hash_block({k: v for k, v in b.items() if k != "hash"})
        assert calc == b["hash"], f"block {b['index']} hash mismatch — chain was tampered"
        prev = b["hash"]
    return f"{len(chain)} blocks, every hash verified"
check("local chain integrity", c_chain)

# 7. SerpAPI live (costs 1 credit; warn-only)
def c_serp_live():
    import requests
    r = requests.get("https://serpapi.com/account", params={"api_key": os.getenv("SERPAPI_API_KEY", "")}, timeout=10)
    j = r.json()
    remaining = j.get("total_searches_left", j.get("searches_left", "?"))
    return f"{remaining} searches left this month"
warn_check("SerpAPI account (1 free call)", c_serp_live)


def main():
    print("=" * 62)
    print("HH GOA TASK 3 — PRE-FLIGHT DOCTOR")
    print("=" * 62)
    for mark, name, note in results:
        print(f" {mark} {name:<32} {note}")
    bad = sum(1 for m, _n, _t in results if m == BAD)
    warn = sum(1 for m, _n, _t in results if m == WARN)
    print("=" * 62)
    if bad:
        print(f"✗ {bad} blocker(s) — fix above before the demo")
        sys.exit(1)
    print(f"READY for the demo ({warn} warning(s))" if warn else "READY for the demo — all green")
    sys.exit(0 if not bad else 1)


if __name__ == "__main__":
    main()
