"""Build the 12-season Open NHL Trades Database (2015-16 through 2026-27).

Primary Source (100% Official NHL.com, zero cap-database sites):
  - Official NHL.com Trade Tracker Forge API stories for 2015-16, 2016-17, 2018-19 through 2026-27
  - Official NHL.com 2017-18 Trade Tracker (Wayback Machine snapshot from March 25, 2018,
    before NHL.com reused story ID 289968698 for 2021-22)
  - Linked official NHL.com individual trade articles (via Forge API) for retained-salary %
    extraction and canonical URL verification

Cross-Linking:
  - Matches traded players deterministically against our 4,691-player NHLPA/NHL canonical
    nhlId index (cache/nhlpa/compensation_history.json) plus embedded NHL.com forge-entity IDs.

Outputs (CC0-1.0 Public Domain):
  - data/trades_2015_2027.csv         (one row per trade, with assets_by_team_json & retained_salary_json)
  - data/trades_2015_2027.json        (full structured JSON database + season summary)
  - data/trade_assets_2015_2027.csv   (tidy 1-row-per-asset table for SQL/pandas/trade-tree queries)
  - data/trades_summary.json          (season-by-season trade & asset counts)
"""

from __future__ import annotations

import csv
import json
import re
import sys
import unicodedata
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import discover_v3 as dv3

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
TRADES_CACHE = ROOT / "cache" / "trades"
NHLPA_HIST_PATH = ROOT / "cache" / "nhlpa" / "compensation_history.json"
DRAFT_PICKS_PATH = ROOT / "cache" / "nhl" / "draft_picks_2015_2026.json"
LAST_COMPLETED_DRAFT_YEAR = 2026

FORBIDDEN_CAP_DOMAINS = (
    "puckpedia.com", "capwages.com", "spotrac.com", "capfriendly.com",
    "thestanleycap.com", "puckboss.com", "capfreeze.com", "salaryswish.com",
)

# Official NHL.com Trade Trackers (2015-16 through 2026-27)
OFFICIAL_TRACKERS = [
    ("2026-27", "forge", "2026-27-nhl-trades", "https://www.nhl.com/news/topic/trade-coverage/2026-27-nhl-trades"),
    ("2025-26", "forge", "2025-26-nhl-trades", "https://www.nhl.com/news/topic/trade-coverage/2025-26-nhl-trades"),
    ("2024-25", "forge", "2024-25-nhl-trades", "https://www.nhl.com/news/topic/trade-coverage/2024-25-nhl-trades"),
    ("2023-24", "forge", "2023-24-nhl-trades-344953916", "https://www.nhl.com/news/topic/trade-coverage/2023-24-nhl-trades-344953916"),
    ("2022-23", "forge", "2022-23-nhl-trades-334953354", "https://www.nhl.com/news/2022-23-nhl-trades-334953354"),
    ("2021-22", "forge", "2021-22-nhl-trades-289968698", "https://www.nhl.com/news/2021-22-nhl-trades-289968698"),
    ("2020-21", "forge", "2020-21-nhl-trades-325638984", "https://www.nhl.com/news/2020-21-nhl-trades-325638984"),
    ("2019-20", "forge", "2019-20-nhl-trades-319313416", "https://www.nhl.com/news/2019-20-nhl-trades-319313416"),
    ("2018-19", "forge", "2018-19-nhl-trades-307839254", "https://www.nhl.com/news/2018-19-nhl-trades-307839254"),
    ("2017-18", "wayback", "2017-18_wayback.html", "https://web.archive.org/web/20180325152342/https://www.nhl.com/news/2017-18-nhl-trades/c-289968698"),
    ("2016-17", "forge", "2016-17-nhl-trades-281003458", "https://www.nhl.com/news/2016-17-nhl-trades-281003458"),
    ("2015-16", "forge", "2015-16-nhl-trades-278555954", "https://www.nhl.com/news/2015-16-nhl-trades-278555954"),
]

# Full NHL team name & nickname matching table (includes ARI/PHX and Utah Hockey Club / Mammoth)
TEAM_PATTERNS: list[tuple[str, str]] = [
    ("ANA", r"(?:Anaheim\s+)?Ducks|Anaheim"),
    ("ARI", r"(?:Arizona|Phoenix)\s+Coyotes|Arizona|Coyotes"),
    ("BOS", r"(?:Boston\s+)?Bruins|Boston"),
    ("BUF", r"(?:Buffalo\s+)?Sabres|Buffalo"),
    ("CGY", r"(?:Calgary\s+)?Flames|Calgary"),
    ("CAR", r"(?:Carolina\s+)?Hurricanes|Carolina"),
    ("CHI", r"(?:Chicago\s+)?Blackhawks|Chicago"),
    ("COL", r"(?:Colorado\s+)?Avalanche|Colorado"),
    ("CBJ", r"(?:Columbus\s+)?Blue\s+Jackets|Columbus"),
    ("DAL", r"(?:Dallas\s+)?Stars|Dallas"),
    ("DET", r"(?:Detroit\s+)?Red\s+Wings|Detroit"),
    ("EDM", r"(?:Edmonton\s+)?Oilers|Edmonton"),
    ("FLA", r"(?:Florida\s+)?Panthers|Florida"),
    ("LAK", r"(?:Los\s+Angeles\s+|L\.?A\.?\s+)?Kings|Los\s+Angeles"),
    ("MIN", r"(?:Minnesota\s+|Minnestoa\s+)?Wild|Minnesota|Minnestoa"),
    ("MTL", r"(?:Montr[eé]al\s+)?Canadiens|Montr[eé]al"),
    ("NSH", r"(?:Nashville\s+)?Predators|Nashville"),
    ("NJD", r"(?:New\s+Jersey\s+)?Devils|New\s+Jersey"),
    ("NYI", r"(?:New\s+York\s+|N\.?Y\.?\s+)?Islanders"),
    ("NYR", r"(?:New\s+York\s+|N\.?Y\.?\s+)?Rangers"),
    ("OTT", r"(?:Ottawa\s+)?Senators|Ottawa"),
    ("PHI", r"(?:Philadelphia\s+)?Flyers|Philadelphia"),
    ("PIT", r"(?:Pittsburgh\s+)?Penguins|Pittsburgh"),
    ("SJS", r"(?:San\s+Jose\s+)?Sharks|San\s+Jose"),
    ("SEA", r"(?:Seattle\s+)?Kraken|Seattle"),
    ("STL", r"(?:St\.?\s+Louis\s+)?Blues|St\.?\s+Louis"),
    ("TBL", r"(?:Tampa\s+Bay\s+)?Lightning|Tampa\s+Bay"),
    ("TOR", r"(?:Toronto\s+)?Maple\s+Leafs|Toronto"),
    ("UTA", r"Utah\s+Mammoth|Utah\s+Hockey\s+Club|Mammoth|Utah"),
    ("VAN", r"(?:Vancouver\s+)?Canucks|Vancouver"),
    ("VGK", r"(?:Vegas\s+)?Golden\s+Knights|Vegas"),
    ("WSH", r"(?:Washington\s+)?Capitals|Washington"),
    ("WPG", r"(?:Winnipeg\s+)?Jets|Winnipeg"),
]

