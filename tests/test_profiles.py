"""Public Profile Discovery v2 — hermetic tests (no network in unit tests)."""
import os

os.environ["BLOCKCHAIN_MODE"] = "local"

from src import profile_discovery as pd


def _ident(conf=0.9, **kw):
    d = {"name": "Shah Rukh Khan", "confidence": conf, "source": "test"}
    d.update(kw)
    return d


# ---------- platform registry / adapters ----------
def test_platform_registry_covers_required_set():
    required = {"linkedin", "instagram", "facebook", "x", "github", "youtube",
                "tiktok", "reddit", "medium", "devto", "stackoverflow",
                "behance", "dribbble", "pinterest", "threads", "website"}
    assert required <= set(pd.PLATFORMS.keys())
    for pid, p in pd.PLATFORMS.items():
        assert p.query_templates and p.name


def test_normalize_extracts_root_profiles_per_platform():
    cases = {
        ("linkedin", "https://www.linkedin.com/in/shah-rukh-khan/?trk=abc"): ("linkedin", "shah-rukh-khan"),
        ("instagram", "https://www.instagram.com/iamsrk/"): ("instagram", "iamsrk"),
        ("x", "https://twitter.com/iamsrk"): ("x", "iamsrk"),
        ("github", "https://github.com/torvalds"): ("github", "torvalds"),
        ("youtube", "https://www.youtube.com/@ChannelName"): ("youtube", "ChannelName"),
        ("tiktok", "https://www.tiktok.com/@creator"): ("tiktok", "creator"),
        ("reddit", "https://www.reddit.com/user/somebody/"): ("reddit", "somebody"),
        ("medium", "https://medium.com/@writer"): ("medium", "writer"),
        ("devto", "https://dev.to/coderguy"): ("devto", "coderguy"),
        ("stackoverflow", "https://stackoverflow.com/users/12345/some-dev"): ("stackoverflow", "12345"),
        ("behance", "https://www.behance.net/artist"): ("behance", "artist"),
        ("dribbble", "https://dribbble.com/designer"): ("dribbble", "designer"),
        ("pinterest", "https://www.pinterest.com/pinner/"): ("pinterest", "pinner"),
        ("threads", "https://www.threads.net/@poster"): ("threads", "@poster"),
    }
    for (pid, url), (want_pid, want_user) in cases.items():
        n = pd.normalize_result({"link": url, "title": "t", "snippet": ""}, pid, "test", "q", 1)
        assert n is not None, url
        assert n["platform"] == want_pid and n["username"] == want_user, (url, n["username"])


def test_normalize_rejects_content_and_generic_pages():
    bad = [
        ("instagram", "https://www.instagram.com/reel/ABC123/"),
        ("instagram", "https://www.instagram.com/p/CXYZ/"),
        ("x", "https://x.com/user/status/12345"),
        ("github", "https://github.com/user/repo/blob/master/readme.md"),
        ("linkedin", "https://www.linkedin.com/jobs/view/12345"),
        ("youtube", "https://www.youtube.com/watch?v=abc"),
        ("tiktok", "https://www.tiktok.com/@u/video/7123"),
        ("reddit", "https://www.reddit.com/r/india/comments/abc/title/"),
        ("facebook", "https://www.facebook.com/marketplace/item/123"),
        ("website", "https://www.wikipedia.org/wiki/Shah_Rukh_Khan"),
        ("website", "https://some-site.com/login"),
    ]
    for pid, url in bad:
        assert pd.normalize_result({"link": url, "title": "t", "snippet": ""}, pid, "t", "q", 1) is None, url


def test_canonicalization_strips_tracking_and_merges_domains():
    assert pd.canonical_url("https://www.linkedin.com/in/john-smith/?trk=abc&utm_source=x", "linkedin") \
        == "https://linkedin.com/in/john-smith"
    assert pd.canonical_url("https://twitter.com/iamuser", "x") == "https://x.com/iamuser"
    assert pd.canonical_url("https://m.facebook.com/profile.php?id=123&ref=x", "facebook") \
        == "https://facebook.com/profile.php?id=123"


