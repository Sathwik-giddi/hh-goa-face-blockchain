"""Public Profile Discovery — social profiles for a VERIFIED identity only.

Runs after the identity-verification pipeline has established who the subject
is, via either:
  1. the Identity Vault (consented enrollment), or
  2. a confidently verified public figure (top_confident ArcFace match).

It never profiles unidentified strangers: `discover_for_identity()` requires a
verified identity and refuses otherwise. All data comes from live public web
search; nothing is fabricated. Association is scored transparently with
human-readable reasons, and uncertain matches are labeled LOW, never claimed.
"""
import os
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from urllib.parse import urlparse

import requests

def _demo_mode() -> bool:
    return os.getenv("PROFILE_DISCOVERY_DEMO", "").strip().lower() in ("1", "true", "yes")


IDENTITY_MIN_CONF = float(os.getenv("PROFILE_IDENTITY_MIN_CONF", "0.80"))
CONF_HIGH = float(os.getenv("PROFILE_CONF_HIGH", "0.85"))
CONF_MEDIUM = float(os.getenv("PROFILE_CONF_MEDIUM", "0.65"))

# Platform registry — add a row to support a new platform; nothing else changes.
PLATFORMS = {
    "linkedin": {"filter": "site:linkedin.com/in", "pattern": r"linkedin\.com/in/([\w\-%]+)"},
    "instagram": {"filter": "site:instagram.com", "pattern": r"instagram\.com/([\w.\-]+)/?"},
    "x": {"filter": "(site:x.com OR site:twitter.com)", "pattern": r"(?:x|twitter)\.com/@?(\w+)"},
    "facebook": {"filter": "site:facebook.com", "pattern": r"facebook\.com/([\w.\-]+)"},
    "youtube": {"filter": "site:youtube.com", "pattern": r"youtube\.com/@([\w.\-]+)"},
    "github": {"filter": "site:github.com", "pattern": r"github\.com/(\w+)"},
    "tiktok": {"filter": "site:tiktok.com", "pattern": r"tiktok\.com/@([\w.\-]+)"},
    "reddit": {"filter": "site:reddit.com/user", "pattern": r"reddit\.com/user/([\w\-]+)"},
}
# URL shapes that are content, not a person's profile page.
NON_PROFILE_URL = re.compile(
    r"/(p|reels?|watch|status|gist|gists|comments|posts|video|videos|shorts|popular|explore|marketplace|events"
    r"|groups|blob|tree|pull|issues|questions|topic|tag)(/|$)", re.I)

# Domains that are about-pages/directories, not the person's own profile.
AGGREGATORS = {"wikipedia.org", "wikidata.org", "famousbirthdays.com", "imdb.com",
               "imdb.name", "sportskeeda.com", "wallsdesk.com", "alchetron.com"}


class ProviderError(Exception):
    """A search provider failed (network, rate limit, bad key)."""


def _demo_provider(query: str, api_key: str, num: int = 5) -> list:
    """Isolated Demo Mode provider — active ONLY when PROFILE_DISCOVERY_DEMO=true.
    Clearly marked at every stage so it can never masquerade as real evidence
    and never reaches a real Evidence Case File or the blockchain workflow."""
    return [{
        "_demo": True,
        "link": "https://example.com/",
        "title": "DEMO DATA — Example Domain (synthetic fixture, not a real discovery)",
        "snippet": "Synthetic demo record. Shown only because Demo Mode is enabled; never evidence.",
    }]
    """A search provider failed (network, rate limit, bad key)."""


def _provider():
    """Search provider abstraction — add another provider here, nothing else changes.

    Demo Mode (PROFILE_DISCOVERY_DEMO=true) force-routes everything to the
    isolated demo provider: real search is never mixed with demo fixtures.
    """
    if _demo_mode():
        return _demo_provider
    provider = os.getenv("PROFILE_SEARCH_PROVIDER", "serpapi_google")

    def serpapi_google(query: str, api_key: str, num: int = 5) -> list:
        r = requests.get("https://serpapi.com/search.json", params={
            "engine": "google", "q": query, "num": num, "api_key": api_key,
        }, timeout=45)
        r.raise_for_status()
        j = r.json()
        if j.get("error"):
            raise ProviderError(f"provider error: {j['error']}")
        return j.get("organic_results") or []

    providers = {"serpapi_google": serpapi_google}
    return providers.get(provider, serpapi_google)


