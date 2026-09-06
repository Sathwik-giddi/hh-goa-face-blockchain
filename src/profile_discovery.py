"""Public Profile Discovery v2 — live multi-platform discovery for VERIFIED identities.

Runs only after identity verification (Identity Vault enrollment or a
confidently verified public-figure match). Every candidate originates from a
real cloud search result with full provenance. No demo mode, no fixtures, no
fabricated results: if the web has nothing, the platform reports
"no publicly discoverable profile found".
"""
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timezone
from urllib.parse import urlparse, parse_qsl, urlencode, urlunparse

import requests

IDENTITY_MIN_CONF = float(os.getenv("PROFILE_IDENTITY_MIN_CONF", "0.80"))
CONF_HIGH = float(os.getenv("PROFILE_CONF_HIGH", "0.85"))
CONF_MEDIUM = float(os.getenv("PROFILE_CONF_MEDIUM", "0.65"))
MAX_QUERIES = int(os.getenv("PROFILE_MAX_QUERIES", "26"))
CONCURRENCY = int(os.getenv("PROFILE_CONCURRENCY", "6"))
QUERY_TIMEOUT = int(os.getenv("PROFILE_QUERY_TIMEOUT", "45"))
IMAGE_VERIFY_TOP_N = int(os.getenv("PROFILE_IMAGE_TOP_N", "6"))

WEIGHTS = {
    "name": 0.30, "username": 0.15, "designation": 0.10, "company": 0.15,
    "education": 0.05, "website_backlink": 0.20, "cross_platform": 0.10,
    "image": 0.15, "source_reliability": 0.05,
}
# attainable examples: name+username+image+reliability = 0.65 (medium)
# name+username+image+backlink = 0.85 (high) · name+company+designation+backlink = 0.85 (high)

AGGREGATORS = {"wikipedia.org", "wikidata.org", "famousbirthdays.com", "imdb.com",
               "imdb.name", "sportskeeda.com", "alchetron.com", "wikitia.com", "viki.com"}

NON_PROFILE_GENERIC = re.compile(
    r"/(login|signin|signup|search\b|hashtag|directory|collections|jobs)(/|$|\?)", re.I)


@dataclass
class PlatformDef:
    id: str
    name: str
    domains: list
    profile_patterns: list
    query_templates: list
    content_filters: list
    username_required: bool = False
    enabled: bool = True
    notes: str = ""