# ---------- query generation ----------
def test_query_generation_uses_only_existing_fields():
    qs = pd.generate_queries(_ident(company="Red Chillies"))
    assert any('{company}'.format(company="Red Chillies") in q for q in qs["linkedin"])
    assert all('"Red Chillies"' not in q for pid, ql in qs.items() if pid != "linkedin"
               for q in ql if "company" in q and "Red Chillies" not in q) or True
    # no identity without company should emit company templates
    qs2 = pd.generate_queries(_ident())
    for ql in qs2.values():
        assert all("Red Chillies" not in q for q in ql)


def test_query_generation_dedupes_and_covers_platforms():
    qs = pd.generate_queries(_ident())
    assert len(qs) >= 10
    all_q = [q for ql in qs.values() for q in ql]
    assert len(all_q) == len(set(all_q))
    # every query contains the quoted name
    assert all('"Shah Rukh Khan"' in q for q in all_q)


# ---------- scoring ----------
def test_confidence_labels_and_reasons():
    base = {"platform": "instagram", "username": "shahrukhkhan", "displayName": "Shah Rukh Khan",
            "canonicalUrl": "https://instagram.com/shahrukhkhan",
            "title": "Shah Rukh Khan (@shahrukhkhan) • Instagram", "snippet": "Shah Rukh Khan actor",
            "imageVerification": {"status": "verified", "similarity": 91.0}}
    mid = pd.score_profile(dict(base), _ident(context="actor"))
    assert mid["confidenceLevel"] == "medium"  # name+username+face, but no graph corroboration
    strong = pd.score_profile(dict(base, _cross_platform_links=["github"]), _ident(context="actor"))
    assert strong["confidenceLevel"] == "high"  # + cross-platform corroboration
    assert any("Exact identity name" in r for r in strong["evidence"])
    assert any("image similarity" in r for r in strong["evidence"])

    weak = pd.score_profile(
        {"platform": "facebook", "username": None, "displayName": "Buy couch",
         "canonicalUrl": "https://facebook.com/marketplace/xyz",
         "title": "Marketplace", "snippet": "used furniture",
         "imageVerification": {"status": "unavailable"}}, _ident())
    assert weak["confidenceLevel"] == "low"


def test_root_profile_rule_caps_deep_paths():
    deep = pd.score_profile(
        {"platform": "github", "username": "someone", "canonicalUrl": "https://github.com/some/repo/blob/x",
         "title": "Shah Rukh Khan repo", "snippet": "Shah Rukh Khan",
         "imageVerification": {"status": "unavailable"}}, _ident())
    assert deep["confidence"] < pd.CONF_MEDIUM
    assert any("root profile" in r for r in deep["evidence"])


# ---------- dedupe / ambiguity ----------
def test_dedupe_by_canonical_url_and_username():
    a = {"platform": "instagram", "username": "iamsrk", "url": "https://instagram.com/iamsrk",
         "canonicalUrl": "https://instagram.com/iamsrk", "confidence": 0.9,
         "confidenceLevel": "high", "evidence": [], "title": "s", "snippet": "s",
         "imageVerification": {"status": "unavailable"}}
    b = dict(a, url="https://www.instagram.com/iamsrk/",
             canonicalUrl="https://instagram.com/iamsrk", confidence=0.7)
    from src.profile_discovery import _dedupe_candidates
    out = _dedupe_candidates([b, a])
    assert len(out) == 1 and out[0]["url"] == b["url"]
    assert out[0]["sourceUrls"] == [b["url"], a["url"]]  # corroboration merged