TEAM_FULL_PAT = "|".join(f"(?P<T_{code}>(?:{pat}))" for code, pat in TEAM_PATTERNS)
TEAM_FULL_RE = re.compile(rf"\b(?:the\s+)?(?:{TEAM_FULL_PAT})\b", re.I)

MONTH_MAP = {
    "january": 1, "jan": 1, "february": 2, "feb": 2, "march": 3, "mar": 3,
    "april": 4, "apr": 4, "may": 5, "june": 6, "jun": 6, "july": 7, "jul": 7,
    "august": 8, "aug": 8, "september": 9, "sept": 9, "sep": 9,
    "october": 10, "oct": 10, "november": 11, "nov": 11, "december": 12, "dec": 12,
}

ORDINAL_ROUND = {
    "1st": 1, "first": 1, "2nd": 2, "second": 2, "3rd": 3, "third": 3,
    "4th": 4, "fourth": 4, "5th": 5, "fifth": 5, "6th": 6, "sixth": 6,
    "7th": 7, "seventh": 7,
}

DATE_PREFIX_RE = re.compile(
    r"^\*{0,2}(JANUARY|FEBRUARY|MARCH|APRIL|MAY|JUNE|JULY|AUGUST|SEPTEMBER|OCTOBER|NOVEMBER|DECEMBER|"
    r"JAN\.?|FEB\.?|MAR\.?|APR\.?|AUG\.?|SEPT?\.?|OCT\.?|NOV\.?|DEC\.?)\s+(\d{1,2})\*{0,2}\s*:\s*",
    re.I,
)


def ascii_fold(s: str) -> str:
    return unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode("ascii")


def norm_name(s: str) -> str:
    s = ascii_fold(s).lower()
    s = re.sub(r"[^a-z0-9\s]", "", s)
    return re.sub(r"\s+", " ", s).strip()


def assert_no_cap_site(url: str | None) -> None:
    if not url:
        return
    low = url.lower()
    for bad in FORBIDDEN_CAP_DOMAINS:
        if bad in low:
            raise RuntimeError(f"Forbidden cap-site URL detected: {url}")


def find_team(text: str) -> str | None:
    m = TEAM_FULL_RE.search(text)
    if not m:
        return None
    for k, v in m.groupdict().items():
        if v is not None:
            return k[2:]
    return None


def find_all_teams(text: str) -> list[str]:
    found = []
    for m in TEAM_FULL_RE.finditer(text):
        for k, v in m.groupdict().items():
            if v is not None:
                code = k[2:]
                if code not in found:
                    found.append(code)
                break
    return found


def canonicalize_nhl_url(url: str | None) -> tuple[str | None, str | None]:
    """Normalize old NHL.com URLs (/news/slug/c-12345?tid=...) to canonical Forge slugs & URLs."""
    if not url:
        return None, None
    u = url.strip().split("?")[0].rstrip("/")
    # Strip Wayback prefix if present
    m_wb = re.match(r"https?://web\.archive\.org/web/\d+/(https?://.+)", u)
    if m_wb:
        u = m_wb.group(1)
    if u.startswith("/"):
        u = "https://www.nhl.com" + u
    if "nhl.com" not in u.lower():
        return u, None
    # Old format: https://www.nhl.com/.../news/{slug}/c-{id}
    m_old = re.match(r"^(https?://www\.nhl\.com(?:/[a-z]+)?/news)/([^/]+)/c-(\d+)$", u, re.I)
    if m_old:
        slug = f"{m_old.group(2)}-{m_old.group(3)}"
        return f"{m_old.group(1)}/{slug}", slug
    # Modern format: https://www.nhl.com/.../{slug}
    slug = u.split("/")[-1]
    return u, slug


def extract_embedded_player_ids(raw_chunk: str) -> dict[str, int]:
    """Extract canonical nhlId embedded in <forge-entity slug="name-8475763"> or nhl.com/player/name-8475763."""
    out: dict[str, int] = {}
    for m in re.finditer(r'slug="([a-z0-9\-]+)-(\d{7})"[^>]*>([^<]+)<', raw_chunk, re.I):
        out[norm_name(m.group(3))] = int(m.group(2))
    for m in re.finditer(r"\[([^\]]+)\]\(https?://www\.nhl\.com/player/[a-z0-9\-]*?(\d{7})[^)]*\)", raw_chunk, re.I):
        out[norm_name(m.group(1))] = int(m.group(2))
    return out


def strip_markdown_links(text: str) -> str:
    # Convert [Label](url) -> Label
    text = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", text)
    text = text.replace("**", "").replace("__", "").replace("\u00a0", " ")
    return re.sub(r"\s+", " ", text).strip()


def normalize_trade_typos(body: str) -> str:
    body = body.replace("\u00a0", " ").replace("’", "'").replace("‘", "'")
    body = re.sub(r"\baquires\b|\baqcuires\b", "acquires", body, flags=re.I)
    body = re.sub(r"\baquire\b|\baqcuire\b", "acquire", body, flags=re.I)
    body = re.sub(r"\bMinnestoa\b", "Minnesota", body, flags=re.I)
    body = re.sub(r"\bacquire([A-Z])", r"acquire \1", body)
    body = re.sub(r"([a-z])from\s+the\b", r"\1 from the", body)
    body = re.sub(r"\bin exchange\s+(defensemen|defenseman|forwards|forward|goaltender|goalie|a\s+\d)", r"in exchange for \1", body, flags=re.I)
    return re.sub(r"\s+", " ", body).strip()


def split_asset_items(clause: str) -> list[str]:
    """Split a trade asset list without breaking inside parentheses like '(Nos. 23, 29)'."""
    clause = clause.strip(" .;,")
    if not clause:
        return []
    # Protect commas inside parentheses by temporarily replacing them
    def _protect_parens(m: re.Match) -> str:
        return m.group(0).replace(",", ";").replace(" and ", " & ")
    safe = re.sub(r"\([^)]*\)", _protect_parens, clause)

    raw_parts = re.split(r"\s*,\s*(?:and\s+)?|\s+\band\b\s+", safe)
    parts = [p.strip(" .;,") for p in raw_parts if p.strip(" .;,")]

    # Merge stray 'in the 2027 NHL Draft' fragments back onto preceding pick if split
    merged: list[str] = []
    for p in parts:
        if merged and re.match(r"^(?:in\s+(?:the\s+)?)?20\d{2}\b", p, re.I) and "pick" in merged[-1].lower() and not re.search(r"20\d{2}", merged[-1]):
            merged[-1] = merged[-1] + " " + p
        else:
            merged.append(p)
    return merged


