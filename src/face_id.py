"""Face detection — OpenCV YuNet (handles 3-quarter / glasses / side profile, M1 native, fast).

YuNet is OpenCV's built-in DNN face detector. ~230KB model, downloads once.
Handles 3-quarter, side profile, glasses, and small faces much better than Haar.

Fallback: OpenCV Haar (frontal only), then DeepFace if installed.
"""
import os
import hashlib
from pathlib import Path
import cv2
import numpy as np
from .utils import ensure_image

# Try optional DeepFace (better embeddings) — fallback if missing
try:
    from deepface import DeepFace  # type: ignore
    HAS_DEEPFACE = True
except Exception:
    HAS_DEEPFACE = False


# Downloaded once, cached in repo dir
YUNET_PATH = Path(__file__).parent.parent / "models" / "face_detection_yunet_2023mar.onnx"
YUNET_URL = "https://github.com/opencv/opencv_zoo/raw/main/models/face_detection_yunet/face_detection_yunet_2023mar.onnx"

_yunet = None
def _get_yunet():
    """Get cached YuNet detector, downloading if needed."""
    global _yunet
    if _yunet is not None:
        return _yunet
    if not YUNET_PATH.exists():
        YUNET_PATH.parent.mkdir(parents=True, exist_ok=True)
        import urllib.request
        try:
            urllib.request.urlretrieve(YUNET_URL, str(YUNET_PATH))
        except Exception as e:
            print(f"[face_id] YuNet download failed: {e}")
            return None
    try:
        _yunet = cv2.FaceDetectorYN.create(
            str(YUNET_PATH), "", (320, 320),
            score_threshold=0.5, nms_threshold=0.3, top_k=100,
        )
        return _yunet
    except Exception as e:
        print(f"[face_id] YuNet init failed: {e}")
        return None


def detect_faces_yunet(image_path: str | Path):
    """OpenCV YuNet — handles 3-quarter / side profile / glasses."""
    img = ensure_image(image_path)
    det = _get_yunet()
    if det is None:
        return img, []
    h, w = img.shape[:2]
    det.setInputSize((w, h))
    retval, faces = det.detect(img)
    if faces is None or retval == 0:
        return img, []
    results = []
    for f in faces:
        # YuNet returns [x, y, w, h, ...landmarks..., score]
        x, y, ww, hh = int(f[0]), int(f[1]), int(f[2]), int(f[3])
        conf = float(f[14])
        if conf < 0.5:
            continue
        # clamp
        x = max(0, x); y = max(0, y)
        ww = min(w - x, ww); hh = min(h - y, hh)
        if ww < 20 or hh < 20:
            continue
        results.append({"bbox": [x, y, ww, hh], "conf": conf, "engine": "yunet"})
    results.sort(key=lambda r: r["bbox"][2] * r["bbox"][3], reverse=True)
    return img, results


def detect_faces_haar(image_path: str | Path):
    """Haar — frontal only, kept as last-resort fallback."""
    img = ensure_image(image_path)
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    cascade = cv2.CascadeClassifier(cv2.data.haarcascades + "haarcascade_frontalface_default.xml")
    faces = cascade.detectMultiScale(gray, scaleFactor=1.1, minNeighbors=5, minSize=(60, 60))
    results = []
    for (x, y, w, h) in faces:
        conf = float(min(0.99, 0.6 + (w * h) / (img.shape[0] * img.shape[1]) * 2))
        results.append({"bbox": [int(x), int(y), int(w), int(h)], "conf": conf, "engine": "opencv:haar"})
    results.sort(key=lambda r: r["bbox"][2] * r["bbox"][3], reverse=True)
    return img, results


def crop_face(img: np.ndarray, bbox, out_path: Path) -> Path:
    x, y, w, h = bbox
    pad_w, pad_h = int(w * 0.20), int(h * 0.20)
    x1 = max(0, x - pad_w); y1 = max(0, y - pad_h)
    x2 = min(img.shape[1], x + w + pad_w); y2 = min(img.shape[0], y + h + pad_h)
    crop = img[y1:y2, x1:x2]
    out_path.parent.mkdir(parents=True, exist_ok=True)
    cv2.imwrite(str(out_path), crop)
    return out_path


def encode_phash(image_path: str | Path) -> str:
    img = ensure_image(image_path)
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    small = cv2.resize(gray, (32, 32), interpolation=cv2.INTER_AREA)
    mean = small.mean()
    bits = (small > mean).astype(np.uint8)
    return hashlib.sha256(bits.tobytes()).hexdigest()[:16]


def detect_and_encode(image_path: str | Path, out_dir: Path = Path("outputs")) -> dict:
    """Primary entry: detect face, crop, encode. Returns dict for pipeline.

    Tries YuNet (handles 3-quarter / glasses / side profile) → Haar (frontal) → full image.
    """
    image_path = Path(image_path)
    if not image_path.exists():
        raise FileNotFoundError(f"Input not found: {image_path}")
    out_dir.mkdir(parents=True, exist_ok=True)

    img, faces, engine_used = [], [], "none"

    # 1. YuNet (best for 3-quarter / side profile / glasses)
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

    # No face — save full image
    if not faces:
        crop_path = out_dir / "face_crop.jpg"
        cv2.imwrite(str(crop_path), img)
        return {
            "engine": "none",
            "bbox": None, "conf": 0.0, "num_faces": 0,
            "crop_path": str(crop_path),
            "embedding_hash": encode_phash(crop_path),
            "warning": "no face detected — uploading full image to Lens",
        }

    best = faces[0]
    crop_path = crop_face(img, best["bbox"], out_dir / "face_crop.jpg")
    return {
        "engine": engine_used,
        "bbox": best["bbox"], "conf": best["conf"],
        "num_faces": len(faces),
        "crop_path": str(crop_path),
        "embedding_hash": encode_phash(crop_path),
        "all_faces": faces,
    }