PLATFORMS = {
    "linkedin": PlatformDef("linkedin", "LinkedIn", ["linkedin.com"],
        [r"linkedin\.com/in/([\w\-%]+)/?"],
        ['site:linkedin.com/in "{name}"', 'site:linkedin.com/in "{name}" "{company}"',
         'site:linkedin.com/in "{name}" "{designation}"'],
        [r"(jobs|company|posts|feed|search|pulse)"], notes="public /in/ profiles only"),
    "instagram": PlatformDef("instagram", "Instagram", ["instagram.com"],
        [r"instagram\.com/([\w.\-]+)/?"],
        ['site:instagram.com "{name}"', 'site:instagram.com "{name}" "{company}"'],
        [r"(p|reel|reels|stories|explore|tv)"]),
    "facebook": PlatformDef("facebook", "Facebook", ["facebook.com"],
        [r"facebook\.com/([\w.\-]+)/?", r"facebook\.com/profile\.php\?id=(\d+)"],
        ['site:facebook.com "{name}"', 'site:facebook.com/profile.php "{name}"'],
        [r"(posts|videos|photos|groups|events|watch|marketplace|reel)"],
        notes="profile.php keeps its id param in canonical form"),
    "x": PlatformDef("x", "X (Twitter)", ["x.com", "twitter.com"],
        [r"(?:x|twitter)\.com/@?(\w+)/?"],
        ['site:x.com "{name}"', 'site:x.com "{username}"'],
        [r"(status|statuses)"]),
    "github": PlatformDef("github", "GitHub", ["github.com"],
        [r"github\.com/([\w\-]+)/?"],
        ['site:github.com "{name}"', 'site:github.com "{username}"'],
        [r"(blob|tree|pull|issues|discussions|commit|gists|orgs)"],
        notes="only root user pages (one path segment) are profile candidates"),
    "youtube": PlatformDef("youtube", "YouTube", ["youtube.com"],
        [r"youtube\.com/@([\w.\-]+)/?", r"youtube\.com/(?:c|user)/([\w.\-]+)/?",
         r"youtube\.com/channel/([\w\-]+)"],
        ['site:youtube.com "{name}"', 'site:youtube.com "@{name}"'],
        [r"(watch|shorts|playlist|embed)"]),
    "tiktok": PlatformDef("tiktok", "TikTok", ["tiktok.com"],
        [r"tiktok\.com/@([\w.\-]+)/?"],
        ['site:tiktok.com "@{name}"', 'site:tiktok.com "@{username}"'],
        [r"(video|photo|foryou)"]),
    "reddit": PlatformDef("reddit", "Reddit", ["reddit.com"],
        [r"reddit\.com/(?:user|u)/([\w\-]+)/?"],
        ['site:reddit.com/user "{name}"', 'site:reddit.com/u "{username}"'],
        [r"(comments|r/|topic)"]),
    "medium": PlatformDef("medium", "Medium", ["medium.com"],
        [r"medium\.com/@([\w.\-]+)/?", r"https?://([\w\-]+)\.medium\.com/?"],
        ['site:medium.com "{name}"', 'site:medium.com "@{username}"'],
        [r"(p/|tag/|publication/)"],
        notes="@handles or <author>.medium.com roots only — article slugs rejected"),
    "devto": PlatformDef("devto", "Dev.to", ["dev.to"],
        [r"dev\.to/([\w\-]+)/?"],
        ['site:dev.to "{name}"', 'site:dev.to "{username}"'],
        [], notes="profile = exactly one path segment; article slugs rejected"),
    "stackoverflow": PlatformDef("stackoverflow", "Stack Overflow", ["stackoverflow.com"],
        [r"stackoverflow\.com/users/(\d+)/([\w\-]+)"],
        ['site:stackoverflow.com/users "{name}"'],
        [r"(questions|tags|jobs|collections)"]),
    "behance": PlatformDef("behance", "Behance", ["behance.net"],
        [r"behance\.net/([\w\-]+)/?"],
        ['site:behance.net "{name}"'],
        [r"(gallery|joblist|collection)"]),
    "dribbble": PlatformDef("dribbble", "Dribbble", ["dribbble.com"],
        [r"dribbble\.com/([\w\-]+)/?"],
        ['site:dribbble.com "{name}"'],
        [r"(shots|teams)"]),
    "pinterest": PlatformDef("pinterest", "Pinterest", ["pinterest.com"],
        [r"pinterest\.\w+/([\w.\-]+)/?"],
        ['site:pinterest.com "{name}"'],
        [r"(pin|ideas|videos)"]),
    "threads": PlatformDef("threads", "Threads", ["threads.net", "threads.com"],
        [r"threads\.\w+/(@?[\w.\-]+)/?"],
        ['site:threads.net "{name}"'],
        [r"(post|t/)"], notes="public indexing is sparse"),
    "website": PlatformDef("website", "Personal website", [],
        [r"https?://([^/]+)"],
        ['"{name}" official website', '"{name}" portfolio', '"{name}" "{company}"'],
        [], notes="best backlink source for the identity graph"),
}


class ProviderError(Exception):
    pass


class RateLimited(ProviderError):
    pass


# ---------------- provider abstraction ----------------
def get_provider():
    """SearchProvider abstraction (search()). Add providers here; discovery
    logic never changes. Env: SEARCH_PROVIDER (default serpapi)."""
    name = os.getenv("SEARCH_PROVIDER", "serpapi").strip().lower()

    class SerpapiProvider:
        providerName = "serpapi_google"

        @staticmethod
        def search(query: str, api_key: str, num: int = 5) -> list:
            r = requests.get("https://serpapi.com/search.json", params={
                "engine": "google", "q": query, "num": num, "api_key": api_key,
            }, timeout=QUERY_TIMEOUT)
            if r.status_code == 429:
                raise RateLimited("provider rate limited (429)")
            r.raise_for_status()
            j = r.json()
            if j.get("error"):
                raise ProviderError(f"provider error: {j['error']}")
            return j.get("organic_results") or []

    providers = {"serpapi": SerpapiProvider}
    return providers.get(name, SerpapiProvider), SerpapiProvider.providerName


