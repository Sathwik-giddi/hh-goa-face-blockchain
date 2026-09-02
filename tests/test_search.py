"""Search-module tests: pure helpers, hermetic error paths, and ranking logic.

The live SerpAPI calls are monkeypatched away; ranking/fallback production
behavior is exercised with synthetic hits.
"""
import pytest

from src.search import _hamming, _phash, reverse_image_search


def _hit(source="reddit", dist=None, link="http://x", title="t", dl=None, thumb="http://t/x"):
    h = {"source": source, "title": title, "link": link, "thumbnail": thumb}
    if dl is not None:
        h["displayed_link"] = dl
    if dist is not None:
        h["_face_distance"] = dist
    return h


def test_hamming_mismatched_or_invalid_inputs():
    assert _hamming("", "0" * 16) == 999
    assert _hamming("0" * 16, "0" * 8) == 999
    assert _hamming("zz", "aa") == 999  # invalid hex -> 999, not crash
    assert _hamming("0" * 16, "0" * 16) == 0
    assert _hamming("00", "0f") == 4


def test_phash_non_image_returns_empty(tmp_path):
    f = tmp_path / "garbage.jpg"
    f.write_bytes(b"this is definitely not an image")
    assert _phash(f) == ""


def test_reverse_search_requires_api_key(tmp_path, monkeypatch):
    monkeypatch.delenv("SERPAPI_API_KEY", raising=False)
    img = tmp_path / "face.jpg"
    img.write_bytes(b"x")
    with pytest.raises(RuntimeError, match="SERPAPI_API_KEY"):
        reverse_image_search(img, api_key=None)


def test_reverse_search_missing_file_raises():
    with pytest.raises(FileNotFoundError):
        reverse_image_search("/tmp/does-not-exist.jpg", api_key="k")


def _input_image(tmp_path):
    img = tmp_path / "face.jpg"
    img.write_bytes(b"fake image bytes")
    return img


def _score_map(url_to_dist):
    """Stub scorer for src.search._score_thumbnail keyed on the thumbnail URL."""

    def fake(url, qh):
        for prefix, d in url_to_dist.items():
            if prefix in url:
                return (d, None)
        return (None, "unscored")

    return fake


def test_ranking_prefers_face_similar_and_reddit_boost(tmp_path, monkeypatch):
    img = _input_image(tmp_path)
    hits = [
        _hit(source="instagram", link="http://ig/1", title="ig close", thumb="http://t/ig"),
        _hit(source="reddit", link="http://reddit/1", title="reddit farther", thumb="http://t/reddit"),
        _hit(source="twitter", link="http://tw/1", title="unscored", thumb="http://t/tw"),
        _hit(source="google", link="http://g/1", dl="reddit.com/r/x", title="reddit via dl", thumb="http://t/g"),
    ]
    # distances are assigned by the scorer (production overwrites hit-distances)
    monkeypatch.setattr("src.search._fetch_hits", lambda *a, **k: (hits, [("fake", {})]))
    monkeypatch.setattr(
        "src.search._score_thumbnail",
        _score_map({"t/ig": 3, "t/reddit": 10, "t/g": 20}),
    )
    r = reverse_image_search(img, api_key="k")
    # reddit boost (-15) lifts dist-10 reddit above dist-3 instagram
    assert r["top_match"]["link"] == "http://reddit/1"
    assert r["reddit_found"] is True
    # face-similar threshold (<=12) excludes the dist-20 and unscored hits
    assert r["face_similar_count"] == 2
    assert len(r["visual_matches"]) == 2
    assert all(h["_face_distance"] is not None for h in r["visual_matches"])
    # all_hits keeps every hit including unscored (distance None)
    assert len(r["all_hits"]) == 4
    assert {h["link"] for h in r["all_hits"]} == {"http://ig/1", "http://reddit/1", "http://tw/1", "http://g/1"}


def test_ranking_face_similar_carries_existing_distances(tmp_path, monkeypatch):
    """Distances computed by the scorer are inserted into the hits."""
    img = _input_image(tmp_path)
    hits = [_hit(source="x", link="http://a/1", thumb="http://t/a"), _hit(source="x", link="http://b/1", thumb="http://t/b")]
    monkeypatch.setattr("src.search._fetch_hits", lambda *a, **k: (hits, [("fake", {})]))
    monkeypatch.setattr("src.search._score_thumbnail", _score_map({"t/a": 4, "t/b": 99}))
    r = reverse_image_search(img, api_key="k")
    assert r["visual_matches"] == [hits[0]]
    assert r["face_similar_count"] == 1
    assert hits[0]["_face_distance"] == 4 and hits[1]["_face_distance"] == 99


def test_ranking_falls_back_to_all_when_none_face_similar(tmp_path, monkeypatch):
    img = _input_image(tmp_path)
    hits = [
        _hit(source="instagram", link="http://ig/1", title="a", thumb="http://t/a"),
        _hit(source="twitter", link="http://tw/1", title="b", thumb="http://t/b"),
    ]
    monkeypatch.setattr("src.search._fetch_hits", lambda *a, **k: (hits, [("fake", {})]))
    monkeypatch.setattr("src.search._score_thumbnail", lambda url, qh: (None, None))
    r = reverse_image_search(img, api_key="k")
    assert r["face_similar_count"] == 0
    # fallback: visual_matches == everything
    assert len(r["visual_matches"]) == 2
    assert r["top_match"] is not None
    # no reddit source and no reddit link -> reddit_found False
    assert r["reddit_found"] is False
    assert r["mode"] == "live"


def test_ranking_displayed_link_reddit_boost(tmp_path, monkeypatch):
    """A hit whose displayed_link is reddit.com gets the same boost as source=reddit."""
    img = _input_image(tmp_path)
    hits = [
        _hit(source="google", link="http://g/1", dl="reddit.com/r/x", title="dl reddit", thumb="http://t/g"),
        _hit(source="instagram", link="http://ig/1", title="closer", thumb="http://t/ig"),
    ]
    monkeypatch.setattr("src.search._fetch_hits", lambda *a, **k: (hits, [("fake", {})]))
    monkeypatch.setattr("src.search._score_thumbnail", _score_map({"t/g": 20, "t/ig": 3}))
    r = reverse_image_search(img, api_key="k")
    # 20 - 15 = 5 > 3, so the closer instagram hit still wins
    assert r["top_match"]["link"] == "http://ig/1"
    assert r["reddit_found"] is True