POS_PREFIX_RE = re.compile(
    r"^(?:(?:an?\s+)?(?:unsigned\s+draft\s+choice|draft\s+choice|prospect|rights\s+to|signing\s+rights\s+to|negotiating\s+rights\s+to)\s+)*"
    r"(forwards?|centers?|centres?|wingers?|left\s+wing(?:er)?s?|right\s+wing(?:er)?s?|defensemen|defenseman|defencemen|defenceman|goaltenders?|goalies?|[FDGCLRW]\.?)\s+",
    re.I,
)


def classify_assets(
    clause: str,
    player_index: dict[str, list[dict]],
    embedded_ids: dict[str, int],
    season_start_yr: int,
) -> list[dict]:
    items = split_asset_items(clause)
    results = []
    carried_pos: str | None = None
    carried_is_rights = False
    carried_is_udc = False

    # Look ahead to capture draft year if e.g. 'a 3rd-round pick and a 7th-round pick in the 2024 NHL Draft'
    for idx, raw in enumerate(items):
        low = raw.lower()
        # Strip leading articles / possessives like "Toronto's"
        if "future considerations" in low:
            results.append({"asset_type": "future_considerations", "raw": raw})
            carried_pos = None
            continue

        # Draft pick detection
        m_rd = re.search(r"\b(1st|2nd|3rd|4th|5th|6th|7th|first|second|third|fourth|fifth|sixth|seventh)[\s-]round\b", low)
        if m_rd or ("draft pick" in low) or re.search(r"\b20\d{2}\s+(?:conditional\s+)?(?:pick|selection)\b", low):
            rd = ORDINAL_ROUND.get(m_rd.group(1).lower()) if m_rd else None
            m_yr = re.search(r"\b(20\d{2})\b", raw)
            yr = int(m_yr.group(1)) if m_yr else None
            if yr is None and idx + 1 < len(items):
                m_ny = re.search(r"\b(20\d{2})\b", items[idx + 1])
                if m_ny:
                    yr = int(m_ny.group(1))
            cond = "conditional" in low
            orig_tm = find_team(raw)
            results.append({
                "asset_type": "draft_pick",
                "pick_year": yr,
                "pick_round": rd,
                "pick_conditional": cond,
                "pick_original_team": orig_tm,
                "raw": raw,
            })
            carried_pos = None
            carried_is_rights = False
            carried_is_udc = False
            continue

        if "cash" in low and len(low) < 25:
            results.append({"asset_type": "cash", "raw": raw})
            carried_pos = None
            continue

        # Player / Signing Rights / Unsigned Draft Choice
        is_rights = bool(re.search(r"\b(?:signing\s+|negotiating\s+)?rights\s+to\b", low)) or (carried_is_rights and carried_pos is not None)
        is_udc = bool(re.search(r"\bunsigned(?:\s+draft\s+choice)?\b", low)) or (carried_is_udc and carried_pos is not None)

        name_part = re.sub(r"^(?:and\s+)?(?:the\s+)?(?:signing\s+|negotiating\s+)?rights\s+to\s+", "", raw, flags=re.I)
        name_part = re.sub(r"^(?:an?\s+)?unsigned(?:\s+draft\s+choice)?\s+", "", name_part, flags=re.I)
        name_part = re.sub(r"^(?:[A-Z][a-z]+(?:\s+[A-Z][a-z]+)?'s\s+)", "", name_part)  # e.g., "Carolina's"
        # Strip leading team nickname if phrased like "Carolina forward prospect Kirill Slepets"
        name_part = re.sub(
            r"^(?:Anaheim|Arizona|Boston|Buffalo|Calgary|Carolina|Chicago|Colorado|Columbus|Dallas|Detroit|Edmonton|Florida|Los Angeles|Minnesota|Montreal|Nashville|New Jersey|New York|Ottawa|Philadelphia|Pittsburgh|San Jose|Seattle|St\.? Louis|Tampa Bay|Toronto|Utah|Vancouver|Vegas|Washington|Winnipeg)\s+(?=(?:forward|defenseman|defenceman|goalie|goaltender|prospect))",
            "",
            name_part,
            flags=re.I,
        )

        pos_hint = None
        m_pos = re.match(
            r"^(forwards?|centers?|centres?|wingers?|defensemen|defenseman|defencemen|defenceman|goaltenders?|goalies?|[FDG]\b)\s+(?:prospect\s+)?",
            name_part,
            re.I,
        )
        if m_pos:
            tok = m_pos.group(1).lower()
            if tok.startswith(("f", "c", "w")):
                pos_hint = "F"
            elif tok.startswith("d"):
                pos_hint = "D"
            elif tok.startswith("g"):
                pos_hint = "G"
            # Propagate plural position (e.g. "forwards Evan Rodrigues, Jesper Boqvist and Ben Steeves")
            carried_pos = pos_hint if tok.endswith(("s", "men")) else None
            carried_is_rights = is_rights and carried_pos is not None
            carried_is_udc = is_udc and carried_pos is not None
            name_part = name_part[m_pos.end():].strip()
        else:
            name_part = re.sub(r"^(?:prospect\s+)", "", name_part, flags=re.I).strip()
            if carried_pos:
                pos_hint = carried_pos

        # Strip trailing notes like "(retained ...)" or "to complete ..."
        name_part = re.sub(r"\s*\([^)]*\)\s*", " ", name_part).strip()
        name_part = re.sub(r"\s+to\s+complete\b.*$", "", name_part, flags=re.I).strip()
        # Ignore empty / non-name / pure numeric fragments like "76)"
        if not name_part or len(name_part) < 3 or not re.search(r"[a-zA-Z]{2}", name_part) or name_part.lower() in ("a", "the", "draft", "pick"):
            continue

        # Match canonical nhlId
        nk = norm_name(name_part)
        pid = embedded_ids.get(nk)
        matched_pos = pos_hint
        if pid is None and nk in player_index:
            cands = player_index[nk]
            if len(cands) == 1:
                pid = cands[0]["nhl_id"]
                matched_pos = cands[0].get("position") or pos_hint
            else:
                # Disambiguate by season proximity & position
                best = sorted(
                    cands,
                    key=lambda x: (
                        pos_hint is not None and (
                            (pos_hint == "G" and x.get("position") == "G")
                            or (pos_hint == "D" and x.get("position") == "D")
                            or (pos_hint == "F" and x.get("position") in ("C", "L", "R", "F"))
                        ),
                        any(abs(int(s.split("-")[0]) - season_start_yr) <= 1 for s in x.get("seasons", [])),
                    ),
                    reverse=True,
                )[0]
                pid = best["nhl_id"]
                matched_pos = best.get("position") or pos_hint

        atype = "signing_rights" if is_rights else ("unsigned_draft_choice" if is_udc else "player")
        results.append({
            "asset_type": atype,
            "player_id": pid,
            "player_name": name_part,
            "position": matched_pos,
            "raw": raw,
        })

    return results


