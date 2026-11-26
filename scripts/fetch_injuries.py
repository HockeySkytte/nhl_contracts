"""Fetch live current NHL injuries across all 32 teams (no historical archive — current injuries only).

Sources (Zero cap-database sites):
  1. ESPN Public NHL Injuries API (https://site.api.espn.com/apis/site/v2/sports/hockey/nhl/injuries)
     - Real-time status (Day-To-Day, Injured Reserve, Out), injury type/detail, estimated return date,
       editorial report, timestamp, and ESPN player URL.
  2. Official NHL.com Forge API (tags.slug=injury + recent NHL Status Reports)
     - Attaches recent official NHL.com injury/IR press release URLs where available.
  3. Canonical Cross-Linking:
     - Joins every injured player to canonical NHL player_id (nhlId) and current 2026-27 AAV
       from data/all_contracts.json and cache/nhlpa/compensation_history.json.

Outputs (CC0-1.0 Public Domain):
  - data/current_injuries.csv
  - data/current_injuries.json
"""

from __future__ import annotations

import csv
import json
import re
import sys
import unicodedata
import urllib.request
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import discover_v3 as dv3

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
ACTIVE_2026_PATH = DATA_DIR / "all_contracts.json"
NHLPA_HIST_PATH = ROOT / "cache" / "nhlpa" / "compensation_history.json"

ESPN_INJURIES_URL = "https://site.api.espn.com/apis/site/v2/sports/hockey/nhl/injuries"
ESPN_INJURIES_WEB = "https://www.espn.com/nhl/injuries"

FORBIDDEN_CAP_DOMAINS = (
    "puckpedia.com", "capwages.com", "spotrac.com", "capfriendly.com",
    "thestanleycap.com", "puckboss.com", "capfreeze.com", "salaryswish.com",
)

# Map ESPN team displayName -> canonical 3-letter NHL tricode
TEAM_NAME_TO_CODE = {
    v[3].lower(): code for code, v in dv3.TEAM_INFO.items()
}
TEAM_NAME_TO_CODE.update({
    "montréal canadiens": "MTL",
    "montreal canadiens": "MTL",
    "st. louis blues": "STL",
    "st louis blues": "STL",
    "utah hockey club": "UTA",
    "utah mammoth": "UTA",
})

# Common ESPN <-> NHL name aliases
NAME_ALIASES = {
    "j j moser": "janis moser",
    "jj moser": "janis moser",
    "matthew beniers": "matty beniers",
    "cameron york": "cam york",
    "nicholas paul": "nick paul",
    "mitchell marner": "mitch marner",
    "alexander ovechkin": "alex ovechkin",
    "joshua morrissey": "josh morrissey",
    "joseph woll": "joe woll",
    "jacob middleton": "jake middleton",
    "vinnie hinostroza": "vincent hinostroza",
    "zachary werenski": "zach werenski",
    "michael matheson": "mike matheson",
    "thomas chabot": "thomas chabot",
    "anthony deangelo": "tony deangelo",
    "emil lilleberg": "emil martinsen lilleberg",
}


