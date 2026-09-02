"""10/10 frontend API — LIVE ONLY, serves forensic luxury UI."""
import os
import json
import re
import shutil
from pathlib import Path
from datetime import datetime, timezone
from dotenv import load_dotenv

# load .env from repo root, not cwd
load_dotenv(Path(__file__).parent / ".env")

from fastapi import FastAPI, UploadFile, File, HTTPException, Query
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware

from src.face_id import detect_and_encode
from src.search import reverse_image_search
from src.utils import fingerprint_post, is_hex64, safe_filename
from src.blockchain import anchor, verify
from src.blockchain_local import _load_chain

app = FastAPI(title="HH Goa — Face→Social→Chain", version="4.1-sface")

# CORS: lock to local dev origins (override via ALLOWED_ORIGINS env if needed)
allowed = os.getenv("ALLOWED_ORIGINS", "http://127.0.0.1:8000,http://localhost:8000,http://127.0.0.1:3000,http://localhost:3000")
app.add_middleware(
    CORSMiddleware,
    allow_origins=[o.strip() for o in allowed.split(",") if o.strip()],
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["Content-Type", "X-Requested-With"],
    allow_credentials=False,
)

REPO_ROOT = Path(__file__).parent
FRONTEND = REPO_ROOT / "frontend"
UPLOADS = REPO_ROOT / "outputs" / "_uploads"
UPLOADS.mkdir(parents=True, exist_ok=True)
OUTPUTS = REPO_ROOT / "outputs"
CHAIN = Path(os.getenv("CHAIN_FILE", str(REPO_ROOT / "chain.json")))

ALLOWED_IMG = {"image/jpeg", "image/jpg", "image/png", "image/webp", "image/heic", "image/avif"}
MAX_UPLOAD_BYTES = 10 * 1024 * 1024  # 10MB cap


def _hex64(s: str) -> bool:
    return is_hex64(s)


@app.get("/api/health")
def health():
    try:
        chain = _load_chain(CHAIN) if CHAIN.exists() else []
        height = len(chain)
    except Exception as e:
        return {"ok": False, "error": f"chain load failed: {e}"}
    return {
        "ok": True,
        "version": app.version,
        "chain_height": height,
        "mode": os.getenv("BLOCKCHAIN_MODE", "local"),
        "live": bool(os.getenv("SERPAPI_API_KEY")),
    }


@app.get("/api/chain")
def chain_view():
    try:
        c = _load_chain(CHAIN) if CHAIN.exists() else []
    except Exception as e:
        raise HTTPException(500, f"chain load failed: {e}")
    return {"chain": c[-10:][::-1], "height": len(c)}


@app.get("/api/verify")
def verify_api(hash: str = Query("")):
    if not _hex64(hash):
        raise HTTPException(400, "hash must be 64-hex SHA-256")
    try:
        return verify(hash, chain_file=str(CHAIN))
    except Exception as e:
        raise HTTPException(500, f"verify failed: {e}")


