"""10/10 frontend API — LIVE ONLY, serves forensic luxury UI."""
import os
import json
import shutil
import hashlib
from pathlib import Path
from datetime import datetime, timezone
from dotenv import load_dotenv
load_dotenv()

from fastapi import FastAPI, UploadFile, File, HTTPException
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware

from src.face_id import detect_and_encode
from src.search import reverse_image_search
from src.utils import fingerprint_post
from src.blockchain import anchor, verify
from src.blockchain_local import _load_chain

app = FastAPI(title="HH Goa — Face→Social→Chain", version="1.0-live")

app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

FRONTEND = Path("frontend")
OUTPUTS = Path("outputs")
CHAIN = Path("chain.json")

@app.get("/api/health")
def health():
    chain = _load_chain(CHAIN) if CHAIN.exists() else []
    return {"ok": True, "chain_height": len(chain), "mode": os.getenv("BLOCKCHAIN_MODE","local"), "live": bool(os.getenv("SERPAPI_API_KEY"))}

@app.get("/api/chain")
def chain_view():
    c = _load_chain(CHAIN) if CHAIN.exists() else []
    return {"chain": c[-10:][::-1], "height": len(c)}

@app.get("/api/verify")
def verify_api(hash: str):
    v = verify(hash, chain_file=str(CHAIN))
    return v

@app.post("/api/scan")
async def scan(file: UploadFile = File(...)):
    if not file.content_type or not file.content_type.startswith("image/"):
        raise HTTPException(400, "Upload an image (jpg/png/webp)")
    tmp = Path(f"outputs/_upload_{datetime.now(timezone.utc).strftime('%H%M%S')}_{file.filename}")
    tmp.parent.mkdir(parents=True, exist_ok=True)
    with open(tmp, "wb") as f:
        shutil.copyfileobj(file.file, f)

    # 1 face
    face = detect_and_encode(tmp, out_dir=OUTPUTS)
    if face["num_faces"] == 0:
        # still proceed for demo, but flag
        pass

    # 2 live search — face re-ranked, tries crop + original
    try:
        search = reverse_image_search(face["crop_path"], original_path=str(tmp))
    except Exception as e:
        raise HTTPException(500, f"Live search failed: {e}. Set SERPAPI_API_KEY in .env (free 250 at serpapi.com)")

    vm = search.get("visual_matches", [])
    top = search.get("top_match")
    if not top:
        raise HTTPException(404, "Live Lens returned 0 hits — try a public face (celebrity/IG). Private images aren't indexed.")

    # 3 fingerprint + anchor
    fp = fingerprint_post(top, image_path=face["crop_path"])
    fingerprint = fp["fingerprint_sha256"]
    payload = {"post": top, "face": {k: face[k] for k in ["engine","conf","bbox","embedding_hash"] if k in face}, "search_mode": search["mode"], "reddit_found": search.get("reddit_found")}
    receipt = anchor(fingerprint, payload, chain_file=str(CHAIN))

    # also save live outputs for API
    return {
        "face": face,
        "search": {"mode": search["mode"], "reddit_found": search.get("reddit_found"), "num_hits": len(vm), "top": top, "hits": vm[:8]},
        "fingerprint": fp,
        "receipt": receipt,
        "verify": verify(fingerprint, chain_file=str(CHAIN))
    }

# serve frontend last
if FRONTEND.exists():
    app.mount("/assets", StaticFiles(directory=str(FRONTEND)), name="assets")

@app.get("/")
def index():
    p = FRONTEND / "index.html"
    if p.exists():
        return FileResponse(str(p))
    return JSONResponse({"hint": "Frontend not built yet. Run pipeline via POST /api/scan or CLI: python -m src.pipeline --image data/samples/lena.jpg"})

# also serve outputs for preview
@app.get("/outputs/{path:path}")
def outputs_file(path: str):
    fp = OUTPUTS / path
    if not fp.exists() or not str(fp.resolve()).startswith(str(OUTPUTS.resolve())):
        raise HTTPException(404)
    return FileResponse(str(fp))