def _slug_variants(name: str) -> list:
    """URL/username slugs a person's name plausibly maps to."""
    tokens = [t for t in re.findall(r"[a-z0-9]+", name.lower()) if len(t) > 1]
    joined = "".join(tokens)
    return {joined, "-".join(tokens), "_".join(tokens), ".".join(tokens)} - {""}


def normalize_result(organic: dict) -> dict | None:
    """One provider result → normalized candidate profile (or None)."""
    url = organic.get("link") or ""
    if not url.startswith("http"):
        return None
    host = urlparse(url).netloc.lower()
    platform, username = "website", None
    for plat, cfg in PLATFORMS.items():
        m = re.search(cfg["pattern"], url, re.I)
        if m:
            platform, username = plat, m.group(1)
            break
    if NON_PROFILE_URL.search(urlparse(url).path) or host == "gist.github.com":
        return None  # a post/reel/video/repo-file — content, not a profile
    if platform == "website" and any(a in host for a in AGGREGATORS):
        return None  # directory/about pages are not the person's profile
    return {
        "platform": platform,
        "username": username,
        "displayName": (organic.get("title") or "").split("|")[0].split("•")[0].strip()[:90],
        "url": url,
        "host": host,
        "title": organic.get("title") or "",
        "snippet": organic.get("snippet") or "",
        "_query_platform": organic.get("_query_platform"),
        "_discovered_via": organic.get("_discovered_via"),
        "_demo": bool(organic.get("_demo")),
    }


def score_profile(candidate: dict, identity: dict) -> dict:
    """Transparent association scoring. Every point comes with a reason.

    identity: {"name": str, "context": str|None}
    Returns the candidate with confidence (0..1), confidenceLevel, reasons.
    """
    name = identity["name"]
    text = f"{candidate.get('title','')} {candidate.get('snippet','')}".lower()
    tokens = [t for t in re.findall(r"[a-z0-9]+", name.lower()) if len(t) > 1]
    reasons, conf = [], 0.0

    if name.lower() in text:
        conf += 0.45; reasons.append("Exact identity name in page title/snippet")
    elif tokens and all(t in text for t in tokens):
        conf += 0.30; reasons.append("All identity name tokens present")

    slugs = _slug_variants(name)
    handle = (candidate.get("username") or "").lower().replace("-", "").replace("_", "").replace(".", "")
    if candidate.get("username") and any(s.replace("-", "").replace("_", "").replace(".", "") == handle for s in slugs):
        conf += 0.15; reasons.append("Username consistent with identity name")
    elif candidate.get("username") and any(s[:5] in handle for s in slugs if len(s) >= 5):
        conf += 0.08; reasons.append("Username partially matches identity name")

    ctx_tokens = [t for t in re.findall(r"[a-z0-9]+", (identity.get("context") or "").lower()) if len(t) > 2]
    ctx_hits = [t for t in ctx_tokens if t in text]
    if ctx_tokens:
        ratio = len(ctx_hits) / len(ctx_tokens)
        if ratio >= 0.5:
            conf += 0.15; reasons.append(f"Professional context matches ({', '.join(ctx_hits[:4])})")
        elif ctx_hits:
            conf += 0.06; reasons.append(f"Partial professional context match ({', '.join(ctx_hits[:3])})")

    if re.search(r"[@•|]\s*$|\(@\w+\)", candidate.get("title", "")):
        conf += 0.10; reasons.append("Official profile-page title format")

    if candidate.get("_query_platform") == candidate["platform"]:
        conf += 0.15; reasons.append(f"Surfaced by dedicated {candidate['platform']} profile search")
    if any(h in candidate.get("snippet", "").lower() for h in ("instagram.com", "linkedin.com", "x.com", "twitter.com", "youtube.com", "github.com")):
        conf += 0.05; reasons.append("Snippet references the person's other public profiles")

    if candidate["host"] in AGGREGATORS:
        conf -= 0.20; reasons.append("Directory/about page penalty")

    # A profile is a root user page. Any deeper path (repo file, thread,
    # sub-page) means this is content ABOUT the person, not their account.
    path = urlparse(candidate["url"]).path.strip("/")
    depth_ok = candidate["platform"] in ("website",) or path.count("/") <= 0
    if not depth_ok:
        conf = min(conf, CONF_MEDIUM - 0.05)
        reasons.append("Deeper page (content), not a root profile — capped")

    conf = max(0.0, min(1.0, round(conf, 2)))
    level = "high" if conf >= CONF_HIGH else ("medium" if conf >= CONF_MEDIUM else "low")
    candidate.update({
        "confidence": conf,
        "confidenceLevel": level,
        "verificationReasons": reasons or ["Insufficient public signals"],
    })
    return candidate