@app.post("/api/scan")
async def scan(file: UploadFile = File(...), face_index: int | None = Query(None)):
    ct = (file.content_type or "").lower()
    if not (ct.startswith("image/") or ct in ALLOWED_IMG):
        raise HTTPException(400, "Upload an image (jpg/png/webp/heic)")

    safe = safe_filename(file.filename or "img")
    # unique per-request — no race
    tmp = UPLOADS / f"upload_{datetime.now(timezone.utc).strftime('%H%M%S%f')}_{safe}"
    size = 0
    try:
        with open(tmp, "wb") as f:
            while True:
                chunk = await file.read(64 * 1024)
                if not chunk:
                    break
                size += len(chunk)
                if size > MAX_UPLOAD_BYTES:
                    raise HTTPException(413, f"file too large (max {MAX_UPLOAD_BYTES // 1024 // 1024}MB)")
                f.write(chunk)
    except HTTPException:
        if tmp.exists():
            tmp.unlink(missing_ok=True)
        raise
    except Exception as e:
        if tmp.exists():
            tmp.unlink(missing_ok=True)
        raise HTTPException(500, f"upload failed: {e}")

    if size == 0:
        if tmp.exists():
            tmp.unlink(missing_ok=True)
        raise HTTPException(400, "empty file")

    try:
        face = detect_and_encode(tmp, out_dir=OUTPUTS, face_index=face_index)
        if not face.get("crop_path"):
            raise HTTPException(422, face.get("warning", "no face detected"))

        # Multiple faces and no explicit choice → ask the user to pick.
        opts = face.get("face_options") or []
        if len(opts) > 1 and face_index is None:
            return {
                "multi_face": True,
                "num_faces": face["num_faces"],
                "faces": opts,
            }

        try:
            search = reverse_image_search(face["crop_path"], original_path=str(tmp))
        except FileNotFoundError as e:
            raise HTTPException(500, f"search input missing: {e}")
        except Exception as e:
            raise HTTPException(500, f"Live search failed: {e}. Set SERPAPI_API_KEY in .env (free 250 at serpapi.com).")

        vm = search.get("visual_matches", [])
        top = search.get("top_match")
        if not top:
            raise HTTPException(404, "Live Lens returned 0 hits — no public indexed copy of this face. Try a publicly posted image (IG/X/Reddit).")
        MIN_FACE_SIM = 36.3  # SFace same-person cosine threshold
        confident = top.get("_face_sim") is not None and top["_face_sim"] >= MIN_FACE_SIM

        # §26: never convert "no match" into anchored look-alike evidence.
        # Stop here, honestly, before the blockchain stage.
        if not confident:
            face_bearing = [h for h in search.get("all_hits", []) if h.get("_face_sim") is not None]
            return {
                "no_match": True,
                "face": face,
                "search": {
                    "mode": search["mode"],
                    "reddit_found": search.get("reddit_found"),
                    "num_hits": len(search.get("all_hits", [])),
                    "face_similar_count": search.get("face_similar_count"),
                    "confident": False,
                    "top": None,
                    "hits": face_bearing[:4],
                },
                "reason": ("No publicly indexed content contains a similar enough face to this scan "
                           "(best candidates had no comparable face). Nothing was anchored on-chain — "
                           "the pipeline does not record look-alike pages as evidence."),
            }

        fp = fingerprint_post(top, image_path=face["crop_path"])
        from src.utils import download_image
        post_image = download_image(top.get("thumbnail") or "", OUTPUTS / "_post_image.jpg") \
            or (download_image(top.get("image"), OUTPUTS / "_post_image.jpg") if top.get("image") else None)
        if post_image:
            fp = fingerprint_post(top, image_path=post_image)
        fingerprint = fp["fingerprint_sha256"]
        payload = {
            "post": top,
            "face": {k: face[k] for k in ["engine", "conf", "bbox", "embedding_hash"] if k in face},
            "search_mode": search["mode"],
            "reddit_found": search.get("reddit_found"),
            "face_similar_count": search.get("face_similar_count"),
        }
        try:
            receipt = anchor(fingerprint, payload, chain_file=str(CHAIN))
        except Exception as e:
            raise HTTPException(500, f"anchor failed: {e}")

        from src.utils import reverify_independent
        reverify = reverify_independent(fp, out_dir=OUTPUTS)

        return {
            "face": face,
            "search": {
                "mode": search["mode"],
                "reddit_found": search.get("reddit_found"),
                "num_hits": len(vm),
                "face_similar_count": search.get("face_similar_count"),
                "confident": confident,
                "top": top,
                "hits": vm[:8],
            },
            "fingerprint": fp,
            "receipt": receipt,
            "verify": verify(fingerprint, chain_file=str(CHAIN)),
            "reverify": reverify,
        }
    finally:
        if tmp.exists():
            tmp.unlink(missing_ok=True)


# Static mount — only AFTER all API routes
if FRONTEND.exists():
    app.mount("/static", StaticFiles(directory=str(FRONTEND)), name="static")


@app.get("/")
def index():
    p = FRONTEND / "index.html"
    if p.exists():
        return FileResponse(str(p))
    return JSONResponse({"hint": "frontend missing"}, status_code=500)


@app.get("/outputs/{path:path}")
def outputs_file(path: str):
    fp = OUTPUTS / path
    try:
        resolved = fp.resolve(strict=False)
        out_resolved = OUTPUTS.resolve()
        # Python 3.9+: is_relative_to
        if not resolved.is_relative_to(out_resolved):
            raise HTTPException(404)
    except (ValueError, RuntimeError):
        raise HTTPException(404)
    if not fp.exists() or not fp.is_file():
        raise HTTPException(404)
    return FileResponse(str(fp))
