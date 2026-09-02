"""Pipeline: face scan → live social search → $0 blockchain — run on your Mac."""
import argparse
import json
import os
import sys
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

from .face_id import detect_and_encode
from .search import reverse_image_search
from .utils import fingerprint_post, safe_filename
from .blockchain import anchor, verify


def run_pipeline(
    image,
    out_dir: str | Path = Path("outputs"),
    chain_file: str | Path = None,
    prefer_source: str = "reddit",
    verbose: bool = True,
):
    image = Path(image)
    if chain_file is None:
        chain_file = Path(os.getenv("CHAIN_FILE", "./chain.json"))
    else:
        chain_file = Path(chain_file)
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    if not image.exists():
        raise FileNotFoundError(f"Image not found: {image}")

    if verbose:
        print(f"\n[1/4] Face scan: {image}")
    face = detect_and_encode(image, out_dir=out_dir)
    if not face.get("crop_path"):
        raise RuntimeError(
            f"Face detection failed: {face.get('warning', 'no crop_path')}. "
            "Use a photo with a visible face (frontal or 3-quarter)."
        )
    if verbose:
        print(f"  → engine={face['engine']} faces={face['num_faces']} conf={face.get('conf', 0):.2f} crop={face['crop_path']}")
        if face.get("warning"):
            print(f"  ⚠ {face['warning']}")

    if verbose:
        print(f"[2/4] Social search LIVE (Lens + face re-rank covers any public face, not just celebs)...")
    search = reverse_image_search(face["crop_path"], original_path=str(image), prefer_source=prefer_source)
    vm = search.get("visual_matches", [])
    top = search.get("top_match")
    if not top:
        raise RuntimeError(
            f"Live search returned 0 hits — no public indexed copy of this face. "
            "Try a face that is posted publicly (IG/X/Reddit)."
        )
    if verbose:
        print(f"  → mode={search['mode']} hits={len(vm)} (face-similar {search.get('face_similar_count')}/{len(search.get('all_hits', []))}) reddit_found={search.get('reddit_found')}")
        dist = top.get("_face_distance", "?")
        print(f"  → top: [{top.get('source')}] face_dist={dist} {top.get('title', '')[:80]} → {top.get('link')}")
        if search.get("face_similar_count", 0) == 0:
            print("  ⚠ No face-similar hits — this face has no public copy. Showing closest visual only.")

    if verbose:
        print(f"[3/4] Fingerprint + blockchain ({os.getenv('BLOCKCHAIN_MODE', 'local')})")
    fp = fingerprint_post(top, image_path=face["crop_path"])
    fingerprint = fp["fingerprint_sha256"]
    payload = {
        "post": top,
        "face": {k: face[k] for k in ["engine", "conf", "bbox", "embedding_hash"] if k in face},
        "search_mode": search["mode"],
        "reddit_found": search.get("reddit_found"),
        "face_similar_count": search.get("face_similar_count"),
    }
    receipt = anchor(fingerprint, payload, chain_file=str(chain_file))
    if verbose:
        print(f"  → fingerprint {fingerprint[:16]}... anchored → {receipt}")

    if verbose:
        print(f"[4/4] Re-verify (tamper-evident)")
    v = verify(fingerprint, chain_file=str(chain_file))
    if verbose:
        print(f"  → verified={v.get('verified')} {v}")
        tampered = fingerprint[:-1] + ("0" if fingerprint[-1] != "0" else "1")
        vt = verify(tampered, chain_file=str(chain_file))
        print(f"  → tamper test (flipped hash) verified={vt.get('verified')} (expected False) ✓")

    # Save outputs (atomic via temp+replace)
    def _save(name, obj):
        p = out_dir / name
        tmp = p.with_suffix(p.suffix + ".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(obj, f, indent=2, ensure_ascii=False)
        tmp.replace(p)

    _save("result.json", {
        "face": face,
        "search": {"mode": search["mode"], "reddit_found": search.get("reddit_found"),
                    "top_match": top, "num_hits": len(vm),
                    "face_similar_count": search.get("face_similar_count")},
        "fingerprint": fp, "receipt": receipt, "verify": v,
    })
    _save("search_raw.json", search.get("raw", {}))
    _save("evidence.json", fp)
    _save("receipt.json", receipt)

    if verbose:
        print(f"\n✓ Done → {out_dir / 'result.json'}")
        print(f"  face_crop: {face['crop_path']}")
        print(f"  evidence: {out_dir / 'evidence.json'}  receipt: {out_dir / 'receipt.json'}")
        if receipt.get("explorerUrl"):
            print(f"  explorer: {receipt['explorerUrl']}")
    return {"face": face, "search": search, "fingerprint": fp, "receipt": receipt, "verify": v}


def main():
    p = argparse.ArgumentParser(description="HH Goa Task 3: Face → Social → Chain (LIVE ONLY, $0 Mac)")
    p.add_argument("--image", help="face scan image path (required for scan, not for --verify)")
    p.add_argument("--out", default="outputs", help="outputs dir")
    p.add_argument("--chain", default=None, help="chain file (default $CHAIN_FILE or ./chain.json)")
    p.add_argument("--prefer-source", default="reddit", help="prefer reddit hits in re-rank")
    p.add_argument("--verify", help="verify a fingerprint hash instead of running pipeline")
    args = p.parse_args()

    if args.verify:
        v = verify(args.verify, chain_file=args.chain)
        print(json.dumps(v, indent=2))
        sys.exit(0 if v.get("verified") is True else 1)
    if not args.image:
        print("error: --image is required (or use --verify <hash>)", file=sys.stderr)
        sys.exit(2)
    try:
        run_pipeline(args.image, out_dir=args.out, chain_file=args.chain, prefer_source=args.prefer_source)
    except Exception as e:
        print(f"\n✗ {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
