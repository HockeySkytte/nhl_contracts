"""Hardened, disk-cached 2-layer NHL contract & trade discovery engine (v3).

Key upgrades over v2:
  1. Persistent local HTTP disk cache (cache/http/) so no URL is fetched twice.
  2. Polite rate-limited fetching for ProHockeyRumors (max 1 concurrent, >=0.4s gap)
     while NHL Web API & NHL Forge API run fast in parallel.
  3. Sentence-level player isolation in multi-player signing articles.
  4. Strict two-way contract matching (ignores 'two-way forward/defenseman').
  5. Current-team filtering on trade/waiver acquisition_url.
  6. Automatic signing_team inference from article headline/tags.
  7. Strict separation of active 2026-27 contract vs future 2027-28+ extension.
  8. Optional PHR 2026 Salary Cap Deep Dive parser for ELC cap hits & dead cap.

Zero cap/contract database sites (PuckPedia, CapWages, Spotrac, etc.) are queried.
"""

from __future__ import annotations

import hashlib
import json
import re
import sys
import threading
import time
import unicodedata
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CACHE_DIR = ROOT / "cache" / "http"
DISCOVERY_DIR = ROOT / "cache" / "discovery"

HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}

CURRENT_START_YEAR = 2026
CURRENT_SEASON = "2026-27"
CURRENT_SEASON_INT = 20262027

# (teamId, nhl_path, forge_prefix, full_name, nicknames_regex)
TEAM_INFO = {
    "ANA": (24, "ducks", "ana", "Anaheim Ducks", r"anaheim|ducks"),
    "BOS": (6, "bruins", "bos", "Boston Bruins", r"boston|bruins"),
    "BUF": (7, "sabres", "buf", "Buffalo Sabres", r"buffalo|sabres"),
    "CGY": (20, "flames", "cgy", "Calgary Flames", r"calgary|flames"),
    "CAR": (12, "hurricanes", "car", "Carolina Hurricanes", r"carolina|hurricanes"),
    "CHI": (16, "blackhawks", "chi", "Chicago Blackhawks", r"chicago|blackhawks"),
    "COL": (21, "avalanche", "col", "Colorado Avalanche", r"colorado|avalanche"),
    "CBJ": (29, "bluejackets", "cbj", "Columbus Blue Jackets", r"columbus|blue jackets"),
    "DAL": (25, "stars", "dal", "Dallas Stars", r"dallas|stars"),
    "DET": (17, "redwings", "det", "Detroit Red Wings", r"detroit|red wings"),
    "EDM": (22, "oilers", "edm", "Edmonton Oilers", r"edmonton|oilers"),
    "FLA": (13, "panthers", "fla", "Florida Panthers", r"florida|panthers"),
    "LAK": (26, "kings", "lak", "Los Angeles Kings", r"los angeles|kings"),
    "MIN": (30, "wild", "min", "Minnesota Wild", r"minnesota|wild"),
    "MTL": (8, "canadiens", "mtl", "Montreal Canadiens", r"montreal|canadiens|habs"),
    "NSH": (18, "predators", "nsh", "Nashville Predators", r"nashville|predators"),
    "NJD": (1, "devils", "njd", "New Jersey Devils", r"new jersey|devils"),
    "NYI": (2, "islanders", "nyi", "New York Islanders", r"islanders"),
    "NYR": (3, "rangers", "nyr", "New York Rangers", r"rangers"),
    "OTT": (9, "senators", "ott", "Ottawa Senators", r"ottawa|senators"),
    "PHI": (4, "flyers", "phi", "Philadelphia Flyers", r"philadelphia|flyers"),
    "PIT": (5, "penguins", "pit", "Pittsburgh Penguins", r"pittsburgh|penguins"),
    "SJS": (28, "sharks", "sjs", "San Jose Sharks", r"san jose|sharks"),
    "SEA": (55, "kraken", "sea", "Seattle Kraken", r"seattle|kraken"),
    "STL": (19, "blues", "stl", "St. Louis Blues", r"st\.?\s*louis|blues"),
    "TBL": (14, "lightning", "tbl", "Tampa Bay Lightning", r"tampa bay|lightning"),
    "TOR": (10, "mapleleafs", "tor", "Toronto Maple Leafs", r"toronto|maple leafs|leafs"),
    "UTA": (68, "utah", "uta", "Utah Mammoth", r"utah|mammoth|hockey club"),
    "VAN": (23, "canucks", "van", "Vancouver Canucks", r"vancouver|canucks"),
    "VGK": (54, "goldenknights", "vgk", "Vegas Golden Knights", r"vegas|golden knights"),
    "WSH": (15, "capitals", "wsh", "Washington Capitals", r"washington|capitals|caps"),
    "WPG": (52, "jets", "wpg", "Winnipeg Jets", r"winnipeg|jets"),
}