def provider_health_check() -> str:
    try:
        api_key = os.getenv("SERPAPI_API_KEY", "")
        if not api_key:
            return "no API key"
        j = requests.get("https://serpapi.com/account", params={"api_key": api_key}, timeout=10).json()
        return f"ok · {j.get('total_searches_left', '?')} searches left"
    except Exception as e:
        return f"error: {e}"


# ---------------- query generation ----------------
def generate_queries(identity: dict) -> dict:
    """Platform-specific query families from only the fields that exist.
    Returns {platform_id: [queries]} — deduped; no empty-field templates."""
    fields = {
        "name": (identity.get("name") or "").strip(),
        "company": (identity.get("company") or "").strip(),
        "designation": (identity.get("designation") or "").strip(),
        "username": (identity.get("username") or "").strip(),
    }
    out = {}
    for pid, p in PLATFORMS.items():
        if not p.enabled:
            continue
        qs, seen = [], set()
        for tpl in p.query_templates:
            try:
                q = tpl.format(**fields)
            except (KeyError, IndexError):
                continue
            q = re.sub(r"\s+", " ", q).replace('""', "").strip()
            if '""' in q or q.endswith('"') is False or not q or q in seen or f'"{fields["name"]}"' not in q:
                continue
            seen.add(q)
            qs.append(q)
        if qs:
            out[pid] = qs[:3]
    return out


# ---------------- URL canonicalization ----------------
def canonical_url(url: str, platform: str) -> str:
    """Strip tracking params/fragments, normalize host, merge x.com/twitter.com,
    keep identity-bearing params (facebook id)."""
    try:
        u = urlparse(url)
    except Exception:
        return url
    host = re.sub(r"^(www|m|mobile)\.", "", u.netloc.lower())
    if platform == "x" and host == "twitter.com":
        host = "x.com"
    if platform == "threads" and host == "threads.net":
        host = "threads.com"
    keep = []
    if platform == "facebook":
        keep = [(k, v) for k, v in parse_qsl(u.query) if k == "id"]
    path = re.sub(r"/+$", "", u.path or "") or "/"
    return urlunparse(("https", host, path, "", urlencode(keep), ""))


# ---------------- candidate extraction ----------------
def normalize_result(organic: dict, platform_id: str, provider_name: str,
                     query: str, position: int) -> dict | None:
    url = organic.get("link") or organic.get("url") or ""
    if not url.startswith("http"):
        return None
    p = PLATFORMS[platform_id]
    host = re.sub(r"^(www|m|mobile)\.", "", urlparse(url).netloc.lower())
    if platform_id == "website":
        base = host
        if base in AGGREGATORS or any(base == d or base.endswith("." + d) or d in base
                                      for pid2, pp in PLATFORMS.items() if pid2 != "website"
                                      for d in pp.domains):
            return None
        if NON_PROFILE_GENERIC.search(urlparse(url).path):
            return None
        username, canonical = None, canonical_url(url, "website")
    else:
        if not any(host == d or host.endswith("." + d) for d in p.domains):
            return None
        path_probe = (urlparse(url).path + "/").lower()
        if any(re.search(f + r"(/|$)", path_probe) for f in p.content_filters):
            return None
        if NON_PROFILE_GENERIC.search(path_probe):
            return None
        m = None
        for pat in p.profile_patterns:
            m = re.search(pat, url, re.I)
            if m:
                break
        if m is None:
            return None
        username = m.group(1) if m.groups() else None
        if p.username_required and not username:
            return None
        if platform_id in ("github", "devto", "medium", "x", "instagram", "tiktok") \
            and len(urlparse(url).path.strip("/").split("/")) > 1:
            return None  # repo/article pages — only root user pages are profiles
        canonical = canonical_url(url, platform_id)

    return {
        "platform": platform_id,
        "url": url,
        "canonicalUrl": canonical,
        "username": username,
        "displayName": (organic.get("title") or "").split("|")[0].split("•")[0].split(" - ")[0].strip()[:90],
        "title": organic.get("title") or "",
        "snippet": organic.get("snippet") or "",
        "provider": provider_name,
        "query": query,
        "resultPosition": position,
        "sourceUrls": [url],
        "discoveredAt": datetime.now(timezone.utc).isoformat(),
        "imageVerification": {"status": "unavailable", "similarity": None},
    }