def parse_trade_sides(body: str) -> tuple[list[str], dict[str, str], bool]:
    """Parse a trade summary into (teams_list, {receiving_team: raw_assets_received_text}, is_three_team)."""
    clean = normalize_trade_typos(body)

    # 1. Three-team trade handling
    is_3way = bool(re.search(r"\b(?:three-team|3-team|three-way)\b", clean, re.I)) or (
        len(re.findall(r"\b(?:acquire[sd]?|receive[sd]?|gets?)\b", clean, re.I)) >= 2
        and len(find_all_teams(clean)) >= 3
        and "previously acquired" not in clean.lower()
    )
    if is_3way:
        assets_by_team: dict[str, str] = {}
        teams = find_all_teams(clean)
        sents = re.split(r"(?<=[.!?])\s+|;\s*", clean)
        for sent in sents:
            # Split both ', and <Team> receives/acquires' AND ', <Team> receives/acquires' inside one sentence
            sub_clauses = re.split(
                r",\s*(?:and\s+)?(?=(?:the\s+)?[A-Z][a-z]+(?:\s+[A-Z][a-z]+){0,2}\s+(?:acquire[sd]?|receive[sd]?|gets?)\b)",
                sent,
            )
            for sc in sub_clauses:
                sc = re.sub(r"^and\s+", "", sc.strip(" ."), flags=re.I)
                # Handle "<TeamA> acquires <X> from <TeamB> ... in exchange for <Y>" inside a 3-team deal
                m_ex = re.search(
                    r"^(?P<ta>.+?)\s+\b(?:acquire[sd]?|receive[sd]?|gets?)\b\s+(?P<aa>.+?)\s+\bfrom\b\s+(?P<tb>.+?)\s+\b(?:in\s+exchange\s+for|for)\s+(?P<ab>.+)$",
                    sc,
                    re.I,
                )
                if m_ex:
                    ta = find_team(m_ex.group("ta"))
                    tb = find_team(m_ex.group("tb"))
                    if ta and tb:
                        assets_by_team[ta] = (assets_by_team.get(ta, "") + ", " + m_ex.group("aa").strip(" .,")).strip(", ")
                        assets_by_team[tb] = (assets_by_team.get(tb, "") + ", " + m_ex.group("ab").strip(" .,")).strip(", ")
                        continue

                m_ar = re.search(r"^(.*?)\b(acquire[sd]?|receive[sd]?|gets?)\s+(.+)$", sc, re.I)
                if m_ar:
                    tm = find_team(m_ar.group(1))
                    if tm:
                        haul = m_ar.group(3)
                        # Remove 'from the <Team>' embedded inside 'X from the Rangers and Y from the Coyotes'
                        haul = re.sub(r"\s+in\s+a\s+(?:three-team|3-team|three-way)\s+trade\b.*$", "", haul, flags=re.I)
                        haul = re.sub(r",?\s+as\s+part\s+of\s+a\s+(?:three-team|3-team|three-way)\s+trade\b.*$", "", haul, flags=re.I)
                        haul = re.sub(r"\s+from\s+(?:the\s+)?(?:" + TEAM_FULL_PAT + r")\b", "", haul, flags=re.I)
                        haul = haul.strip(" .,")
                        if haul:
                            assets_by_team[tm] = (assets_by_team.get(tm, "") + ", " + haul).strip(", ")
                # Also handle "Carolina sends X to Colorado"
                m_send = re.search(r"^(.*?)\b(?:sends?|trades?)\s+(.+?)\s+to\s+((?:the\s+)?(?:" + TEAM_FULL_PAT + r"))\b(.*)$", sc, re.I)
                if m_send:
                    recv_tm = find_team(m_send.group(3))
                    if recv_tm:
                        haul = m_send.group(2).strip(" .,")
                        extra = m_send.group(len(m_send.groups())).strip(" .,")
                        if extra.lower().startswith("with "):
                            haul = haul + ", " + extra[5:]
                        assets_by_team[recv_tm] = (assets_by_team.get(recv_tm, "") + ", " + haul).strip(", ")
        return teams, assets_by_team, True

    # 2. Standard 2-team: "<TeamA> acquire(s/d) <AssetsA> from <TeamB> [in exchange] for <AssetsB>"
    m_acq = re.search(
        r"^(?P<ta>.+?)\s+\bacquire[sd]?\b\s+(?P<aa>.+?)\s+\b(?:from|in\s+a\s+trade\s+with)\s+(?P<tb>.+?)\s+\b(?:in\s+exchange\s+for|for)\s+(?P<ab>.+)$",
        clean,
        re.I,
    )
    if m_acq:
        ta = find_team(m_acq.group("ta"))
        tb = find_team(m_acq.group("tb"))
        if ta and tb:
            return [ta, tb], {ta: m_acq.group("aa").strip(" ."), tb: m_acq.group("ab").strip(" .")}, False

    # 3. Standard 2-team reverse: "<TeamA> trade(s/d) <AssetsB> to <TeamB> [in exchange] for <AssetsA>"
    m_tr = re.search(
        r"^(?P<ta>.+?)\s+\btrade[sd]?\b\s+(?P<ab>.+?)\s+\bto\s+(?P<tb>.+?)\s+\b(?:in\s+exchange\s+for|for)\s+(?P<aa>.+)$",
        clean,
        re.I,
    )
    if m_tr:
        ta = find_team(m_tr.group("ta"))
        tb = find_team(m_tr.group("tb"))
        if ta and tb:
            return [ta, tb], {ta: m_tr.group("aa").strip(" ."), tb: m_tr.group("ab").strip(" .")}, False

    # 4. Cap-dump / no-return 2-team: "<TeamA> acquire(s/d) <AssetsA> from <TeamB>." (e.g. Andrew Ladd cap dump)
    m_dump = re.search(
        r"^(?P<ta>.+?)\s+\bacquire[sd]?\b\s+(?P<aa>.+?)\s+\bfrom\s+(?P<tb>.+?)\.?$",
        clean,
        re.I,
    )
    if m_dump:
        ta = find_team(m_dump.group("ta"))
        tb = find_team(m_dump.group("tb"))
        if ta and tb:
            return [ta, tb], {ta: m_dump.group("aa").strip(" ."), tb: "future considerations"}, False

    # 4. Fallback: "<TeamA> acquire(s/d) <AssetsA> [in exchange] for <AssetsB>" (rare omission of "from <TeamB>")
    m_nf = re.search(
        r"^(?P<ta>.+?)\s+\bacquire[sd]?\b\s+(?P<aa>.+?)\s+\b(?:in\s+exchange\s+for|for)\s+(?P<ab>.+)$",
        clean,
        re.I,
    )
    if m_nf:
        ta = find_team(m_nf.group("ta"))
        teams = find_all_teams(clean)
        tb = next((t for t in teams if t != ta), None)
        if ta and tb:
            return [ta, tb], {ta: m_nf.group("aa").strip(" ."), tb: m_nf.group("ab").strip(" .")}, False

    return find_all_teams(clean), {}, False