TEAM_TAG_TO_PATH = {f"{pfx}-created": path for _, path, pfx, _, _ in TEAM_INFO.values()}
TEAM_ID_TO_ABBREV = {tid: code for code, (tid, _, _, _, _) in TEAM_INFO.items()}

WORD_NUMS = {
    "one": 1, "two": 2, "three": 3, "four": 4,
    "five": 5, "six": 6, "seven": 7, "eight": 8,
}

BUYOUT_RE = re.compile(r"\b(bought out|buyout|buy out|termination|terminated)\b", re.I)
UNDISCLOSED_RE = re.compile(
    r"(?:financial\s+)?terms\s+(?:of\s+the\s+(?:contract|deal)\s+)?were\s+not\s+(?:disclosed|announced|released)",
    re.I,
)
SIGN_HL_RE = re.compile(
    r"\b(sign|signs|signed|re-sign|re-signs|re-signed|extend|extends|extended|extension|agree|agrees|agreed|entry-level|elc)\b",
    re.I,
)
EXCLUDE_SIGN_HL_RE = re.compile(
    r"\b(bought out|buyout|buy out|recall|recalled|loan|loaned|assign|assigned|reassign|reassigned|activate|activated|place.*on ir|waivers|pto|professional tryout|notes:|snapshots:|rumor|rumour|interested|open to|talks|could|might|projected|lineup|recap|deep dive|captain|suspension|hearing|injury|out with|out one|awards|finalists|declines|poll:|hoping to|focus:|buzz)\b",
    re.I,
)
TRADE_HL_RE = re.compile(r"\b(trade|traded|acquire|acquires|acquired|claim|claimed|waivers|offer sheet)\b", re.I)

_phr_lock = threading.Lock()
_phr_last_ts = 0.0
PHR_MIN_INTERVAL = 0.45  # ~2 req/sec max to avoid CDN rate limits


def slugify_name(name: str) -> str:
    s = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode("ascii")
    s = s.lower().replace("'", "").replace(".", "")
    return re.sub(r"[^a-z0-9]+", "-", s).strip("-")


_NON_FIRST_TOKENS = {
    "sign", "signs", "signed", "re", "with", "for", "of", "to", "on", "and",
    "the", "a", "an", "forward", "defenseman", "defenceman", "goaltender",
    "goalie", "center", "centre", "winger", "captain", "prospect", "veteran",
    "rookie", "contract", "extension", "deal", "terms", "new", "agree",
    "agrees", "agreed", "acquire", "acquires", "acquired", "trade", "traded",
    "match", "matches", "matched", "extend", "extends", "extended",
}


def has_conflicting_first_name(slug_or_hl: str, first_name: str, last_name: str) -> bool:
    """Return True if slug/headline explicitly names a DIFFERENT first name before last_name
    (prevents brother/same-surname collisions such as Jack/Luke Hughes or Marcus/Nick Foligno)."""
    txt = unicodedata.normalize("NFKD", slug_or_hl).encode("ascii", "ignore").decode("ascii").lower()
    first = unicodedata.normalize("NFKD", first_name).encode("ascii", "ignore").decode("ascii").lower().replace("'", "").replace(".", "")
    last = unicodedata.normalize("NFKD", last_name).encode("ascii", "ignore").decode("ascii").lower().replace("'", "").replace(".", "")
    if not first or not last:
        return False
    # Check hyphenated/spaced '<word> <last>' occurrences
    matches = re.findall(rf"\b([a-z]+)[-\s]+{re.escape(last)}\b", txt)
    if not matches:
        return False
    # If the player's own first name (or 3-char prefix / compound part) appears before last, no conflict
    first_parts = [p for p in re.split(r"[-\s]+", first) if len(p) >= 2]
    for tok in matches:
        if any(tok == fp or fp.startswith(tok[:3]) or tok.startswith(fp[:3]) for fp in first_parts if len(fp) >= 3):
            return False
    # Otherwise, if a plausible different first name appears immediately before last, reject!
    for tok in matches:
        if len(tok) >= 3 and tok not in _NON_FIRST_TOKENS:
            return True
    return False


def _cache_path(url: str) -> Path:
    h = hashlib.sha256(url.encode("utf-8")).hexdigest()[:24]
    domain = re.sub(r"[^a-z0-9]+", "_", url.split("/")[2].lower())
    d = CACHE_DIR / domain
    d.mkdir(parents=True, exist_ok=True)
    return d / f"{h}.txt"


def fetch_cached(url: str, timeout: int = 16, allow_404_cache: bool = True) -> str | None:
    cp = _cache_path(url)
    if cp.exists():
        txt = cp.read_text(encoding="utf-8", errors="replace")
        return None if txt == "__HTTP_404__" else txt

    is_phr = "prohockeyrumors.com" in url
    if is_phr:
        global _phr_last_ts
        with _phr_lock:
            wait = PHR_MIN_INTERVAL - (time.monotonic() - _phr_last_ts)
            if wait > 0:
                time.sleep(wait)
            _phr_last_ts = time.monotonic()

    try:
        req = urllib.request.Request(url, headers=HEADERS)
        with urllib.request.urlopen(req, timeout=timeout) as r:
            body = r.read().decode("utf-8", errors="replace")
        cp.write_text(body, encoding="utf-8")
        return body
    except urllib.error.HTTPError as e:
        if e.code == 404 and allow_404_cache:
            cp.write_text("__HTTP_404__", encoding="utf-8")
        return None
    except Exception:
        return None


