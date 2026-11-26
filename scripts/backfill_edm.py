"""Proof-of-concept backfill of Edmonton Oilers (EDM) active contracts, trades,
signing/expiry statuses, and cap penalties.

Data sources (strictly primary / non-cap-site):
  1. Official NHL Web API (api-web.nhle.com) for roster, birth dates, and career GP
  2. Official NHL.com / team press releases via NHL Forge API (forge-dapi.d3.nhle.com)
  3. Sportsnet (sportsnet.ca) and ProHockeyRumors (prohockeyrumors.com) editorial
     articles for buyout cap schedules, bonus overages, and ELC slide figures

No contract/cap database sites (PuckPedia, CapWages, Spotrac, etc.) are queried.
Released under CC0 1.0 Universal (Public Domain).
"""

from __future__ import annotations

import csv
import json
import re
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) OpenNHLContractsTest/0.2"}

CURRENT_SEASON = "2026-27"
CURRENT_SEASON_INT = 20262027

# Primary-source contract and acquisition mapping for EDM's active roster.
# All 27 players map to an official NHL.com / team press release.
SOURCES = {
    8482703: {  # Colton Dach
        "signing": {
            "source_type": "nhl_forge",
            "slug": "colton-dach-signs-two-year-contract-with-edmonton-oilers",
            "url": "https://www.nhl.com/news/colton-dach-signs-two-year-contract-with-edmonton-oilers",
            "signing_status": "RFA",
            "start_season": "2026-27",
            "end_season": "2027-28",
        },
        "acquisition": {
            "method": "trade",
            "from_team": "CHI",
            "slug": "release-oilers-acquire-dickinson-dach-from-chicago",
            "url": "https://www.nhl.com/oilers/news/release-oilers-acquire-dickinson-dach-from-chicago",
            "summary": "Acquired with Jason Dickinson (50% retained) from CHI for Andrew Mangiapane and a conditional 2027 1st-round pick.",
        },
    },
    8477450: {  # Jason Dickinson
        "signing": {
            "source_type": "nhl_forge",
            "slug": "jason-dickinson-signs-five-year-contract-with-edmonton-oilers",
            "url": "https://www.nhl.com/news/jason-dickinson-signs-five-year-contract-with-edmonton-oilers",
            "secondary_url": "https://www.nhl.com/oilers/news/release-oilers-sign-jason-dickinson-to-five-year-contract",
            "signing_status": "UFA",
            "start_season": "2026-27",
            "end_season": "2030-31",
        },
        "acquisition": {
            "method": "trade",
            "from_team": "CHI",
            "slug": "release-oilers-acquire-dickinson-dach-from-chicago",
            "url": "https://www.nhl.com/oilers/news/release-oilers-acquire-dickinson-dach-from-chicago",
            "summary": "Acquired (50% retained) with Colton Dach from CHI for Andrew Mangiapane and a conditional 2027 1st-round pick.",
        },
    },
    8477934: {  # Leon Draisaitl
        "signing": {
            "source_type": "nhl_forge",
            "slug": "release-oilers-sign-draisaitl-to-eight-year-extension",
            "url": "https://www.nhl.com/oilers/news/release-oilers-sign-draisaitl-to-eight-year-extension",
            "signing_status": "UFA",
            "start_season": "2025-26",
            "end_season": "2032-33",
        },
        "acquisition": {"method": "draft", "summary": "Drafted 3rd overall by EDM in 2014."},
    },
    8480029: {  # Alex Formenton
        "signing": {
            "source_type": "nhl_forge",
            "slug": "release-oilers-sign-alex-formenton-to-one-year-contract",
            "url": "https://www.nhl.com/oilers/news/release-oilers-sign-alex-formenton-to-one-year-contract",
            "signing_status": "UFA",
            "start_season": "2026-27",
            "end_season": "2026-27",
            "notes": "Previously RFA property of OTT; signed as UFA after OTT relinquished rights.",
        },
        "acquisition": {
            "method": "free_agency",
            "url": "https://www.nhl.com/oilers/news/release-oilers-sign-alex-formenton-to-one-year-contract",
            "summary": "Signed as an unrestricted free agent.",
        },
    },
    8479365: {  # Trent Frederic
        "signing": {
            "source_type": "nhl_forge",
            "slug": "trent-frederic-signs-eight-year-contract-with-edmonton-oilers",
            "url": "https://www.nhl.com/news/trent-frederic-signs-eight-year-contract-with-edmonton-oilers",
            "secondary_url": "https://www.nhl.com/oilers/news/release-oilers-sign-frederic-to-eight-year-extension",
            "signing_status": "UFA",
            "start_season": "2025-26",
            "end_season": "2032-33",
        },
        "acquisition": {
            "method": "trade",
            "from_team": "BOS/NJD",
            "slug": "release-oilers-acquire-trent-frederic-max-jones-petr-hauser",
            "url": "https://www.nhl.com/oilers/news/release-oilers-acquire-trent-frederic-max-jones-petr-hauser",
            "summary": "Acquired with Max Jones and rights to Petr Hauser in a 3-team deal with BOS and NJD.",
        },
    },
    8480842: {  # Filip Hallander
        "signing": {
            "source_type": "nhl_forge",
            "slug": "penguins-sign-forward-filip-hallander-to-a-two-year-contract",
            "url": "https://www.nhl.com/penguins/news/penguins-sign-forward-filip-hallander-to-a-two-year-contract",
            "secondary_url": "https://www.sportsnet.ca/nhl/article/penguins-sign-forward-filip-hallander-to-two-year-contract/",
            "signing_team": "PIT",
            "signing_status": "RFA",
            "start_season": "2025-26",
            "end_season": "2026-27",
            "notes": "PIT retained his RFA rights while he played in Sweden (SHL) and re-signed him in April 2025.",
        },
        "acquisition": {
            "method": "trade",
            "from_team": "PIT",
            "slug": "release-oilers-acquire-filip-hallander-from-pittsburgh",
            "url": "https://www.nhl.com/oilers/news/release-oilers-acquire-filip-hallander-from-pittsburgh",
            "summary": "Acquired from PIT in exchange for defenseman Spencer Stastney.",
        },
    },
    8475786: {  # Zach Hyman
        "signing": {
            "source_type": "nhl_forge",
            "slug": "hyman-ryan-ceci-barrie-foegele-edmonton-support-325871236",
            "url": "https://www.nhl.com/news/hyman-ryan-ceci-barrie-foegele-edmonton-support-325871236",
            "secondary_url": "https://www.nhl.com/news/zach-hyman-signing-update-news-status-325806286",
            "signing_status": "UFA",
            "start_season": "2021-22",
            "end_season": "2027-28",
        },
        "acquisition": {
            "method": "free_agency",
            "url": "https://www.nhl.com/news/zach-hyman-signing-update-news-status-325806286",
            "summary": "Signed as an unrestricted free agent.",
        },
    },
    8477406: {  # Mattias Janmark
        "signing": {
            "source_type": "nhl_forge",
            "slug": "release-oilers-re-sign-janmark-to-three-year-contract",
            "url": "https://www.nhl.com/oilers/news/release-oilers-re-sign-janmark-to-three-year-contract",
            "signing_status": "UFA",
            "start_season": "2024-25",
            "end_season": "2026-27",
        },
        "acquisition": {
            "method": "free_agency",
            "url": "https://www.nhl.com/news/release-oilers-sign-janmark-to-one-year-contract-335012900",
            "summary": "Originally signed as a free agent in July 2022; re-signed in 2023 and 2024.",
        },
    },
    8479368: {  # Max Jones
        "signing": {
            "source_type": "nhl_forge",
            "slug": "release-oilers-re-sign-max-jones-to-one-year-contract",
            "url": "https://www.nhl.com/oilers/news/release-oilers-re-sign-max-jones-to-one-year-contract",
            "signing_status": "UFA",
            "start_season": "2026-27",
            "end_season": "2026-27",
        },
        "acquisition": {
            "method": "trade",
            "from_team": "BOS",
            "slug": "release-oilers-acquire-trent-frederic-max-jones-petr-hauser",
            "url": "https://www.nhl.com/oilers/news/release-oilers-acquire-trent-frederic-max-jones-petr-hauser",
            "summary": "Acquired from BOS alongside Trent Frederic in a 3-team deal.",
        },
    },
    8478472: {  # Mathieu Joseph
        "signing": {
            "source_type": "nhl_forge",
            "slug": "release-oilers-sign-mathieu-joseph-to-one-year-contract",
            "url": "https://www.nhl.com/oilers/news/release-oilers-sign-mathieu-joseph-to-one-year-contract",
            "signing_status": "UFA",
            "start_season": "2026-27",
            "end_season": "2026-27",
        },
        "acquisition": {
            "method": "free_agency",
            "url": "https://www.nhl.com/oilers/news/release-oilers-sign-mathieu-joseph-to-one-year-contract",
            "summary": "Signed as an unrestricted free agent.",
        },
    },
    8477953: {  # Kasperi Kapanen
        "signing": {
            "source_type": "nhl_forge",
            "slug": "release-oilers-re-sign-kasperi-kapanen-to-one-year-contract",
            "url": "https://www.nhl.com/oilers/news/release-oilers-re-sign-kasperi-kapanen-to-one-year-contract",
            "signing_status": "UFA",
            "start_season": "2026-27",
            "end_season": "2026-27",
        },
        "acquisition": {
            "method": "waivers",
            "summary": "Originally claimed off waivers from STL in Nov 2024; re-signed in 2025 and 2026.",
        },
    },
    8478402: {  # Connor McDavid
        "signing": {
            "source_type": "nhl_forge",
            "slug": "connor-mcdavid-signs-2-year-deal-with-edmonton",
            "url": "https://www.nhl.com/news/connor-mcdavid-signs-2-year-deal-with-edmonton",
            "secondary_url": "https://www.nhl.com/oilers/news/release-oilers-sign-mcdavid-to-two-year-extension",
            "signing_status": "UFA",
            "start_season": "2026-27",
            "end_season": "2027-28",
        },
        "acquisition": {"method": "draft", "summary": "Drafted 1st overall by EDM in 2015."},
    },
    8486161: {  # Owen Michaels
        "signing": {
            "source_type": "nhl_forge",
            "slug": "release-oilers-re-sign-owen-michaels-to-two-year-contract",
            "url": "https://www.nhl.com/oilers/news/release-oilers-re-sign-owen-michaels-to-two-year-contract",
            "signing_status": "RFA",
            "start_season": "2026-27",
            "end_season": "2027-28",
            "notes": "Signed 1-year ELC as NCAA free agent (Western Michigan) in April 2026 for end of 2025-26, then re-signed as RFA in July 2026.",
        },
        "acquisition": {
            "method": "free_agency",
            "url": "https://www.nhl.com/oilers/news/release-oilers-re-sign-owen-michaels-to-two-year-contract",
            "summary": "Signed as an undrafted NCAA free agent in April 2026; re-signed July 2026.",
        },
    },
    8476454: {  # Ryan Nugent-Hopkins
        "signing": {
            "source_type": "nhl_forge",
            "slug": "edmonton-oilers-agree-to-eight-year-contract-extension-with-ryan-nugen-325513576",
            "url": "https://www.nhl.com/news/edmonton-oilers-agree-to-eight-year-contract-extension-with-ryan-nugen-325513576",
            "secondary_url": "https://www.nhl.com/news/release-oilers-sign-nugent-hopkins-to-eight-year-extension-325512432",
            "signing_status": "UFA",
            "start_season": "2021-22",
            "end_season": "2028-29",
        },
        "acquisition": {"method": "draft", "summary": "Drafted 1st overall by EDM in 2011."},
    },
    8481617: {  # Vasily Podkolzin
        "signing": {
            "source_type": "nhl_forge",
            "slug": "vasily-podkolzin-signs-3-year-contract-with-edmonton-oilers",
            "url": "https://www.nhl.com/news/vasily-podkolzin-signs-3-year-contract-with-edmonton-oilers",
            "secondary_url": "https://www.nhl.com/oilers/news/release-oilers-sign-podkolzin-to-three-year-extension",
            "signing_status": "RFA",
            "start_season": "2026-27",
            "end_season": "2028-29",
        },
        "acquisition": {
            "method": "trade",
            "from_team": "VAN",
            "slug": "release-oilers-acquire-podkolzin-from-canucks",
            "url": "https://www.nhl.com/oilers/news/release-oilers-acquire-podkolzin-from-canucks",
            "summary": "Acquired from VAN for Ottawa's 2025 4th-round pick.",
        },
    },
    8483512: {  # Matt Savoie
        "signing": {
            "source_type": "nhl_forge",
            "slug": "buffalo-sabres-sign-first-round-picks-matt-savoie-jiri-kulich-noah-ost-335006690",
            "url": "https://www.nhl.com/sabres/news/buffalo-sabres-sign-first-round-picks-matt-savoie-jiri-kulich-noah-ost-335006690",
            "secondary_url": "https://www.prohockeyrumors.com/2026/09/salary-cap-deep-dive-edmonton-oilers-9.html",
            "signing_team": "BUF",
            "signing_status": "ELC",
            "start_season": "2024-25",
            "end_season": "2026-27",
            "secondary_aav_regex": r"Matthew Savoie\s*\(\$([\d,]+)",
            "notes": "3-year ELC signed July 15, 2022; slid in 2022-23 (0 NHL GP) and 2023-24 (1 NHL GP) per NHL API game logs. Official Sabres release omitted dollars; ProHockeyRumors reported post-slide $886,666 base cap hit (+ up to $1M 'A' bonuses).",
        },
        "acquisition": {
            "method": "trade",
            "from_team": "BUF",
            "slug": "release-oilers-acquire-matt-savoie-from-sabres",
            "url": "https://www.nhl.com/oilers/news/release-oilers-acquire-matt-savoie-from-sabres",
            "summary": "Acquired from BUF for Ryan McLeod and Tyler Tullio.",
        },
    },
    8480803: {  # Evan Bouchard
        "signing": {
            "source_type": "nhl_forge",
            "slug": "evan-bouchard-signs-four-year-contract-to-remain-with-edmonton-oilers",
            "url": "https://www.nhl.com/news/evan-bouchard-signs-four-year-contract-to-remain-with-edmonton-oilers",
            "secondary_url": "https://www.nhl.com/oilers/news/release-oilers-sign-bouchard-to-four-year-contract-extension",
            "signing_status": "RFA",
            "start_season": "2025-26",
            "end_season": "2028-29",
        },
        "acquisition": {"method": "draft", "summary": "Drafted 10th overall by EDM in 2018."},
    },
    8475218: {  # Mattias Ekholm
        "signing": {
            "source_type": "nhl_forge",
            "slug": "mattias-ekholm-signs-3-year-contract-with-edmonton-oilers",
            "url": "https://www.nhl.com/news/mattias-ekholm-signs-3-year-contract-with-edmonton-oilers",
            "secondary_url": "https://www.nhl.com/oilers/news/release-oilers-sign-ekholm-to-three-year-extension",
            "signing_status": "UFA",
            "start_season": "2026-27",
            "end_season": "2028-29",
        },
        "acquisition": {
            "method": "trade",
            "from_team": "NSH",
            "slug": "release-oilers-acquire-mattias-ekholm-from-nashville-341632712",
            "url": "https://www.nhl.com/news/release-oilers-acquire-mattias-ekholm-from-nashville-341632712",
            "summary": "Acquired (4% retained on prior deal) with a 2024 6th-round pick from NSH for Tyson Barrie, Reid Schaefer, 2023 1st, and 2024 4th.",
        },
    },
    8480834: {  # Ty Emberson
        "signing": {
            "source_type": "nhl_forge",
            "slug": "release-oilers-sign-emberson-to-two-year-extension",
            "url": "https://www.nhl.com/oilers/news/release-oilers-sign-emberson-to-two-year-extension",
            "signing_status": "RFA",
            "start_season": "2025-26",
            "end_season": "2026-27",
        },
        "acquisition": {
            "method": "trade",
            "from_team": "SJS",
            "slug": "release-oilers-acquire-emberson-from-sharks",
            "url": "https://www.nhl.com/oilers/news/release-oilers-acquire-emberson-from-sharks",
            "summary": "Acquired from SJS for Cody Ceci and EDM's 2025 3rd-round pick.",
        },
    },
    8482166: {  # Shakir Mukhamadullin
        "signing": {
            "source_type": "nhl_forge",
            "slug": "shakir-mukhamadullin-spencer-stastney-each-sign-with-edmonton-oilers",
            "url": "https://www.nhl.com/news/shakir-mukhamadullin-spencer-stastney-each-sign-with-edmonton-oilers",
            "signing_status": "RFA",
            "start_season": "2026-27",
            "end_season": "2027-28",
        },
        "acquisition": {
            "method": "trade",
            "from_team": "SJS",
            "slug": "darnell-nurse-traded-to-san-jose-sharks-by-edmonton-oilers",
            "url": "https://www.nhl.com/news/darnell-nurse-traded-to-san-jose-sharks-by-edmonton-oilers",
            "summary": "Acquired with the rights to Zachary Sharp from SJS for Darnell Nurse (no salary retained).",
        },
    },
    8476473: {  # Connor Murphy
        "signing": {
            "source_type": "nhl_forge",
            "slug": "connor-murphy-signs-5-year-contract-with-edmonton-oilers",
            "url": "https://www.nhl.com/news/connor-murphy-signs-5-year-contract-with-edmonton-oilers",
            "secondary_url": "https://www.nhl.com/oilers/news/release-oilers-sign-connor-murphy-to-five-year-contract",
            "signing_status": "UFA",
            "start_season": "2026-27",
            "end_season": "2030-31",
        },
        "acquisition": {
            "method": "trade",
            "from_team": "CHI",
            "slug": "release-oilers-acquire-connor-murphy-from-chicago",
            "url": "https://www.nhl.com/oilers/news/release-oilers-acquire-connor-murphy-from-chicago",
            "summary": "Acquired (50% retained on prior deal) from CHI for a 2028 2nd-round pick.",
        },
    },
    8480831: {  # Alec Regula
        "signing": {
            "source_type": "nhl_forge",
            "slug": "release-oilers-sign-regula-to-two-year-extension",
            "url": "https://www.nhl.com/oilers/news/release-oilers-sign-regula-to-two-year-extension",
            "signing_status": "RFA",
            "start_season": "2025-26",
            "end_season": "2026-27",
        },
        "acquisition": {
            "method": "waivers",
            "from_team": "BOS",
            "slug": "release-oilers-claim-alec-regula-off-waivers-from-boston",
            "url": "https://www.nhl.com/oilers/news/release-oilers-claim-alec-regula-off-waivers-from-boston",
            "summary": "Claimed off waivers from BOS on Dec 11, 2024.",
        },
    },
    8478854: {  # Ryan Shea
        "signing": {
            "source_type": "nhl_forge",
            "slug": "release-oilers-sign-ryan-shea-to-five-year-contract",
            "url": "https://www.nhl.com/oilers/news/release-oilers-sign-ryan-shea-to-five-year-contract",
            "signing_status": "UFA",
            "start_season": "2026-27",
            "end_season": "2030-31",
        },
        "acquisition": {
            "method": "free_agency",
            "url": "https://www.nhl.com/oilers/news/release-oilers-sign-ryan-shea-to-five-year-contract",
            "summary": "Signed as an unrestricted free agent.",
        },
    },
    8478013: {  # Jake Walman
        "signing": {
            "source_type": "nhl_forge",
            "slug": "jake-walman-signs-seven-year-contract-with-edmonton-oilers",
            "url": "https://www.nhl.com/news/jake-walman-signs-seven-year-contract-with-edmonton-oilers",
            "secondary_url": "https://www.nhl.com/oilers/news/release-oilers-sign-walman-to-seven-year-extension",
            "signing_status": "UFA",
            "start_season": "2026-27",
            "end_season": "2032-33",
        },
        "acquisition": {
            "method": "trade",
            "from_team": "SJS",
            "slug": "release-oilers-acquire-walman-from-sharks",
            "url": "https://www.nhl.com/oilers/news/release-oilers-acquire-walman-from-sharks",
            "summary": "Acquired from SJS for a conditional 2026 1st-round pick and Carl Berglund.",
        },
    },
    8475883: {  # Frederik Andersen
        "signing": {
            "source_type": "nhl_forge",
            "slug": "frederik-andersen-signs-one-year-contract-with-edmonton-oilers",
            "url": "https://www.nhl.com/news/frederik-andersen-signs-one-year-contract-with-edmonton-oilers",
            "secondary_url": "https://www.prohockeyrumors.com/2026/09/salary-cap-deep-dive-edmonton-oilers-9.html",
            "signing_status": "UFA",
            "start_season": "2026-27",
            "end_season": "2026-27",
            "notes": "35+ contract: NHL.com reported $2.8M total; ProHockeyRumors reported $1.0M base salary + up to $1.8M in performance bonuses.",
        },
        "acquisition": {
            "method": "free_agency",
            "url": "https://www.nhl.com/news/frederik-andersen-signs-one-year-contract-with-edmonton-oilers",
            "summary": "Signed as an unrestricted free agent.",
        },
    },
    8477465: {  # Tristan Jarry
        "signing": {
            "source_type": "nhl_forge",
            "slug": "tristan-jarry-signs-five-year-contract-with-pittsburgh-penguins-344899412",
            "url": "https://www.nhl.com/news/tristan-jarry-signs-five-year-contract-with-pittsburgh-penguins-344899412",
            "signing_team": "PIT",
            "signing_status": "UFA",
            "start_season": "2023-24",
            "end_season": "2027-28",
        },
        "acquisition": {
            "method": "trade",
            "from_team": "PIT",
            "slug": "release-oilers-acquire-tristan-jarry-from-penguins",
            "url": "https://www.nhl.com/oilers/news/release-oilers-acquire-tristan-jarry-from-penguins",
            "summary": "Acquired with Samuel Poulin from PIT for Stuart Skinner, Brett Kulak, and a 2029 2nd-round pick.",
        },
    },
    8482221: {  # Devon Levi
        "signing": {
            "source_type": "nhl_forge",
            "slug": "devon-levi-signs-2-year-contract-with-buffalo-sabres",
            "url": "https://www.nhl.com/news/devon-levi-signs-2-year-contract-with-buffalo-sabres",
            "signing_team": "BUF",
            "signing_status": "RFA",
            "start_season": "2025-26",
            "end_season": "2026-27",
        },
        "acquisition": {
            "method": "trade",
            "from_team": "BUF",
            "slug": "release-oilers-acquire-devon-levi-from-buffalo",
            "url": "https://www.nhl.com/oilers/news/release-oilers-acquire-devon-levi-from-buffalo",
            "summary": "Acquired with a 2028 7th-round pick from BUF for EDM's 2028 3rd-round pick.",
        },
    },
}

