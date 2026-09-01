import hashlib
import json
from pathlib import Path
from datetime import datetime, timezone
import requests
from PIL import Image
import cv2
import numpy as np


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: str | Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            h.update(chunk)
    return h.hexdigest()


def sha256_json(obj: dict) -> str:
    canon = json.dumps(obj, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(canon.encode()).hexdigest()


def fingerprint_post(post: dict, image_path: str | Path | None = None) -> dict:
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


def download_image(url: str, dest: Path) -> Path | None:
    try:
        r = requests.get(url, timeout=15, stream=True)
        r.raise_for_status()
        dest.parent.mkdir(parents=True, exist_ok=True)
        with open(dest, "wb") as f:
            for chunk in r.iter_content(8192):
                f.write(chunk)
        return dest
    except Exception as e:
        print(f"[utils] download failed {url}: {e}")
        return None


def ensure_image(path: str | Path) -> np.ndarray:
    img = cv2.imread(str(path))
    if img is None:
        pil = Image.open(path).convert("RGB")
        img = cv2.cvtColor(np.array(pil), cv2.COLOR_RGB2BGR)
    if img is None:
        raise FileNotFoundError(f"Cannot read image: {path}")
    return img


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()