def parse_retention_from_body(body: str) -> list[dict]:
    """Extract structured salary retention mentions from an official NHL.com trade article body."""
    if not body or "retain" not in body.lower():
        return []
    out = []
    seen = set()
    for sent in re.split(r"(?<=[.!?])\s+", body):
        if "retain" not in sent.lower():
            continue
        if not re.search(r"percent|%|salary|cap|contract|half|portion", sent, re.I):
            continue
        m_pct = re.search(r"(\d{1,2}(?:\.\d+)?)\s*(?:percent|%)", sent, re.I)
        pct = float(m_pct.group(1)) if m_pct else (50.0 if re.search(r"\bhalf\b", sent, re.I) else None)
        # Find retaining team right before 'retain'
        pre = sent[: sent.lower().find("retain")]
        tm = find_team(pre) or find_team(sent)
        key = (tm, pct)
        if key in seen:
            continue
        seen.add(key)
        out.append({
            "retaining_team": tm,
            "retention_pct": pct,
            "note": sent.strip()[:220],
        })
    return out


def resolve_trade_date(month: int, day: int, season_start_yr: int, article_date: str | None) -> str:
    """Resolve exact YYYY-MM-DD using article_date (if within season window) or NHL season calendar."""
    if article_date and re.match(r"^\d{4}-\d{2}-\d{2}$", article_date):
        ay = int(article_date[:4])
        if season_start_yr <= ay <= season_start_yr + 1:
            return f"{ay}-{month:02d}-{day:02d}"
    # Season calendar: July-Dec -> season_start_yr; Jan-June -> season_start_yr + 1
    # Special case: 2019-20 COVID offseason ran into Aug/Sept 2020
    if season_start_yr == 2019 and month in (8, 9):
        yr = 2020
    elif month >= 7:
        yr = season_start_yr
    else:
        yr = season_start_yr + 1
    return f"{yr}-{month:02d}-{day:02d}"


def extract_tracker_chunks(season: str, kind: str, ref: str) -> list[str]:
    if kind == "forge":
        d = dv3.fetch_json_cached(f"https://forge-dapi.d3.nhle.com/v2/content/en-us/stories/{ref}")
        if not d:
            return []
        md = "\n".join(p["content"] for p in d.get("parts", []) if p.get("type") == "markdown" and p.get("content"))
        raw_lines = [c.strip() for c in re.split(r"<br\s*/?>|\r?\n", md) if c.strip()]
    else:
        html = (TRADES_CACHE / ref).read_text(encoding="utf-8")
        ps = re.findall(r"<p[^>]*>(.*?)</p>", html, re.S | re.I)
        # Convert <a href="...">label</a> to markdown [label](href) so link extraction works identically!
        raw_lines = []
        for p in ps:
            p_md = re.sub(
                r'<a[^>]*href="([^"]+)"[^>]*>(.*?)</a>',
                lambda m: f"[{dv3.clean_html(m.group(2))}]({m.group(1)})",
                p,
                flags=re.S | re.I,
            )
            p_md = dv3.clean_html(p_md)
            if p_md:
                raw_lines.append(p_md)

    # Stitch continuation chunks (e.g. 2018-19 newline right before player markdown link)
    stitched: list[str] = []
    for ln in raw_lines:
        if DATE_PREFIX_RE.search(ln[:45]):
            stitched.append(ln)
        elif stitched and not ln.startswith("*Welcome") and not re.match(r"^\*\*\[20\d{2}", ln):
            # Only stitch if previous line looks incomplete (doesn't already have a pipe+article link)
            if "|" not in stitched[-1]:
                stitched[-1] = stitched[-1] + " " + ln
    return stitched


def build_player_index() -> dict[str, list[dict]]:
    hist = json.loads(NHLPA_HIST_PATH.read_text(encoding="utf-8"))
    idx: dict[str, list[dict]] = defaultdict(list)
    for pid_str, p in hist["players"].items():
        nk = norm_name(p["name"])
        idx[nk].append({
            "nhl_id": int(pid_str),
            "name": p["name"],
            "position": p.get("position"),
            "seasons": list((p.get("yearly_compensation") or {}).keys()),
        })
    return idx


def _team_eq(a: str | None, b: str | None, draft_year: int) -> bool:
    if not a or not b:
        return False
    if a == b:
        return True
    # Arizona Coyotes relocated to Utah Mammoth in April 2024
    if draft_year >= 2024 and {a, b} == {"ARI", "UTA"}:
        return True
    return False


def _has_directed_edge(chain: list[str], src: str, dst: str, draft_year: int, adjacent_only: bool = True) -> bool:
    for i in range(len(chain)):
        if _team_eq(chain[i], src, draft_year):
            if adjacent_only:
                if i + 1 < len(chain) and _team_eq(chain[i + 1], dst, draft_year):
                    return True
            else:
                for j in range(i + 1, len(chain)):
                    if _team_eq(chain[j], dst, draft_year):
                        return True
    return False


def _extract_explicit_overall(raw: str) -> int | None:
    m = re.search(r"\bNo\.?\s*(\d{1,3})\b", raw, re.I)
    if m:
        return int(m.group(1))
    m2 = re.search(r"\b(\d{1,3})(?:st|nd|rd|th)\s+overall\b", raw, re.I)
    if m2:
        return int(m2.group(1))
    return None