def verify_profile(candidate: dict) -> dict:
    """Fetch the profile URL: live page required; dead/deleted → dropped."""
    from .search import _validate_link
    ok, note, final = _validate_link(candidate["url"])
    candidate["_link_ok"] = ok
    candidate["_link_note"] = note
    if not ok:
        candidate["_drop_reason"] = f"source unreachable ({note}) — profile may be deleted or private"
    else:
        candidate["verificationReasons"] = candidate["verificationReasons"] + ["Source page reachable"]
    return candidate


def rank_profiles(candidates: list) -> list:
    """Dedupe (URL and platform+username), drop dead links, resolve ambiguity,
    sort by confidence."""
    by_url, out = set(), []
    for c in sorted(candidates, key=lambda x: -x.get("confidence", 0)):
        key = c["url"].rstrip("/").lower()
        pkey = (c["platform"], (c.get("username") or "").lower())
        if key in by_url or (c.get("username") and pkey in {(o["platform"], (o.get("username") or "").lower()) for o in out}):
            continue
        by_url.add(key)
        out.append(c)

    # Ambiguity: two live same-platform profiles with different usernames →
    # neither may be claimed; downgrade both to low with a stated reason.
    seen = {}
    for c in out:
        if not c.get("_link_ok"):
            continue
        k = c["platform"]
        seen.setdefault(k, []).append(c)
    for plat, group in seen.items():
        if len(group) > 1 and group[0]["confidenceLevel"] == group[1]["confidenceLevel"]:
            for c in group[:2]:
                c["confidence"] = round(min(c["confidence"], CONF_MEDIUM - 0.01), 2)
                c["confidenceLevel"] = "low"
                c["verificationReasons"] = c["verificationReasons"] + \
                    [f"Ambiguous: multiple live {plat} profiles match — treated as unconfirmed"]

    out.sort(key=lambda c: -c.get("confidence", 0))
    return out


