"""Live reverse-image search — SerpAPI Google Lens. LIVE ONLY, no mock."""
import os
from pathlib import Path
import requests
from dotenv import load_dotenv

load_dotenv()

def _serpapi_lens_live(image_path: Path, api_key: str, hl="en", country="us"):
    """Two-step: POST image → image_id → GET google_lens. Works without hosting."""
    # Step 1: upload
    with open(image_path, "rb") as f:
        files = {"image": (image_path.name, f, "image/jpeg")}
        data = {"api_key": api_key}
        r = requests.post("https://serpapi.com/image", files=files, data=data, timeout=30)
        r.raise_for_status()
        j = r.json()
        image_id = j.get("image_id") or j.get("imageId")
        if not image_id:
            raise RuntimeError(f"SerpAPI upload no image_id: {j}")

    # Step 2: search
    params = {"engine": "google_lens", "image_id": image_id, "api_key": api_key, "hl": hl, "country": country}
    r2 = requests.get("https://serpapi.com/search.json", params=params, timeout=30)
    r2.raise_for_status()
    return r2.json()


def reverse_image_search(image_path: str | Path, api_key: str | None = None, prefer_source: str = "reddit") -> dict:
    """Returns {mode, raw, visual_matches, top_match, reddit_found} — LIVE ONLY"""
    image_path = Path(image_path)
    api_key = api_key or os.getenv("SERPAPI_API_KEY") or ""
    if not api_key:
        raise RuntimeError("SERPAPI_API_KEY missing — get free 250 at serpapi.com/users/sign_up (email only, no card). Set in .env. No mock fallback in LIVE mode.")

    raw = _serpapi_lens_live(image_path, api_key)
    vm = raw.get("visual_matches") or raw.get("image_results") or []
    if not vm and "search_metadata" not in raw:
        print(f"[search] unexpected raw keys: {list(raw.keys())[:10]}")
    if not vm:
        vm = raw.get("exact_matches") or []
    if not vm:
        raise RuntimeError(f"Live Lens returned 0 hits for {image_path.name} — try a public-web face (celebrity/IG). Private selfies aren't indexed by Google. Raw search_metadata: {raw.get('search_metadata', {})}")
    if prefer_source.lower() == "reddit":
        vm_sorted = sorted(vm, key=lambda x: 0 if (x.get("source") or "").lower()=="reddit" else 1)
    else:
        vm_sorted = vm
    top = vm_sorted[0] if vm_sorted else None
    reddit_found = any((v.get("source") or "").lower()=="reddit" for v in vm)
    return {"mode": "live", "raw": raw, "visual_matches": vm_sorted, "top_match": top, "reddit_found": reddit_found}
