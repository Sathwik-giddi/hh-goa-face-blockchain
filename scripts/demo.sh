#!/bin/zsh
# $0 demo — run this for screen recording (no edit needed, all live)
set -e
echo "== HH Goa Task 3 — $0 on Mac =="
echo "Mode: SEARCH_MODE=${SEARCH_MODE:-live}  BLOCKCHAIN_MODE=${BLOCKCHAIN_MODE:-evm}"
if [ -z "$SERPAPI_API_KEY" ]; then
  echo "  ⚠ SERPAPI_API_KEY missing — get free 250 at serpapi.com/users/sign_up (30s, email only, no card)"
  exit 1
fi
echo "  ✓ SERPAPI_API_KEY set (live Lens)"
if [ -z "$EVM_PRIVATE_KEY" ] && [ "${BLOCKCHAIN_MODE:-evm}" = "evm" ]; then
  echo "  ⚠ EVM_PRIVATE_KEY missing — generate throwaway + fund 0.2 POL at faucet.polygon.technology"
  exit 1
fi
echo "  ✓ EVM_PRIVATE_KEY set (live Amoy)"
echo ""
echo "1) Face scan (YuNet) → 2) Live Reddit-aware Lens search → 3) Polygon Amoy anchor → 4) Verify"
echo "Running..."
/opt/homebrew/bin/python3.11 -m src.pipeline --image "${1:-data/samples/lena.jpg}" --out outputs --chain chain.json
echo ""
echo "Outputs: outputs/result.json outputs/evidence.json outputs/receipt.json outputs/face_crop.jpg outputs/search_raw.json chain.json evm_mirror.json"
echo ""
echo "Verify the just-anchored fingerprint (should be True):"
FP=$(cat outputs/evidence.json | python3 -c "import json,sys; print(json.load(sys.stdin)['fingerprint_sha256'])")
/opt/homebrew/bin/python3.11 -m src.pipeline --verify "$FP" --chain chain.json
echo ""
echo "Verify tampered (last char flipped) — should be False:"
LAST=$(echo -n "$FP" | tail -c 1)
[ "$LAST" = "0" ] && NEW=1 || NEW=0
BAD="${FP%?}$NEW"
/opt/homebrew/bin/python3.11 -m src.pipeline --verify "$BAD" --chain chain.json || true
echo "Done."
