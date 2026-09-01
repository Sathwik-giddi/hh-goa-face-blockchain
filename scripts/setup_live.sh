#!/bin/zsh
# LIVE setup — $0, no card — run this line-by-line
set -e
cd /Users/sathwik/hh-goa-face-blockchain

echo "=== 1. SerpAPI key (Google Lens, free 250/mo, email only) ==="
echo "Step A: open https://serpapi.com/users/sign_up  → sign up with email → verify inbox"
echo "Step B: open https://serpapi.com/manage-api-key → copy API Key (starts with ...)"
echo "Step C: paste below (or edit .env manually)"
echo ""
if [ -z "$SERPAPI_API_KEY" ]; then
  echo -n "Paste SERPAPI_API_KEY (or press Enter to skip for now): "
  read key
  if [ -n "$key" ]; then
    echo "SERPAPI_API_KEY=$key" > .env.tmp
    # keep other vars
    [ -f .env ] && grep -v SERPAPI_API_KEY .env >> .env.tmp || true
    mv .env.tmp .env
    echo "✓ Saved to .env"
  else
    echo "Skipped — run: echo 'SERPAPI_API_KEY=xxx' > .env"
  fi
else
  echo "✓ SERPAPI_API_KEY already in env"
  echo "SERPAPI_API_KEY=${SERPAPI_API_KEY:0:8}..."
fi

echo ""
echo "=== 2. Polygon Amoy wallet (free faucet, no mainnet ETH needed) ==="
if ! /opt/homebrew/bin/python3.11 -c "import eth_account" 2>/dev/null; then
  echo "Installing web3..."
  /opt/homebrew/bin/python3.11 -m pip install --quiet web3 eth-account
fi
WALLET_OUT=$(/opt/homebrew/bin/python3.11 -c "from eth_account import Account; a=Account.create(); print(a.address); print(a.key.hex())")
ADDR=$(echo "$WALLET_OUT" | sed -n '1p')
PK=$(echo "$WALLET_OUT" | sed -n '2p')
echo "Generated throwaway (DO NOT use for mainnet):"
echo "  ADDRESS=$ADDR"
echo "  PRIVATE_KEY=$PK"
echo ""
echo "Step A: open https://faucet.polygon.technology/ → select 'Amoy' → paste $ADDR → 0.2 POL free (instant)"
echo "Step B: wait 10s, then we save to .env"
echo -n "Press Enter after faucet says 'Tokens sent'... "
read dummy
cat >> .env <<EOF
EVM_PRIVATE_KEY=$PK
EVM_RPC_URL=https://rpc-amoy.polygon.technology
BLOCKCHAIN_MODE=evm
EVM_CHAIN_ID=80002
EOF
echo "✓ Saved EVM_PRIVATE_KEY to .env (gitignored)"

echo ""
echo "=== 3. Test LIVE ==="
echo "Running: python -m src.pipeline --image data/samples/lena.jpg"
cat .env | grep -E "SERPAPI|EVM|BLOCKCHAIN" | sed 's/=.*/=***/'
/opt/homebrew/bin/python3.11 -m src.pipeline --image data/samples/lena.jpg --out outputs --chain chain.json
echo ""
echo "✓ Check outputs/search_raw.json has real search_metadata.id"
echo "✓ Check outputs/receipt.json explorerUrl → amoy.polygonscan.com/tx/0x..."
