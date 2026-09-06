"""Public Profile Discovery — unit tests (hermetic, no network)."""
import os
import sys

os.environ["BLOCKCHAIN_MODE"] = "local"

from src import profile_discovery as pd


def _ident(conf=0.9):
    return {"name": "Shah Rukh Khan", "confidence": conf, "source": "test"}


def test_normalize_extracts_platform_and_username():
    n = pd.normalize_result({"link": "https://www.instagram.com/iamsrk/", "title": "Shah Rukh Khan (@iamsrk)", "snippet": ""})
    assert n["platform"] == "instagram" and n["username"] == "iamsrk"
    n2 = pd.normalize_result({"link": "https://x.com/udupendra", "title": "Udupendra", "snippet": ""})
    assert n2["platform"] == "x" and n2["username"] == "udupendra"
    n3 = pd.normalize_result({"link": "https://youtube.com/@channel", "title": "t", "snippet": ""})
    assert n3["platform"] == "youtube"


def test_normalize_rejects_content_urls():
    # statuses, posts, reels, videos are content — not the person's profile page
    assert pd.normalize_result({"link": "https://x.com/udupendra/status/123", "title": "t", "snippet": ""}) is None
    assert pd.normalize_result({"link": "https://www.instagram.com/reel/ABC123/", "title": "t", "snippet": ""}) is None
    assert pd.normalize_result({"link": "https://gist.github.com/abc123", "title": "t", "snippet": ""}) is None


def test_normalize_rejects_bad_and_aggregator_urls():
    assert pd.normalize_result({"link": "not-a-url", "title": "", "snippet": ""}) is None
    assert pd.normalize_result({"link": "https://en.wikipedia.org/wiki/Shah_Rukh_Khan", "title": "t", "snippet": ""}) is None
    assert pd.normalize_result({"link": "https://www.famousbirthdays.com/people/shah-rukh-khan.html", "title": "t", "snippet": ""}) is None


def test_confidence_labels_and_reasons():
    strong = pd.score_profile(
        {"platform": "instagram", "username": "shahrukhkhan", "displayName": "Shah Rukh Khan",
         "url": "https://instagram.com/shahrukhkhan", "host": "instagram.com",
         "title": "Shah Rukh Khan (@shahrukhkhan) • Instagram", "snippet": "Shah Rukh Khan actor bollywood",
         "_query_platform": "instagram"},
        _ident())
    assert strong["confidenceLevel"] == "high" and strong["confidence"] >= pd.CONF_HIGH
    assert any("Exact identity name" in r for r in strong["verificationReasons"])

    weak = pd.score_profile(
        {"platform": "facebook", "username": None, "displayName": "Buy couch cheap",
         "url": "https://facebook.com/marketplace/xyz", "host": "facebook.com",
         "title": "Marketplace — couch", "snippet": "used furniture", "_query_platform": "facebook"},
        _ident())
    assert weak["confidenceLevel"] == "low" and weak["confidence"] < pd.CONF_MEDIUM


def test_duplicate_removal_keeps_best():
    a = {"platform": "instagram", "username": "iamsrk", "url": "https://instagram.com/iamsrk/",
         "host": "instagram.com", "title": "Shah Rukh Khan", "snippet": "Shah Rukh Khan actor",
         "_query_platform": "instagram", "confidence": 0.9, "confidenceLevel": "high",
         "verificationReasons": [], "_link_ok": True}
    b = dict(a, url="https://instagram.com/iamsrk", confidence=0.7, confidenceLevel="medium")
    ranked = pd.rank_profiles([b, a])
    assert len(ranked) == 1 and ranked[0]["confidence"] == 0.9


def test_ambiguous_same_platform_downgraded():
    def prof(u, conf):
        return {"platform": "instagram", "username": u, "url": f"https://instagram.com/{u}",
                "host": "instagram.com", "title": "Shah Rukh Khan", "snippet": "Shah Rukh Khan actor",
                "_query_platform": "instagram", "confidence": conf, "confidenceLevel": "high",
                "verificationReasons": [], "_link_ok": True}
    ranked = pd.rank_profiles([prof("iamsrk", 0.9), prof("shahrukhkhanfan", 0.88)])
    tops = [c for c in ranked if c["confidenceLevel"] == "high"]
    assert len(tops) <= 1
    assert any("Ambiguous" in r for c in ranked for r in c["verificationReasons"])


def test_unverified_identity_is_refused():
    out = pd.discover_for_identity({"name": "Shah Rukh Khan", "confidence": 0.5, "source": "t"})
    assert out["skipped"] and "insufficient" in out["reason"].lower()
    out2 = pd.discover_for_identity({"name": "", "confidence": 0.99, "source": "t"})
    assert out2["skipped"]


def test_empty_provider_results_shape(monkeypatch):
    monkeypatch.setenv("SERPAPI_API_KEY", "test-key")

    def empty_provider(query, api_key, num=5):
        return []
    monkeypatch.setattr(pd, "_provider", lambda: empty_provider)
    out = pd.discover_for_identity(_ident())
    assert out["profiles"] == [] and "disclaimer" in out


def test_provider_failure_is_contained(monkeypatch):
    monkeypatch.setenv("SERPAPI_API_KEY", "test-key")

    def broken(query, api_key, num=5):
        raise pd.ProviderError("rate limited")
    monkeypatch.setattr(pd, "_provider", lambda: broken)
    out = pd.discover_for_identity(_ident())
    assert out["profiles"] == []  # failed cleanly, never fabricated


def test_case_file_section_rendering():
    discovery = {
        "identity": {"name": "Shah Rukh Khan", "confidence": 0.996},
        "discovered_at": "2026-09-05T00:00:00+00:00",
        "profiles": [{"platform": "instagram", "username": "iamsrk",
                      "url": "https://www.instagram.com/iamsrk/", "confidence": 0.94,
                      "confidenceLevel": "high",
                      "verificationReasons": ["Exact identity name", "Source page reachable"]}],
    }
    html = pd.render_case_file_section(discovery)
    assert "Public profile discovery" in html
    assert "iamsrk" in html and "94%" in html
    assert "may not be exhaustive" in html
    assert pd.render_case_file_section({"profiles": []})  # renders empty-state row
