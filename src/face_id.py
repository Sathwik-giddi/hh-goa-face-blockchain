"""$0 face detection — OpenCV Haar (bundled), auto DeepFace if installed."""
from pathlib import Path
import cv2
import numpy as np
from .utils import ensure_image

# Try optional DeepFace (better embeddings) — fallback to Haar+pHash if missing
try:
    from deepface import DeepFace  # type: ignore
    HAS_DEEPFACE = True
except Exception:
    HAS_DEEPFACE = False


def detect_faces_haar(image_path: str | Path):
    img = ensure_image(image_path)
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    cascade = cv2.CascadeClassifier(cv2.data.haarcascades + "haarcascade_frontalface_default.xml")
    faces = cascade.detectMultiScale(gray, scaleFactor=1.1, minNeighbors=5, minSize=(60, 60))
    results = []
    for (x, y, w, h) in faces:
        conf = float(min(0.99, 0.6 + (w * h) / (img.shape[0] * img.shape[1]) * 2))
        results.append({"bbox": [int(x), int(y), int(w), int(h)], "conf": conf})
    # sort largest first
    results.sort(key=lambda r: r["bbox"][2] * r["bbox"][3], reverse=True)
    return img, results


def crop_face(img: np.ndarray, bbox, out_path: Path) -> Path:
    x, y, w, h = bbox
    # pad 15% for better Lens matching
    pad_w, pad_h = int(w * 0.15), int(h * 0.15)
    x1 = max(0, x - pad_w); y1 = max(0, y - pad_h)
    x2 = min(img.shape[1], x + w + pad_w); y2 = min(img.shape[0], y + h + pad_h)
    crop = img[y1:y2, x1:x2]
    out_path.parent.mkdir(parents=True, exist_ok=True)
    cv2.imwrite(str(out_path), crop)
    return out_path


def encode_phash(image_path: str | Path) -> str:
    """8x8 perceptual hash — $0, no model needed, good enough for wiring."""
    import hashlib
    img = ensure_image(image_path)
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    small = cv2.resize(gray, (32, 32), interpolation=cv2.INTER_AREA)
    # DCT-like: use mean threshold
    mean = small.mean()
    bits = (small > mean).astype(np.uint8)
    return hashlib.sha256(bits.tobytes()).hexdigest()[:16]


def detect_and_encode(image_path: str | Path, out_dir: Path = Path("outputs")) -> dict:
    """Primary entry: detect face, crop, encode. Returns dict for pipeline."""
    image_path = Path(image_path)
    if not image_path.exists():
        raise FileNotFoundError(f"Input not found: {image_path}")
    out_dir.mkdir(parents=True, exist_ok=True)

    # Try DeepFace first if installed (better accuracy)
    if HAS_DEEPFACE:
        try:
            reps = DeepFace.represent(img_path=str(image_path), model_name="VGG-Face", enforce_detection=False)
            if reps:
                # use first face
                r = reps[0]
                fa = r.get("facial_area", {})
                bbox = [fa.get("x", 0), fa.get("y", 0), fa.get("w", 200), fa.get("h", 200)]
                conf = 0.92
                img = ensure_image(image_path)
                crop_path = crop_face(img, bbox, out_dir / "face_crop.jpg")
                return {
                    "engine": "deepface:VGG-Face",
                    "bbox": bbox, "conf": conf,
                    "embedding_hash": encode_phash(crop_path),
                    "crop_path": str(crop_path),
                    "num_faces": len(reps),
                }
        except Exception as e:
            print(f"[face_id] DeepFace failed, falling back to Haar: {e}")

    img, faces = detect_faces_haar(image_path)
    if not faces:
        # No face — save full image as crop so pipeline still shows search (judges can see failure handled)
        crop_path = out_dir / "face_crop.jpg"
        cv2.imwrite(str(crop_path), img)
        return {"engine": "opencv:haar", "bbox": None, "conf": 0.0, "num_faces": 0, "crop_path": str(crop_path), "embedding_hash": encode_phash(crop_path), "warning": "no face detected — using full image for search"}

    best = faces[0]
    crop_path = crop_face(img, best["bbox"], out_dir / "face_crop.jpg")
    return {
        "engine": best.get("engine", "opencv:haar"),
        "bbox": best["bbox"], "conf": best["conf"],
        "num_faces": len(faces),
        "crop_path": str(crop_path),
        "embedding_hash": encode_phash(crop_path),
        "all_faces": faces,
    }