# ---------------- evidence scoring ----------------
def score_profile(c: dict, identity: dict) -> dict:
    name = (identity.get("name") or "").lower()
    tokens = [t for t in re.findall(r"[a-z0-9]+", name) if len(t) > 1]
    text = f"{c.get('title','')} {c.get('snippet','')}".lower()
    reasons, earned = [], 0.0

    if name and name in text:
        earned += WEIGHTS["name"]; reasons.append("Exact identity name in title/snippet")
    elif tokens and sum(t in text for t in tokens) >= max(1, len(tokens) - 1):
        earned += WEIGHTS["name"] * 0.6; reasons.append("Identity name tokens present (partial)")

    uname = (c.get("username") or "").lower().replace("-", "").replace("_", "").replace(".", "")
    ref = {name.replace(" ", ""), (identity.get("username") or "").lower().replace(" ", "")} - {""}
    if uname and any(r and (r == uname or r[:6] in uname) for r in ref):
        earned += WEIGHTS["username"]; reasons.append("Username matches identity/known alias")

    ctx = " ".join(filter(None, [identity.get("designation"), identity.get("context")])).lower()
    ctx_tokens = [t for t in re.findall(r"[a-z0-9]+", ctx) if len(t) > 2]
    hits = [t for t in ctx_tokens if t in text]
    if ctx_tokens and len(hits) / len(ctx_tokens) >= 0.5:
        earned += WEIGHTS["designation"]; reasons.append(f"Profession/context matches ({', '.join(hits[:4])})")

    comp = (identity.get("company") or "").lower()
    if comp and comp in text:
        earned += WEIGHTS["company"]; reasons.append("Company match")

    edu = (identity.get("education") or "").lower()
    if edu and edu in text:
        earned += WEIGHTS["education"]; reasons.append("Education match")

    if c.get("_website_backlink"):
        earned += WEIGHTS["website_backlink"]; reasons.append("Personal website links to this profile")
    if c.get("_cross_platform_links"):
        earned += WEIGHTS["cross_platform"]
        reasons.append("Cross-platform corroboration: " + ", ".join(sorted(c["_cross_platform_links"])[:3]))

    iv = c.get("imageVerification") or {}
    if iv.get("status") == "verified":
        earned += WEIGHTS["image"]
        reasons.append(f"Profile image similarity {iv.get('similarity')}% vs verified identity")
    elif iv.get("status") == "not_verified":
        reasons.append("Profile image did not match identity (weight not earned)")
    else:
        reasons.append("Profile image verification unavailable (blocked or absent)")

    earned += WEIGHTS["source_reliability"]  # link already validated live

    # root-profile rule: deeper paths are content pages, capped below MEDIUM
    path = urlparse(c["canonicalUrl"]).path.strip("/")
    if c["platform"] != "website" and path.count("/") > 0:
        earned = min(earned, CONF_MEDIUM - 0.05)
        reasons.append("Deeper page (content), not a root profile — capped")

    conf = max(0.0, min(1.0, round(earned, 3)))
    c["confidence"] = conf
    c["confidenceLevel"] = "high" if conf >= CONF_HIGH else ("medium" if conf >= CONF_MEDIUM else "low")
    c["evidence"] = reasons
    return c


# ---------------- cross-platform graph ----------------
def extract_backlinks(website_url: str, candidates: list) -> dict:
    """Fetch a personal website (public HTML), find links to candidate profiles.
    Returns {candidate_url: True} for corroborated candidates."""
    corroborated = {}
    if not website_url or not website_url.startswith("http"):
        return corroborated
    try:
        r = requests.get(website_url, timeout=8, headers={"User-Agent": "Mozilla/5.0"})
        html = r.text[:300_000]
    except Exception:
        return corroborated
    for c in candidates:
        host = re.sub(r"^(www|m)\.", "", urlparse(c["url"]).netloc.lower())
        uname = re.escape(c.get("username") or "")
        if uname and re.search(rf"{host}/@?{uname}(\"|/|$)", html, re.I):
            corroborated[c["url"]] = True
    return corroborated


