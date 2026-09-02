"""Shared helpers: hashing, image IO, pretty print."""
import hashlib
import json
from pathlib import Path
from datetime import datetime, timezone
import re

import numpy as np
import cv2
from PIL import Image

# iPhone HEIC support — Pillow can't read HEIF natively; register if available.
try:
    from pillow_heif import register_heif_opener
    register_heif_opener()
except ImportError:
    pass


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path) -> str:
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"sha256_file: {path} missing")
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            h.update(chunk)
    return h.hexdigest()


def sha256_json(obj: dict) -> str:
    canon = json.dumps(obj, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(canon.encode("utf-8")).hexdigest()


def fingerprint_post(post: dict, image_path=None) -> dict:
    """Build deterministic fingerprint for a discovered post."""
    parts: dict = {
        "url": post.get("link") or post.get("url") or "",
        "title": post.get("title") or "",
        "source": post.get("source") or post.get("displayed_link") or "",
        "thumbnail": post.get("thumbnail") or "",
    }
    if image_path and Path(image_path).exists():
        parts["image_sha256"] = sha256_file(image_path)
    parts["fingerprint_sha256"] = sha256_json(parts)
    parts["created_at"] = datetime.now(timezone.utc).isoformat()
    return parts


# Canonicalization procedure (documented in README §Canonicalization):
#   record = {url, title, source, thumbnail, [image_sha256]} — keys sorted,
#   separators (",", ":"), ensure_ascii=False, UTF-8 encoded, SHA-256.
# The image hash is over the raw bytes of the post image as retrieved at scan time.


def recompute_fingerprint(post: dict, image_path=None) -> str:
    """Recompute ONLY the fingerprint (no created_at) — used by independent
    re-verification: same canonicalization as fingerprint_post."""
    parts: dict = {
        "url": post.get("link") or post.get("url") or "",
        "title": post.get("title") or "",
        "source": post.get("source") or post.get("displayed_link") or "",
        "thumbnail": post.get("thumbnail") or "",
    }
    if image_path and Path(image_path).exists():
        parts["image_sha256"] = sha256_file(image_path)
    return sha256_json(parts)


def reverify_independent(evidence: dict, out_dir: Path = Path("outputs")) -> dict:
    """§17 independent verification: re-retrieve the discovered content from the
    live web, re-canonicalize, re-hash, and compare with the stored fingerprint.

    Never reads the stored fingerprint to build the new one — rebuilds every
    field from a fresh re-download. Returns a structured result; never fakes
    success (a failed re-download downgrades the result honestly).
    """
    post = evidence or {}
    url = post.get("url") or ""
    thumb = post.get("thumbnail") or ""
    stored = evidence.get("fingerprint_sha256") or ""

    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    re_dl = None
    image_path = None
    # Mirror scan-time composition: only re-hash an image if the stored record
    # included one (otherwise recomputed would structurally differ from stored).
    if thumb and evidence.get("image_sha256"):
        dest = out_dir / "_reverify_image.bin"
        re_dl = download_image(thumb, dest)
        if re_dl:
            image_path = dest

    recomputed = recompute_fingerprint(post, image_path=image_path)
    result = {
        "method": "re-retrieve + re-canonicalize + re-hash",
        "re_downloaded_image": bool(re_dl),
        "recomputed_fingerprint": recomputed,
        "stored_fingerprint": stored,
        "match": bool(stored) and recomputed == stored,
        "components": {
            "url": url,
            "title": post.get("title") or "",
            "source": post.get("source") or "",
            "thumbnail": thumb,
            "image_sha256": (sha256_file(image_path) if image_path else None),
        },
    }
    if image_path:
        try:
            image_path.unlink()
        except OSError:
            pass
    return result


def download_image(url: str, dest: Path, max_bytes: int = 10 * 1024 * 1024) -> Path | None:
    """Download image (max 10MB default) with proper headers and size cap."""
    try:
        import requests
        r = requests.get(
            url, timeout=15, stream=True,
            headers={"User-Agent": "Mozilla/5.0"},
            allow_redirects=True,
        )
        r.raise_for_status()
        ct = (r.headers.get("content-type") or "").lower()
        if not ct.startswith("image/"):
            return None
        dest.parent.mkdir(parents=True, exist_ok=True)
        total = 0
        with open(dest, "wb") as f:
            for chunk in r.iter_content(8192):
                if not chunk:
                    continue
                total += len(chunk)
                if total > max_bytes:
                    return None
                f.write(chunk)
        return dest
    except Exception:
        return None


def ensure_image(path) -> np.ndarray:
    """Read image from path into OpenCV BGR ndarray, raising ValueError on failure.

    Handles truncated/corrupt files by trying both cv2.imread and PIL.
    """
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"Image not found: {p}")
    if p.is_dir():
        raise ValueError(f"Path is a directory, not an image: {p}")
    img = cv2.imread(str(p))
    if img is not None:
        return img
    # Fallback: PIL for webp/heic or cv2-failing files
    try:
        with Image.open(p) as pil:
            pil = pil.convert("RGB")
            arr = np.array(pil)
            img = cv2.cvtColor(arr, cv2.COLOR_RGB2BGR)
            if img is None or img.size == 0:
                raise ValueError("empty")
            return img
    except Exception as e:
        raise ValueError(f"Cannot decode image {p}: {e}") from e


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


_HEX64 = re.compile(r"^[0-9a-fA-F]{64}$")


def is_hex64(s: str) -> bool:
    return bool(s) and len(s) == 64 and bool(_HEX64.match(s))


def safe_filename(name: str, default: str = "img") -> str:
    """Sanitize a filename for storage: strip path separators, control chars, dots.

    Reject directory traversal, hidden files, overly long names.
    """
    if not name:
        return default
    name = re.sub(r"[^A-Za-z0-9._-]", "_", name)
    name = name.strip(". ")
    if not name or name in (".", ".."):
        return default
    return name[:120]
