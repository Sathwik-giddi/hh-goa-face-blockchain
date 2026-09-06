"""Identity Vault — enroll a person once, recognize them on every scan.

Enrollment stores an ArcFace embedding per labeled person (identities.json,
gitignored — biometric templates never leave this machine). Scans then carry a
stable subject-identity label that does not depend on Google's rotating index.

Search itself is untouched: Lens still runs live, the on-chain fingerprint
still comes from the live-discovered post. The vault only labels WHO the
scanned subject is — it is verification, never a substitute for the search.
"""
import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
from filelock import FileLock

VAULT_PATH = Path(os.getenv("IDENTITY_VAULT", Path(__file__).parent.parent / "identities.json"))
MIN_ENROLL_SIM = 60.0   # subject vs enrolled reference (both clean faces)
MIN_MARGIN = 5.0        # over the second-best enrolled identity


def _atomic_write(data) -> None:
    VAULT_PATH.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=VAULT_PATH.name + ".", dir=str(VAULT_PATH.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, VAULT_PATH)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _load() -> list:
    if not VAULT_PATH.exists():
        return []
    try:
        with open(VAULT_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return []


def enroll(name: str, embedding) -> dict:
    """Enroll a person's ArcFace embedding under a label. Re-enrolling the same
    name updates the reference (last write wins, documented)."""
    name = (name or "").strip()[:80]
    if not name:
        raise ValueError("name required")
    vec = np.asarray(embedding, dtype=np.float32).reshape(-1)
    if vec.size != 512:
        raise ValueError("embedding must be 512-D ArcFace")
    norm = float(np.linalg.norm(vec))
    if norm == 0:
        raise ValueError("degenerate embedding")
    with FileLock(str(VAULT_PATH) + ".lock", timeout=10):
        people = _load()
        people = [p for p in people if p["name"].lower() != name.lower()]
        people.append({
            "name": name,
            "embedding": [round(float(x), 6) for x in (vec / norm)],
            "enrolled_at": datetime.now(timezone.utc).isoformat(),
        })
        _atomic_write(people)
    return {"name": name, "dimensions": int(vec.size)}


def list_identities() -> list:
    return [{"name": p["name"], "enrolled_at": p["enrolled_at"]} for p in _load()]


def identify(query_embedding) -> dict | None:
    """Best enrolled identity for a face, or None when not decisive.

    Decisive = similarity clears MIN_ENROLL_SIM AND beats the runner-up
    enrolled identity by MIN_MARGIN (an ambiguous vault match labels nobody).
    """
    q = np.asarray(query_embedding, dtype=np.float32).reshape(-1)
    norm = float(np.linalg.norm(q))
    if norm == 0:
        return None
    q = q / norm
    scored = []
    for p in _load():
        ref = np.asarray(p["embedding"], dtype=np.float32)
        if ref.size != q.size:
            continue
        scored.append((float(np.dot(q, ref)) * 100.0, p["name"]))
    if not scored:
        return None
    scored.sort(reverse=True)
    best_sim, best_name = scored[0]
    margin = (best_sim - scored[1][0]) if len(scored) > 1 else 100.0
    if best_sim < MIN_ENROLL_SIM or margin < MIN_MARGIN:
        return None
    return {"name": best_name, "similarity": round(best_sim, 1),
            "margin_over_second": round(margin, 1)}