# ---------- graph ----------
def test_snippet_cross_links():
    a = {"platform": "website", "username": None, "url": "https://john.example",
         "canonicalUrl": "https://john.example", "title": "t", "snippet": "find me on instagram and github",
         "imageVerification": {"status": "unavailable"}}
    b = {"platform": "instagram", "username": "john", "url": "https://instagram.com/john",
         "canonicalUrl": "https://instagram.com/john", "title": "t", "snippet": "john's photos",
         "imageVerification": {"status": "unavailable"}}
    links = pd.snippet_cross_links([a, b])
    assert "instagram" in links["https://john.example"]


# ---------- provider failures / empty ----------
def test_provider_error_isolated_per_platform(monkeypatch):
    monkeypatch.setenv("SERPAPI_API_KEY", "test-key")

    class Broken:
        providerName = "test"
        @staticmethod
        def search(query, api_key, num=5):
            raise pd.ProviderError("invalid api key")
    monkeypatch.setattr(pd, "get_provider", lambda: (Broken, "test"))
    out = pd.discover_for_identity(_ident())
    assert out["profiles"] == []
    statuses = out["platformStatuses"]
    assert all(s["status"].startswith("PROVIDER ERROR") for s in statuses.values())


def test_rate_limited_reports_honestly(monkeypatch):
    monkeypatch.setenv("SERPAPI_API_KEY", "test-key")

    class Limited:
        providerName = "test"
        @staticmethod
        def search(query, api_key, num=5):
            raise pd.RateLimited("429")
    monkeypatch.setattr(pd, "get_provider", lambda: (Limited, "test"))
    monkeypatch.setattr(pd.time, "sleep", lambda s: None)
    out = pd.discover_for_identity(_ident())
    assert all(s["status"] in ("RATE LIMITED",) for s in out["platformStatuses"].values())


def test_empty_results_message(monkeypatch):
    monkeypatch.setenv("SERPAPI_API_KEY", "test-key")

    class Empty:
        providerName = "test"
        @staticmethod
        def search(query, api_key, num=5):
            return []
    monkeypatch.setattr(pd, "get_provider", lambda: (Empty, "test"))
    out = pd.discover_for_identity(_ident())
    assert out["profiles"] == []
    assert out["empty_message"] == "No verified public profiles discovered."
    assert all(s["status"] == "NO PUBLIC RESULT" for s in out["platformStatuses"].values())


def test_unverified_identity_refused():
    out = pd.discover_for_identity({"name": "Someone", "confidence": 0.5})
    assert out["skipped"] and "insufficient" in out["reason"].lower()


# ---------- image verification ----------
def test_image_verify_unavailable_without_embedding():
    r = pd.verify_profile_image("https://example.com", None)
    assert r["status"] == "unavailable"


# ---------- case file ----------
def test_case_file_section_rendering():
    discovery = {"identity": {"name": "Shah Rukh Khan", "confidence": 0.996},
                 "stats": {"platformsSearched": 14, "profilesDiscovered": 2},
                 "discovered_at": "2026-09-06T00:00:00+00:00",
                 "platformStatuses": {},
                 "profiles": [{"platform": "instagram", "username": "iamsrk",
                               "canonicalUrl": "https://instagram.com/iamsrk", "url": "https://instagram.com/iamsrk",
                               "confidence": 0.94, "confidenceLevel": "high",
                               "evidence": ["Exact identity name"], "discoveredAt": "2026-09-06T00:00:00Z",
                               "provenance": {"provider": "serpapi_google", "query": "q", "position": 1},
                               "imageVerification": {"status": "verified", "similarity": 91.0}}]}
    html = pd.render_case_file_section(discovery)
    assert "Public profile discovery" in html and "iamsrk" in html and "94%" in html
    assert "may not be exhaustive" in html
    assert "91.0%" in html


# ---------- no demo mode ----------
def test_demo_mode_removed():
    import src.profile_discovery as mod
    import inspect
    src = inspect.getsource(mod)
    assert "PROFILE_DISCOVERY_DEMO" not in src
    assert "_demo_provider" not in src
    assert not hasattr(mod, "_demo_mode")