def fetch_json_cached(url: str) -> dict | None:
    raw = fetch_cached(url)
    if not raw:
        return None
    try:
        return json.loads(raw)
    except Exception:
        return None


def clean_html(raw: str) -> str:
    raw = re.sub(r"<script[^>]*>.*?</script>", " ", raw, flags=re.S | re.I)
    raw = re.sub(r"<style[^>]*>.*?</style>", " ", raw, flags=re.S | re.I)
    raw = re.sub(r"<forge-entity[^>]*>(.*?)</forge-entity>", r"\1", raw)
    raw = re.sub(r"<[^>]+>", " ", raw)
    raw = (
        raw.replace("&#8217;", "'")
        .replace("&#8216;", "'")
        .replace("&#8211;", "-")
        .replace("&#8212;", "-")
        .replace("&#8220;", '"')
        .replace("&#8221;", '"')
        .replace("&amp;", "&")
        .replace("&nbsp;", " ")
    )
    return re.sub(r"\s+", " ", raw).strip()


def parse_money(val_str: str, unit: str | None) -> int:
    num = float(val_str.replace(",", ""))
    if unit:
        u = unit.lower()
        if u.startswith("m"):
            return int(round(num * 1_000_000))
        if u.startswith("k"):
            return int(round(num * 1_000))
    return int(round(num))


def forge_web_url(slug: str, tags: list[str]) -> str:
    if "nhl-created" not in tags:
        for t in tags:
            if t in TEAM_TAG_TO_PATH:
                return f"https://www.nhl.com/{TEAM_TAG_TO_PATH[t]}/news/{slug}"
    return f"https://www.nhl.com/news/{slug}"


def fetch_forge_pages(tag_expr: str, max_pages: int = 8) -> list[dict]:
    items: list[dict] = []
    seen: set[str] = set()
    for page in range(max_pages):
        url = (
            f"https://forge-dapi.d3.nhle.com/v2/content/en-us/stories"
            f"?tags.slug={tag_expr}&$skip={page * 25}&$limit=25"
        )
        d = fetch_json_cached(url)
        if not d:
            break
        batch = d.get("items", [])
        if not batch:
            break
        new_c = 0
        for it in batch:
            sl = it.get("slug", "")
            if sl and sl not in seen:
                seen.add(sl)
                items.append(it)
                new_c += 1
        if new_c == 0 or len(batch) < 25:
            break
    return items


def fetch_story_full(slug: str) -> dict | None:
    d = fetch_json_cached(f"https://forge-dapi.d3.nhle.com/v2/content/en-us/stories/{slug}")
    if not d:
        return None
    tags = [t.get("slug", "") for t in d.get("tags", []) if isinstance(t, dict)]
    body = " ".join(
        clean_html(p["content"])
        for p in d.get("parts", [])
        if p.get("type") == "markdown" and p.get("content")
    )
    return {
        "slug": slug,
        "date": (d.get("contentDate") or "")[:10],
        "headline": d.get("headline") or d.get("title") or "",
        "tags": tags,
        "url": forge_web_url(slug, tags),
        "body": body,
    }


def infer_signing_team_from_text(headline: str, body: str, tags: list[str], fallback: str) -> str:
    # 1. Check headline team name first (highest precision)
    hl_low = headline.lower()
    for code, (_, _, _, _, pat) in TEAM_INFO.items():
        if re.search(rf"\b({pat})\b", hl_low, re.I):
            return code
    # 2. Check team-created tag
    for t in tags:
        m = re.match(r"^([a-z]{3})-created$", t)
        if m:
            for code, (_, _, pfx, _, _) in TEAM_INFO.items():
                if pfx == m.group(1):
                    return code
    # 3. Check first 250 chars of body
    lead_low = body[:250].lower()
    for code, (_, _, _, _, pat) in TEAM_INFO.items():
        if re.search(rf"\b({pat})\b", lead_low, re.I):
            return code
    return fallback