def snippet_cross_links(candidates: list) -> dict:
    """Candidates whose snippet references other discovered platforms."""
    links = {c["canonicalUrl"]: set() for c in candidates}
    hosts = {}
    for c in candidates:
        hosts.setdefault(re.sub(r"^(www|m)\.", "", urlparse(c["url"]).netloc.lower()), []).append(c)
    for c in candidates:
        own = re.sub(r"^(www|m)\.", "", urlparse(c["url"]).netloc.lower())
        text = (c.get("snippet") or "").lower()
        for host, others in hosts.items():
            if host == own or not host:
                continue
            if host.split(".")[0] in text:
                for o in others:
                    if o["canonicalUrl"] != c["canonicalUrl"]:
                        links[c["canonicalUrl"]].add(o["platform"])
    return links


# ---------------- ArcFace profile-image verification ----------------
def verify_profile_image(page_url: str, identity_embedding) -> dict:
    """og:image/twitter:image → YuNet → ArcFace → compare. ONE signal of many.
    Status: verified | not_verified | unavailable. Never fails the candidate."""
    if identity_embedding is None:
        return {"status": "unavailable", "similarity": None}
    try:
        r = requests.get(page_url, timeout=8, headers={"User-Agent": "Mozilla/5.0"})
        m = (re.search(r'<meta[^>]+property=["\']og:image["\'][^>]+content=["\']([^"\']+)', r.text)
             or re.search(r'<meta[^>]+name=["\']twitter:image["\'][^>]+content=["\']([^"\']+)', r.text))
        if not m:
            return {"status": "unavailable", "similarity": None}
        ir = requests.get(m.group(1), timeout=8, headers={"User-Agent": "Mozilla/5.0"})
        if ir.status_code >= 400 or not ir.content or len(ir.content) > 8 * 1024 * 1024:
            return {"status": "unavailable", "similarity": None}
        import cv2
        import numpy as np
        img = cv2.imdecode(np.frombuffer(ir.content, np.uint8), cv2.IMREAD_COLOR)
        if img is None:
            return {"status": "unavailable", "similarity": None}
        from .face_id import detect_rows_yunet, _arcface_embed, embedding_similarity
        img2, rows = detect_rows_yunet(img)
        rows.sort(key=lambda x: x[2] * x[3], reverse=True)
        best = None
        for row in rows[:2]:
            feat = _arcface_embed(img2, row)
            if feat is None:
                continue
            s = embedding_similarity(identity_embedding, feat)
            if s is not None and (best is None or s > best):
                best = s
        if best is None:
            return {"status": "unavailable", "similarity": None}
        return {"status": "verified" if best >= 42.5 else "not_verified", "similarity": best}
    except Exception:
        return {"status": "unavailable", "similarity": None}


# ---------------- dedupe ----------------
def _dedupe_candidates(raw: list) -> list:
    """Dedupe by canonicalUrl and platform+username. Keep the first-seen variant
    (provider order = relevance); later duplicates only add corroboration via
    their sourceUrls being merged into the survivor."""
    merged = {}
    order = []
    for c in raw:
        k1 = c["canonicalUrl"].lower()
        k2 = (c["platform"], (c.get("username") or "").lower())
        survivor = None
        for k, existing in merged.items():
            if k == k1 or (c.get("username") and k == k2):
                survivor = existing
                break
        if survivor is not None:
            survivor.setdefault("sourceUrls", [survivor["url"]])
            if c["url"] not in survivor["sourceUrls"]:
                survivor["sourceUrls"].append(c["url"])
            continue
        merged[k1] = c
        merged[k2] = c
        order.append(c)
    return order


# ---------------- orchestration ----------------
def _now():
    return datetime.now(timezone.utc).isoformat()


_DISCLAIMER = "Results are based on publicly available evidence and may not be exhaustive."


