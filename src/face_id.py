"""Face detection — OpenCV YuNet (handles 3-quarter / glasses / side profile).

YuNet is OpenCV's built-in DNN face detector (~230KB ONNX, downloaded once).
Handles 3-quarter, side profile, glasses, and small faces much better than Haar.

Fallback: OpenCV Haar (frontal only) if YuNet unavailable.
"""
import hashlib
from pathlib import Path
import cv2
import numpy as np
import urllib.request
import socket

from .utils import ensure_image


# --- YuNet singleton with safe download ---
YUNET_PATH = Path(__file__).parent.parent / "models" / "face_detection_yunet_2023mar.onnx"
YUNET_URL = (
    "https://github.com/opencv/opencv_zoo/raw/main/models/"
    "face_detection_yunet/face_detection_yunet_2023mar.onnx"
)
YUNET_MIN_BYTES = 100_000  # sanity floor for valid ONNX

_yunet = None
_yunet_failed = False


def _get_yunet():
    """Get cached YuNet detector, downloading if missing or truncated."""
    global _yunet, _yunet_failed
    if _yunet is not None:
        return _yunet
    if _yunet_failed:
        return None
    try:
        if not YUNET_PATH.exists() or YUNET_PATH.stat().st_size < YUNET_MIN_BYTES:
            YUNET_PATH.parent.mkdir(parents=True, exist_ok=True)
            socket.setdefaulttimeout(30)  # 30s timeout for download
            try:
                urllib.request.urlretrieve(YUNET_URL, str(YUNET_PATH))
            except Exception as e:
                print(f"[face_id] YuNet download failed: {e}")
                # clean up partial
                if YUNET_PATH.exists() and YUNET_PATH.stat().st_size < YUNET_MIN_BYTES:
                    YUNET_PATH.unlink(missing_ok=True)
                _yunet_failed = True
                return None
        # Confirm OpenCV supports FaceDetectorYN
        if not hasattr(cv2, "FaceDetectorYN"):
            _yunet_failed = True
            return None
        _yunet = cv2.FaceDetectorYN.create(
            str(YUNET_PATH), "", (320, 320),
            score_threshold=0.5, nms_threshold=0.3, top_k=100,
        )
        return _yunet
    except Exception as e:
        print(f"[face_id] YuNet init failed: {e}")
        _yunet_failed = True
        return None


def detect_faces_yunet(image_path):
    """OpenCV YuNet — handles 3-quarter / side profile / glasses."""
    img = ensure_image(image_path)
    det = _get_yunet()
    if det is None:
        return img, []
    h, w = img.shape[:2]
    if w < 32 or h < 32:
        return img, []
    # Cap very large images to keep inference fast
    if max(w, h) > 1280:
        scale = 1280 / max(w, h)
        img = cv2.resize(img, (int(w * scale), int(h * scale)))
        h, w = img.shape[:2]
    det.setInputSize((w, h))
    try:
        retval, faces = det.detect(img)
    except Exception as e:
        print(f"[face_id] YuNet detect failed: {e}")
        return img, []
    if faces is None or retval == 0:
        return img, []
    results = []
    for f in faces:
        # YuNet: [x, y, w, h, ...landmarks..., score]
        x, y, ww, hh = int(f[0]), int(f[1]), int(f[2]), int(f[3])
        conf = float(f[14])
        if conf < 0.5:
            continue
        # clamp into image
        x = max(0, x); y = max(0, y)
        ww = max(1, min(w - x, ww)); hh = max(1, min(h - y, hh))
        if ww < 20 or hh < 20:
            continue
        results.append({"bbox": [x, y, ww, hh], "conf": conf, "engine": "opencv:yunet"})
    results.sort(key=lambda r: r["bbox"][2] * r["bbox"][3], reverse=True)
    return img, results