def norm_name(s: str) -> str:
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode("ascii").lower()
    s = re.sub(r"[^a-z0-9\s]", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def assert_no_cap_site(url: str | None) -> None:
    if not url:
        return
    low = url.lower()
    for bad in FORBIDDEN_CAP_DOMAINS:
        if bad in low:
            raise RuntimeError(f"Forbidden cap-site URL detected: {url}")


def build_player_lookup() -> tuple[dict[tuple[str, str], dict], dict[str, list[dict]]]:
    by_team_name: dict[tuple[str, str], dict] = {}
    by_name: dict[str, list[dict]] = defaultdict(list)

    # 1. Active 2026-27 32-team contracts (highest priority)
    if ACTIVE_2026_PATH.exists():
        league = json.loads(ACTIVE_2026_PATH.read_text(encoding="utf-8"))
        for c in league.get("contracts") or []:
            nk = norm_name(c["player_name"])
            rec = {
                "player_id": c["player_id"],
                "player_name": c["player_name"],
                "team": c["current_team"],
                "position": c.get("position"),
                "aav_usd": c.get("aav_usd"),
            }
            by_team_name[(c["current_team"], nk)] = rec
            by_name[nk].append(rec)

    # 2. Full NHLPA compensation history (catches IR/LTIR/non-roster players not on active 23-man roster!)
    if NHLPA_HIST_PATH.exists():
        hist = json.loads(NHLPA_HIST_PATH.read_text(encoding="utf-8"))
        for pid_str, p in hist.get("players", {}).items():
            nk = norm_name(p["name"])
            tm = p.get("current_team_tricode")
            cur = p.get("current_2026_27") or {}
            rec = {
                "player_id": int(pid_str),
                "player_name": p["name"],
                "team": tm,
                "position": p.get("position"),
                "aav_usd": cur.get("aav_usd"),
            }
            if tm and (tm, nk) not in by_team_name:
                by_team_name[(tm, nk)] = rec
            if not any(x["player_id"] == rec["player_id"] for x in by_name[nk]):
                by_name[nk].append(rec)

    return by_team_name, by_name


def fetch_recent_nhl_injury_articles() -> dict[int, dict]:
    """Pull recent official NHL.com Forge injury stories (last 60 days) indexed by playerId tag."""
    by_pid: dict[int, dict] = {}
    for expr, pages in [("injury", 4), ("nhl-created,news", 2)]:
        for it in dv3.fetch_forge_pages(expr, max_pages=pages):
            dt = (it.get("contentDate") or "")[:10]
            if dt < "2026-09-01":
                continue
            hl = it.get("headline") or ""
            tags = [t.get("slug", "") for t in it.get("tags", []) if isinstance(t, dict)]
            if expr == "nhl-created,news" and not any(k in hl.lower() for k in ("status report", "injur", "placed on ir", "out ", "surgery")):
                continue
            url = dv3.forge_web_url(it["slug"], tags)
            for t in tags:
                m = re.match(r"^playerid-(\d+)$", t)
                if m:
                    pid = int(m.group(1))
                    if pid not in by_pid or dt > by_pid[pid]["date"]:
                        by_pid[pid] = {"date": dt, "headline": hl, "url": url}
    return by_pid


def match_player(
    espn_name: str,
    team_code: str | None,
    by_team_name: dict[tuple[str, str], dict],
    by_name: dict[str, list[dict]],
) -> dict | None:
    nk = norm_name(espn_name)
    candidates_keys = [nk]
    if nk in NAME_ALIASES:
        candidates_keys.append(NAME_ALIASES[nk])

    for k in candidates_keys:
        if team_code and (team_code, k) in by_team_name:
            return by_team_name[(team_code, k)]
    for k in candidates_keys:
        cands = by_name.get(k) or []
        if len(cands) == 1:
            return cands[0]
        if cands and team_code:
            tm_c = [c for c in cands if c.get("team") == team_code]
            if tm_c:
                return tm_c[0]
            # Prefer one with active 2026-27 aav
            act_c = [c for c in cands if c.get("aav_usd") is not None]
            if len(act_c) == 1:
                return act_c[0]
    # Last-name + first-initial fallback within same team (handles nicknames like "Mitch" vs "Mitchell")
    if team_code and " " in nk:
        parts = nk.split()
        first_init, last = parts[0][0], parts[-1]
        same_tm = [
            rec for (tm, k), rec in by_team_name.items()
            if tm == team_code and k.split()[-1] == last and k.split()[0].startswith(first_init)
        ]
        if len(same_tm) == 1:
            return same_tm[0]
    return None


def main() -> None:
    req = urllib.request.Request(ESPN_INJURIES_URL, headers=dv3.HEADERS)
    with urllib.request.urlopen(req, timeout=20) as r:
        espn = json.loads(r.read().decode("utf-8"))

    by_team_name, by_name = build_player_lookup()
    nhl_articles = fetch_recent_nhl_injury_articles()

    records: list[dict] = []
    status_counts = Counter()
    team_summary: dict[str, dict] = defaultdict(lambda: {"injured_players": 0, "injured_aav_usd": 0, "ir_count": 0, "dtd_count": 0})

    for team_block in espn.get("injuries") or []:
        team_name = team_block.get("displayName") or ""
        team_code = TEAM_NAME_TO_CODE.get(team_name.lower())
        for inj in team_block.get("injuries") or []:
            ath = inj.get("athlete") or {}
            espn_name = ath.get("displayName") or f"{ath.get('firstName', '')} {ath.get('lastName', '')}".strip()
            pos = (ath.get("position") or {}).get("abbreviation") or ""
            espn_player_url = None
            for lnk in ath.get("links") or []:
                if "playercard" in (lnk.get("rel") or []):
                    espn_player_url = lnk.get("href")
                    break

            details = inj.get("details") or {}
            injury_type = details.get("type") or ""
            injury_detail = details.get("detail") or ""
            injury_side = details.get("side") or ""
            return_date = details.get("returnDate") or ""
            status = inj.get("status") or ""
            updated_utc = inj.get("date") or ""
            short_comment = (inj.get("shortComment") or "").strip()
            long_comment = (inj.get("longComment") or "").strip()

            matched = match_player(espn_name, team_code, by_team_name, by_name)
            pid = matched["player_id"] if matched else None
            aav = matched["aav_usd"] if matched else None
            if not pos and matched:
                pos = matched.get("position") or ""

            nhl_art = nhl_articles.get(pid) if pid else None
            nhl_art_url = nhl_art["url"] if nhl_art else None
            source_url = nhl_art_url or espn_player_url or ESPN_INJURIES_WEB
            secondary_url = espn_player_url if nhl_art_url else ESPN_INJURIES_WEB

            assert_no_cap_site(source_url)
            assert_no_cap_site(secondary_url)

            status_counts[status] += 1
            if team_code:
                ts = team_summary[team_code]
                ts["injured_players"] += 1
                if aav:
                    ts["injured_aav_usd"] += aav
                if "reserve" in status.lower() or "ir" in status.lower() or "out" in status.lower():
                    ts["ir_count"] += 1
                else:
                    ts["dtd_count"] += 1

            records.append({
                "team": team_code or team_name,
                "team_name": team_name,
                "player_id": pid,
                "player_name": matched["player_name"] if matched else espn_name,
                "position": pos,
                "aav_usd": aav,
                "injury_status": status,
                "injury_type": injury_type,
                "injury_detail": injury_detail,
                "injury_side": injury_side,
                "estimated_return_date": return_date,
                "updated_at_utc": updated_utc,
                "short_comment": short_comment,
                "long_comment": long_comment,
                "source_url": source_url,
                "espn_player_url": espn_player_url,
                "nhl_article_url": nhl_art_url,
                "nhl_article_date": nhl_art["date"] if nhl_art else None,
            })

    records.sort(key=lambda x: (x["team"], x["injury_status"], x["player_name"]))

    # Write CSV
    fields = [
        "team", "player_id", "player_name", "position", "aav_usd",
        "injury_status", "injury_type", "injury_detail", "injury_side",
        "estimated_return_date", "updated_at_utc", "short_comment",
        "source_url", "nhl_article_url", "espn_player_url",
    ]
    csv_path = DATA_DIR / "current_injuries.csv"
    with csv_path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for r in records:
            w.writerow({k: (r.get(k) if r.get(k) is not None else "") for k in fields})

    matched_pid = sum(1 for r in records if r["player_id"] is not None)
    matched_aav = sum(1 for r in records if r["aav_usd"] is not None)
    with_nhl_art = sum(1 for r in records if r["nhl_article_url"])
    total_inj_aav = sum(r["aav_usd"] for r in records if r["aav_usd"])

    payload = {
        "license": "CC0-1.0",
        "season": "2026-27",
        "snapshot_at_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "sources": [ESPN_INJURIES_WEB, "https://www.nhl.com/news"],
        "totals": {
            "teams_with_injuries": len(team_summary),
            "total_injured_players": len(records),
            "matched_to_nhl_player_id": matched_pid,
            "player_id_match_pct": round(100.0 * matched_pid / max(1, len(records)), 1),
            "matched_to_aav": matched_aav,
            "total_injured_aav_usd": total_inj_aav,
            "linked_to_official_nhl_article": with_nhl_art,
            "status_breakdown": dict(status_counts),
            "cap_site_urls": 0,
        },
        "by_team": dict(sorted(team_summary.items())),
        "injuries": records,
    }
    (DATA_DIR / "current_injuries.json").write_text(json.dumps(payload, indent=2), encoding="utf-8")

    print("=== CURRENT NHL INJURIES SNAPSHOT COMPLETE ===")
    print(json.dumps(payload["totals"], indent=2))
    unmatched = [f"{r['team']}:{r['player_name']}" for r in records if r["player_id"] is None]
    if unmatched:
        print(f"Unmatched player IDs ({len(unmatched)}): {unmatched}")
    print("\nTop 8 Teams by Injured Cap Hit:")
    for tm, s in sorted(team_summary.items(), key=lambda kv: kv[1]["injured_aav_usd"], reverse=True)[:8]:
        print(f"  {tm} | {s['injured_players']:2} injured ({s['ir_count']} IR/Out, {s['dtd_count']} DTD) | ${s['injured_aav_usd']:>11,}")
    print("\nSample 6 Current Injury Records:")
    for r in records[:6]:
        aav_s = f"${r['aav_usd']:,}" if r["aav_usd"] else "N/A"
        print(f"  {r['team']} {r['player_name']:20} ({r['player_id']}) | {r['injury_status']:15} | {r['injury_type']:12} | ret={r['estimated_return_date'] or 'TBD':10} | {aav_s}")


if __name__ == "__main__":
    main()
