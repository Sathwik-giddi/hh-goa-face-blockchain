"""Face-ID failure-path tests: degenerate crops, bad inputs, corrupt files.

Detection success paths (YuNet/Haar) already covered by tests/test_pipeline.py.
"""
import cv2
import numpy as np
import pytest

from src.face_id import crop_face, detect_and_encode, encode_phash


def test_crop_face_degenerate_bbox_raises(tmp_path):
    img = np.zeros((20, 20, 3), dtype=np.uint8)
    out = tmp_path / "crop.jpg"
    # 5x5 bbox -> ~1px padding -> 7x7 crop, below the 8px floor
    with pytest.raises(ValueError, match="degenerate"):
        crop_face(img, [0, 0, 5, 5], out)
    assert not out.exists()


def test_crop_face_writes_padded_crop(tmp_path):
    img = np.zeros((100, 100, 3), dtype=np.uint8)
    out = tmp_path / "crop.jpg"
    crop_face(img, [20, 20, 40, 40], out)
    assert out.exists()
    cropped = cv2.imread(str(out))
    assert cropped.shape[0] >= 48 and cropped.shape[1] >= 48  # 40 + 20% padding
    # padding must not exceed image bounds
    assert cropped.shape[0] <= 100 and cropped.shape[1] <= 100


def test_detect_and_encode_missing_input_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        detect_and_encode(tmp_path / "missing.jpg", out_dir=tmp_path / "o")


def test_detect_and_encode_directory_raises(tmp_path):
    with pytest.raises(ValueError, match="directory"):
        detect_and_encode(tmp_path, out_dir=tmp_path / "o")


def test_detect_and_encode_corrupt_file_warns_not_crashes(tmp_path):
    bad = tmp_path / "bad.jpg"
    bad.write_bytes(b"not an image at all")
    out = tmp_path / "o"
    r = detect_and_encode(bad, out_dir=out)
    assert r["engine"] == "none"
    assert r["crop_path"] is None
    assert r["num_faces"] == 0
    assert "cannot decode" in r["warning"]


def test_detect_and_encode_no_face_warns(tmp_path):
    img = np.ones((100, 100, 3), dtype=np.uint8) * 200
    p = tmp_path / "blank.jpg"
    cv2.imwrite(str(p), img)
    r = detect_and_encode(p, out_dir=tmp_path / "o")
    assert r["crop_path"] is None
    assert "no face detected" in r["warning"]


def test_encode_phash_directory_raises(tmp_path):
    with pytest.raises(ValueError):
        encode_phash(tmp_path)