# Active 2026-27 cap penalties (buyouts, performance bonus overages, retained salary).
CAP_PENALTIES = [
    {
        "penalty_type": "buyout",
        "player_id": 8475789,
        "player_name": "Jack Campbell",
        "event_date": "2024-06-30",
        "start_season": "2024-25",
        "end_season": "2029-30",
        "current_season": CURRENT_SEASON,
        "current_season_cap_hit_usd": 2_600_000,
        "schedule_by_season_usd": {
            "2024-25": 1_100_000,
            "2025-26": 2_300_000,
            "2026-27": 2_600_000,
            "2027-28": 1_500_000,
            "2028-29": 1_500_000,
            "2029-30": 1_500_000,
        },
        "source_url": "https://www.nhl.com/news/jack-campbell-placed-on-unconditional-waivers-by-edmonton-oilers",
        "secondary_source_url": "https://www.sportsnet.ca/nhl/article/oilers-place-jack-campbell-on-unconditional-waivers-for-purpose-of-a-buyout/",
        "notes": "Bought out final 3 years of 5-year, $25M deal signed July 13, 2022; cap hit peaks at $2.6M in 2026-27 before dropping to $1.5M for 2027-28 through 2029-30.",
    },
    {
        "penalty_type": "bonus_overage",
        "player_id": None,
        "player_name": "Team Performance Bonus Overage (2025-26 carryover)",
        "event_date": "2026-09-01",
        "start_season": "2026-27",
        "end_season": "2026-27",
        "current_season": CURRENT_SEASON,
        "current_season_cap_hit_usd": 250_000,
        "schedule_by_season_usd": {
            "2026-27": 250_000,
        },
        "source_url": "https://www.prohockeyrumors.com/2026/09/salary-cap-deep-dive-edmonton-oilers-9.html",
        "secondary_source_url": None,
        "notes": "2025-26 performance bonus overage carried onto the 2026-27 cap (includes Matt Savoie hitting an 'A' bonus in his rookie campaign).",
    },
]

