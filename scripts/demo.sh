#!/bin/zsh
# $0 demo — run this for screen recording (no edit needed)
set -e
echo "== HH Goa Task 3 — $0 on Mac =="
echo "Env:"
echo "  SEARCH_MODE=${SEARCH_MODE:-live}  BLOCKCHAIN_MODE=${BLOCKCHAIN_MODE:-local}"
echo "  SERPAPI_API_KEY=${SERPAPI_API_KEY:+set (free 250)} / ${SERPAPI_API_KEY:-missing → mock (get at serpapi.com)}"
echo ""
echo "1) Face scan → 2) Reddit-aware Lens search → 3) Anchor → 4) Verify"
echo "Running..."
/opt/homebrew/bin/python3.11 -m src.pipeline --image "${1:-data/samples/face_sample.jpg}" --out outputs --chain chain.json
echo ""
echo "Outputs: outputs/result.json outputs/evidence.json outputs/receipt.json outputs/face_crop.jpg outputs/search_raw.json"
echo "Verify tampered hash (should be false):"
/opt/homebrew/bin/python3.11 -m src.pipeline --verify deadbeef --chain chain.json || true
echo "Done."