def parse_contract_text(body: str, player_last: str, is_nhl_official: bool = False) -> dict:
    """Extract contract term, AAV, total value, ELC/two-way flags with sentence isolation."""
    idx = body.lower().find(player_last.lower())
    window = body[max(0, idx - 120) : idx + 1400] if idx != -1 else body[:1500]

    # Split into sentences and prioritize sentences explicitly mentioning the player
    sentences = re.split(r"(?<=[.!?])\s+", window)
    player_sents = [s for s in sentences if re.search(rf"\b{re.escape(player_last)}\b", s, re.I)]
    # Combine player-mentioning sentences + adjacent sentence for "It has an AAV of..." follow-ups
    focused_parts = []
    for i, s in enumerate(sentences):
        if re.search(rf"\b{re.escape(player_last)}\b", s, re.I):
            focused_parts.append(s)
            if i + 1 < len(sentences) and not re.search(r"\b[A-Z][a-z]+(?:'s)?\s+(?:deal|contract)\b", sentences[i + 1]):
                focused_parts.append(sentences[i + 1])
    focused = " ".join(dict.fromkeys(focused_parts)) if focused_parts else window[:900]

    def _extract_money(text: str, term_years: int | None) -> tuple[int | None, int | None]:
        aav = None
        total_value = None
        aav_pats = [
            r"(?:average annual value(?:\s*\(AAV\))?|annual average value|AAV|cap hit)\s+(?:of\s+)?\$([\d,]+(?:\.\d+)?)\s*(million\b|MM\b|M\b|K\b)?",
            r"\$([\d,]+(?:\.\d+)?)\s*(million\b|MM\b|M\b|K\b)?\s*(?:average annual value|AAV|cap hit|per season|per year|annually|salary)",
        ]
        for pat in aav_pats:
            m = re.search(pat, text, re.I)
            if m:
                val = parse_money(m.group(1), m.group(2))
                if 700_000 <= val <= 25_000_000:
                    aav = val
                    break

        tot_pats = [
            r"(?:one|two|three|four|five|six|seven|eight|[1-8])[\s-]year,\s+(?:two-way\s+|one-way\s+)?\$([\d,]+(?:\.\d+)?)\s*(million\b|MM\b|M\b|K\b)?",
            r"(?:worth|totaling|valued at)\s+(?:a total of\s+)?\$([\d,]+(?:\.\d+)?)\s*(million\b|MM\b|M\b|K\b)?",
        ]
        for pat in tot_pats:
            m = re.search(pat, text, re.I)
            if m:
                val = parse_money(m.group(1), m.group(2))
                if term_years and 700_000 * term_years <= val <= 25_000_000 * term_years:
                    total_value = val
                    break
                if term_years == 1 and 700_000 <= val <= 25_000_000:
                    total_value = val
                    break

        if not aav and not total_value:
            m_k = re.search(
                r"\$([\d,]+(?:\.\d+)?)\s*(K\b|million\b|MM\b|M\b)?\s*(?:at the NHL level|in the NHL|base salary)",
                text,
                re.I,
            )
            if m_k:
                val = parse_money(m_k.group(1), m_k.group(2))
                if 700_000 <= val <= 25_000_000:
                    aav = val

        if term_years and total_value and not aav:
            aav = int(round(total_value / term_years))
        elif term_years and aav and not total_value:
            total_value = aav * term_years

        if aav is not None and not (700_000 <= aav <= 25_000_000):
            return None, None
        return aav, total_value

    # Term years (prefer focused sentences mentioning player)
    term_years = None
    for blob in (focused, window[:800]):
        m_term = re.search(r"\b(one|two|three|four|five|six|seven|eight|[1-8])[\s-]year\b", blob, re.I)
        if m_term:
            tok = m_term.group(1).lower()
            term_years = WORD_NUMS.get(tok, int(tok) if tok.isdigit() else None)
            break

    is_elc = bool(re.search(r"\bentry[\s-]level\b|\bELC\b", focused)) or bool(
        re.search(r"\bentry[\s-]level\b|\bELC\b", window[:600])
    )
    # Strict two-way contract match (never matches 'two-way defenseman' or 'two-way forward')
    two_way = bool(
        re.search(r"\btwo[\s-]way\s+(?:contract|deal|agreement|extension|pact)\b", window[:900], re.I)
    )

    aav = None
    total_value = None
    if not (is_nhl_official and UNDISCLOSED_RE.search(window[:650])):
        aav, total_value = _extract_money(focused, term_years)
        if aav is None and len(player_sents) <= 2:
            # Fallback to broader window only if focused had no money
            aav, total_value = _extract_money(window[:1000], term_years)

    explicit_start = None
    m_start = re.search(
        r"(?:begins|begin|starts|starting|commencing|kicks in|takes effect)\s+(?:with\s+the\s+|in\s+(?:the\s+)?|at\s+the\s+start\s+of\s+(?:the\s+)?)?(20\d{2}-\d{2})",
        window[:1000],
        re.I,
    )
    if m_start:
        explicit_start = m_start.group(1)
    elif re.search(r"(?:begins|starts|starting|start)\s+(?:next\s+season|after\s+this\s+season)", window[:1000], re.I):
        explicit_start = "NEXT_SEASON"

    explicit_end = None
    m_end = re.search(r"through\s+(?:the\s+)?(20\d{2}-\d{2})", window[:1000], re.I)
    if m_end:
        explicit_end = m_end.group(1)

    status_hint = None
    if is_elc:
        status_hint = "ELC"
    elif re.search(r"\brestricted free agent\b|\bRFA\b", focused, re.I):
        status_hint = "RFA"
    elif re.search(r"\bunrestricted free agent\b|\bUFA\b", focused, re.I):
        status_hint = "UFA"

    quote = " ".join(sentences[:2]).strip()[:320]
    return {
        "term_years": term_years,
        "aav_usd": aav,
        "total_value_usd": total_value,
        "is_elc": is_elc,
        "two_way": two_way,
        "explicit_start": explicit_start,
        "explicit_end": explicit_end,
        "status_hint": status_hint,
        "quote": quote,
    }


