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


def _fetch_hits(image_path: Path, api_key: str) -> tuple:
    """Run Lens + fallback google_reverse_image, return (deduped_hits, [raw])."""
    image_id = _serpapi_upload(image_path, api_key)
    raws = []
    raw = _lens_search(image_id, api_key)
    raws.append(("lens", raw))
    vm = raw.get("visual_matches") or raw.get("image_results") or []
    if len(vm) < 5:
        ex = raw.get("exact_matches") or []
        if ex:
            vm = vm + ex
    if len(vm) < 5:
        try:
            raw2 = _reverse_image_search(image_id, api_key)
            raws.append(("reverse", raw2))
            vm2 = raw2.get("image_results") or raw2.get("visual_matches") or []
            vm = vm + vm2
        except Exception as e:
            print(f"[search] reverse_image fallback failed: {e}")

    # dedup by link (keep first), but keep hits without link (only thumbnail)
    seen = set()
    out = []
    for v in vm:
        link = v.get("link") or v.get("url") or ""
        key = link or ("nokey:" + (v.get("thumbnail") or ""))
        if key in seen:
            continue
        seen.add(key)
        out.append(v)
    return out, raws


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
    min_face_similarity: float = 36.3,  # SFace cosine threshold for same person
) -> dict:
    """Live only. Re-ranks Lens hits by SFace face-embedding similarity (parallel)."""
    image_path = Path(image_path)
    api_key = api_key or os.getenv("SERPAPI_API_KEY") or ""
    if not api_key:
        raise RuntimeError(
            "SERPAPI_API_KEY missing — get free 250 at serpapi.com/users/sign_up "
            "(email only, no card). No mock fallback in LIVE mode."
        )
    if not image_path.exists():
        raise FileNotFoundError(f"image not found: {image_path}")

    query_hash = _phash(image_path)
    query_feat, embed_method = face_embedding(image_path)
    hits, raws = _fetch_hits(image_path, api_key)
    sparse_first_pass = (len(hits) < 8)

    # Only do full-image fallback if crop search was sparse
    if sparse_first_pass and original_path and Path(original_path).exists():
        try:
            orig_hash = _phash(Path(original_path))
            if orig_hash and orig_hash != query_hash:
                hits2, raws2 = _fetch_hits(Path(original_path), api_key)
                raws.extend(raws2)
                seen = set((h.get("link") or h.get("thumbnail") or "") for h in hits)
                for h in hits2:
                    key = h.get("link") or h.get("thumbnail") or ""
                    if key not in seen:
                        hits.append(h)
                        seen.add(key)
        except Exception as e:
            print(f"[search] full-image fallback failed: {e}")

    if not hits:
        raise RuntimeError(
            f"Live search 0 hits for {image_path.name} — no public indexed copy. "
            "Try a face that is posted publicly (IG/X/Reddit). Private faces have no web hits by design."
        )

    # Score candidate faces in parallel with real face embeddings (SFace).
    from concurrent.futures import ThreadPoolExecutor
    sims = [None] * len(hits)
    with ThreadPoolExecutor(max_workers=8) as ex:
        futures = {ex.submit(_score_thumbnail_face, h.get("thumbnail") or "", h.get("image") or "", query_feat): i for i, h in enumerate(hits)}
        for fut, i in futures.items():
            try:
                s, _err = fut.result(timeout=15)
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
    }
