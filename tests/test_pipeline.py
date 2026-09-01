from pathlib import Path
from src.pipeline import run_pipeline

def test_e2e_mock():
    r = run_pipeline("data/samples/lena.jpg", out_dir=Path("outputs"), chain_file=Path("chain.json"), verbose=False)
    assert r["face"]["num_faces"] >= 0
    assert "fingerprint" in r
    assert r["verify"]["verified"] is True
    # tamper should fail
    from src.blockchain_local import verify_local
    tampered = r["fingerprint"]["fingerprint_sha256"][:-1] + "0"
    assert verify_local(tampered, chain_file="chain.json")["verified"] is False