def resolve_seasons(
    signed_date: str,
    term_years: int | None,
    explicit_start: str | None,
    explicit_end: str | None,
    is_elc: bool,
    is_extension_hl: bool,
    bio: dict,
) -> tuple[str | None, str | None]:
    if not signed_date or not term_years:
        return None, None
    sy, sm, sd = map(int, signed_date.split("-"))

    if explicit_end:
        end_start = int(explicit_end.split("-")[0])
        start_yr = end_start - term_years + 1
    elif explicit_start and explicit_start != "NEXT_SEASON":
        start_yr = int(explicit_start.split("-")[0])
    elif explicit_start == "NEXT_SEASON":
        start_yr = sy + 1 if sm >= 7 else sy
    elif is_elc:
        by, bm, bd = map(int, bio["birthDate"].split("-"))
        nhl_gp: dict[int, int] = {}
        for st in bio.get("seasonTotals", []):
            if st.get("gameTypeId") == 2 and st.get("leagueAbbrev") == "NHL":
                nhl_gp[st["season"]] = nhl_gp.get(st["season"], 0) + (st.get("gamesPlayed", 0) or 0)
        cand = sy if sm >= 6 else (sy - 1 if nhl_gp.get((sy - 1) * 10000 + sy, 0) > 0 else sy)
        slides = 0
        while slides < 2 and cand < CURRENT_START_YEAR:
            age_sep15 = cand - by - ((9, 15) < (bm, bd))
            if age_sep15 <= 19 and nhl_gp.get(cand * 10000 + (cand + 1), 0) < 10:
                cand += 1
                slides += 1
            else:
                break
        start_yr = cand
    else:
        if is_extension_hl and sm >= 7 and not (sm == 7 and sd <= 15):
            start_yr = sy + 1
        else:
            start_yr = sy

    end_yr = start_yr + term_years - 1
    return f"{start_yr}-{str(start_yr + 1)[-2:]}", f"{end_yr}-{str(end_yr + 1)[-2:]}"


def fetch_phr_player_articles(player_name: str, max_pages: int = 4) -> list[dict]:
    slug = slugify_name(player_name)
    items = []
    seen = set()
    for page in range(1, max_pages + 1):
        u = f"https://www.prohockeyrumors.com/players/{slug}/feed" + (f"?paged={page}" if page > 1 else "")
        xml_txt = fetch_cached(u)
        if not xml_txt:
            break
        xml_txt = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", xml_txt)
        try:
            root = ET.fromstring(xml_txt)
        except Exception:
            break
        batch = root.findall(".//item")
        if not batch:
            break
        for it in batch:
            link = it.findtext("link") or ""
            if not link or link in seen:
                continue
            seen.add(link)
            title = it.findtext("title") or ""
            pub = it.findtext("pubDate") or ""
            dt = ""
            m_d = re.search(r"(\d{2})\s+([A-Za-z]{3})\s+(\d{4})", pub)
            if m_d:
                months = {"Jan":"01","Feb":"02","Mar":"03","Apr":"04","May":"05","Jun":"06","Jul":"07","Aug":"08","Sep":"09","Oct":"10","Nov":"11","Dec":"12"}
                dt = f"{m_d.group(3)}-{months.get(m_d.group(2),'01')}-{m_d.group(1)}"
            items.append({"date": dt, "title": title, "url": link})
        if len(batch) < 15:
            break
    return items


def fetch_phr_full_body(url: str) -> str:
    html = fetch_cached(url)
    if not html:
        return ""
    ps = [clean_html(p) for p in re.findall(r"<p[^>]*>(.*?)</p>", html, re.I | re.S)]
    ps = [p for p in ps if len(p) > 35 and "Pro Hockey Rumors" not in p[:40]]
    return " ".join(ps[:12])