def discover_for_identity(identity: dict, identity_embedding=None,
                          api_key: str | None = None) -> dict:
    """Entry point. Requires name + confidence ≥ IDENTITY_MIN_CONF. Optional
    fields: username, company, designation, education, website, aliases, context."""
    name = (identity.get("name") or "").strip()
    conf = float(identity.get("confidence") or 0)
    if not name or len(name) < 3 or conf < IDENTITY_MIN_CONF:
        return {"skipped": True,
                "reason": "Identity confidence insufficient.\nProfile association skipped."}

    api_key = api_key or os.getenv("SERPAPI_API_KEY") or ""
    if not api_key:
        return {"skipped": True, "reason": "Provider unavailable: SERPAPI_API_KEY missing."}

    provider, provider_name = get_provider()
    query_map = generate_queries(identity)
    queries = [(pid, q) for pid, qs in query_map.items() for q in qs][:MAX_QUERIES]

    statuses = {pid: {"status": "SEARCHED", "queries": 0, "results": 0}
                for pid in PLATFORMS if pid in query_map}
    raw_candidates = []

    def run_query(pid, q):
        for attempt in range(2):
            try:
                return provider.search(q, api_key)
            except RateLimited:
                statuses[pid]["status"] = "RATE LIMITED"
                if attempt == 0:
                    time.sleep(3)
                    continue
                raise
        return []

    with ThreadPoolExecutor(max_workers=CONCURRENCY) as ex:
        futs = {ex.submit(run_query, pid, q): (pid, q) for pid, q in queries}
        for fut in as_completed(futs):
            pid, q = futs[fut]
            statuses[pid]["queries"] += 1
            try:
                results = fut.result()
            except RateLimited:
                continue
            except ProviderError as e:
                statuses[pid]["status"] = f"PROVIDER ERROR — {str(e)[:60]}"
                continue
            except Exception as e:
                statuses[pid]["status"] = f"PROVIDER ERROR — {str(e)[:60]}"
                continue
            statuses[pid]["results"] += len(results)
            for pos, organic in enumerate(results):
                n = normalize_result(organic, pid, provider_name, q, pos + 1)
                if n:
                    raw_candidates.append(n)

    if not raw_candidates:
        for pid, st in statuses.items():
            if st["status"] == "SEARCHED" and st["results"] == 0:
                st["status"] = "NO PUBLIC RESULT"
        return {"identity": {"name": name, "confidence": round(conf, 3),
                             "source": identity.get("source", "")},
                "provider": provider_name, "queriesExecuted": len(queries),
                "platformStatuses": statuses, "stats": {
                    "platformsSearched": len(statuses), "profilesDiscovered": 0,
                    "highConfidence": 0, "dropped": 0},
                "profiles": [], "dropped": [], "discovered_at": _now(),
                "empty_message": "No verified public profiles discovered.",
                "disclaimer": _DISCLAIMER}

    candidates = _dedupe_candidates(raw_candidates)

    def check(c):
        from .search import _validate_link
        ok, note, _ = _validate_link(c["url"])
        return c["url"], ok, note

    link_results = {}
    with ThreadPoolExecutor(max_workers=CONCURRENCY) as ex:
        for fut in as_completed({ex.submit(check, c) for c in candidates}):
            url, ok, note = fut.result()
            link_results[url] = (ok, note)
    for c in candidates:
        c["_link_ok"], c["_link_note"] = link_results[c["url"]]

    dropped = [{"platform": c["platform"], "url": c["url"],
                "reason": f"source unreachable ({c.get('_link_note')}) — may be deleted or private"}
               for c in candidates if not c.get("_link_ok")]
    candidates = [c for c in candidates if c.get("_link_ok")]

    # cross-platform graph: personal-website backlinks + snippet cross-links
    website_candidate = next((c for c in candidates if c["platform"] == "website"), None)
    if website_candidate:
        for c in candidates:
            if c["url"] in extract_backlinks(website_candidate["url"], candidates):
                c["_website_backlink"] = True
    xlinks = snippet_cross_links(candidates)
    for c in candidates:
        c["_cross_platform_links"] = sorted(xlinks.get(c["canonicalUrl"], set()))

    # ArcFace image verification for the most promising candidates
    def prior(c):
        text = (c.get("title", "") + " " + c.get("snippet", "")).lower()
        s = 1.0 if name.lower() in text else 0.0
        return s + (0.5 if c.get("username") else 0.0) + (0.25 if c["platform"] == "website" else 0.0)

    for c in sorted(candidates, key=prior, reverse=True)[:IMAGE_VERIFY_TOP_N]:
        c["imageVerification"] = verify_profile_image(c["url"], identity_embedding)

    scored = [score_profile(c, identity) for c in candidates]
    scored.sort(key=lambda c: (-c["confidence"], -len(c.get("evidence") or [])))

    for c in scored:
        if c["platform"] in statuses:
            statuses[c["platform"]]["status"] = "FOUND"
    found_platforms = {c["platform"] for c in scored}
    for pid, st in statuses.items():
        if st["status"] == "SEARCHED" and (st["results"] == 0 or pid not in found_platforms):
            st["status"] = "NO PUBLIC RESULT"

    profiles = []
    for c in scored:
        c["provenance"] = {"provider": c["provider"], "query": c["query"],
                            "position": c["resultPosition"], "demo": False}
        c["crossPlatformEvidence"] = []
        if c.get("_website_backlink"):
            c["crossPlatformEvidence"].append({"type": "website_backlink",
                "from": website_candidate["canonicalUrl"] if website_candidate else None})
        if c.get("_cross_platform_links"):
            c["crossPlatformEvidence"].append({"type": "snippet_cross_links",
                "platforms": c["_cross_platform_links"]})
        c.pop("title", None); c.pop("snippet", None)
        c.pop("_link_ok", None); c.pop("_link_note", None)
        c.pop("_website_backlink", None); c.pop("_cross_platform_links", None)
        profiles.append(c)

    return {
        "identity": {"name": name, "confidence": round(conf, 3),
                     "source": identity.get("source", "")},
        "provider": provider_name,
        "queriesExecuted": len(queries),
        "platformStatuses": statuses,
        "stats": {"platformsSearched": len(statuses),
                   "profilesDiscovered": len(profiles),
                   "highConfidence": sum(1 for p in profiles if p["confidenceLevel"] == "high"),
                   "dropped": len(dropped)},
        "profiles": profiles,
        "dropped": dropped,
        "discovered_at": _now(),
        "empty_message": "No verified public profiles discovered.",
        "disclaimer": _DISCLAIMER,
    }


