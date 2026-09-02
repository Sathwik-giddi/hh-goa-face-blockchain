"""Live reverse-image search — SerpAPI Google Lens + face-similarity re-rank.

Picks Reddit-aware ranking, supports Lens + google_reverse_image fallback, and
uses real 64-bit pHash Hamming distance (not sha256-of-bits) so any face on the
open web — not just celebrities — surfaces true matches.
"""
import io
import os
import tempfile
import threading
from pathlib import Path

import cv2
import numpy as np
import requests
from dotenv import load_dotenv

from .face_id import face_embedding, embedding_similarity

load_dotenv()

_THREAD_LOCAL = threading.local()


def _phash(image_path) -> str:
    """64-bit pHash, returns 16 hex chars.

    Returns empty string on failure. Thread-safe per-call (no shared state).
    """
    try:
        img = cv2.imread(str(image_path), cv2.IMREAD_GRAYSCALE)
        if img is None or img.size == 0:
            from PIL import Image
            pil = Image.open(image_path).convert("L")
            arr = np.array(pil)
            if arr.ndim != 2:
                return ""
            img = arr
        small = cv2.resize(img, (8, 8), interpolation=cv2.INTER_AREA)
        m = small.mean()
        bits = (small > m).flatten().astype(np.uint8)
        packed = int(np.packbits(bits).tobytes().hex(), 16)
        return f"{packed:016x}"
    except Exception:
        return ""


def _hamming(a: str, b: str) -> int:
    """Hamming distance on two 64-bit hex strings (returns 0..64)."""
    if not a or not b or len(a) != len(b):
        return 999
    try:
        return bin(int(a, 16) ^ int(b, 16)).count("1")
    except Exception:
        return 999


def _serpapi_upload(image_path: Path, api_key: str, timeout: int = 30) -> str:
    """Two-step upload: POST image -> image_id."""
    with open(image_path, "rb") as f:
        files = {"image": (image_path.name, f, "image/jpeg")}
        data = {"api_key": api_key}
        r = requests.post("https://serpapi.com/image", files=files, data=data, timeout=timeout)
    r.raise_for_status()
    j = r.json()
    image_id = j.get("image_id") or j.get("imageId")
    if not image_id:
        raise RuntimeError(f"SerpAPI upload no image_id: {j}")
    return image_id


def _lens_search(image_id: str, api_key: str, hl: str = "en", country: str = "us", timeout: int = 30) -> dict:
    params = {"engine": "google_lens", "image_id": image_id, "api_key": api_key, "hl": hl, "country": country}
    r = requests.get("https://serpapi.com/search.json", params=params, timeout=timeout)
    r.raise_for_status()
    return r.json()


def _reverse_image_search(image_id: str, api_key: str, timeout: int = 30) -> dict:
    params = {"engine": "google_reverse_image", "image_id": image_id, "api_key": api_key}
    r = requests.get("https://serpapi.com/search.json", params=params, timeout=timeout)
    r.raise_for_status()
    return r.json()


def _prepare_upload(image_path: Path, max_bytes: int = 480_000) -> Path:
    """SerpAPI image upload caps at 500KB — compress oversized query images.

    Returns the original path when small enough, else a temp JPEG (~1400px,
    quality stepped down until the cap fits). Content is unchanged for search.
    """
    import tempfile
    p = Path(image_path)
    if p.stat().st_size <= max_bytes:
        return p
    img = cv2.imread(str(p))
    if img is None:
        img = np.asarray(Image.open(p).convert("RGB"))[:, :, ::-1]
    h, w = img.shape[:2]
    if max(h, w) > 1400:
        s = 1400 / max(h, w)
        img = cv2.resize(img, (int(w * s), int(h * s)), interpolation=cv2.INTER_AREA)
    buf = None
    for q in (85, 78, 70, 60, 50):
        ok, enc = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, q])
        if ok and len(enc) <= max_bytes:
            buf = enc
            break
        buf = enc  # keep the smallest even if still over
    tmp = Path(tempfile.gettempdir()) / f"q_{p.stem}_{abs(hash(str(p))) % 9999}.jpg"
    tmp.write_bytes(buf.tobytes())
    return tmp


