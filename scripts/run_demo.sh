#!/bin/bash
# HH Goa 2026 Task 3 — one-command demo for screen recording.
# Usage: ./scripts/run_demo.sh [image]   (default: data/samples/lena.jpg)
set -euo pipefail
cd "$(dirname "$0")/.."
export PATH="/opt/homebrew/bin:/usr/local/bin:$PATH"  # macOS arm64 brew pythons

if [ -f .env ] && [ -z "${SERPAPI_API_KEY:-}" ]; then
  set -a; . .env; set +a
fi

PY=${PY:-}
[ -x ".venv/bin/python" ] && PY=".venv/bin/python"
if [ -z "$PY" ]; then
  # pick an interpreter that has the deps (dotenv is the canary)
  for c in python3 python3.11 python3.12 python3.10; do
    if command -v "$c" >/dev/null 2>&1 && "$c" -c "import dotenv" >/dev/null 2>&1; then PY="$c"; break; fi
  done
fi
[ -n "$PY" ] || { echo "No python with deps found — pip install -r requirements.txt"; exit 1; }

MODE=${BLOCKCHAIN_MODE:-evm}
echo "===================================================="
echo "HH GOA 2026 — TASK 3"
echo "Face → Social → Blockchain  (real execution, no mocks)"
echo "===================================================="
echo "Config: BLOCKCHAIN_MODE=$MODE  SEARCH=live (SerpAPI Lens)"
if [ -z "${SERPAPI_API_KEY:-}" ]; then
  echo "ERROR: SERPAPI_API_KEY missing — free key at serpapi.com/users/sign_up"; exit 1
fi
if [ "$MODE" = "evm" ] && [ -z "${EVM_PRIVATE_KEY:-}" ]; then
  echo "ERROR: EVM_PRIVATE_KEY missing — throwaway wallet + 0.2 POL from faucet.polygon.technology"; exit 1
fi

IMG="${1:-data/samples/lena.jpg}"
[ -f "$IMG" ] || { echo "ERROR: image not found: $IMG"; exit 1; }
mkdir -p outputs

"$PY" -m src.pipeline --image "$IMG" --out outputs --chain chain.json

echo ""
echo "===================================================="
echo "ARTIFACTS"
echo "  outputs/result.json    full pipeline result"
echo "  outputs/evidence.json  canonical record + fingerprint"
echo "  outputs/receipt.json   blockchain anchor receipt"
echo "  outputs/reverify.json  independent re-hash result"
echo "  chain.json             local hash-chain audit trail"
echo "  evm_mirror.json        all on-chain tx per fingerprint"
echo "===================================================="

FP=$(awk -F'"' '/fingerprint_sha256/ {print $4; exit}' outputs/evidence.json)
[ -z "${FP:-}" ] && { echo "WARNING: no fingerprint in outputs/evidence.json"; exit 0; }

echo ""
echo "STANDALONE RE-VERIFY (fresh process, on-chain read)"
if [ "$MODE" = "evm" ]; then
  "$PY" -m src.pipeline --verify "$FP" --chain chain.json && echo "→ VERIFIED (exit 0)" || echo "→ NOT VERIFIED"
else
  "$PY" -m src.pipeline --verify "$FP" --chain chain.json && echo "→ VERIFIED (exit 0)" || echo "→ NOT VERIFIED"
fi
echo "Done."