def resolve_traded_draft_picks(trades_out: list[dict], assets_out: list[dict]) -> dict:
    """Resolve every traded draft pick from 2015-2026 to its exact overall pick number,
    full NHL teamPickHistory chain, drafting team, and drafted player (player_id + player_name)
    using official NHL Draft API records (cache/nhl/draft_picks_2015_2026.json)."""
    if not DRAFT_PICKS_PATH.exists():
        return {"resolved_used": 0, "upcoming": 0, "unresolved": 0}

    draft_db = json.loads(DRAFT_PICKS_PATH.read_text(encoding="utf-8"))["by_year"]
    by_yr_ov: dict[tuple[int, int], dict] = {}
    by_yr_rd: dict[tuple[int, int], list[dict]] = defaultdict(list)
    for yr_s, plist in draft_db.items():
        yr = int(yr_s)
        for p in plist:
            by_yr_ov[(yr, p["overall_pick"])] = p
            by_yr_rd[(yr, p["round"])].append(p)

    # Index tidy asset rows so we update both trades_out and assets_out in sync
    assets_by_key: dict[tuple[str, str, str], list[dict]] = defaultdict(list)
    for row in assets_out:
        if row["asset_type"] == "draft_pick":
            assets_by_key[(row["trade_id"], row["receiving_team"], row["raw_asset"])].append(row)

    used_edges: set[tuple[int, int, str, str]] = set()
    counts = Counter()

    def _attach(asset: dict, pick_rec: dict | None, status: str, method: str, trade_id: str, recv_tm: str) -> None:
        asset["pick_status"] = status
        asset["pick_resolution_method"] = method
        if pick_rec:
            asset["pick_overall"] = pick_rec["overall_pick"]
            asset["pick_in_round"] = pick_rec["pick_in_round"]
            asset["pick_history"] = pick_rec["team_pick_history"]
            asset["drafted_by_team"] = pick_rec["drafted_by_team"]
            asset["drafted_player_id"] = pick_rec["player_id"]
            asset["drafted_player_name"] = pick_rec["player_name"]
            asset["drafted_player_position"] = pick_rec["position"]
            if not asset.get("pick_original_team"):
                asset["pick_original_team"] = pick_rec["original_team"]
            if not asset.get("pick_round"):
                asset["pick_round"] = pick_rec["round"]
        else:
            asset.setdefault("pick_overall", None)
            asset.setdefault("pick_in_round", None)
            asset.setdefault("pick_history", None)
            asset.setdefault("drafted_by_team", None)
            asset.setdefault("drafted_player_id", None)
            asset.setdefault("drafted_player_name", None)
            asset.setdefault("drafted_player_position", None)

        # Sync into flat trade_assets rows
        for row in assets_by_key.get((trade_id, recv_tm, asset.get("raw") or ""), []):
            if asset.get("pick_year") and not row.get("pick_year"):
                row["pick_year"] = asset["pick_year"]
            row["pick_status"] = status
            row["pick_resolution_method"] = method
            row["pick_overall"] = asset["pick_overall"] or ""
            row["pick_in_round"] = asset["pick_in_round"] or ""
            row["pick_history"] = asset["pick_history"] or ""
            row["drafted_by_team"] = asset["drafted_by_team"] or ""
            row["drafted_player_id"] = asset["drafted_player_id"] or ""
            row["drafted_player_name"] = asset["drafted_player_name"] or ""
            row["drafted_player_position"] = asset["drafted_player_position"] or ""
            if asset.get("pick_original_team") and not row.get("pick_original_team"):
                row["pick_original_team"] = asset["pick_original_team"]

        counts[status] += 1
        counts[f"method:{method}"] += 1

    # Process trades chronologically (oldest first) so multi-hop edges claim cleanly
    for t in sorted(trades_out, key=lambda x: (x["trade_date"], x["trade_id"])):
        teams = t["teams"]
        for recv_tm, alist in t["assets_by_team"].items():
            other_tms = [tm for tm in teams if tm != recv_tm]
            for a in alist:
                if a["asset_type"] != "draft_pick":
                    continue
                yr = a.get("pick_year")
                rd = a.get("pick_round")
                raw = a.get("raw") or ""
                orig_hint = a.get("pick_original_team")
                is_cond = bool(a.get("pick_conditional"))

                if yr and yr > LAST_COMPLETED_DRAFT_YEAR:
                    _attach(a, None, "upcoming", "upcoming_draft", t["trade_id"], recv_tm)
                    continue

                # 1. Explicit overall pick number in trade text (e.g. "(No. 44)" or draft-day "(No. 71)")
                exp_ov = _extract_explicit_overall(raw)
                if exp_ov:
                    eff_yr = yr or int(t["trade_date"][:4])
                    if 2015 <= eff_yr <= LAST_COMPLETED_DRAFT_YEAR and (eff_yr, exp_ov) in by_yr_ov:
                        prec = by_yr_ov[(eff_yr, exp_ov)]
                        a["pick_year"] = eff_yr
                        src_used = other_tms[0] if other_tms else prec["original_team"]
                        used_edges.add((eff_yr, exp_ov, src_used, recv_tm))
                        _attach(a, prec, "used", "explicit_overall_in_trade", t["trade_id"], recv_tm)
                        continue

                if not yr or yr < 2015:
                    _attach(a, None, "unresolved", "missing_or_pre2015_year", t["trade_id"], recv_tm)
                    continue

                if not rd:
                    _attach(a, None, "unresolved", "missing_round", t["trade_id"], recv_tm)
                    continue

                def _find_candidates(target_rd: int, adjacent_only: bool) -> list[tuple[dict, str]]:
                    cands = []
                    for prec in by_yr_rd.get((yr, target_rd), []):
                        chain = prec["history_chain"]
                        for src in other_tms:
                            if _has_directed_edge(chain, src, recv_tm, yr, adjacent_only=adjacent_only):
                                if (yr, prec["overall_pick"], src, recv_tm) not in used_edges:
                                    cands.append((prec, src))
                                    break
                    if orig_hint and len(cands) > 1:
                        narrowed = [(p, s) for p, s in cands if _team_eq(p["original_team"], orig_hint, yr)]
                        if narrowed:
                            cands = narrowed
                    # Prefer picks whose original team IS src when no orig_hint was stated
                    if not orig_hint and len(cands) > 1:
                        own = [(p, s) for p, s in cands if _team_eq(p["original_team"], s, yr)]
                        if len(own) == 1:
                            cands = own
                    return cands

                # 2. Exact adjacent directed edge (src -> recv_tm) in official NHL teamPickHistory
                cands = _find_candidates(rd, adjacent_only=True)
                if len(cands) == 1:
                    prec, src = cands[0]
                    used_edges.add((yr, prec["overall_pick"], src, recv_tm))
                    _attach(a, prec, "used", "nhl_pick_history_edge", t["trade_id"], recv_tm)
                    continue

                # 3. Non-adjacent directed pair in same round (e.g. 3-team pass-through)
                if not cands:
                    cands_na = _find_candidates(rd, adjacent_only=False)
                    if len(cands_na) == 1:
                        prec, src = cands_na[0]
                        used_edges.add((yr, prec["overall_pick"], src, recv_tm))
                        _attach(a, prec, "used", "nhl_pick_history_chain", t["trade_id"], recv_tm)
                        continue

                # 4. Conditional pick round-shift check (e.g., conditional 2nd upgraded to 1st or downgraded to 3rd)
                if is_cond and not cands:
                    shifted = []
                    for alt_rd in (rd - 1, rd + 1):
                        if 1 <= alt_rd <= 7:
                            shifted.extend(_find_candidates(alt_rd, adjacent_only=True))
                    if len(shifted) == 1:
                        prec, src = shifted[0]
                        used_edges.add((yr, prec["overall_pick"], src, recv_tm))
                        _attach(a, prec, "used", "nhl_pick_history_conditional_round_shift", t["trade_id"], recv_tm)
                        continue

                # 5. Multiple unused exact-edge picks in same round between same teams -> pick lowest unclaimed overall if exact edge matched
                if len(cands) > 1:
                    prec, src = cands[0]
                    used_edges.add((yr, prec["overall_pick"], src, recv_tm))
                    _attach(a, prec, "used", "nhl_pick_history_edge_multi", t["trade_id"], recv_tm)
                    continue

                status = "conditional_unmet_or_unresolved" if is_cond else "unresolved"
                _attach(a, None, status, "no_matching_pick_history_edge", t["trade_id"], recv_tm)

        # Refresh flat draft_picks_traded summary inside each trade object
        refreshed_picks = []
        for recv_tm, alist in t["assets_by_team"].items():
            for a in alist:
                if a["asset_type"] == "draft_pick":
                    refreshed_picks.append({
                        "to_team": recv_tm,
                        "year": a.get("pick_year"),
                        "round": a.get("pick_round"),
                        "overall_pick": a.get("pick_overall"),
                        "pick_in_round": a.get("pick_in_round"),
                        "conditional": a.get("pick_conditional"),
                        "original_team": a.get("pick_original_team"),
                        "drafted_by_team": a.get("drafted_by_team"),
                        "drafted_player_id": a.get("drafted_player_id"),
                        "drafted_player_name": a.get("drafted_player_name"),
                        "pick_history": a.get("pick_history"),
                        "pick_status": a.get("pick_status"),
                    })
        t["draft_picks_traded"] = refreshed_picks

    return dict(counts)


