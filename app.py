"""10/10 frontend API — LIVE ONLY, serves forensic luxury UI."""
import os
import json
import shutil
import re
import hashlib
from pathlib import Path
from datetime import datetime, timezone
from dotenv import load_dotenv
load_dotenv()

from fastapi import FastAPI, UploadFile, File, HTTPException, Query
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware

from src.face_id import detect_and_encode
from src.search import reverse_image_search
from src.utils import fingerprint_post
from src.blockchain import anchor, verify
from src.blockchain_local import _load_chain

app = FastAPI(title="HH Goa — Face→Social→Chain", version="2.0-live")

app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

FRONTEND = Path("frontend")
OUTPUTS = Path("outputs")
CHAIN = Path("chain.json")

ALLOWED_IMG = {"image/jpeg", "image/jpg", "image/png", "image/webp", "image/heic"}


@app.get("/api/health")
def health():
    chain = _load_chain(CHAIN) if CHAIN.exists() else []
    return {
        "ok": True,
        "chain_height": len(chain),
        "mode": os.getenv("BLOCKCHAIN_MODE", "local"),
        "live": bool(os.getenv("SERPAPI_API_KEY")),
    }


@app.get("/api/chain")
def chain_view():
    c = _load_chain(CHAIN) if CHAIN.exists() else []
    return {"chain": c[-10:][::-1], "height": len(c)}


def _hex64_ok(s: str) -> bool:
    return bool(s) and len(s) == 64 and bool(re.match(r"^[0-9a-fA-F]{64}$", s))


@app.get("/api/verify")
def verify_api(hash: str = Query("")):
    if not _hex64_ok(hash):
        raise HTTPException(400, "hash must be 64-hex SHA-256 (got: '" + hash + "')")
    return verify(hash, chain_file=str(CHAIN))


@app.post("/api/scan")
async def scan(file: UploadFile = File(...)):
    ct = (file.content_type or "").lower()
    if not (ct.startswith("image/") or ct in ALLOWED_IMG or ct == ""):
        raise HTTPException(400, "Upload an image (jpg/png/webp)")

    safe = re.sub(r"[^A-Za-z0-9._-]", "_", file.filename or "img")
    tmp = Path(f"outputs/_upload_{datetime.now(timezone.utc).strftime('%H%M%S%f')}_{safe}")
    tmp.parent.mkdir(parents=True, exist_ok=True)
    with open(tmp, "wb") as f:
        shutil.copyfileobj(file.file, f)

    if tmp.stat().st_size == 0:
        raise HTTPException(400, "Empty file")

    # 1 face detect
    face = detect_and_encode(tmp, out_dir=OUTPUTS)
    if face.get("num_faces", 0) == 0:
        # still try search on full image
        pass

    # 2 live search (face re-rank)
    try:
        search = reverse_image_search(face["crop_path"], original_path=str(tmp))
    except Exception as e:
        raise HTTPException(500, f"Live search failed: {e}. Set SERPAPI_API_KEY in .env (free 250 at serpapi.com)")

    vm = search.get("visual_matches", [])
    top = search.get("top_match")
    if not top:
        raise HTTPException(404, "Live Lens returned 0 hits — no public indexed copy of this face. Try a publicly posted image (IG/X/Reddit).")

    # 3 fingerprint + anchor
    fp = fingerprint_post(top, image_path=face["crop_path"])
    fingerprint = fp["fingerprint_sha256"]
    payload = {
        "post": top,
        "face": {k: face[k] for k in ["engine", "conf", "bbox", "embedding_hash"] if k in face},
        "search_mode": search["mode"],
        "reddit_found": search.get("reddit_found"),
        "face_similar_count": search.get("face_similar_count"),
    }
    receipt = anchor(fingerprint, payload, chain_file=str(CHAIN))

    return {
        "face": face,
        "search": {
            "mode": search["mode"],
            "reddit_found": search.get("reddit_found"),
            "num_hits": len(vm),
            "face_similar_count": search.get("face_similar_count"),
            "top": top,
            "hits": vm[:8],
        },
        "fingerprint": fp,
        "receipt": receipt,
        "verify": verify(fingerprint, chain_file=str(CHAIN)),
    }


# serve frontend
if FRONTEND.exists():
    app.mount("/static", StaticFiles(directory=str(FRONTEND)), name="static")


@app.get("/")
def index():
    p = FRONTEND / "index.html"
    if p.exists():
        return FileResponse(str(p))
    return JSONResponse({"hint": "Frontend missing"})


@app.get("/outputs/{path:path}")
def outputs_file(path: str):
    fp = OUTPUTS / path
    try:
        if not fp.exists() or not str(fp.resolve()).startswith(str(OUTPUTS.resolve())):
            raise HTTPException(404)
    except ValueError:
        raise HTTPException(400)
    return FileResponse(str(fp))