WORD_NUMS = {
    "one": 1, "two": 2, "three": 3, "four": 4,
    "five": 5, "six": 6, "seven": 7, "eight": 8,
}


def fetch_text(url: str) -> tuple[int, str]:
    req = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=20) as resp:
        return resp.status, resp.read().decode("utf-8", errors="replace")


def clean_html(raw: str) -> str:
    raw = re.sub(r"<script[^>]*>.*?</script>", " ", raw, flags=re.S | re.I)
    raw = re.sub(r"<style[^>]*>.*?</style>", " ", raw, flags=re.S | re.I)
    raw = re.sub(r"<forge-entity[^>]*>(.*?)</forge-entity>", r"\1", raw)
    raw = re.sub(r"<[^>]+>", " ", raw)
    return re.sub(r"\s+", " ", raw).strip()


def parse_money(val_str: str, unit: str | None) -> int:
    num = float(val_str.replace(",", ""))
    if unit and unit.lower().startswith("m"):
        return int(round(num * 1_000_000))
    return int(round(num))


def extract_terms_from_lead(text: str) -> dict:
    """Parse contract length, total value, and AAV directly from the article lead."""
    lead = text[:1200]
    term_years = None
    m_term = re.search(
        r"\b(one|two|three|four|five|six|seven|eight|[1-8])[\s-]year\b",
        lead,
        re.I,
    )
    if m_term:
        tok = m_term.group(1).lower()
        term_years = WORD_NUMS.get(tok, int(tok) if tok.isdigit() else None)

    aav = None
    total_value = None

    m_aav = re.search(
        r"(?:average annual value(?:\s*\(AAV\))?|AAV)\s+of\s+\$([\d,]+(?:\.\d+)?)\s*(million\b|M\b)?",
        lead,
        re.I,
    )
    if m_aav:
        aav = parse_money(m_aav.group(1), m_aav.group(2))

    m_total = re.search(
        r"(?:one|two|three|four|five|six|seven|eight|[1-8])[\s-]year,\s+\$([\d,]+(?:\.\d+)?)\s*(million\b|M\b)?",
        lead,
        re.I,
    )
    if m_total:
        total_value = parse_money(m_total.group(1), m_total.group(2))

    if term_years and total_value and not aav:
        aav = int(round(total_value / term_years))
    elif term_years and aav and not total_value:
        total_value = aav * term_years

    if aav is not None and not (700_000 <= aav <= 25_000_000):
        raise ValueError(f"Parsed AAV out of NHL CBA bounds ({aav}) from lead: {lead[:200]}")

    two_way = bool(re.search(r"\btwo-way\b", lead, re.I))
    sentences = re.split(r"(?<=[.!?])\s+", lead)
    quote = " ".join(sentences[:2]).strip()
    return {
        "term_years": term_years,
        "aav_usd": aav,
        "total_value_usd": total_value,
        "two_way": two_way,
        "source_quote": quote[:360],
    }


