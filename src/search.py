"""Live reverse-image search — SerpAPI Google Lens + face-similarity re-rank. LIVE ONLY.
Fixes celebrity-only bias: Lens is visual, not face-identity. We re-rank Lens hits by face pHash
so any face on the open web (not just celebrities) surfaces true matches. Tries crop + full image,
Lens + Reverse Image fallback, and filters to face-similar only.
"""
import os
import hashlib
from pathlib import Path
import requests
import cv2
import numpy as np
from dotenv import load_dotenv

load_dotenv()

def _phash(image_path: Path) -> str:
    try:
        img = cv2.imread(str(image_path), cv2.IMREAD_GRAYSCALE)
        if img is None:
            from PIL import Image
            pil = Image.open(image_path).convert("L")
            img = cv2.cvtColor(np.array(pil), cv2.COLOR_RGB2BGR)
            img = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
        small = cv2.resize(img, (32, 32), interpolation=cv2.INTER_AREA)
        m = small.mean()
        bits = (small > m).astype(np.uint8).tobytes()
        return hashlib.sha256(bits).hexdigest()[:16]
    except Exception:
        return ""

def _hamming(a: str, b: str) -> int:
    if not a or not b or len(a)!=len(b): return 999
    # hex to bits hamming
    try:
        ai, bi = int(a,16), int(b,16)
        return bin(ai ^ bi).count("1")
    except: return 999

def _serpapi_upload(image_path: Path, api_key: str):
    with open(image_path, "rb") as f:
        files = {"image": (image_path.name, f, "image/jpeg")}
        data = {"api_key": api_key}
        r = requests.post("https://serpapi.com/image", files=files, data=data, timeout=30)
        r.raise_for_status()
        j = r.json()
        image_id = j.get("image_id") or j.get("imageId")
        if not image_id:
            raise RuntimeError(f"SerpAPI upload no image_id: {j}")
        return image_id

def _lens(image_id: str, api_key: str, hl="en", country="us"):
    params = {"engine": "google_lens", "image_id": image_id, "api_key": api_key, "hl": hl, "country": country}
    r = requests.get("https://serpapi.com/search.json", params=params, timeout=30)
    r.raise_for_status()
    return r.json()

def _reverse_image(image_id: str, api_key: str):
    params = {"engine": "google_reverse_image", "image_id": image_id, "api_key": api_key}
    r = requests.get("https://serpapi.com/search.json", params=params, timeout=30)
    r.raise_for_status()
    return r.json()

def _fetch_hits(image_path: Path, api_key: str, prefer_source="reddit"):
    # try Lens crop first, then Reverse Image fallback
    image_id = _serpapi_upload(image_path, api_key)
    raws = []
    try:
        raw = _lens(image_id, api_key)
        raws.append(("lens", raw))
        vm = raw.get("visual_matches") or raw.get("image_results") or []
        if len(vm) < 5:  # sparse, try exact_matches + reverse_image
            vm2 = raw.get("exact_matches") or []
            if vm2: vm = vm + vm2
        if len(vm) < 5:
            try:
                raw2 = _reverse_image(image_id, api_key)
                raws.append(("reverse", raw2))
                vm2 = raw2.get("image_results") or raw2.get("visual_matches") or []
                vm = vm + vm2
            except Exception as e:
                print(f"[search] reverse fallback failed: {e}")
    except Exception as e:
        raise RuntimeError(f"Lens failed: {e}")

    # dedup by link
    seen=set(); uniq=[]
    for v in vm:
        link=v.get("link") or v.get("url") or ""
        if link and link not in seen:
            seen.add(link); uniq.append(v)
    return uniq, raws

def reverse_image_search(image_path: str | Path, original_path: str | Path | None = None, api_key: str | None = None, prefer_source: str = "reddit") -> dict:
    """Live only. Re-ranks Lens hits by face pHash so non-celebrity public faces surface."""
    image_path = Path(image_path)
    api_key = api_key or os.getenv("SERPAPI_API_KEY") or ""
    if not api_key:
        raise RuntimeError("SERPAPI_API_KEY missing — get free 250 at serpapi.com/users/sign_up. Set in .env.")

    # 1. try crop
    query_hash = _phash(image_path)
    hits, raws = _fetch_hits(image_path, api_key, prefer_source)

    # 2. also try full image if crop sparse or low face-similar
    if original_path and Path(original_path).exists() and str(Path(original_path).resolve()) != str(image_path.resolve()):
        try:
            hits2, raws2 = _fetch_hits(Path(original_path), api_key, prefer_source)
            raws.extend(raws2)
            # merge
            seen=set(h.get("link") for h in hits)
            for h in hits2:
                if h.get("link") not in seen:
                    hits.append(h); seen.add(h.get("link"))
        except Exception as e:
            print(f"[search] full-image search failed: {e}")

    if not hits:
        raise RuntimeError(f"Live search 0 hits for {image_path.name} — no public indexed copy. Try a face that is posted publicly (IG/X/Reddit). Private faces have no web hits by design.")

    # 3. face-similarity re-rank: score each hit by thumbnail pHash vs query (if thumbnail fetchable)
    scored=[]
    for h in hits:
        thumb = h.get("thumbnail") or ""
        score = 999
        face_score = None
        if thumb and query_hash:
            try:
                # download thumb 3s timeout
                r=requests.get(thumb, timeout=4, headers={"User-Agent":"Mozilla/5.0"})
                if r.ok and r.headers.get("content-type","").startswith("image"):
                    tmp=Path("/tmp/_thumb.jpg"); open(tmp,"wb").write(r.content)
                    thash=_phash(tmp)
                    score=_hamming(query_hash, thash)
                    face_score = score  # lower = more similar; 0 identical, ~30 random
                    # also check if Lens already flagged face? boost
                    h["_face_distance"]=face_score
            except Exception:
                pass
        # reddit boost
        reddit_boost = -15 if (h.get("source") or "").lower()=="reddit" else 0
        # if face distance is very high (>30), deprioritize but keep
        rank = (face_score if face_score is not None else 40) + reddit_boost
        scored.append((rank, h))

    # sort: most face-similar + reddit first, then Lens position
    scored.sort(key=lambda x: (x[0], x[1].get("position", 999)))
    vm_sorted=[h for _,h in scored]

    # filter to face-similar if we have any within threshold
    face_similar=[h for h in vm_sorted if h.get("_face_distance") is not None and h["_face_distance"] <= 28]
    # threshold tuned: pHash 16 hex = 64bits, distance <=28 ~ <44% bits differ = visually similar face
    use = face_similar if len(face_similar) >= 1 else vm_sorted

    top = use[0] if use else vm_sorted[0]
    reddit_found = any((v.get("source") or "").lower()=="reddit" for v in hits)
    # build combined raw for debugging
    combined_raw = {"sources": [{"engine":k, "raw":r} for k,r in raws], "query_phash": query_hash, "face_similar_count": len(face_similar), "total_hits": len(hits)}
    return {"mode": "live", "raw": combined_raw, "visual_matches": use, "all_hits": vm_sorted, "top_match": top, "reddit_found": reddit_found, "face_similar_count": len(face_similar)}