def _lens_hits(image_path: Path, api_key: str) -> tuple:
    """Upload one query image + run Lens (type=all: visual AND exact matches).

    Returns (image_id, deduped_hits, [raw]) so the image_id can be reused by
    the reverse-image fallback without a second upload.
    """
    upload_path = _prepare_upload(image_path)
    image_id = _serpapi_upload(upload_path, api_key)
    raw = _lens_search(image_id, api_key)
    vm = list(raw.get("visual_matches") or raw.get("image_results") or [])
    vm += list(raw.get("exact_matches") or [])
    return image_id, _dedupe(vm), [("lens:" + image_path.name, raw)]


def _reverse_hits(image_id: str, api_key: str) -> tuple:
    """google_reverse_image on an already-uploaded image_id → (hits, [raw])."""
    raw = _reverse_image_search(image_id, api_key)
    vm = list(raw.get("image_results") or raw.get("visual_matches") or [])
    return _dedupe(vm), [("reverse", raw)]


def _dedupe(vm: list) -> list:
    """Dedup by link (keep first); keep hits without link (thumbnail-only)."""
    seen = set()
    out = []
    for v in vm:
        link = v.get("link") or v.get("url") or ""
        key = link or ("nokey:" + (v.get("thumbnail") or ""))
        if key in seen:
            continue
        seen.add(key)
        out.append(v)
    return out


def _merge_hits(base: list, extra: list) -> list:
    """Union two hit lists, preserving order, deduped."""
    return _dedupe(list(base) + list(extra))


def _score_image_face(url: str, query_feat) -> tuple:
    """Download image, embed its largest face with SFace, return (similarity 0-100 or None, error).

    Real face recognition: matches the person, not the picture. Falls back to
    (None, error) when no face is found or the recognizer is unavailable.
    """
    if not url or query_feat is None:
        return None, "no url or query"
    try:
        r = requests.get(
            url, timeout=6,
            headers={"User-Agent": "Mozilla/5.0", "Accept": "image/*,*/*;q=0.5"},
        )
        if r.status_code >= 400:
            return None, f"HTTP {r.status_code}"
        arr = np.frombuffer(r.content, dtype=np.uint8)
        if arr.size == 0 or arr.size > 8 * 1024 * 1024:
            return None, "empty or too large"
        img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
        if img is None or img.size == 0:
            return None, "decode fail"
        feat, _why = face_embedding(img)
        if feat is None:
            return None, "no_face"
        return embedding_similarity(query_feat, feat), None
    except Exception as e:
        return None, str(e)[:40]


def _score_thumbnail_face(url: str, image_url: str, query_feat) -> tuple:
    """Score a candidate by its face, preferring the full-size image over the
    thumbnail (higher resolution → far more reliable embeddings).
    """
    if image_url and image_url != url:
        s, err = _score_image_face(image_url, query_feat)
        if s is not None:
            return s, None
    return _score_image_face(url, query_feat)


