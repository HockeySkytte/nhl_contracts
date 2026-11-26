r"""Incremental daily updater for NHL Contracts, Trades, and Current Injuries.

Designed for fast (~10-25s) daily execution in GitHub Actions or locally:
  1. Checks ONLY live current-season endpoints (uncached HTTP):
       - NHLPA current-season compensation (1 request: nhlpa.com/api/compensation/{year})
       - Official NHL.com current-season Trade Tracker (1 request: Forge API)
       - Live ESPN NHL Injuries API + recent NHL.com injury articles
  2. Diffs each feed independently against the committed repository snapshot.
  3. Rebuilds & upserts to Supabase ONLY the tables whose data actually changed:
       - contracts / contract_years / contract_teams_years -> only if NHLPA current-season changed
       - trades -> only new/modified current-season trades (preserving all historical trade_ids)
       - injuries -> only if current injury list or statuses changed
  4. Writes a concise summary (and appends to GITHUB_STEP_SUMMARY when running in Actions).

Zero cap-database sites (PuckPedia, CapWages, Spotrac, CapFriendly, etc.) — enforced by assertion.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import re
import sys
import urllib.request
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

import psycopg2
from psycopg2.extras import Json, execute_values

SCRIPTS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS_DIR))

import build_historical_contracts as bhc
import build_trades as bt
import discover_v3 as dv3
import fetch_injuries as fi
import fetch_nhl_bios as fnb
import fetch_nhlpa as fnhlpa
import load_supabase as lsb

ROOT = SCRIPTS_DIR.parent
DATA_DIR = ROOT / "data"
NHLPA_HIST_PATH = ROOT / "cache" / "nhlpa" / "compensation_history.json"
BIOS_PATH = ROOT / "cache" / "nhl" / "player_bios_2010_2026.json"
TRADES_JSON_PATH = DATA_DIR / "trades_2015_2027.json"
TRADES_CSV_PATH = DATA_DIR / "trades_2015_2027.csv"
TRADE_ASSETS_CSV_PATH = DATA_DIR / "trade_assets_2015_2027.csv"
TRADES_SUMMARY_PATH = DATA_DIR / "trades_summary.json"
INJURIES_JSON_PATH = DATA_DIR / "current_injuries.json"
INJURIES_CSV_PATH = DATA_DIR / "current_injuries.csv"

DEFAULT_SEASON_YEAR = 2026  # 2026-27 season


def fetch_json_live(url: str, timeout: int = 25) -> dict | list | None:
    """Fetch JSON directly from the live endpoint (always bypasses local disk cache)."""
    req = urllib.request.Request(url, headers=dv3.HEADERS)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8", errors="replace"))


def season_label(yr: int) -> str:
    return f"{yr}-{str(yr + 1)[-2:]}"


# ---------------------------------------------------------------------------
# 1. INCREMENTAL CONTRACTS CHECK (Live NHLPA Current Season)
# ---------------------------------------------------------------------------

def check_and_update_contracts(season_yr: int, dry_run: bool = False) -> dict:
    s_lbl = season_label(season_yr)
    url = f"https://www.nhlpa.com/api/compensation/{season_yr}"
    print(f"[Contracts] Fetching live NHLPA {s_lbl} ({url})...", flush=True)
    live_data = fetch_json_live(url)
    if not isinstance(live_data, list) or len(live_data) < 25:
        raise RuntimeError(f"Unexpected NHLPA payload for {s_lbl}: {type(live_data)} len={len(live_data) if isinstance(live_data, list) else 'N/A'}")

    hist = json.loads(NHLPA_HIST_PATH.read_text(encoding="utf-8"))
    players = hist["players"]

    # Build snapshot of existing current-season records for diffing
    old_season: dict[str, dict] = {}
    for pid_str, entry in players.items():
        rec = (entry.get("yearly_compensation") or {}).get(s_lbl)
        if rec:
            old_season[pid_str] = rec

    # Parse live current-season payload
    live_season: dict[str, dict] = {}
    live_meta: dict[str, dict] = {}
    for team_block in live_data:
        team = team_block.get("team") or {}
        raw_tc = team.get("tricode")
        tricode = fnhlpa.TRICODE_NORM.get(raw_tc, raw_tc)
        team_slug = team.get("slug")
        for row in team_block.get("players") or []:
            pl = row.get("player") or {}
            comp = row.get("compensation") or {}
            nhl_id = pl.get("nhlId")
            if not nhl_id:
                continue
            pid_str = str(int(nhl_id))
            base = int(comp.get("baseSalary") or 0)
            sb = int(comp.get("signingBonus") or 0)
            tot = int(comp.get("totalSalary") or (base + sb))
            aav = int(comp.get("aav") or tot)
            minor = int(comp.get("minorLeagueSalary") or 0)

            live_season[pid_str] = {
                "team": tricode,
                "aav_usd": aav,
                "total_salary_usd": tot,
                "base_salary_usd": base,
                "signing_bonus_usd": sb,
                "minor_league_salary_usd": minor,
            }
            live_meta[pid_str] = {
                "nhl_id": int(nhl_id),
                "nhlpa_player_id": pl.get("playerId"),
                "name": f"{pl.get('firstName', '')} {pl.get('lastName', '')}".strip(),
                "position": pl.get("positionCode"),
                "slug": pl.get("slug"),
                "team": tricode,
                "team_slug": team_slug,
            }

    added_pids = sorted(set(live_season) - set(old_season))
    removed_pids = sorted(set(old_season) - set(live_season))
    modified_pids = sorted(
        pid for pid in (set(live_season) & set(old_season))
        if live_season[pid] != old_season[pid]
    )

    changed = bool(added_pids or removed_pids or modified_pids)
    summary = {
        "changed": changed,
        "season": s_lbl,
        "live_count": len(live_season),
        "added": len(added_pids),
        "removed": len(removed_pids),
        "modified": len(modified_pids),
        "added_names": [live_meta[p]["name"] for p in added_pids[:15]],
        "modified_names": [live_meta[p]["name"] for p in modified_pids[:15]],
    }

    if not changed:
        print(f"[Contracts] No changes in {s_lbl} ({len(live_season):,} players identical).", flush=True)
        return summary

    print(
        f"[Contracts] Changes detected in {s_lbl}: "
        f"+{len(added_pids)} added, -{len(removed_pids)} removed, ~{len(modified_pids)} modified.",
        flush=True,
    )
    if dry_run:
        return summary

    # 1) Merge live current season into compensation_history.json (historical seasons untouched)
    for pid_str in removed_pids:
        entry = players.get(pid_str)
        if entry and s_lbl in (entry.get("yearly_compensation") or {}):
            del entry["yearly_compensation"][s_lbl]
            entry.pop("current_2026_27", None)

    for pid_str, rec in live_season.items():
        meta = live_meta[pid_str]
        entry = players.setdefault(pid_str, {
            "nhl_id": meta["nhl_id"],
            "nhlpa_player_id": meta["nhlpa_player_id"],
            "name": meta["name"],
            "position": meta["position"],
            "slug": meta["slug"],
            "current_team_tricode": None,
            "current_team_slug": None,
            "nhlpa_compensation_url": None,
            "nhlpa_player_url": (
                f"https://www.nhlpa.com/player/{meta['nhlpa_player_id']}/{meta['slug']}"
                if meta["nhlpa_player_id"] and meta["slug"] else None
            ),
            "yearly_compensation": {},
        })
        entry["current_team_tricode"] = meta["team"]
        entry["current_team_slug"] = meta["team_slug"]
        if meta["team_slug"]:
            entry["nhlpa_compensation_url"] = f"https://www.nhlpa.com/compensation/{meta['team_slug']}"
        entry["yearly_compensation"][s_lbl] = rec
        entry["yearly_compensation"] = dict(sorted(entry["yearly_compensation"].items()))
        inferred_start = fnhlpa.infer_contract_start_from_history(entry["yearly_compensation"], s_lbl)
        entry["inferred_contract_start_season"] = inferred_start
        entry["current_2026_27"] = {
            **rec,
            "two_way": rec["minor_league_salary_usd"] > 0,
        }
        if inferred_start:
            s0 = int(inferred_start.split("-")[0])
            seg = [v["total_salary_usd"] for k, v in entry["yearly_compensation"].items() if int(k.split("-")[0]) >= s0]
            entry["observed_contract_years"] = len(seg)
            entry["observed_mean_salary_usd"] = int(round(sum(seg) / len(seg))) if seg else rec["aav_usd"]

    hist["seasons"][s_lbl] = len(live_season)
    hist["total_unique_players"] = len(players)
    hist["generated_at_utc"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    NHLPA_HIST_PATH.write_text(json.dumps(hist, indent=2), encoding="utf-8")

    # 2) Fetch official NHL Web API bios ONLY for brand-new player_ids missing from bios cache
    bios = json.loads(BIOS_PATH.read_text(encoding="utf-8")) if BIOS_PATH.exists() else {}
    missing_bio_pids = [int(pid) for pid in live_season if pid not in bios or not bios[pid].get("birth_date")]
    if missing_bio_pids:
        print(f"[Contracts] Fetching NHL API bios for {len(missing_bio_pids)} brand-new player(s)...", flush=True)
        for pid in missing_bio_pids:
            try:
                raw = fetch_json_live(f"https://api-web.nhle.com/v1/player/{pid}/landing", timeout=20)
                bio = fnb.compact_bio(pid, raw)
                if bio:
                    bios[str(pid)] = bio
            except Exception as exc:
                print(f"  WARN: Could not fetch bio for player_id={pid}: {exc}", flush=True)
        BIOS_PATH.write_text(json.dumps(bios, separators=(",", ":")), encoding="utf-8")

    # 3) Rebuild local historical contracts & player-seasons CSV/JSON (~2s, 100% local)
    print("[Contracts] Rebuilding historical_contracts_2010_2027.{csv,json}...", flush=True)
    bhc.main()
    return summary


# ---------------------------------------------------------------------------
# 2. INCREMENTAL TRADES CHECK (Live Official NHL.com Current-Season Tracker)
# ---------------------------------------------------------------------------

def _extract_forge_chunks_live(slug: str) -> list[str]:
    url = f"https://forge-dapi.d3.nhle.com/v2/content/en-us/stories/{slug}"
    st = fetch_json_live(url)
    if not isinstance(st, dict):
        return []
    chunks: list[str] = []
    for p in st.get("parts") or []:
        if p.get("type") == "markdown" and p.get("content"):
            for line in re.split(r"\r?\n\s*\r?\n|\r?\n", p["content"]):
                line = line.strip()
                if line and bt.DATE_PREFIX_RE.search(line[:45]):
                    chunks.append(line)
    return chunks


def _fetch_forge_story_live(slug: str) -> dict | None:
    url = f"https://forge-dapi.d3.nhle.com/v2/content/en-us/stories/{slug}"
    try:
        st = fetch_json_live(url, timeout=20)
    except Exception:
        return None
    if not isinstance(st, dict):
        return None
    body_parts = [p.get("content", "") for p in (st.get("parts") or []) if p.get("type") == "markdown"]
    tags = [t.get("slug", "") for t in (st.get("tags") or []) if isinstance(t, dict)]
    return {
        "slug": slug,
        "headline": st.get("headline") or "",
        "date": (st.get("contentDate") or "")[:10],
        "url": dv3.forge_web_url(slug, tags),
        "body": "\n".join(body_parts),
    }


def _trade_content_key(summary: str) -> str:
    return bt.norm_name(summary[:90])


def _stable_new_trade_id(season: str, trade_date: str, summary: str) -> str:
    digest = hashlib.sha1(bt.norm_name(summary).encode("utf-8")).hexdigest()[:8]
    return f"{season.replace('-', '')}_{trade_date.replace('-', '')}_{digest}"


def _write_trades_outputs(trades_out: list[dict], summary_prev: dict) -> None:
    # Rebuild flat trade_assets rows from trades_out so CSV & JSON stay 100% consistent
    trades_out.sort(key=lambda x: (x["trade_date"], x["trade_id"]), reverse=True)
    assets_out: list[dict] = []
    season_counts = Counter()
    for t in trades_out:
        season_counts[t["season"]] += 1
        tms = t.get("teams") or []
        for recv_tm, alist in (t.get("assets_by_team") or {}).items():
            other_tms = [x for x in tms if x != recv_tm]
            from_tm = other_tms[0] if len(other_tms) == 1 else (",".join(other_tms) if other_tms else "")
            for a in alist:
                at = a.get("asset_type") or ""
                assets_out.append({
                    "trade_id": t["trade_id"],
                    "season": t["season"],
                    "trade_date": t["trade_date"],
                    "receiving_team": recv_tm,
                    "from_team": from_tm,
                    "asset_type": at,
                    "player_id": a.get("player_id") or "",
                    "player_name": a.get("player_name") or "",
                    "position": a.get("position") or "",
                    "pick_year": a.get("pick_year") or "",
                    "pick_round": a.get("pick_round") or "",
                    "pick_overall": a.get("pick_overall") or "",
                    "pick_in_round": a.get("pick_in_round") or "",
                    "pick_conditional": a.get("pick_conditional", "") if at == "draft_pick" else "",
                    "pick_original_team": a.get("pick_original_team") or "",
                    "pick_history": a.get("pick_history") or "",
                    "pick_status": a.get("pick_status") or "",
                    "pick_resolution_method": a.get("pick_resolution_method") or "",
                    "drafted_by_team": a.get("drafted_by_team") or "",
                    "drafted_player_id": a.get("drafted_player_id") or "",
                    "drafted_player_name": a.get("drafted_player_name") or "",
                    "drafted_player_position": a.get("drafted_player_position") or "",
                    "raw_asset": a.get("raw") or "",
                    "trade_article_url": t.get("trade_article_url") or "",
                })

    t_fields = [
        "trade_id", "season", "trade_date", "num_teams", "is_three_team", "teams",
        "team_a", "team_b", "team_c", "summary",
        "assets_by_team_json", "players_traded_json", "draft_picks_traded_json",
        "has_retained_salary", "retained_salary_json", "has_future_considerations",
        "trade_article_url", "tracker_url",
    ]
    with TRADES_CSV_PATH.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=t_fields)
        w.writeheader()
        for t in trades_out:
            tms = t.get("teams") or []
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

    a_fields = [
        "trade_id", "season", "trade_date", "receiving_team", "from_team",
        "asset_type", "player_id", "player_name", "position",
        "pick_year", "pick_round", "pick_overall", "pick_in_round",
        "pick_conditional", "pick_original_team", "pick_history",
        "pick_status", "pick_resolution_method",
        "drafted_by_team", "drafted_player_id", "drafted_player_name", "drafted_player_position",
        "raw_asset", "trade_article_url",
    ]
    with TRADE_ASSETS_CSV_PATH.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=a_fields)
        w.writeheader()
        for row in assets_out:
            w.writerow({k: row.get(k, "") for k in a_fields})

    new_summary = dict(summary_prev)
    new_summary["generated_at_utc"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    totals = dict(new_summary.get("totals") or {})
    totals["total_trades"] = len(trades_out)
    totals["total_asset_rows"] = len(assets_out)
    totals["three_team_trades"] = sum(1 for t in trades_out if t.get("is_three_team"))
    totals["trades_with_retained_salary"] = sum(1 for t in trades_out if t.get("has_retained_salary"))
    totals["future_considerations_trades"] = sum(1 for t in trades_out if t.get("has_future_considerations"))
    new_summary["totals"] = totals
    new_summary["by_season"] = dict(sorted(season_counts.items()))

    TRADES_SUMMARY_PATH.write_text(json.dumps(new_summary, indent=2), encoding="utf-8")
    TRADES_JSON_PATH.write_text(json.dumps({"summary": new_summary, "trades": trades_out}, indent=2), encoding="utf-8")


def check_and_update_trades(season_yr: int, dry_run: bool = False) -> dict:
    s_lbl = season_label(season_yr)
    slug = f"{s_lbl}-nhl-trades"
    tracker_url = f"https://www.nhl.com/news/topic/trade-coverage/{slug}"
    print(f"[Trades] Fetching live NHL.com {s_lbl} Trade Tracker ({slug})...", flush=True)

    chunks = _extract_forge_chunks_live(slug)
    if not chunks:
        print(f"[Trades] Tracker {slug} returned 0 chunks (offseason or uncreated yet). Skipping.", flush=True)
        return {"changed": False, "season": s_lbl, "new_trades": [], "updated_trades": []}

    tdb = json.loads(TRADES_JSON_PATH.read_text(encoding="utf-8"))
    all_trades: list[dict] = tdb["trades"]
    existing_season = [t for t in all_trades if t["season"] == s_lbl]
    existing_by_key = {_trade_content_key(t["summary"]): t for t in existing_season}

    # Parse live chunks cleanly
    live_parsed: list[dict] = []
    seen_keys: set[str] = set()
    for chunk in chunks:
        m_dt = bt.DATE_PREFIX_RE.search(chunk[:45])
        if not m_dt:
            continue
        mon_tok = m_dt.group(1).rstrip(".").lower()
        month = bt.MONTH_MAP.get(mon_tok)
        day = int(m_dt.group(2))
        if not month:
            continue

        embedded_ids = bt.extract_embedded_player_ids(chunk)
        if "|" in chunk:
            left_raw, right_raw = chunk.split("|", 1)
            m_lnk = re.search(r"\((https?://[^)\s]+|/[^)\s]+)\)", right_raw)
            raw_url = m_lnk.group(1) if m_lnk else None
        else:
            left_raw = chunk
            raw_url = None

        art_url, art_slug = bt.canonicalize_nhl_url(raw_url)
        summary_clean = bt.strip_markdown_links(dv3.clean_html(left_raw))
        summary_clean = re.sub(r"^[A-Za-z.]+\s+\d{1,2}\s*:\s*", "", summary_clean).strip()
        if len(summary_clean) < 25:
            continue

        ck = _trade_content_key(summary_clean)
        if ck in seen_keys:
            continue
        seen_keys.add(ck)

        live_parsed.append({
            "content_key": ck,
            "month": month,
            "day": day,
            "summary": summary_clean,
            "embedded_ids": embedded_ids,
            "article_url": art_url,
            "article_slug": art_slug,
        })

    new_items = [x for x in live_parsed if x["content_key"] not in existing_by_key]
    updated_items = [
        x for x in live_parsed
        if x["content_key"] in existing_by_key
        and existing_by_key[x["content_key"]]["summary"] != x["summary"]
    ]

    if not new_items and not updated_items:
        print(f"[Trades] No changes in {s_lbl} ({len(live_parsed)} trades identical).", flush=True)
        return {"changed": False, "season": s_lbl, "new_trades": [], "updated_trades": []}

    print(
        f"[Trades] Changes detected in {s_lbl}: +{len(new_items)} new, ~{len(updated_items)} updated.",
        flush=True,
    )
    if dry_run:
        return {
            "changed": True,
            "season": s_lbl,
            "new_trades": [x["summary"][:100] for x in new_items],
            "updated_trades": [x["summary"][:100] for x in updated_items],
            "upsert_records": [],
        }

    player_index = bt.build_player_index()
    upsert_records: list[dict] = []

    for item in new_items + updated_items:
        art = _fetch_forge_story_live(item["article_slug"]) if item["article_slug"] else None
        art_date = art["date"] if art else None
        trade_date = bt.resolve_trade_date(item["month"], item["day"], season_yr, art_date)
        trade_url = (art["url"] if art else item["article_url"]) or tracker_url
        bt.assert_no_cap_site(trade_url)
        bt.assert_no_cap_site(tracker_url)

        teams, raw_by_team, is_3way = bt.parse_trade_sides(item["summary"])
        assets_by_team: dict[str, list[dict]] = {}
        for tm, raw_clause in raw_by_team.items():
            assets_by_team[tm] = bt.classify_assets(raw_clause, player_index, item["embedded_ids"], season_yr)

        ret_list = bt.parse_retention_from_body((art["body"] if art else "") + " " + item["summary"])
        existing_match = existing_by_key.get(item["content_key"])
        trade_id = existing_match["trade_id"] if existing_match else _stable_new_trade_id(s_lbl, trade_date, item["summary"])

        players_traded = []
        picks_traded = []
        for recv_tm, alist in assets_by_team.items():
            for a in alist:
                at = a["asset_type"]
                if at in ("player", "signing_rights", "unsigned_draft_choice"):
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
                        "pick_status": "upcoming" if (a.get("pick_year") or 9999) > bt.LAST_COMPLETED_DRAFT_YEAR else "unresolved",
                    })

        rec = {
            "trade_id": trade_id,
            "season": s_lbl,
            "trade_date": trade_date,
            "num_teams": max(len(teams), len(assets_by_team)),
            "is_three_team": is_3way or len(teams) >= 3,
            "teams": teams,
            "summary": item["summary"],
            "assets_by_team": assets_by_team,
            "players_traded": players_traded,
            "draft_picks_traded": picks_traded,
            "has_retained_salary": bool(ret_list),
            "retained_salary": ret_list,
            "has_future_considerations": "future considerations" in item["summary"].lower(),
            "trade_article_url": trade_url,
            "tracker_url": tracker_url,
        }

        # Tag upcoming draft picks cleanly inside assets_by_team
        for alist in rec["assets_by_team"].values():
            for a in alist:
                if a.get("asset_type") == "draft_pick" and (a.get("pick_year") or 9999) > bt.LAST_COMPLETED_DRAFT_YEAR:
                    a["pick_status"] = "upcoming"

        if existing_match:
            idx = next(i for i, t in enumerate(all_trades) if t["trade_id"] == trade_id)
            all_trades[idx] = rec
        else:
            all_trades.append(rec)
        upsert_records.append(rec)

    _write_trades_outputs(all_trades, tdb.get("summary") or {})
    return {
        "changed": True,
        "season": s_lbl,
        "new_trades": [r["summary"][:100] for r in upsert_records[:len(new_items)]],
        "updated_trades": [r["summary"][:100] for r in upsert_records[len(new_items):]],
        "upsert_records": upsert_records,
    }


# ---------------------------------------------------------------------------
# 3. INCREMENTAL INJURIES CHECK (Live ESPN API + Recent NHL.com Injury News)
# ---------------------------------------------------------------------------

def _fetch_recent_nhl_injury_articles_live() -> dict[int, dict]:
    by_pid: dict[int, dict] = {}
    for expr, pages in [("injury", 4), ("nhl-created,news", 2)]:
        for page in range(pages):
            skip = page * 25
            url = (
                f"https://forge-dapi.d3.nhle.com/v2/content/en-us/stories"
                f"?tags.slug={expr}&$skip={skip}&$limit=25"
            )
            try:
                data = fetch_json_live(url, timeout=20)
            except Exception:
                break
            items = data.get("items") or [] if isinstance(data, dict) else []
            if not items:
                break
            for it in items:
                dt = (it.get("contentDate") or "")[:10]
                if dt < "2026-09-01":
                    continue
                hl = it.get("headline") or ""
                tags = [t.get("slug", "") for t in (it.get("tags") or []) if isinstance(t, dict)]
                if expr == "nhl-created,news" and not any(
                    k in hl.lower() for k in ("status report", "injur", "placed on ir", "out ", "surgery")
                ):
                    continue
                art_url = dv3.forge_web_url(it["slug"], tags)
                for t in tags:
                    m = re.match(r"^playerid-(\d+)$", t)
                    if m:
                        pid = int(m.group(1))
                        if pid not in by_pid or dt > by_pid[pid]["date"]:
                            by_pid[pid] = {"date": dt, "headline": hl, "url": art_url}
    return by_pid


def _injury_sig(r: dict) -> tuple:
    return (
        r.get("team") or "",
        r.get("player_id"),
        r.get("player_name") or "",
        r.get("injury_status") or "",
        r.get("injury_type") or "",
        r.get("injury_detail") or "",
        r.get("injury_side") or "",
        r.get("estimated_return_date") or "",
        r.get("short_comment") or "",
        r.get("nhl_article_url") or "",
    )


def check_and_update_injuries(season_yr: int, dry_run: bool = False) -> dict:
    s_lbl = season_label(season_yr)
    print("[Injuries] Fetching live ESPN NHL Injuries API + recent NHL.com injury news...", flush=True)
    espn = fetch_json_live(fi.ESPN_INJURIES_URL, timeout=25)
    if not isinstance(espn, dict) or not espn.get("injuries"):
        raise RuntimeError("ESPN NHL Injuries API returned empty payload")

    by_team_name, by_name = fi.build_player_lookup()
    nhl_articles = _fetch_recent_nhl_injury_articles_live()

    records: list[dict] = []
    status_counts = Counter()
    team_summary: dict[str, dict] = defaultdict(
        lambda: {"injured_players": 0, "injured_aav_usd": 0, "ir_count": 0, "dtd_count": 0}
    )

    for team_block in espn.get("injuries") or []:
        team_name = team_block.get("displayName") or ""
        team_code = fi.TEAM_NAME_TO_CODE.get(team_name.lower())
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
            status = inj.get("status") or ""
            matched = fi.match_player(espn_name, team_code, by_team_name, by_name)
            pid = matched["player_id"] if matched else None
            aav = matched["aav_usd"] if matched else None
            if not pos and matched:
                pos = matched.get("position") or ""

            nhl_art = nhl_articles.get(pid) if pid else None
            nhl_art_url = nhl_art["url"] if nhl_art else None
            source_url = nhl_art_url or espn_player_url or fi.ESPN_INJURIES_WEB
            fi.assert_no_cap_site(source_url)

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
                "injury_type": details.get("type") or "",
                "injury_detail": details.get("detail") or "",
                "injury_side": details.get("side") or "",
                "estimated_return_date": details.get("returnDate") or "",
                "updated_at_utc": inj.get("date") or "",
                "short_comment": (inj.get("shortComment") or "").strip(),
                "long_comment": (inj.get("longComment") or "").strip(),
                "source_url": source_url,
                "espn_player_url": espn_player_url,
                "nhl_article_url": nhl_art_url,
                "nhl_article_date": nhl_art["date"] if nhl_art else None,
            })

    records.sort(key=lambda x: (x["team"], x["injury_status"], x["player_name"]))

    # Diff against existing current_injuries.json
    old_records = []
    if INJURIES_JSON_PATH.exists():
        old_records = json.loads(INJURIES_JSON_PATH.read_text(encoding="utf-8")).get("injuries") or []

    old_sigs = {_injury_sig(r) for r in old_records}
    new_sigs = {_injury_sig(r) for r in records}
    changed = old_sigs != new_sigs

    old_keys = {(r.get("team"), r.get("player_name")): r for r in old_records}
    new_keys = {(r.get("team"), r.get("player_name")): r for r in records}
    added = sorted(set(new_keys) - set(old_keys))
    removed = sorted(set(old_keys) - set(new_keys))
    status_changed = sorted(
        k for k in (set(old_keys) & set(new_keys))
        if _injury_sig(old_keys[k]) != _injury_sig(new_keys[k])
    )

    summary = {
        "changed": changed,
        "live_count": len(records),
        "added": len(added),
        "removed": len(removed),
        "updated": len(status_changed),
        "added_list": [f"{tm} {nm} ({new_keys[(tm, nm)]['injury_status']})" for tm, nm in added[:15]],
        "removed_list": [f"{tm} {nm}" for tm, nm in removed[:15]],
        "updated_list": [f"{tm} {nm} ({new_keys[(tm, nm)]['injury_status']})" for tm, nm in status_changed[:15]],
    }

    if not changed:
        print(f"[Injuries] No changes ({len(records)} injured players identical).", flush=True)
        return summary

    print(
        f"[Injuries] Changes detected ({len(records)} total): "
        f"+{len(added)} newly injured, -{len(removed)} cleared/activated, ~{len(status_changed)} status/note updates.",
        flush=True,
    )
    if dry_run:
        return summary

    # Write CSV & JSON
    fields = [
        "team", "player_id", "player_name", "position", "aav_usd",
        "injury_status", "injury_type", "injury_detail", "injury_side",
        "estimated_return_date", "updated_at_utc", "short_comment",
        "source_url", "nhl_article_url", "espn_player_url",
    ]
    with INJURIES_CSV_PATH.open("w", newline="", encoding="utf-8") as f:
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
        "season": s_lbl,
        "snapshot_at_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "sources": [fi.ESPN_INJURIES_WEB, "https://www.nhl.com/news"],
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
    INJURIES_JSON_PATH.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return summary


# ---------------------------------------------------------------------------
# 4. SELECTIVE SUPABASE UPSERT
# ---------------------------------------------------------------------------

def upsert_specific_trades(cur, trade_records: list[dict]) -> int:
    if not trade_records:
        return 0
    rows = []
    for t in trade_records:
        s_yr = lsb.parse_season_start(t["season"]) or int(t["trade_date"][:4])
        tms = t.get("teams") or []
        rows.append((
            t["trade_id"],
            t["season"],
            s_yr,
            lsb.season_to_id(s_yr),
            lsb.parse_date(t["trade_date"]),
            int(t["num_teams"]),
            bool(t.get("is_three_team", False)),
            tms,
            tms[0] if len(tms) > 0 else None,
            tms[1] if len(tms) > 1 else None,
            tms[2] if len(tms) > 2 else None,
            t["summary"],
            Json(t.get("assets_by_team") or {}),
            Json(t.get("players_traded") or []),
            Json(t.get("draft_picks_traded") or []),
            bool(t.get("has_retained_salary", False)),
            Json(t["retained_salary"]) if t.get("retained_salary") else None,
            bool(t.get("has_future_considerations", False)),
            t.get("trade_article_url") or None,
            t.get("tracker_url") or None,
        ))
    execute_values(
        cur,
        """
        INSERT INTO public.trades (
            trade_id, season, season_year, season_id, trade_date,
            num_teams, is_three_team, teams, team_a, team_b, team_c,
            summary, assets_by_team, players_traded, draft_picks_traded,
            has_retained_salary, retained_salary, has_future_considerations,
            trade_article_url, tracker_url
        ) VALUES %s
        ON CONFLICT (trade_id) DO UPDATE SET
            season = EXCLUDED.season,
            season_year = EXCLUDED.season_year,
            season_id = EXCLUDED.season_id,
            trade_date = EXCLUDED.trade_date,
            num_teams = EXCLUDED.num_teams,
            is_three_team = EXCLUDED.is_three_team,
            teams = EXCLUDED.teams,
            team_a = EXCLUDED.team_a,
            team_b = EXCLUDED.team_b,
            team_c = EXCLUDED.team_c,
            summary = EXCLUDED.summary,
            assets_by_team = EXCLUDED.assets_by_team,
            players_traded = EXCLUDED.players_traded,
            draft_picks_traded = EXCLUDED.draft_picks_traded,
            has_retained_salary = EXCLUDED.has_retained_salary,
            retained_salary = EXCLUDED.retained_salary,
            has_future_considerations = EXCLUDED.has_future_considerations,
            trade_article_url = EXCLUDED.trade_article_url,
            tracker_url = EXCLUDED.tracker_url,
            updated_at = NOW()
        """,
        rows,
        page_size=100,
    )
    return len(rows)


def write_step_summary(c_res: dict, t_res: dict, i_res: dict, synced: list[str], dry_run: bool) -> None:
    lines = [
        "## NHL Daily Update Summary",
        f"- **Run time (UTC):** {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%SZ')}",
        f"- **Mode:** {'Dry run (no DB writes)' if dry_run else 'Live sync'}",
        f"- **Supabase tables updated:** {', '.join(synced) if synced else 'None (already up to date)'}",
        "",
        "| Feed | Changed? | Details |",
        "| :--- | :---: | :--- |",
        f"| **Contracts ({c_res.get('season', '')})** | {'YES' if c_res.get('changed') else 'No'} | "
        f"+{c_res.get('added', 0)} added, -{c_res.get('removed', 0)} removed, ~{c_res.get('modified', 0)} modified |",
        f"| **Trades ({t_res.get('season', '')})** | {'YES' if t_res.get('changed') else 'No'} | "
        f"+{len(t_res.get('new_trades', []))} new, ~{len(t_res.get('updated_trades', []))} updated |",
        f"| **Injuries** | {'YES' if i_res.get('changed') else 'No'} | "
        f"{i_res.get('live_count', 0)} active (+{i_res.get('added', 0)} new, -{i_res.get('removed', 0)} cleared, ~{i_res.get('updated', 0)} updated) |",
    ]
    if i_res.get("added_list"):
        lines.append("\n**Newly Injured:** " + ", ".join(i_res["added_list"]))
    if i_res.get("removed_list"):
        lines.append("\n**Cleared / Activated:** " + ", ".join(i_res["removed_list"]))
    if i_res.get("updated_list"):
        lines.append("\n**Injury Status Updates:** " + ", ".join(i_res["updated_list"]))
    if t_res.get("new_trades"):
        lines.append("\n**New Trades:**\n" + "\n".join(f"- {s}" for s in t_res["new_trades"]))
    if c_res.get("added_names"):
        lines.append("\n**New NHLPA Contracts:** " + ", ".join(c_res["added_names"]))

    md = "\n".join(lines) + "\n"
    print("\n" + md, flush=True)
    gh_sum = os.environ.get("GITHUB_STEP_SUMMARY")
    if gh_sum:
        with open(gh_sum, "a", encoding="utf-8") as f:
            f.write(md)


def main() -> None:
    parser = argparse.ArgumentParser(description="Incremental daily NHL contracts/trades/injuries updater")
    parser.add_argument("--season", type=int, default=DEFAULT_SEASON_YEAR, help="Season start year (e.g. 2026 for 2026-27)")
    parser.add_argument("--force", action="store_true", help="Force Supabase sync even if no local diff was detected")
    parser.add_argument("--dry-run", action="store_true", help="Check live sources and print diffs without modifying files or DB")
    parser.add_argument("--only", choices=("contracts", "trades", "injuries"), default=None, help="Only check one feed")
    args = parser.parse_args()

    c_res = {"changed": False, "season": season_label(args.season)}
    t_res = {"changed": False, "season": season_label(args.season), "new_trades": [], "updated_trades": []}
    i_res = {"changed": False}

    if args.only in (None, "contracts"):
        c_res = check_and_update_contracts(args.season, dry_run=args.dry_run)
    if args.only in (None, "trades"):
        t_res = check_and_update_trades(args.season, dry_run=args.dry_run)
    if args.only in (None, "injuries"):
        i_res = check_and_update_injuries(args.season, dry_run=args.dry_run)

    need_c = c_res.get("changed") or (args.force and args.only in (None, "contracts"))
    need_t = t_res.get("changed") or (args.force and args.only in (None, "trades"))
    need_i = i_res.get("changed") or (args.force and args.only in (None, "injuries"))

    synced: list[str] = []
    if not args.dry_run and (need_c or need_t or need_i):
        db_url = lsb.load_database_url()
        conn = psycopg2.connect(db_url)
        try:
            with conn:
                with conn.cursor() as cur:
                    if need_c:
                        print("[Supabase] Upserting contracts, contract_years, and contract_teams_years...", flush=True)
                        n_c, n_cy, n_proj, n_cty = lsb.load_contracts_years_and_teams(cur)
                        print(f"  Upserted {n_c:,} contracts, {n_cy:,} contract_years ({n_proj:,} projected), {n_cty:,} team-years.", flush=True)
                        synced.extend(["contracts", "contract_years", "contract_teams_years"])
                    if need_t:
                        if args.force and not t_res.get("upsert_records"):
                            print("[Supabase] Force-upserting all trades...", flush=True)
                            n_t = lsb.load_trades(cur)
                        else:
                            recs = t_res.get("upsert_records") or []
                            print(f"[Supabase] Upserting {len(recs)} new/updated trade(s)...", flush=True)
                            n_t = upsert_specific_trades(cur, recs)
                        print(f"  Upserted {n_t:,} trade row(s).", flush=True)
                        synced.append("trades")
                    if need_i:
                        print("[Supabase] Refreshing current injuries snapshot...", flush=True)
                        n_i = lsb.load_injuries(cur)
                        print(f"  Loaded {n_i:,} current injuries.", flush=True)
                        synced.append("injuries")
        finally:
            conn.close()
    elif not args.dry_run:
        print("[Supabase] No changes detected across any feed — 0 database writes performed.", flush=True)

    write_step_summary(c_res, t_res, i_res, synced, args.dry_run)

    # Expose changed categories to GitHub Actions outputs if running in Actions
    gh_out = os.environ.get("GITHUB_OUTPUT")
    if gh_out:
        changed_parts = []
        if c_res.get("changed"):
            changed_parts.append("contracts")
        if t_res.get("changed"):
            changed_parts.append("trades")
        if i_res.get("changed"):
            changed_parts.append("injuries")
        with open(gh_out, "a", encoding="utf-8") as f:
            f.write(f"changed={'true' if changed_parts else 'false'}\n")
            f.write(f"changed_feeds={', '.join(changed_parts)}\n")


if __name__ == "__main__":
    main()