def parse_phr_deep_dive(url: str) -> dict:
    """Parse a ProHockeyRumors 2026 Salary Cap Deep Dive article into:
      - player_aavs: {lowercase_name: {aav_usd, expiry_year, expiry_status, note}}
      - dead_cap_lines: raw dead-cap sentences for penalty extraction
    """
    html = fetch_cached(url)
    if not html:
        return {"url": url, "error": "fetch_failed", "players": {}, "dead_cap_lines": []}
    ps = [clean_html(p) for p in re.findall(r"<p[^>]*>(.*?)</p>", html, re.I | re.S)]
    dead_cap_lines = [
        p for p in ps
        if any(k in p.lower() for k in ("dead cap:", "retention charge", "buyout charge", "bonus carryover", "overage"))
    ]

    # Walk paragraphs tracking current forecast year (2026-27 -> exp 2027, 2027-28 -> exp 2028, etc.)
    players_map: dict[str, dict] = {}
    exp_year = 2027
    for p in ps:
        if "Upper Limit: $104" in p:
            exp_year = 2027
        elif "Upper Limit: $113.5" in p:
            exp_year = 2028
        elif "Upper Limit: $123" in p or "Upper Limit: $127.5" in p:
            exp_year = 2029
        elif "Upper Limit: $133.5" in p or "Upper Limit: $138" in p:
            exp_year = 2030

        m_exp = re.match(r"Expiring Contracts \((RFA|UFA)\):\s*(.+)", p, re.I)
        if not m_exp:
            continue
        status = m_exp.group(1).upper()
        rest = m_exp.group(2)
        if rest.strip().lower() == "none":
            continue
        # Match entries like: F Macklin Celebrini ($975K + ...), D Jake McCabe ($4,513,102)
        for m in re.finditer(
            r"(?:[FDG]\s+)?([A-Z][A-Za-z'\-\.\s]+?)\s*\(\$([\d,]+(?:\.\d+)?)\s*(MM|M|K)?([^)]*)\)",
            rest,
        ):
            name = m.group(1).strip(" ,")
            aav = parse_money(m.group(2), m.group(3))
            extra = (m.group(4) or "").strip()
            if 700_000 <= aav <= 25_000_000:
                end_season = f"{exp_year - 1}-{str(exp_year)[-2:]}"
                players_map[slugify_name(name)] = {
                    "name": name,
                    "aav_usd": aav,
                    "end_season": end_season,
                    "expiry_status": status,
                    "extra": extra,
                    "url": url,
                }
    return {"url": url, "players": players_map, "dead_cap_lines": dead_cap_lines}


