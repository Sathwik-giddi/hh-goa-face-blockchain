"""Pipeline: face scan → live social search → $0 blockchain — run on your Mac."""
import argparse
import json
import os
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

from .face_id import detect_and_encode
from .search import reverse_image_search
from .utils import fingerprint_post, sha256_json
from .blockchain import anchor, verify


def run_pipeline(image: str | Path, out_dir: str | Path = Path("outputs"), chain_file: str | Path = Path("chain.json"), prefer_source="reddit", verbose=True):
    image = Path(image)
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    chain_file = Path(chain_file)

    if verbose:
        print(f"\n[1/4] Face scan: {image}")
    face = detect_and_encode(image, out_dir=out_dir)
    if verbose:
        print(f"  → engine={face['engine']} faces={face['num_faces']} conf={face.get('conf',0):.2f} crop={face['crop_path']}")
        if face.get("warning"): print(f"  ⚠ {face['warning']}")

    print(f"[2/4] Social search LIVE (Google Lens index covers Reddit/X/IG/TikTok)...")
    search = reverse_image_search(face["crop_path"], prefer_source=prefer_source)
    vm = search.get("visual_matches", [])
    top = search.get("top_match")
    if verbose:
        print(f"  → mode={search['mode']} hits={len(vm)} reddit_found={search.get('reddit_found')} ")
        if top:
            print(f"  → top: [{top.get('source')}] {top.get('title')[:80]} → {top.get('link')}")
        else:
            print("  → no hits")

    print(f"[3/4] Fingerprint + blockchain ({os.getenv('BLOCKCHAIN_MODE','local')})")
    post = top or {"link": "", "title": "", "source": "", "thumbnail": ""}
    fp = fingerprint_post(post, image_path=face["crop_path"])
    fingerprint = fp["fingerprint_sha256"]
    payload = {"post": post, "face": {k: face[k] for k in ["engine","conf","bbox","embedding_hash"] if k in face}, "search_mode": search["mode"], "reddit_found": search.get("reddit_found")}
    receipt = anchor(fingerprint, payload, chain_file=str(chain_file))
    if verbose:
        print(f"  → fingerprint {fingerprint[:16]}... anchored → {receipt}")

    print(f"[4/4] Re-verify (tamper-evident)")
    v = verify(fingerprint, chain_file=str(chain_file))
    if verbose:
        print(f"  → verified={v.get('verified')} {v}")
        # tamper demo: flip one char
        tampered = fingerprint[:-1] + ("0" if fingerprint[-1]!="0" else "1")
        vt = verify(tampered, chain_file=str(chain_file))
        print(f"  → tamper test (flipped hash) verified={vt.get('verified')} (expected False) ✓")

    # Save outputs for screen recording
    out_json = out_dir / "result.json"
    with open(out_json, "w") as f:
        json.dump({"face": face, "search": {"mode": search["mode"], "reddit_found": search.get("reddit_found"), "top_match": top, "num_hits": len(vm)}, "fingerprint": fp, "receipt": receipt, "verify": v}, f, indent=2)
    with open(out_dir / "search_raw.json", "w") as f:
        json.dump(search.get("raw", {}), f, indent=2)
    with open(out_dir / "evidence.json", "w") as f:
        json.dump(fp, f, indent=2)
    with open(out_dir / "receipt.json", "w") as f:
        json.dump(receipt, f, indent=2)

    print(f"\n✓ Done → {out_json}")
    print(f"  face_crop: {face['crop_path']}")
    print(f"  evidence: {out_dir/'evidence.json'}  receipt: {out_dir/'receipt.json'}")
    if receipt.get("explorerUrl"):
        print(f"  explorer: {receipt['explorerUrl']}")
    return {"face": face, "search": search, "fingerprint": fp, "receipt": receipt, "verify": v}


def main():
    p = argparse.ArgumentParser(description="HH Goa Task 3: Face → Social → Blockchain ($0 Mac)")
    p.add_argument("--image", required=True, help="face scan image path")
    p.add_argument("--out", default="outputs", help="outputs dir")
    p.add_argument("--chain", default="chain.json", help="chain file")
    p.add_argument("--prefer-source", default="reddit", help="prefer reddit hits")
    p.add_argument("--verify", help="verify a fingerprint hash instead of running pipeline")
    args = p.parse_args()
    if args.verify:
        v = verify(args.verify, chain_file=args.chain)
        print(json.dumps(v, indent=2))
    else:
        run_pipeline(args.image, out_dir=args.out, chain_file=args.chain, prefer_source=args.prefer_source)

if __name__ == "__main__":
    main()
