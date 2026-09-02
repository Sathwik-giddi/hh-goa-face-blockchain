#!/bin/bash
# $0 demo — run this for screen recording. Works in bash/zsh.
set -euo pipefail
cd "$(dirname "$0")/.."

# Source .env if present (do not override existing env vars)
if [ -f .env ] && [ -z "${SERPAPI_API_KEY:-}" ]; then
  set -a; . .env; set +a
fi

PY=${PY:-python3}
[ -x ".venv/bin/python" ] && PY=".venv/bin/python"
command -v "$PY" >/dev/null 2>&1 || { echo "Python not found: $PY"; exit 1; }

MODE=${BLOCKCHAIN_MODE:-evm}
echo "== HH Goa Task 3 — $0 on Mac =="
echo "Mode: SEARCH_MODE=${SEARCH_MODE:-live}  BLOCKCHAIN_MODE=$MODE  PY=$PY"
if [ -z "${SERPAPI_API_KEY:-}" ]; then
  echo "  ⚠ SERPAPI_API_KEY missing — get free 250 at serpapi.com/users/sign_up (email only, no card)"
  exit 1
fi
echo "  ✓ SERPAPI_API_KEY set (live Lens)"
if [ "$MODE" = "evm" ] && [ -z "${EVM_PRIVATE_KEY:-}" ]; then
  echo "  ⚠ EVM_PRIVATE_KEY missing — generate throwaway + fund 0.2 POL at faucet.polygon.technology"
  exit 1
fi
if [ "$MODE" = "evm" ]; then echo "  ✓ EVM_PRIVATE_KEY set (live Amoy)"; fi

IMG="${1:-data/samples/lena.jpg}"
[ -f "$IMG" ] || { echo "  ⚠ image not found: $IMG"; exit 1; }
mkdir -p outputs

echo ""
echo "1) Face scan (YuNet) → 2) Live Reddit-aware Lens search → 3) Polygon Amoy anchor → 4) Verify"
"$PY" -m src.pipeline --image "$IMG" --out outputs --chain chain.json

echo ""
echo "Outputs: outputs/result.json outputs/evidence.json outputs/receipt.json outputs/face_crop.jpg outputs/search_raw.json chain.json evm_mirror.json"
echo ""
FP=$(awk -F'"' '/fingerprint_sha256/ {print $4; exit}' outputs/evidence.json)
if [ -z "${FP:-}" ]; then echo "  ⚠ no fingerprint in outputs/evidence.json"; exit 1; fi
echo "Verify just-anchored fingerprint (should be True):"
"$PY" -m src.pipeline --verify "$FP" --chain chain.json || true

echo ""
echo "Verify tampered (last char flipped) — should be False:"
LAST=${FP: -1}
if [ "$LAST" = "0" ]; then NEW=1; else NEW=0; fi
BAD="${FP%?}$NEW"
"$PY" -m src.pipeline --verify "$BAD" --chain chain.json || true
echo "Done."
