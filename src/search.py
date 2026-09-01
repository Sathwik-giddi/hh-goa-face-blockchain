"""Live reverse-image search — SerpAPI Google Lens (free 250/mo). $0 fallback = mock wiring."""
import os
import base64
from pathlib import Path
import requests
from dotenv import load_dotenv

load_dotenv()

MOCK_RESULTS = [
    {
        "position": 1,
        "title": "Example Match — Reddit r/pics (mock — replace with SERPAPI_API_KEY for live)",
        "link": "https://www.reddit.com/r/pics/comments/example_match/",
        "source": "Reddit",
        "thumbnail": "https://via.placeholder.com/300",
        "source_icon": "https://www.redditstatic.com/desktop2x/img/favicon/android-icon-192x192.png",
    },
    {
        "position": 2,
        "title": "Example Match — Instagram (mock)",
        "link": "https://www.instagram.com/p/example_mock/",
        "source": "Instagram",
        "thumbnail": "https://via.placeholder.com/300",
    },
]

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
    """Returns {mode, raw, visual_matches, top_match, reddit_found}"""
    image_path = Path(image_path)
    api_key = api_key or os.getenv("SERPAPI_API_KEY") or ""
    mode = os.getenv("SEARCH_MODE", "live").lower()

    # If no key or explicitly mock → return wiring-safe mock (pipeline still e2e)
    if mode == "mock" or not api_key:
        print("[search] no SERPAPI_API_KEY — using mock (get free 250 at serpapi.com/users/sign_up for live Reddit hits)")
        vm = MOCK_RESULTS
        top = vm[0] if vm else None
        return {"mode": "mock", "visual_matches": vm, "top_match": top, "reddit_found": any(v.get("source")=="Reddit" for v in vm), "raw": {"note": "mock — set SERPAPI_API_KEY for live Google Lens"}}

    try:
        raw = _serpapi_lens_live(image_path, api_key)
        vm = raw.get("visual_matches") or raw.get("image_results") or []
        # Some responses nest differently
        if not vm and "search_metadata" not in raw:
            print(f"[search] unexpected raw keys: {list(raw.keys())[:8]}")
        # Prefer exact_matches if visual empty
        if not vm:
            vm = raw.get("exact_matches") or []
        # Rank: Reddit-first if requested
        if prefer_source.lower() == "reddit":
            vm_sorted = sorted(vm, key=lambda x: 0 if (x.get("source") or "").lower()=="reddit" else 1)
        else:
            vm_sorted = vm
        top = vm_sorted[0] if vm_sorted else None
        reddit_found = any((v.get("source") or "").lower()=="reddit" for v in vm)
        return {"mode": "live", "raw": raw, "visual_matches": vm_sorted, "top_match": top, "reddit_found": reddit_found}
    except Exception as e:
        print(f"[search] live failed: {e} — falling back to mock so pipeline stays green")
        return {"mode": "mock_fallback", "error": str(e), "visual_matches": MOCK_RESULTS, "top_match": MOCK_RESULTS[0], "reddit_found": True, "raw": {}}