def detect_faces_haar(image_path):
    """Haar — frontal only, kept as last-resort fallback."""
    img = ensure_image(image_path)
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    cascade_path = cv2.data.haarcascades + "haarcascade_frontalface_default.xml"
    cascade = cv2.CascadeClassifier(cascade_path)
    if cascade.empty():
        return img, []
    faces = cascade.detectMultiScale(gray, scaleFactor=1.1, minNeighbors=5, minSize=(60, 60))
    results = []
    h, w = img.shape[:2]
    for (x, y, fw, fh) in faces:
        x = max(0, x); y = max(0, y)
        fw = max(1, min(w - x, fw)); fh = max(1, min(h - y, fh))
        conf = float(min(0.99, 0.6 + (fw * fh) / (h * w) * 2))
        results.append({"bbox": [int(x), int(y), int(fw), int(fh)], "conf": conf, "engine": "opencv:haar"})
    results.sort(key=lambda r: r["bbox"][2] * r["bbox"][3], reverse=True)
    return img, results


def crop_face(img, bbox, out_path: Path) -> Path:
    """Crop face with 20% padding, write to disk. Returns out_path on success."""
    x, y, w, h = bbox
    pad_w, pad_h = int(w * 0.20), int(h * 0.20)
    x1 = max(0, x - pad_w); y1 = max(0, y - pad_h)
    x2 = min(img.shape[1], x + w + pad_w); y2 = min(img.shape[0], y + h + pad_h)
    if x2 - x1 < 8 or y2 - y1 < 8:
        raise ValueError(f"crop_face: degenerate bbox {bbox} for img {img.shape}")
    crop = img[y1:y2, x1:x2]
    out_path.parent.mkdir(parents=True, exist_ok=True)
    ok = cv2.imwrite(str(out_path), crop)
    if not ok:
        raise IOError(f"imwrite failed: {out_path}")
    return out_path


def encode_phash(image_path) -> str:
    """8x8 pHash (DCT-free, mean threshold) packed to 64-bit hex.

    Returns 16 hex chars (64 bits). Hamming distance on this is meaningful:
    1-bit pHash difference = 1-bit output difference.
    """
    img = ensure_image(image_path)
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    small = cv2.resize(gray, (8, 8), interpolation=cv2.INTER_AREA)
    mean = small.mean()
    bits = (small > mean).flatten().astype(np.uint8)
    packed = int(np.packbits(bits).tobytes().hex(), 16)
    return f"{packed:016x}"


def detect_and_encode(image_path, out_dir: Path = Path("outputs")) -> dict:
    """Primary entry: detect face, crop, encode. Returns dict for pipeline."""
    image_path = Path(image_path)
    if not image_path.exists():
        raise FileNotFoundError(f"Input not found: {image_path}")
    if image_path.is_dir():
        raise ValueError(f"Input is a directory, not an image: {image_path}")
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    # ensure_image raises ValueError on corrupt files — we catch + report as no_face
    try:
        img, faces, engine_used = ensure_image(image_path), [], "none"
    except (ValueError, FileNotFoundError) as e:
        return {
            "engine": "none",
            "bbox": None, "conf": 0.0, "num_faces": 0,
            "crop_path": None,
            "embedding_hash": None,
            "warning": f"cannot decode image: {e}",
        }

    # 1. YuNet (best for 3-quarter / side profile / glasses)
    if not faces:
        try:
            img, faces = detect_faces_yunet(image_path)
            if faces:
                engine_used = "opencv:yunet"
        except Exception as e:
            print(f"[face_id] YuNet failed: {e}")

    # 2. Haar fallback (frontal only)
    if not faces:
        try:
            img, faces = detect_faces_haar(image_path)
            if faces:
                engine_used = "opencv:haar"
        except Exception as e:
            print(f"[face_id] Haar failed: {e}")

    if not faces:
        return {
            "engine": "none",
            "bbox": None, "conf": 0.0, "num_faces": 0,
            "crop_path": None,
            "embedding_hash": None,
            "warning": "no face detected — upload a photo with a visible face",
        }

    best = faces[0]
    crop_path = out_dir / "face_crop.jpg"
    try:
        crop_face(img, best["bbox"], crop_path)
    except Exception as e:
        return {
            "engine": engine_used,
            "bbox": best["bbox"], "conf": best["conf"], "num_faces": len(faces),
            "crop_path": None,
            "embedding_hash": None,
            "warning": f"crop failed: {e}",
        }
    return {
        "engine": engine_used,
        "bbox": best["bbox"], "conf": best["conf"],
        "num_faces": len(faces),
        "crop_path": str(crop_path),
        "embedding_hash": encode_phash(crop_path),
        "all_faces": faces,
    }
