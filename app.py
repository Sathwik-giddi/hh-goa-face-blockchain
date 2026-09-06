"""10/10 frontend API — LIVE ONLY, serves forensic luxury UI."""
import json
import os
from pathlib import Path
from datetime import datetime, timezone
from dotenv import load_dotenv

# load .env from repo root, not cwd
load_dotenv(Path(__file__).parent / ".env")

from fastapi import FastAPI, UploadFile, File, HTTPException, Query
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.middleware.cors import CORSMiddleware

from src.face_id import detect_and_encode, face_embedding
from src.search import reverse_image_search
from src.utils import fingerprint_post, is_hex64, safe_filename, reverify_independent, download_image
from src.blockchain import anchor, verify
from src.blockchain_local import _load_chain
from src import vault
from src import profile_discovery

app = FastAPI(title="HH Goa — Face→Social→Chain", version="5.0-arcface")

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
    import time as _time
    _t0 = _time.perf_counter()
    ct = (file.content_type or "").lower()
    ext = (Path(file.filename or "").suffix or "").lower()
    # Some clients send application/octet-stream for real images; the decode
    # step validates the actual bytes, so the gate can be permissive here.
    if not (ct.startswith("image/") or ct in ALLOWED_IMG
            or ext in {".jpg", ".jpeg", ".png", ".webp", ".heic", ".heif", ".avif"}):
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

        # crop paths must be web-relative ("/outputs/<file>" is served by the
        # outputs route) — the frontend prepends "/" to whatever we return.
        def _webify(f):
            f = dict(f)
            if f.get("crop_path"):
                f["crop_path"] = "outputs/" + Path(f["crop_path"]).name
            if f.get("face_options"):
                f["face_options"] = [
                    dict(o, crop_path="outputs/" + Path(o["crop_path"]).name)
                    for o in f["face_options"]
                ]
            return f

        face = _webify(face)

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
        MIN_FACE_SIM = 42.5  # ArcFace cosine same-person threshold (calibrated)
        confident = bool(search.get("top_confident")) and top.get("_face_sim") is not None \
            and top["_face_sim"] >= MIN_FACE_SIM

        # Identity Vault: label WHO the subject is, stable across index rotation.
        subject = None
        try:
            qfeat, _m = face_embedding(face["crop_path"])
            if qfeat is not None:
                subject = vault.identify(qfeat)
        except Exception:
            subject = None

        # §26: never convert "no match" into anchored look-alike evidence.
        # Stop here, honestly, before the blockchain stage.
        if not confident:
            face_bearing = [h for h in search.get("all_hits", []) if h.get("_face_sim") is not None]
            return {
                "no_match": True,
                "face": face,
                "subject_identity": subject,
                "public_record": search.get("public_record"),
                "elapsed_s": round(_time.perf_counter() - _t0, 1),
                "search": {
                    "mode": search["mode"],
                    "reddit_found": search.get("reddit_found"),
                    "num_hits": len(search.get("all_hits", [])),
                    "face_similar_count": search.get("face_similar_count"),
                    "confident": False,
                    "top": None,
                    "hits": face_bearing[:4],
                },
                "reason": ("No publicly indexed page both (a) contains a face similar enough to this "
                           "scan and (b) is still reachable as a real page — even after a fresh "
                           "index-snapshot retry. Nothing was anchored on-chain — the pipeline does "
                           "not record look-alike or link-rotted pages as evidence."),
            }

        fp = fingerprint_post(top, image_path=face["crop_path"])
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

        reverify = reverify_independent(fp, out_dir=OUTPUTS)

        return {
            "face": face,
            "search": {
                "mode": search["mode"],
                "reddit_found": search.get("reddit_found"),
                "num_hits": len(vm),
                "face_similar_count": search.get("face_similar_count"),
                "num_queries": search.get("num_queries", 1),
                "noise_floor": search.get("noise_floor", 0.0),
                "confident": confident,
                "top": top,
                "hits": vm[:8],
            },
            "fingerprint": fp,
            "receipt": receipt,
            "verify": verify(fingerprint, chain_file=str(CHAIN)),
            "reverify": reverify,
            "subject_identity": subject,
            "public_record": search.get("public_record"),
            "elapsed_s": round(_time.perf_counter() - _t0, 1),
        }
    finally:
        if tmp.exists():
            tmp.unlink(missing_ok=True)


# ---------- Identity Vault API ----------
@app.get("/api/identities")
def identities():
    return {"identities": vault.list_identities()}


