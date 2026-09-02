#!/usr/bin/env python3
"""Deploy FaceAnchor.sol to Polygon Amoy (or any EVM testnet).

Usage:
    source .env  # needs EVM_PRIVATE_KEY
    python3 scripts/deploy_contract.py

Prints the deployed address and appends EVM_CONTRACT_ADDRESS to .env.
Contract anchoring is optional — self-send txs remain the default.
"""
import os
import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv

load_dotenv(ROOT / ".env")

CONTRACT = ROOT / "contracts" / "FaceAnchor.sol"


def main():
    from web3 import Web3
    from eth_account import Account
    from solcx import compile_source, set_solc_version

    pk = os.getenv("EVM_PRIVATE_KEY", "").strip()
    if not pk:
        sys.exit("EVM_PRIVATE_KEY not set in .env")
    rpc = os.getenv("EVM_RPC_URL", "https://polygon-amoy-bor-rpc.publicnode.com")
    chain_id = int(os.getenv("EVM_CHAIN_ID", "80002"))

    set_solc_version("0.8.20")
    compiled = compile_source(
        CONTRACT.read_text(),
        output_values=["abi", "bin"],
        solc_version="0.8.20",
    )
    _key, iface = next(iter(compiled.items()))
    abi, bytecode = iface["abi"], iface["bin"]
    print(f"compiled FaceAnchor (bin {len(bytecode) // 2} bytes)")

    w3 = Web3(Web3.HTTPProvider(rpc, request_kwargs={"timeout": 30}))
    from web3.middleware import ExtraDataToPOAMiddleware
    w3.middleware_onion.inject(ExtraDataToPOAMiddleware, layer=0)
    if not w3.is_connected():
        sys.exit(f"RPC unreachable: {rpc}")
    acct = Account.from_key(pk)
    balance = w3.eth.get_balance(acct.address)
    print(f"deployer {acct.address}  balance {w3.from_wei(balance, 'ether')} POL")
    if balance == 0:
        sys.exit("empty wallet — get Amoy POL at https://faucet.polygon.technology/")

    C = w3.eth.contract(abi=abi, bytecode=bytecode)
    base_fee = w3.eth.get_block("latest").get("baseFeePerGas", 0)
    max_fee = max(w3.to_wei(60, "gwei"), int(base_fee * 2) + w3.to_wei(30, "gwei"))
    tx = C.constructor().build_transaction({
        "from": acct.address,
        "nonce": w3.eth.get_transaction_count(acct.address),
        "gas": 900_000,
        "chainId": chain_id,
        "maxFeePerGas": max_fee,
        "maxPriorityFeePerGas": w3.to_wei(30, "gwei"),
    })
    signed = acct.sign_transaction(tx)
    raw = getattr(signed, "raw_transaction", None) or signed.rawTransaction
    h = w3.eth.send_raw_transaction(raw)
    print(f"deploy tx {h.hex()} — waiting for receipt...")
    rc = w3.eth.wait_for_transaction_receipt(h, timeout=180)
    if rc.status != 1:
        sys.exit("deployment reverted")
    print(f"✓ FaceAnchor deployed at {rc.contractAddress} (block {rc.blockNumber})")
    print(f"  explorer: https://amoy.polygonscan.com/address/{rc.contractAddress}")

    env_path = ROOT / ".env"
    lines = env_path.read_text().splitlines() if env_path.exists() else []
    lines = [l for l in lines if not l.startswith("EVM_CONTRACT_ADDRESS=")]
    lines.append(f"EVM_CONTRACT_ADDRESS={rc.contractAddress}")
    env_path.write_text("\n".join(lines) + "\n")
    print(f"✓ wrote EVM_CONTRACT_ADDRESS to {env_path}")


if __name__ == "__main__":
    main()