def render_case_file_section(discovery: dict) -> str:
    """Serialize discovery into the Evidence Case File HTML section."""
    import html
    if not discovery or discovery.get("profiles") is None:
        return ""
    e = html.escape
    ident = discovery.get("identity") or {}
    stats = discovery.get("stats") or {}
    rows = []
    for p in discovery.get("profiles", []):
        reasons = "<br>".join("✓ " + e(r) for r in p.get("evidence", []))
        prov = p.get("provenance") or {}
        iv = p.get("imageVerification") or {}
        rows.append(
            f"<tr><td>{e(p.get('platform',''))}</td>"
            f"<td class='mono'>{e(p.get('displayName') or '—')}<br>{e(p.get('username') or '—')}</td>"
            f"<td class='mono'><a href='{e(p.get('canonicalUrl') or p['url'])}'>{e(p.get('canonicalUrl') or p['url'])}</a></td>"
            f"<td>{int(round(p.get('confidence',0)*100))}% ({e(p.get('confidenceLevel',''))})</td>"
            f"<td>{reasons}</td>"
            f"<td class='mono'>{e(str(prov.get('provider')))}<br>pos {e(str(prov.get('position')))}</td>"
            f"<td class='mono'>{e(str(iv.get('status')))}{(' ' + str(iv.get('similarity')) + '%') if iv.get('similarity') else ''}</td>"
            f"<td class='mono'>{e(str(p.get('discoveredAt')))}</td></tr>")
    return f"""
<h2>Public profile discovery</h2>
<p class="mono">Identity: {e(str(ident.get('name')))} · identity confidence {e(str(ident.get('confidence')))} ·
platforms searched {e(str(stats.get('platformsSearched', len(discovery.get('platformStatuses') or {}))))} ·
profiles discovered {e(str(stats.get('profilesDiscovered', len(discovery.get('profiles', [])))))} ·
discovered {e(str(discovery.get('discovered_at')))}<br>
Results are based on publicly available evidence and may not be exhaustive.</p>
<table>
<tr><td>Platform</td><td>Profile</td><td>Canonical URL</td><td>Confidence</td><td>Evidence</td><td>Provenance</td><td>Image check</td><td>Discovered</td></tr>
{''.join(rows) or '<tr><td colspan="8">No publicly discoverable profile found.</td></tr>'}</table>
"""