def main() -> None:
    player_index = build_player_index()
    raw_trades: list[dict] = []
    seen_keys: set[tuple] = set()

    for season, kind, ref, tracker_url in OFFICIAL_TRACKERS:
        start_yr = int(season.split("-")[0])
        chunks = extract_tracker_chunks(season, kind, ref)
        for chunk in chunks:
            m_dt = DATE_PREFIX_RE.search(chunk[:45])
            if not m_dt:
                continue
            mon_tok = m_dt.group(1).rstrip(".").lower()
            month = MONTH_MAP.get(mon_tok)
            day = int(m_dt.group(2))
            if not month:
                continue

            embedded_ids = extract_embedded_player_ids(chunk)
            # Separate summary text before '|' from linked NHL.com article after '|'
            if "|" in chunk:
                left_raw, right_raw = chunk.split("|", 1)
                m_lnk = re.search(r"\((https?://[^)\s]+|/[^)\s]+)\)", right_raw)
                raw_url = m_lnk.group(1) if m_lnk else None
            else:
                left_raw = chunk
                raw_url = None

            art_url, art_slug = canonicalize_nhl_url(raw_url)
            summary_clean = strip_markdown_links(dv3.clean_html(left_raw))
            summary_clean = re.sub(r"^[A-Za-z.]+\s+\d{1,2}\s*:\s*", "", summary_clean).strip()
            if len(summary_clean) < 25:
                continue

            # Deduplicate June 14-15 overlap between 2016-17 tracker and 2017-18 Wayback tracker
            dedup_key = (month, day, norm_name(summary_clean[:65]))
            if dedup_key in seen_keys:
                continue
            seen_keys.add(dedup_key)

            raw_trades.append({
                "season": season,
                "season_start_yr": start_yr,
                "month": month,
                "day": day,
                "summary": summary_clean,
                "embedded_ids": embedded_ids,
                "article_url": art_url,
                "article_slug": art_slug,
                "tracker_url": tracker_url,
            })

    # Fetch all linked official NHL.com Forge trade articles in parallel for retained salary % & exact dates
    slugs = sorted({t["article_slug"] for t in raw_trades if t["article_slug"]})
    print(f"Fetching/loading {len(slugs):,} linked official NHL.com trade articles for retained salary %...", flush=True)
    article_map: dict[str, dict] = {}
    with ThreadPoolExecutor(max_workers=20) as ex:
        futs = {ex.submit(dv3.fetch_story_full, sl): sl for sl in slugs}
        for fut in as_completed(futs):
            sl = futs[fut]
            st = fut.result()
            if st:
                article_map[sl] = st
    print(f"  Loaded {len(article_map):,} / {len(slugs):,} official NHL.com trade articles.", flush=True)

    trades_out: list[dict] = []
    assets_out: list[dict] = []
    season_counts = Counter()
    parsed_both_ways = 0
    total_players_assets = 0
    matched_player_ids = 0
    retained_trades = 0

    for seq, rt in enumerate(raw_trades, 1):
        art = article_map.get(rt["article_slug"] or "")
        art_date = art["date"] if art else None
        trade_date = resolve_trade_date(rt["month"], rt["day"], rt["season_start_yr"], art_date)
        trade_url = (art["url"] if art else rt["article_url"]) or rt["tracker_url"]
        assert_no_cap_site(trade_url)
        assert_no_cap_site(rt["tracker_url"])

        teams, raw_by_team, is_3way = parse_trade_sides(rt["summary"])
        # If 2016-17 Drouin/Sergachev omitted 'from Tampa Bay Lightning', infer 2nd team from article headline/body
        if len(teams) == 1 and art:
            art_teams = find_all_teams(art["headline"] + " " + art["body"][:200])
            for t in art_teams:
                if t not in teams:
                    teams.append(t)
                    break
            if len(teams) == 2 and not raw_by_team:
                m_nf = re.search(r"\bacquire[sd]?\b\s+(.+?)\s+\b(?:in\s+exchange\s+for|for)\s+(.+)$", normalize_trade_typos(rt["summary"]), re.I)
                if m_nf:
                    raw_by_team = {teams[0]: m_nf.group(1).strip(" ."), teams[1]: m_nf.group(2).strip(" .")}

        assets_by_team: dict[str, list[dict]] = {}
        for tm, raw_clause in raw_by_team.items():
            classified = classify_assets(raw_clause, player_index, rt["embedded_ids"], rt["season_start_yr"])
            assets_by_team[tm] = classified

        if len(assets_by_team) >= 2 and all(len(v) > 0 for v in assets_by_team.values()):
            parsed_both_ways += 1

        # Extract retained salary from linked article + tracker text
        ret_list = parse_retention_from_body((art["body"] if art else "") + " " + rt["summary"])
        if ret_list:
            retained_trades += 1

        # Collect flat player & draft-pick lists + tidy trade_assets rows
        trade_id = f"{rt['season'].replace('-', '')}_{trade_date.replace('-', '')}_{seq:04d}"
        players_traded = []
        picks_traded = []
        has_fc = "future considerations" in rt["summary"].lower()

        for recv_tm, alist in assets_by_team.items():
            other_tms = [t for t in teams if t != recv_tm]
            from_tm = other_tms[0] if len(other_tms) == 1 else (",".join(other_tms) if other_tms else "")
            for a in alist:
                at = a["asset_type"]
                if at in ("player", "signing_rights", "unsigned_draft_choice"):
                    total_players_assets += 1
                    if a.get("player_id"):
                        matched_player_ids += 1
                    players_traded.append({
                        "to_team": recv_tm,
                        "player_id": a.get("player_id"),
                        "player_name": a.get("player_name"),
                        "asset_type": at,
                    })
                elif at == "draft_pick":
                    picks_traded.append({
                        "to_team": recv_tm,
                        "year": a.get("pick_year"),
                        "round": a.get("pick_round"),
                        "conditional": a.get("pick_conditional"),
                        "original_team": a.get("pick_original_team"),
                    })
                assets_out.append({
                    "trade_id": trade_id,
                    "season": rt["season"],
                    "trade_date": trade_date,
                    "receiving_team": recv_tm,
                    "from_team": from_tm,
                    "asset_type": at,
                    "player_id": a.get("player_id") or "",
                    "player_name": a.get("player_name") or "",
                    "position": a.get("position") or "",
                    "pick_year": a.get("pick_year") or "",
                    "pick_round": a.get("pick_round") or "",
                    "pick_conditional": a.get("pick_conditional", "") if at == "draft_pick" else "",
                    "pick_original_team": a.get("pick_original_team") or "",
                    "raw_asset": a.get("raw") or "",
                    "trade_article_url": trade_url,
                })

        season_counts[rt["season"]] += 1
        trades_out.append({
            "trade_id": trade_id,
            "season": rt["season"],
            "trade_date": trade_date,
            "num_teams": max(len(teams), len(assets_by_team)),
            "is_three_team": is_3way or len(teams) >= 3,
            "teams": teams,
            "summary": rt["summary"],
            "assets_by_team": assets_by_team,
            "players_traded": players_traded,
            "draft_picks_traded": picks_traded,
            "has_retained_salary": bool(ret_list),
            "retained_salary": ret_list,
            "has_future_considerations": has_fc,
            "trade_article_url": trade_url,
            "tracker_url": rt["tracker_url"],
        })

    # Resolve all traded draft picks (2015-2026) to exact overall pick & drafted player via NHL Draft API
    pick_res_stats = resolve_traded_draft_picks(trades_out, assets_out)

    # Sort reverse-chronologically by trade_date
    trades_out.sort(key=lambda x: (x["trade_date"], x["trade_id"]), reverse=True)
    assets_out.sort(key=lambda x: (x["trade_date"], x["trade_id"]), reverse=True)

    # Write trades_2015_2027.csv
    t_fields = [
        "trade_id", "season", "trade_date", "num_teams", "is_three_team", "teams",
        "team_a", "team_b", "team_c", "summary",
        "assets_by_team_json", "players_traded_json", "draft_picks_traded_json",
        "has_retained_salary", "retained_salary_json", "has_future_considerations",
        "trade_article_url", "tracker_url",
    ]
    with (DATA_DIR / "trades_2015_2027.csv").open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=t_fields)
        w.writeheader()
        for t in trades_out:
            tms = t["teams"]
            w.writerow({
                "trade_id": t["trade_id"],
                "season": t["season"],
                "trade_date": t["trade_date"],
                "num_teams": t["num_teams"],
                "is_three_team": t["is_three_team"],
                "teams": ",".join(tms),
                "team_a": tms[0] if len(tms) > 0 else "",
                "team_b": tms[1] if len(tms) > 1 else "",
                "team_c": tms[2] if len(tms) > 2 else "",
                "summary": t["summary"],
                "assets_by_team_json": json.dumps(t["assets_by_team"], separators=(",", ":")),
                "players_traded_json": json.dumps(t["players_traded"], separators=(",", ":")),
                "draft_picks_traded_json": json.dumps(t["draft_picks_traded"], separators=(",", ":")),
                "has_retained_salary": t["has_retained_salary"],
                "retained_salary_json": json.dumps(t["retained_salary"], separators=(",", ":")) if t["retained_salary"] else "",
                "has_future_considerations": t["has_future_considerations"],
                "trade_article_url": t["trade_article_url"],
                "tracker_url": t["tracker_url"],
            })

    # Write tidy trade_assets_2015_2027.csv
    a_fields = [
        "trade_id", "season", "trade_date", "receiving_team", "from_team",
        "asset_type", "player_id", "player_name", "position",
        "pick_year", "pick_round", "pick_overall", "pick_in_round",
        "pick_conditional", "pick_original_team", "pick_history",
        "pick_status", "pick_resolution_method",
        "drafted_by_team", "drafted_player_id", "drafted_player_name", "drafted_player_position",
        "raw_asset", "trade_article_url",
    ]
    with (DATA_DIR / "trade_assets_2015_2027.csv").open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=a_fields)
        w.writeheader()
        for row in assets_out:
            w.writerow({k: row.get(k, "") for k in a_fields})

    used_picks = pick_res_stats.get("used", 0)
    upcoming_picks = pick_res_stats.get("upcoming", 0)
    past_picks = sum(1 for a in assets_out if a["asset_type"] == "draft_pick" and (a.get("pick_year") or 0) and int(a["pick_year"]) <= LAST_COMPLETED_DRAFT_YEAR)

    summary = {
        "license": "CC0-1.0",
        "span": "2015-16 through 2026-27 (12 seasons)",
        "generated_at_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "totals": {
            "seasons": len(OFFICIAL_TRACKERS),
            "total_trades": len(trades_out),
            "parsed_both_ways_trades": parsed_both_ways,
            "parsed_both_ways_pct": round(100.0 * parsed_both_ways / max(1, len(trades_out)), 1),
            "three_team_trades": sum(1 for t in trades_out if t["is_three_team"]),
            "total_asset_rows": len(assets_out),
            "player_assets": total_players_assets,
            "player_assets_with_nhl_id": matched_player_ids,
            "player_id_match_pct": round(100.0 * matched_player_ids / max(1, total_players_assets), 1),
            "draft_pick_assets": sum(1 for a in assets_out if a["asset_type"] == "draft_pick"),
            "past_draft_picks_2015_2026": past_picks,
            "past_draft_picks_resolved_to_overall_and_player": used_picks,
            "past_draft_pick_resolution_pct": round(100.0 * used_picks / max(1, past_picks), 1),
            "upcoming_draft_picks_2027_plus": upcoming_picks,
            "draft_pick_resolution_breakdown": pick_res_stats,
            "future_considerations_trades": sum(1 for t in trades_out if t["has_future_considerations"]),
            "trades_with_retained_salary": retained_trades,
            "trades_with_official_url": sum(1 for t in trades_out if t["trade_article_url"]),
            "cap_site_urls": 0,
        },
        "by_season": dict(sorted(season_counts.items())),
    }

    (DATA_DIR / "trades_summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    (DATA_DIR / "trades_2015_2027.json").write_text(
        json.dumps({"summary": summary, "trades": trades_out}, indent=2),
        encoding="utf-8",
    )

    print("\n=== 12-SEASON OFFICIAL NHL TRADES DATABASE COMPLETE (2015-16..2026-27) ===")
    print(json.dumps(summary["totals"], indent=2))
    print("\nTrades by Season:")
    for s, c in sorted(season_counts.items()):
        print(f"  {s}: {c:3} trades")


if __name__ == "__main__":
    main()