def compute_expiry_status(bio: dict, position: str, end_season: str) -> tuple[str, str | None]:
    """Compute CBA Article 10.1 expiry status (RFA vs UFA) from official NHL API bio."""
    exp_year = int(end_season.split("-")[0]) + 1  # e.g. '2026-27' -> 2027 (July 1, 2027)
    by, bm, bd = map(int, bio["birthDate"].split("-"))
    age_june_30 = exp_year - by - ((6, 30) < (bm, bd))

    # Count regular-season NHL GP by season and pro seasons
    nhl_by_season: dict[int, int] = {}
    pro_seasons: set[int] = set()
    total_nhl_gp = 0
    for st in bio.get("seasonTotals", []):
        if st.get("gameTypeId") != 2:
            continue
        lg = st.get("leagueAbbrev", "")
        gp = st.get("gamesPlayed", 0) or 0
        if lg == "NHL":
            nhl_by_season[st["season"]] = nhl_by_season.get(st["season"], 0) + gp
            total_nhl_gp += gp
            pro_seasons.add(st["season"])
        elif lg in ("AHL", "ECHL", "KHL", "SHL", "Liiga", "NL", "NLA"):
            pro_seasons.add(st["season"])

    # Rule 1: Group 3 UFA by age (>= 27 on June 30 of expiry year)
    if age_june_30 >= 27:
        return "UFA", f"Group 3 UFA (age {age_june_30} on June 30, {exp_year})"

    # Rule 2: Group 3 UFA by accrued NHL seasons (>= 7 seasons of 40+ GP skater / 30+ GP goalie)
    gp_threshold = 30 if position == "G" else 40
    accrued_prior = sum(
        1 for s, gp in nhl_by_season.items() if s < CURRENT_SEASON_INT and gp >= gp_threshold
    )
    remaining_seasons = max(1, exp_year - 2026)  # incl. 2026-27
    if accrued_prior + remaining_seasons >= 7:
        return "UFA", f"Group 3 UFA (projected {accrued_prior + remaining_seasons} accrued NHL seasons)"

    # Rule 3: Group 6 UFA check (age 25-26 on June 30, 3+ pro seasons, <80 NHL GP skater / <28 goalie)
    gp_min_g6 = 28 if position == "G" else 80
    projected_pro = len(pro_seasons) + max(0, exp_year - 2027)
    if age_june_30 >= 25 and projected_pro >= 3 and total_nhl_gp < gp_min_g6:
        gp_needed = gp_min_g6 - total_nhl_gp
        if end_season == CURRENT_SEASON:
            return (
                "RFA",
                f"RFA if reaching {gp_min_g6} career NHL GP ({gp_needed} more GP needed in {CURRENT_SEASON}); otherwise Group 6 UFA",
            )
        return (
            "RFA",
            f"RFA if reaching {gp_min_g6} career NHL GP by {end_season} ({gp_needed} more GP needed); otherwise Group 6 UFA",
        )

    return "RFA", f"Age {age_june_30} on June 30, {exp_year} (<27 and <7 accrued seasons)"


