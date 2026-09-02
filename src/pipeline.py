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
from .utils import fingerprint_post, reverify_independent, download_image
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
        print("\n[1/8] INPUT")
        print(f"  → {image} ({image.stat().st_size} bytes)")
    face = detect_and_encode(image, out_dir=out_dir)
    if not face.get("crop_path"):
        raise RuntimeError(
            f"Face detection failed: {face.get('warning', 'no crop_path')}. "
            "Use a photo with a visible face (frontal or 3-quarter)."
        )
    if verbose:
        print("[2/8] FACE")
        print(f"  ✓ engine={face['engine']} faces={face['num_faces']} conf={face.get('conf', 0):.2f}")
        print(f"  ✓ embedding (pHash) {face.get('embedding_hash')} → crop {face['crop_path']}")
        if face.get("warning"):
            print(f"  ⚠ {face['warning']}")

    if verbose:
        print("[3/8] WEB SEARCH (live — Google Lens via SerpAPI)")
    search = reverse_image_search(face["crop_path"], original_path=str(image), prefer_source=prefer_source)
    vm = search.get("visual_matches", [])
    top = search.get("top_match")
    if not top:
        raise RuntimeError(
            "Live search returned 0 hits — no public indexed copy of this face. "
            "Try a face that is posted publicly (IG/X/Reddit)."
        )
    if verbose:
        print(f"  ✓ mode={search['mode']} queries={search.get('num_queries', 1)} hits={len(search.get('all_hits', []))} (face-embedded {search.get('face_similar_count')}) reddit_found={search.get('reddit_found')}")
        print("[4/8] MATCH")
        sim = top.get("_face_sim")
        link_ok = bool(top.get("_link_valid"))
        print(f"  ✓ [{top.get('source')}] face_sim={sim if sim is not None else 'n/a'}% link={'✓ ' + top.get('_link_note','') if link_ok else '✗ ' + top.get('_link_note','unverified')} {top.get('title', '')[:70]}")
        print(f"    {top.get('link')}")
        if sim is None or sim < 42.5 or not link_ok:
            raise RuntimeError(
                "No citable match: the best candidates either do not contain a similar enough "
                "face or their source pages are unreachable/redirected (link rot). The pipeline "
                "will not anchor unverified evidence — try a face with a public footprint."
            )

    if verbose:
        print("[5/8] FINGERPRINT (deterministic canonical record → SHA-256)")
    # §15: hash the post's image as retrieved from the source (not our crop),
    # so independent re-verification can re-download the same URL and re-hash.
    post_image = download_image(top.get("thumbnail") or "", out_dir / "_post_image.jpg")
    if post_image is None and top.get("image"):
        post_image = download_image(top["image"], out_dir / "_post_image.jpg")
    fp = fingerprint_post(top, image_path=post_image)
    fingerprint = fp["fingerprint_sha256"]
    payload = {
        "post": top,
        "face": {k: face[k] for k in ["engine", "conf", "bbox", "embedding_hash"] if k in face},
        "search_mode": search["mode"],
        "reddit_found": search.get("reddit_found"),
        "face_similar_count": search.get("face_similar_count"),
    }
    if verbose:
        print("  ✓ canonical record fields: url, title, source, thumbnail, image_sha256 (sorted keys, UTF-8)")
        print(f"  ✓ fingerprint {fingerprint}")

    if verbose:
        print(f"[6/8] BLOCKCHAIN ({os.getenv('BLOCKCHAIN_MODE', 'local')})")
    receipt = anchor(fingerprint, payload, chain_file=str(chain_file))
    if verbose:
        if receipt.get("deduplicated"):
            print("  ✓ already anchored on-chain (dedupe) — no new tx needed")
        else:
            print("  ✓ tx submitted + confirmed")
        print(f"    {receipt.get('txHash') or receipt.get('block_hash')}")
        if receipt.get("explorerUrl"):
            print(f"    {receipt['explorerUrl']}")

    if verbose:
        print("[7/8] VERIFICATION (independent)")
    v = verify(fingerprint, chain_file=str(chain_file))
    on_chain = v.get("verified") is True
    if verbose:
        print(f"  {'✓' if on_chain else '✗'} on-chain read: verified={v.get('verified')}")
        for t in v.get("txs", [])[:3]:
            if t.get("via") == "contract":
                print(f"    contract state anchoredAt ✓ ({t.get('anchoredAt')})")
            elif t.get("txHash"):
                print(f"    tx calldata == fingerprint ✓ ({t.get('txHash')[:18]}…)")
    # §17: re-retrieve the discovered content from the live web, re-canonicalize,
    # re-hash — compare against the stored fingerprint. Never fakes success.
    rv = reverify_independent(fp, out_dir=out_dir)
    if verbose:
        m = rv.get("match")
        print(f"  {'✓' if m else '✗'} re-retrieved content re-hash: match={m}"
              f" (re-downloaded={rv.get('re_downloaded_image')})")
        print(f"    recomputed {rv.get('recomputed_fingerprint')[:24]}…")

    if verbose:
        print("[8/8] RESULT" + ("  + TAMPER TEST" if on_chain else ""))
    tampered = fingerprint[:-1] + ("0" if fingerprint[-1] != "0" else "1")
    vt = verify(tampered, chain_file=str(chain_file))
    if verbose:
        print(f"  {'✓ VERIFIED' if on_chain else '✗ NOT VERIFIED'}")
        print(f"    original fingerprint  {fingerprint}")
        print(f"    modified fingerprint  {tampered}")
        print(f"    modified verifies as  {vt.get('verified')} (expected False)")
        print(f"    → {'TAMPER DETECTED ✓' if vt.get('verified') is False else '⚠ tamper NOT detected'}")

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
        "fingerprint": fp, "receipt": receipt, "verify": v, "reverify": rv,
    })
    _save("search_raw.json", search.get("raw", {}))
    _save("evidence.json", fp)
    _save("receipt.json", receipt)
    _save("reverify.json", rv)

    if verbose:
        print(f"\n✓ Done → {out_dir / 'result.json'}")
        print(f"  face_crop: {face['crop_path']}")
        print(f"  evidence: {out_dir / 'evidence.json'}  receipt: {out_dir / 'receipt.json'}  reverify: {out_dir / 'reverify.json'}")
        if receipt.get("explorerUrl"):
            print(f"  explorer: {receipt['explorerUrl']}")
    return {"face": face, "search": search, "fingerprint": fp, "receipt": receipt, "verify": v, "reverify": rv}


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