def discover_team(team_code: str, deep_dive_url: str | None = None) -> dict:
    team_code = team_code.upper()
    tid, _, tag_prefix, team_name, team_pat = TEAM_INFO[team_code]
    team_re = re.compile(rf"\b({team_pat})\b", re.I)

    roster = fetch_json_cached(f"https://api-web.nhle.com/v1/roster/{team_code}/current")
    if not roster:
        raise RuntimeError(f"Failed to fetch roster for {team_code}")

    players = [
        {
            "id": p["id"],
            "name": f"{p['firstName']['default']} {p['lastName']['default']}",
            "first": p["firstName"]["default"],
            "last": p["lastName"]["default"],
            "pos": p["positionCode"],
            "sweater": p.get("sweaterNumber"),
        }
        for grp in ("forwards", "defensemen", "goalies")
        for p in roster[grp]
    ]

    # Parse optional 2026 PHR Salary Cap Deep Dive
    dd = parse_phr_deep_dive(deep_dive_url) if deep_dive_url else {"players": {}, "dead_cap_lines": []}

    # 1. Fetch official NHL API bios in parallel
    with ThreadPoolExecutor(max_workers=10) as ex:
        bios = dict(
            ex.map(
                lambda p: (p["id"], fetch_json_cached(f"https://api-web.nhle.com/v1/player/{p['id']}/landing") or {}),
                players,
            )
        )

    # 2. Fetch NHL.com Forge pool (team + player)
    pool: dict[str, dict] = {}
    for expr, pages in [
        (f"{tag_prefix}-created,transactions", 10),
        (f"teamid-{tid},free-agency", 8),
        (f"teamid-{tid},transactions", 8),
    ]:
        for it in fetch_forge_pages(expr, max_pages=pages):
            pool[it["slug"]] = it

    def scan_pid(p):
        pid = p["id"]
        out = []
        for expr, pg in [(f"playerid-{pid},free-agency", 2), (f"playerid-{pid},transactions", 2), (f"playerid-{pid}", 2)]:
            out.extend(fetch_forge_pages(expr, max_pages=pg))
        return out

    with ThreadPoolExecutor(max_workers=10) as ex:
        for batch in ex.map(scan_pid, players):
            for it in batch:
                pool[it["slug"]] = it

    # 3. Resolve each player (NHL.com Forge first, then polite PHR RSS fallback, then Deep Dive cross-check)
    def resolve_player(p):
        pid = p["id"]
        bio = bios[pid]
        last_re = re.compile(r"\b" + re.escape(p["last"]) + r"\b", re.I)

        nhl_signings = []
        nhl_trades = []
        for sl, it in pool.items():
            hl = it.get("headline") or it.get("title") or ""
            tags = [t.get("slug", "") for t in it.get("tags", []) if isinstance(t, dict)]
            if "game-recap" in tags or BUYOUT_RE.search(hl):
                continue
            if not (f"playerid-{pid}" in tags or last_re.search(hl)):
                continue
            if has_conflicting_first_name(f"{sl} {hl}", p["first"], p["last"]):
                continue
            dt = (it.get("contentDate") or "")[:10]
            if SIGN_HL_RE.search(hl) and not EXCLUDE_SIGN_HL_RE.search(hl):
                nhl_signings.append((dt, sl, hl))
            # Tightened rule: only consider trade/waiver stories that actually involve the CURRENT team!
            if (
                TRADE_HL_RE.search(hl)
                and not re.search(r"recall|loan|assign|activate|ir\b|buzz|rumor|preview|edge stats|takeaways|adjusting|new-forward-group", hl, re.I)
                and (f"teamid-{tid}" in tags or f"{tag_prefix}-created" in tags or team_re.search(hl))
            ):
                nhl_trades.append((dt, sl, hl))

        nhl_signings.sort(reverse=True)
        seen_s = set()
        parsed_contracts = []

        for dt, sl, hl in nhl_signings:
            if sl in seen_s or len(seen_s) >= 4:
                continue
            seen_s.add(sl)
            st = fetch_story_full(sl)
            if not st or not last_re.search(st["body"]):
                continue
            if p["first"].lower() not in st["body"].lower() and f"playerid-{pid}" not in st["tags"]:
                continue
            terms = parse_contract_text(st["body"], p["last"], is_nhl_official=True)
            if not terms["term_years"]:
                continue
            start_s, end_s = resolve_seasons(
                st["date"], terms["term_years"], terms["explicit_start"], terms["explicit_end"],
                terms["is_elc"], "extension" in hl.lower(), bio
            )
            if not start_s:
                continue
            signing_team = infer_signing_team_from_text(st["headline"], st["body"], st["tags"], team_code)
            parsed_contracts.append({
                "source": "nhl.com",
                "url": st["url"],
                "date": st["date"],
                "headline": st["headline"],
                "signing_team": signing_team,
                "start_season": start_s,
                "end_season": end_s,
                "start_yr": int(start_s.split("-")[0]),
                "end_yr": int(end_s.split("-")[0]),
                "terms": terms,
            })

        has_active_nhl_aav = any(
            c["start_yr"] <= CURRENT_START_YEAR <= c["end_yr"] and c["terms"]["aav_usd"] is not None
            for c in parsed_contracts
        )

        # Layer 2: Polite ProHockeyRumors player RSS lookup when NHL.com didn't yield active AAV
        if not has_active_nhl_aav:
            phr_items = fetch_phr_player_articles(p["name"], max_pages=5)
            phr_sign_cands = [
                it for it in phr_items
                if SIGN_HL_RE.search(it["title"])
                and last_re.search(it["title"])
                and not EXCLUDE_SIGN_HL_RE.search(it["title"])
                and not BUYOUT_RE.search(it["title"])
            ]
            for it in phr_sign_cands[:4]:
                body = fetch_phr_full_body(it["url"])
                if not body or not last_re.search(body):
                    continue
                terms = parse_contract_text(body, p["last"], is_nhl_official=False)
                if not terms["term_years"]:
                    continue
                start_s, end_s = resolve_seasons(
                    it["date"], terms["term_years"], terms["explicit_start"], terms["explicit_end"],
                    terms["is_elc"], "extension" in it["title"].lower() or "extend" in it["title"].lower(), bio
                )
                if not start_s:
                    continue
                signing_team = infer_signing_team_from_text(it["title"], body, [], team_code)
                parsed_contracts.append({
                    "source": "prohockeyrumors.com",
                    "url": it["url"],
                    "date": it["date"],
                    "headline": it["title"],
                    "signing_team": signing_team,
                    "start_season": start_s,
                    "end_season": end_s,
                    "start_yr": int(start_s.split("-")[0]),
                    "end_yr": int(end_s.split("-")[0]),
                    "terms": terms,
                })

        active_cands = [c for c in parsed_contracts if c["start_yr"] <= CURRENT_START_YEAR <= c["end_yr"]]
        future_cands = [c for c in parsed_contracts if c["start_yr"] > CURRENT_START_YEAR]

        def rank_cand(c):
            return (c["terms"]["aav_usd"] is not None, c["source"] == "nhl.com", c["date"])

        active_cands.sort(key=rank_cand, reverse=True)
        future_cands.sort(key=rank_cand, reverse=True)

        best_active = active_cands[0] if active_cands else None
        best_future = future_cands[0] if future_cands else None

        official_url = None
        if best_active and best_active["source"] != "nhl.com":
            nhl_same = [c for c in active_cands if c["source"] == "nhl.com"]
            if nhl_same:
                official_url = nhl_same[0]["url"]

        # Layer 1.5 cross-check with PHR 2026 Deep Dive (great for ELC post-slide cap hits & deferred deals!)
        dd_hit = dd["players"].get(slugify_name(p["name"]))
        if not dd_hit:
            # Try matching by last name if unique in Deep Dive
            last_hits = [v for k, v in dd["players"].items() if k.endswith("-" + slugify_name(p["last"]))]
            if len(last_hits) == 1:
                dd_hit = last_hits[0]

        if dd_hit and best_active:
            if best_active["terms"]["aav_usd"] is None:
                best_active["terms"]["aav_usd"] = dd_hit["aav_usd"]
                best_active["terms"]["total_value_usd"] = dd_hit["aav_usd"] * best_active["terms"]["term_years"]
                best_active["deep_dive_url"] = dd_hit["url"]
            elif best_active["terms"]["is_elc"] and abs(best_active["terms"]["aav_usd"] - dd_hit["aav_usd"]) > 1000:
                # ELC post-slide cap hit from Deep Dive is more accurate than initial gross signing figure
                best_active["terms"]["aav_usd"] = dd_hit["aav_usd"]
                best_active["terms"]["total_value_usd"] = dd_hit["aav_usd"] * best_active["terms"]["term_years"]
                best_active["deep_dive_url"] = dd_hit["url"]

        # Guard: never duplicate the active contract inside future_extension
        if best_active and best_future:
            if (
                best_future["url"] == best_active["url"]
                or (
                    best_future["terms"]["aav_usd"] == best_active["terms"]["aav_usd"]
                    and best_future["terms"]["term_years"] == best_active["terms"]["term_years"]
                )
            ):
                best_future = None

        # Best current-team trade/waiver acquisition
        nhl_trades.sort(reverse=True)
        best_trade = None
        for dt, sl, hl in nhl_trades[:3]:
            st = fetch_story_full(sl)
            if st and last_re.search(st["body"]) and team_re.search(st["headline"] + " " + st["body"][:350]):
                best_trade = {"date": st["date"], "headline": st["headline"], "url": st["url"]}
                break

        return {
            "player": p,
            "birth_date": bio.get("birthDate"),
            "draft_team": (bio.get("draftDetails") or {}).get("teamAbbrev"),
            "active": best_active,
            "official_nhl_url": official_url,
            "deep_dive_hit": dd_hit,
            "future_extension": best_future,
            "trade": best_trade,
        }

    # Run with moderate concurrency (4 workers) so NHL Forge stays fast while PHR lock paces cleanly
    with ThreadPoolExecutor(max_workers=4) as ex:
        results = list(ex.map(resolve_player, players))

    return {
        "team": team_code,
        "team_name": team_name,
        "roster_count": len(players),
        "deep_dive_url": deep_dive_url,
        "dead_cap_lines": dd.get("dead_cap_lines", []),
        "results": results,
    }