def reverse_image_search(
    image_path,
    original_path=None,
    api_key: str | None = None,
    prefer_source: str = "reddit",
    min_face_similarity: float = 42.5,  # ArcFace cosine threshold (calibrated: same 50-100, different 0-35)
) -> dict:
    """Live only. Re-ranks hits by SFace face-embedding similarity (parallel).

    Reach strategy: the face crop AND the original photo are both searched on
    Google Lens in parallel (each surfaces different copies of a person), with
    a reverse-image fallback on the crop when the merged pool is thin. Every
    candidate's face is embedded and compared against the query face.
    """
    image_path = Path(image_path)
    api_key = api_key or os.getenv("SERPAPI_API_KEY") or ""
    if not api_key:
        raise RuntimeError(
            "SERPAPI_API_KEY missing — get free 250 at serpapi.com/users/sign_up "
            "(email only, no card). No mock fallback in LIVE mode."
        )
    if not image_path.exists():
        raise FileNotFoundError(f"image not found: {image_path}")

    from concurrent.futures import ThreadPoolExecutor, wait

    query_hash = _phash(image_path)
    query_feat, embed_method = face_embedding(image_path)

    # Query 1 + 2 in parallel: face crop AND original photo on Lens.
    raws = []
    queries = []
    hits = []
    with ThreadPoolExecutor(max_workers=2) as ex:
        fut_a = ex.submit(_lens_hits, image_path, api_key)
        fut_b = ex.submit(_lens_hits, Path(original_path), api_key) \
            if original_path and Path(original_path).exists() \
            and _phash(Path(original_path)) != query_hash else None
        try:
            image_id_a, hits_a, raws_a = fut_a.result(timeout=45)
            hits = hits_a
            raws.extend(raws_a)
            queries.append({"image": image_path.name, "engine": "google_lens", "hits": len(hits_a)})
        except Exception as e:
            fut_b and fut_b.cancel()
            raise RuntimeError(f"Lens search failed: {e}") from e
        if fut_b:
            try:
                _idb, hits_b, raws_b = fut_b.result(timeout=45)
                raws.extend(raws_b)
                before = len(hits)
                hits = _merge_hits(hits, hits_b)
                queries.append({"image": Path(original_path).name, "engine": "google_lens",
                                 "hits": len(hits_b), "new_after_merge": len(hits) - before})
            except Exception as e:
                print(f"[search] original-image query failed (non-fatal): {e}")

    # Query 3 (only when the pool is thin): reverse-image on the crop.
    if len(hits) < 15:
        try:
            hits_c, raws_c = _reverse_hits(image_id_a, api_key)
            raws.extend(raws_c)
            queries.append({"image": image_path.name, "engine": "google_reverse_image", "hits": len(hits_c)})
            hits = _merge_hits(hits, hits_c)
        except Exception as e:
            print(f"[search] reverse_image fallback failed: {e}")

    if not hits:
        raise RuntimeError(
            f"Live search 0 hits for {image_path.name} — no public indexed copy. "
            "Try a face that is posted publicly (IG/X/Reddit). Private faces have no web hits by design."
        )

    # Score every candidate's face in parallel; hard deadline so stragglers
    # never stall the pipeline (unscored = honest None, not a fake number).
    sims = [None] * len(hits)
    with ThreadPoolExecutor(max_workers=12) as ex:
        futures = {ex.submit(_score_thumbnail_face, h.get("thumbnail") or "", h.get("image") or "", query_feat): i
                   for i, h in enumerate(hits)}
        done, _pending = wait(futures, timeout=35)
        for fut in done:
            i = futures[fut]
            try:
                s, _err = fut.result()
            except Exception:
                s = None
            sims[i] = s

    for h, s in zip(hits, sims):
        h["_face_sim"] = s

    prefer = (prefer_source or "reddit").lower()
    def rank(h):
        # Primary: face-embedding similarity (higher = better match).
        # Secondary: reddit preference; tertiary: original Lens position.
        s = h.get("_face_sim")
        sim_key = -(s if s is not None else -1)
        boost = -10 if (h.get("source") or "").lower() == prefer else 0
        if prefer == "reddit":
            dl = (h.get("displayed_link") or h.get("link") or "").lower()
            if "reddit.com" in dl:
                boost = -10
        return (sim_key, boost, h.get("position", 999))

    vm_sorted = sorted(hits, key=rank)
    face_similar = [h for h in vm_sorted if (h.get("_face_sim") or 0) >= min_face_similarity]
    use = face_similar if face_similar else vm_sorted
    top = use[0] if use else vm_sorted[0]

    reddit_found = any(
        (h.get("source") or "").lower() == "reddit" or "reddit.com" in (h.get("displayed_link") or h.get("link") or "").lower()
        for h in hits
    )

    combined_raw = {
        "sources": [{"engine": k, "raw": r} for k, r in raws],
        "queries": queries,
        "query_phash": query_hash,
        "embed_method": embed_method,
        "face_similar_count": len(face_similar),
        "total_hits": len(hits),
    }
    return {
        "mode": "live",
        "raw": combined_raw,
        "visual_matches": use,
        "all_hits": vm_sorted,
        "top_match": top,
        "reddit_found": reddit_found,
        "face_similar_count": len(face_similar),
        "num_queries": len(queries),
    }