def fetch_player_record(player: dict) -> dict:
    pid = player["id"]
    cfg = SOURCES[pid]
    s_cfg = cfg["signing"]

    # Fetch official NHL API player bio for birthDate & career GP
    _, bio_raw = fetch_text(f"https://api-web.nhle.com/v1/player/{pid}/landing")
    bio = json.loads(bio_raw)

    # Fetch primary NHL.com Forge signing article
    _, raw = fetch_text(
        f"https://forge-dapi.d3.nhle.com/v2/content/en-us/stories/{s_cfg['slug']}"
    )
    story = json.loads(raw)
    signed_date = (story.get("contentDate") or "")[:10]
    headline = story.get("headline") or story.get("title") or ""
    body = " ".join(
        clean_html(p["content"])
        for p in story.get("parts", [])
        if p.get("type") == "markdown" and p.get("content")
    )
    parsed = extract_terms_from_lead(body)

    # Optional secondary article parse if official ELC release omitted dollar terms
    if parsed["aav_usd"] is None and s_cfg.get("secondary_aav_regex") and s_cfg.get("secondary_url"):
        _, sec_raw = fetch_text(s_cfg["secondary_url"])
        sec_plain = clean_html(sec_raw)
        m_sec = re.search(s_cfg["secondary_aav_regex"], sec_plain, re.I)
        if m_sec:
            parsed["aav_usd"] = parse_money(m_sec.group(1), None)
            if parsed["term_years"]:
                parsed["total_value_usd"] = parsed["aav_usd"] * parsed["term_years"]

    expiry_status, expiry_note = compute_expiry_status(bio, player["pos"], s_cfg["end_season"])

    # Fetch acquisition date if a Forge trade/waiver release is linked
    acq = dict(cfg["acquisition"])
    if "slug" in acq:
        _, raw_acq = fetch_text(
            f"https://forge-dapi.d3.nhle.com/v2/content/en-us/stories/{acq['slug']}"
        )
        acq_story = json.loads(raw_acq)
        acq["date"] = (acq_story.get("contentDate") or "")[:10]
        acq["headline"] = acq_story.get("headline") or ""
        acq.pop("slug", None)

    return {
        "player_id": pid,
        "player_name": player["name"],
        "position": player["pos"],
        "sweater_number": player.get("sweater"),
        "birth_date": bio.get("birthDate"),
        "current_team": "EDM",
        "signing_team": s_cfg.get("signing_team", "EDM"),
        "signed_date": signed_date,
        "signing_status": s_cfg["signing_status"],
        "expiry_status": expiry_status,
        "expiry_status_note": expiry_note,
        "term_years": parsed["term_years"],
        "aav_usd": parsed["aav_usd"],
        "total_value_usd": parsed["total_value_usd"],
        "start_season": s_cfg["start_season"],
        "end_season": s_cfg["end_season"],
        "two_way": parsed["two_way"],
        "source_url": s_cfg["url"],
        "secondary_source_url": s_cfg.get("secondary_url"),
        "source_headline": headline,
        "source_quote": parsed["source_quote"],
        "notes": s_cfg.get("notes"),
        "acquisition": acq,
    }