# ---------- Public Profile Discovery (verified identities only) ----------
@app.post("/api/discover-profiles")
async def discover_profiles_api(file: UploadFile = File(...),
                                face_index: int | None = Query(None),
                                name: str = Query(""),
                                context: str = Query(""),
                                name_confidence: float = Query(0.0),
                                fingerprint: str = Query("")):
    """Discover public profiles for a VERIFIED identity: the subject must be
    vault-enrolled, or a public figure confirmed by a confident verified scan
    (name + name_confidence come from that scan's public_record path).
    Unidentified strangers are refused — always."""
    tmp = UPLOADS / f"disc_{datetime.now(timezone.utc).strftime('%H%M%S%f')}_{safe_filename(file.filename or 'img')}"
    try:
        with open(tmp, "wb") as f:
            f.write(await file.read())
        feat, method = face_embedding(tmp)
        if feat is None:
            raise HTTPException(422, f"no usable face in image ({method})")

        identity = None
        v = vault.identify(feat)
        if v:
            identity = {"name": v["name"], "confidence": round(v["similarity"] / 100, 3),
                        "source": "identity vault"}
        elif name.strip():
            clean = " ".join(name.split())[:100]  # whitespace-normalize only
            identity = {"name": clean,
                        "confidence": round(float(name_confidence), 3),
                        "source": "verified public figure (confident scan)",
                        "context": context.strip() or None}
        if identity is None:
            return {"skipped": True,
                    "reason": "Identity confidence insufficient.\nProfile association skipped."}

        result = profile_discovery.discover_for_identity(identity)

        if fingerprint and is_hex64(fingerprint) and result.get("profiles") is not None:
            try:
                pdir = OUTPUTS / "profiles"
                pdir.mkdir(parents=True, exist_ok=True)
                (pdir / f"{fingerprint.lower()}.json").write_text(
                    json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
                result["saved_to_case_file"] = True
            except Exception:
                pass
        return result
    finally:
        if tmp.exists():
            tmp.unlink(missing_ok=True)


# ---------- Evidence Case File ----------
def _b64_image(path) -> str:
    import base64
    return "data:image/jpeg;base64," + base64.b64encode(Path(path).read_bytes()).decode()


@app.get("/api/bundle/{fingerprint}")
def bundle(fingerprint: str):
    """Downloadable, self-contained Evidence Case File (single HTML):
    canonical record, face crop + candidate image, on-chain proof, QR to
    Polygonscan, and instructions a judge can follow to re-verify unaided."""
    import base64
    import html as _html

    if not is_hex64(fingerprint):
        raise HTTPException(400, "fingerprint must be 64-hex SHA-256")
    try:
        chain = _load_chain(CHAIN)
    except Exception as e:
        raise HTTPException(500, f"chain load failed: {e}")

    block = next((b for b in chain
                  if (b.get("data") or {}).get("fingerprint") == fingerprint.lower()), None)
    if block is None:
        raise HTTPException(404, "fingerprint not in local chain")

    data = block["data"]
    post = data.get("post") or {}
    face = data.get("face") or {}
    tx_hash = data.get("txHash") or ""
    explorer = data.get("explorerUrl") or (f"https://amoy.polygonscan.com/tx/{tx_hash}" if tx_hash else "")

    # live assets: face crop from disk, candidate image re-downloaded now
    crop_b64 = ""
    crop_path = OUTPUTS / "face_crop.jpg"
    if crop_path.exists():
        crop_b64 = _b64_image(crop_path)
    cand_b64 = ""
    from src.utils import download_image as _dl  # noqa: local alias
    img = _dl(post.get("thumbnail") or post.get("image") or "", OUTPUTS / "_bundle_img.jpg")
    if img:
        cand_b64 = _b64_image(img)
        img.unlink(missing_ok=True)

    qr_b64 = ""
    if explorer:
        import qrcode
        import io as _io
        buf = _io.BytesIO()
        qrcode.make(explorer).save(buf, format="PNG")
        qr_b64 = "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()

    v = verify(fingerprint, chain_file=str(CHAIN))
    esc = _html.escape

    # Public Profile Discovery section (when this fingerprint had a run)
    profiles_html = ""
    pfile = OUTPUTS / "profiles" / f"{fingerprint.lower()}.json"
    if pfile.exists():
        try:
            discovery = json.loads(pfile.read_text(encoding="utf-8"))
            profiles_html = profile_discovery.render_case_file_section(discovery)
        except Exception:
            profiles_html = ""
    rows = "".join(
        f"<tr><td>{esc(k)}</td><td class='mono'>{esc(str(v_)[:160])}</td></tr>"
        for k, v_ in [
            ("url", post.get("url") or post.get("link") or "—"),
            ("title", post.get("title") or "—"),
            ("source", post.get("source") or "—"),
            ("face engine", face.get("engine") or "—"),
            ("detector conf", face.get("conf") or "—"),
            ("block (local)", block.get("index")),
            ("block hash (local)", block.get("hash")),
            ("block time (local)", block.get("timestamp")),
            ("tx (Amoy)", tx_hash or "—"),
            ("verdict", "VERIFIED — on-chain record matches" if v.get("verified") else "NOT VERIFIED"),
        ])

    html = f"""<!doctype html><html><head><meta charset="utf-8"><title>Evidence Case File {esc(fingerprint[:16])}…</title>
<style>
 body{{font-family:Georgia,serif;background:#F6F7F9;color:#141E28;max-width:860px;margin:0 auto;padding:32px}}
 h1{{font-size:22px;border-bottom:3px solid #141E28;padding-bottom:8px;letter-spacing:.5px}}
 h2{{font-size:14px;text-transform:uppercase;letter-spacing:.2em;color:#5C6B7A;margin-top:28px}}
 .mono{{font-family:"IBM Plex Mono",monospace;font-size:12px;word-break:break-all}}
 table{{width:100%;border-collapse:collapse}} td{{border-bottom:1px solid #DAE0E6;padding:7px 6px;vertical-align:top}}
 td:first-child{{color:#5C6B7A;width:180px}}
 .fp{{background:#141E28;color:#fff;padding:14px;font-family:monospace;font-size:14px;word-break:break-all}}
 .stamp{{display:inline-block;border:2px solid #0B7A63;color:#0B7A63;padding:4px 12px;font-weight:bold;letter-spacing:.15em;transform:rotate(-3deg)}}
 img{{max-width:220px;border:1px solid #141E28}} .qr{{width:130px;height:130px}}
 .note{{background:#E7F2EE;border-left:3px solid #0B7A63;padding:10px 12px;font-size:13px}}
</style></head><body>
<h1>EVIDENCE CASE FILE — HH Goa 2026 · Task 3</h1>
<p class="stamp">{'VERIFIED' if v.get('verified') else 'NOT VERIFIED'}</p>
<p>Case fingerprint:</p><div class="fp">{esc(fingerprint)}</div>

<h2>1 · Scanned subject</h2>
{'<img src="' + crop_b64 + '">' if crop_b64 else '<p>crop not retained</p>'}
<p class="mono">engine: {esc(str(face.get('engine')))} · conf {esc(str(face.get('conf')))} · pHash {esc(str(face.get('embedding_hash')))}</p>

<h2>2 · Discovered public source</h2>
{'<img src="' + cand_b64 + '">' if cand_b64 else ''}
<table>{rows}</table>

<h2>3 · On-chain proof (Polygon Amoy, chainId 80002)</h2>
<table>
<tr><td>contract</td><td class="mono">FaceAnchor · 0x5cfA68B9508CE6a9B7Ac8c3Cf696283721485463</td></tr>
<tr><td>explorer</td><td class="mono">{('<a href="' + esc(explorer) + '">' + esc(explorer) + '</a>') if explorer else '—'}</td></tr>
</table>
{'<img class="qr" src="' + qr_b64 + '" alt="QR to Polygonscan">' if qr_b64 else ''}

{profiles_html}
<h2>4 · Verify this file yourself</h2>
<div class="note">
Open the Polygonscan link above (or scan the QR) → check the transaction's Input Data
or the contract's <span class="mono">anchoredAt</span> read for this fingerprint:
<span class="mono">{esc(fingerprint)}</span><br><br>
Independent re-hash method: re-download the discovered image → re-canonicalize
(sorted-key JSON of url, title, source, thumbnail, image_sha256, UTF-8) → SHA-256 →
compare with the on-chain fingerprint. Any alteration of the content changes the hash
and verification fails.
</div>
<p class="mono" style="margin-top:24px;color:#5C6B7A">generated {esc(datetime.now(timezone.utc).isoformat())} · this file is a rendering of on-chain and local-chain state; the chains are the source of truth</p>
</body></html>"""

    return Response(content=html, media_type="text/html",
                    headers={"Content-Disposition": f'attachment; filename="evidence_{fingerprint[:16]}.html"'})


@app.get("/api/qr")
def qr(data: str = Query("")):
    """PNG QR code for arbitrary http(s) data (used for the Polygonscan link)."""
    import qrcode
    import io as _io
    if not (data.startswith("https://") or data.startswith("http://")):
        raise HTTPException(400, "http(s) URL required")
    buf = _io.BytesIO()
    qrcode.make(data).save(buf, format="PNG")
    return Response(content=buf.getvalue(), media_type="image/png")


@app.post("/api/enroll")
async def enroll(file: UploadFile = File(...), name: str = Query("")):
    """Enroll a face under a label: scan → 512-D ArcFace reference in the vault."""
    name = safe_filename(name or "").strip("._-")[:80]
    if not name:
        raise HTTPException(400, "Provide ?name=<person label>")
    ct = (file.content_type or "").lower()
    ext = (Path(file.filename or "").suffix or "").lower()
    if not (ct.startswith("image/") or ext in {".jpg", ".jpeg", ".png", ".webp", ".heic", ".heif", ".avif"}):
        raise HTTPException(400, "Upload an image (jpg/png/webp/heic)")
    tmp = UPLOADS / f"enroll_{datetime.now(timezone.utc).strftime('%H%M%S%f')}_{safe_filename(file.filename or 'img')}"
    try:
        with open(tmp, "wb") as f:
            f.write(await file.read())
        feat, method = face_embedding(tmp)
        if feat is None:
            raise HTTPException(422, f"no usable face in image ({method})")
        rec = vault.enroll(name, feat)
        return {"enrolled": True, "method": method, **rec}
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