def main() -> None:
    DISCOVERY_DIR.mkdir(parents=True, exist_ok=True)
    # Usage: python discover_v3.py BOS BUF ... [--deep-dives path/to/deep_dives.json]
    args = sys.argv[1:]
    dd_map: dict[str, str] = {}
    if "--deep-dives" in args:
        i = args.index("--deep-dives")
        dd_map = json.loads(Path(args[i + 1]).read_text(encoding="utf-8"))
        args = args[:i] + args[i + 2 :]

    for tm in args:
        tm = tm.upper()
        print(f"\n================ {tm} (v3) ================", flush=True)
        res = discover_team(tm, deep_dive_url=dd_map.get(tm))
        (DISCOVERY_DIR / f"{tm.lower()}_v3.json").write_text(json.dumps(res, indent=2), encoding="utf-8")

        n = res["roster_count"]
        ok_aav = 0
        dd_only = 0
        gaps = []
        for r in res["results"]:
            p = r["player"]
            a = r["active"]
            ddh = r["deep_dive_hit"]
            f = r["future_extension"]
            ext = f" [+EXT {f['terms']['term_years']}y ${f['terms']['aav_usd']:,} {f['start_season']}]" if (f and f["terms"]["aav_usd"]) else ""
            if a and a["terms"]["aav_usd"]:
                ok_aav += 1
                src = "NHL" if a["source"] == "nhl.com" else "PHR"
                print(
                    f"  [OK {src}] {p['name']:22} {p['pos']} | {a['terms']['term_years']}y | "
                    f"${a['terms']['aav_usd']:>10,} | {a['start_season']}..{a['end_season']} | st={a['signing_team']}{ext}"
                )
            elif ddh:
                dd_only += 1
                print(
                    f"  [DD    ] {p['name']:22} {p['pos']} | ??y | "
                    f"${ddh['aav_usd']:>10,} | ..{ddh['end_season']} ({ddh['expiry_status']}){ext}"
                )
            else:
                gaps.append(p["name"])
                print(f"  [GAP   ] {p['name']:22} {p['pos']} ({p['id']}){ext}")

        print(f"--> {tm}: {ok_aav}/{n} full w/ AAV, {dd_only}/{n} DeepDive-assisted, {len(gaps)}/{n} gaps", flush=True)
        if res["dead_cap_lines"]:
            print("  Dead cap:", res["dead_cap_lines"][0][:200], flush=True)


if __name__ == "__main__":
    main()