def discover_for_identity(identity: dict, api_key: str | None = None,
                          max_platforms: int = 9) -> dict:
    """Entry point. identity = {"name": str, "confidence": float 0..1,
    "source": str, "context": str|None}.

    Returns {"skipped": reason} when identity is unverified, else
    {"identity": identity, "profiles": [...], "searched": [...], "discovered_at": iso}.
    """
    name = (identity.get("name") or "").strip()
    conf = float(identity.get("confidence") or 0)
    if not name or len(name) < 3:
        return {"skipped": True, "reason": "Identity confidence insufficient.\nProfile association skipped."}
    if conf < IDENTITY_MIN_CONF:
        return {"skipped": True, "reason": "Identity confidence insufficient.\nProfile association skipped."}

    demo = _demo_mode()
    api_key = api_key or os.getenv("SERPAPI_API_KEY") or ""
    if not api_key and not demo:
        return {"skipped": True, "reason": "Search provider unavailable: SERPAPI_API_KEY missing."}

    context = identity.get("context")
    q_ctx = f' "{context}"' if context else ""
    provider = _provider()
    provider_name = "demo" if demo else os.getenv("PROFILE_SEARCH_PROVIDER", "serpapi_google")

    jobs = {}
    queries_log = []
    with ThreadPoolExecutor(max_workers=5) as ex:
        futs = {}
        for plat, cfg in list(PLATFORMS.items())[:max_platforms]:
            q = f'"{name}" {cfg["filter"]}{q_ctx}'
            queries_log.append(q)
            futs[ex.submit(provider, q, api_key)] = (plat, q)
        # one general query for personal websites
        q_site = f'"{name}" official website{q_ctx}'
        queries_log.append(q_site)
        futs[ex.submit(provider, q_site, api_key)] = ("website", q_site)
        for fut in as_completed(futs):
            plat, q = futs[fut]
            try:
                for pos, organic in enumerate(fut.result()):
                    organic["_query_platform"] = plat
                    organic["_discovered_via"] = {"provider": provider_name,
                                                  "query": q, "position": organic.get("position") or pos + 1}
                    jobs[organic.get("link") or id(organic)] = organic
            except ProviderError as e:
                print(f"[profile_discovery] {plat}: {e}")
            except Exception as e:
                print(f"[profile_discovery] {plat}: {e}")

    candidates = []
    for organic in jobs.values():
        n = normalize_result(organic)
        if n:
            candidates.append(score_profile(n, identity))

    candidates = [verify_profile(c) for c in candidates]
    live = [c for c in candidates if c.get("_link_ok")]
    dropped = [{"platform": c["platform"], "url": c["url"], "reason": c.get("_drop_reason")}
               for c in candidates if not c.get("_link_ok")]

    profiles = rank_profiles(live)
    demo_leak = False
    for p in profiles:
        demo = bool(p.pop("_demo", False))
        demo_leak = demo_leak or demo
        via = p.pop("_discovered_via", None)
        p.pop("title", None); p.pop("snippet", None); p.pop("host", None)
        p.pop("_query_platform", None); p.pop("_link_ok", None); p.pop("_link_note", None); p.pop("_link_final", None)
        # Provenance: kept on every profile for the View Evidence inspector.
        p["provenance"] = {
            "provider": (via or {}).get("provider", provider_name),
            "query": (via or {}).get("query"),
            "position": (via or {}).get("position"),
            "discoveredViaUrl": p["url"],
            "demo": demo,
        }
        p["sourceUrls"] = [p["url"]]
        p["discoveredAt"] = datetime.now(timezone.utc).isoformat()
        if demo:
            dn = p.get("displayName") or ""
            p["displayName"] = dn if dn.startswith("DEMO DATA") else "[DEMO DATA] " + dn
            p["verificationReasons"] = ["DEMO MODE — synthetic record, not real evidence"]

    return {
        "identity": {"name": name, "confidence": round(conf, 3), "source": identity.get("source", "")},
        "demo_mode": demo,
        "demo_data": demo_leak or demo,
        "provider": provider_name,
        "queries": queries_log,
        "profiles": profiles,
        "dropped": dropped,
        "searched": [p for p, _ in PLATFORMS.items()][:max_platforms] + ["website"],
        "discovered_at": datetime.now(timezone.utc).isoformat(),
        "disclaimer": "Results are based on publicly available evidence and may not be exhaustive.",
        "empty_message": "No verified public profiles discovered.",
    }


def render_case_file_section(discovery: dict) -> str:
    """Serialize a discovery result into the Evidence Case File HTML section."""
    import html
    if not discovery or discovery.get("profiles") is None:
        return ""
    e = html.escape
    ident = discovery.get("identity") or {}
    if discovery.get("demo_mode"):
        return ""  # demo results are never rendered into a real case file
    rows = []
    for p in discovery.get("profiles", []):
        reasons = "<br>".join("✓ " + e(r) for r in p.get("verificationReasons", []))
        rows.append(
            f"<tr><td>{e(p.get('platform',''))}</td>"
            f"<td class='mono'>{e(p.get('username') or '—')}</td>"
            f"<td class='mono'><a href='{e(p['url'])}'>{e(p['url'])}</a></td>"
            f"<td>{int(round(p.get('confidence',0)*100))}% ({e(p.get('confidenceLevel',''))})</td>"
            f"<td>{reasons}</td></tr>")
    return f"""
<h2>Public profile discovery</h2>
<p class="mono">Identity: {e(str(ident.get('name')))} · identity confidence {e(str(ident.get('confidence')))} ·
discovered {e(str(discovery.get('discovered_at')))}<br>
Results are based on publicly available evidence and may not be exhaustive.</p>
<table><tr><td>Platform</td><td>Username</td><td>URL</td><td>Confidence</td><td>Verification reasons</td></tr>
{''.join(rows) or '<tr><td colspan="5">No public profiles met the evidence bar.</td></tr>'}</table>
"""