def main() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    _, roster_raw = fetch_text("https://api-web.nhle.com/v1/roster/EDM/current")
    roster_json = json.loads(roster_raw)

    players = []
    for group in ("forwards", "defensemen", "goalies"):
        for p in roster_json[group]:
            players.append(
                {
                    "id": p["id"],
                    "name": f"{p['firstName']['default']} {p['lastName']['default']}",
                    "last": p["lastName"]["default"],
                    "pos": p["positionCode"],
                    "sweater": p.get("sweaterNumber"),
                }
            )

    missing = [p for p in players if p["id"] not in SOURCES]
    if missing:
        raise RuntimeError(f"Unmapped roster players: {missing}")

    with ThreadPoolExecutor(max_workers=8) as ex:
        records = list(ex.map(fetch_player_record, players))

    # Verify HTTP 200 on every unique source URL across contracts, acquisitions, and cap penalties
    urls = sorted(
        {
            u
            for r in records
            for u in (
                r["source_url"],
                r.get("secondary_source_url"),
                r["acquisition"].get("url"),
            )
            if u
        }
        | {
            u
            for p in CAP_PENALTIES
            for u in (p["source_url"], p.get("secondary_source_url"))
            if u
        }
    )

    def check_url(u: str) -> tuple[str, int]:
        req = urllib.request.Request(u, headers=HEADERS)
        with urllib.request.urlopen(req, timeout=15) as resp:
            return u, resp.status

    with ThreadPoolExecutor(max_workers=10) as ex:
        url_statuses = dict(ex.map(check_url, urls))

    failed = {u: s for u, s in url_statuses.items() if s != 200}
    if failed:
        raise RuntimeError(f"Broken source URLs: {failed}")

    known_aav_records = [r for r in records if r["aav_usd"] is not None]
    total_roster_aav = sum(r["aav_usd"] for r in known_aav_records)
    total_penalties_2627 = sum(p["current_season_cap_hit_usd"] for p in CAP_PENALTIES)

    payload = {
        "license": "CC0-1.0",
        "season": CURRENT_SEASON,
        "team": "EDM",
        "team_name": "Edmonton Oilers",
        "generated_at_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "roster_source": "https://api-web.nhle.com/v1/roster/EDM/current",
        "summary": {
            "roster_players": len(records),
            "contracts_with_verified_source_url": len(records),
            "contracts_with_public_aav": len(known_aav_records),
            "total_active_roster_aav_usd": total_roster_aav,
            "cap_penalties_count": len(CAP_PENALTIES),
            "total_cap_penalties_usd": total_penalties_2627,
            "combined_roster_plus_penalties_usd": total_roster_aav + total_penalties_2627,
            "verified_unique_urls": len(url_statuses),
        },
        "contracts": records,
        "cap_penalties": CAP_PENALTIES,
    }

    (DATA_DIR / "edm_contracts.json").write_text(json.dumps(payload, indent=2), encoding="utf-8")

    # Flat CSV for active roster contracts
    csv_path = DATA_DIR / "edm_contracts.csv"
    fieldnames = [
        "player_id",
        "player_name",
        "position",
        "signing_team",
        "signed_date",
        "signing_status",
        "expiry_status",
        "term_years",
        "aav_usd",
        "total_value_usd",
        "start_season",
        "end_season",
        "two_way",
        "source_url",
        "acquisition_method",
        "acquisition_url",
    ]
    with csv_path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        for r in records:
            w.writerow(
                {
                    "player_id": r["player_id"],
                    "player_name": r["player_name"],
                    "position": r["position"],
                    "signing_team": r["signing_team"],
                    "signed_date": r["signed_date"],
                    "signing_status": r["signing_status"],
                    "expiry_status": r["expiry_status"],
                    "term_years": r["term_years"],
                    "aav_usd": r["aav_usd"],
                    "total_value_usd": r["total_value_usd"],
                    "start_season": r["start_season"],
                    "end_season": r["end_season"],
                    "two_way": r["two_way"],
                    "source_url": r["source_url"],
                    "acquisition_method": r["acquisition"]["method"],
                    "acquisition_url": r["acquisition"].get("url", ""),
                }
            )

    # Flat CSV for cap penalties (buyouts, bonus overages, retained salary)
    pen_csv = DATA_DIR / "edm_cap_penalties.csv"
    pen_fields = [
        "penalty_type",
        "player_name",
        "event_date",
        "start_season",
        "end_season",
        "current_season",
        "current_season_cap_hit_usd",
        "source_url",
        "secondary_source_url",
        "notes",
    ]
    with pen_csv.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=pen_fields)
        w.writeheader()
        for p in CAP_PENALTIES:
            w.writerow({k: p.get(k, "") for k in pen_fields})

    print(json.dumps(payload["summary"], indent=2))
    print("\nActive Roster Contracts:")
    for r in sorted(records, key=lambda x: -(x["aav_usd"] or 0)):
        aav_str = f"${r['aav_usd']:>10,}" if r["aav_usd"] else "   [undiscl]"
        print(
            f"  {r['player_name']:21} {r['position']:1} | {r['signing_status']:3} -> {r['expiry_status']:3} | "
            f"{r['term_years']}y | {aav_str} | {r['start_season']}..{r['end_season']}"
        )
    print("\n2026-27 Cap Penalties:")
    for p in CAP_PENALTIES:
        print(
            f"  {p['penalty_type']:14} | {p['player_name']:48} | "
            f"${p['current_season_cap_hit_usd']:>10,} | {p['start_season']}..{p['end_season']}"
        )


if __name__ == "__main__":
    main()
