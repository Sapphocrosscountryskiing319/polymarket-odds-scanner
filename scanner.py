#!/usr/bin/env python3
"""
polymarket-odds-scanner — Find mispriced sports markets on Polymarket

Compares Polymarket sports pricing against real sportsbook odds from ESPN (free),
SharpAPI (free), and The Odds API (optional). When Polymarket is mispriced vs.
bookmakers, alerts you with actionable signals.

Covers: NBA, NHL, MLB, NCAA Basketball, EPL, La Liga, Bundesliga, Serie A,
        Ligue 1, Champions League, Europa League, MLS

Setup:
  pip install requests
  python3 scanner.py --scan           # Find edges (ESPN = free, no key needed)
  python3 scanner.py --results        # Show signal track record
  python3 scanner.py --whales         # Find whale-heavy sports markets
  python3 scanner.py --monitor        # Continuous 30-min scanning

Optional env vars (all free tiers):
  SHARP_API_KEY    — SharpAPI key (12 req/min free, sharpapi.io)
  ODDS_API_KEY     — The Odds API key (500 req/month free, the-odds-api.com)
  TELEGRAM_TOKEN   — Telegram bot token for alerts
  TELEGRAM_CHAT_ID — Telegram chat/channel ID for alerts
"""

import argparse
import json
import os
import re
import sqlite3
import time
import logging
import requests
from datetime import datetime, timezone, timedelta
from pathlib import Path
from dataclasses import dataclass, asdict

# ─── Config ──────────────────────────────────────────────────────────────────
GAMMA_API = "https://gamma-api.polymarket.com"
DATA_API = "https://data-api.polymarket.com"
ODDS_API = "https://api.the-odds-api.com/v4"
ODDS_API_KEY = os.environ.get("ODDS_API_KEY", "")

# SharpAPI — free tier: 12 req/min, no CC required
SHARP_API = "https://api.sharpapi.io/api/v1"
SHARP_API_KEY = os.environ.get("SHARP_API_KEY", "")

# Telegram alerts (optional)
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
TELEGRAM_PUBLIC_CHANNEL = os.environ.get("TELEGRAM_PUBLIC_CHANNEL", "")

DB_FILE = Path(__file__).parent / "signals.db"

LOG_DIR = Path(__file__).parent / "logs"
LOG_DIR.mkdir(exist_ok=True)

# Edge thresholds — calibrated from live trading data:
# 5%+ edge signals: 6/6 correct (100% WR)
# 2-3% edge signals: 1/9 correct (11% WR) — noise, not signal
MIN_EDGE_PCT = 5.0
MIN_VOLUME = 1000
MAX_POSITION = 15

# Sport keys to fetch from The Odds API (h2h moneyline odds) — BACKUP SOURCE
SPORT_KEYS = [
    "basketball_nba",
    "basketball_ncaab",
    "icehockey_nhl",
    "soccer_epl",
    "soccer_spain_la_liga",
    "soccer_germany_bundesliga",
    "soccer_italy_serie_a",
    "soccer_france_ligue_one",
    "soccer_uefa_champs_league",
    "soccer_uefa_europa_league",
    "soccer_usa_mls",
    "baseball_mlb",
]

# ESPN hidden API — PRIMARY SOURCE (free, no auth, unlimited)
ESPN_BASE = "https://site.api.espn.com/apis/site/v2/sports"
ESPN_ODDS_BASE = "https://sports.core.api.espn.com/v2/sports"

# Maps our sport_key to ESPN paths: (scoreboard_path, odds_path, is_soccer)
ESPN_SPORTS = {
    "basketball_nba":              ("basketball/nba",                          "basketball/leagues/nba",                          False),
    "basketball_ncaab":            ("basketball/mens-college-basketball",      "basketball/leagues/mens-college-basketball",       False),
    "icehockey_nhl":               ("hockey/nhl",                             "hockey/leagues/nhl",                              False),
    "soccer_epl":                  ("soccer/eng.1",                           "soccer/leagues/eng.1",                            True),
    "soccer_spain_la_liga":        ("soccer/esp.1",                           "soccer/leagues/esp.1",                            True),
    "soccer_germany_bundesliga":   ("soccer/ger.1",                           "soccer/leagues/ger.1",                            True),
    "soccer_italy_serie_a":        ("soccer/ita.1",                           "soccer/leagues/ita.1",                            True),
    "soccer_france_ligue_one":     ("soccer/fra.1",                           "soccer/leagues/fra.1",                            True),
    "soccer_uefa_champs_league":   ("soccer/uefa.champions",                  "soccer/leagues/uefa.champions",                   True),
    "soccer_uefa_europa_league":   ("soccer/uefa.europa",                     "soccer/leagues/uefa.europa",                      True),
    "soccer_usa_mls":              ("soccer/usa.1",                           "soccer/leagues/usa.1",                            True),
    "baseball_mlb":                ("baseball/mlb",                           "baseball/leagues/mlb",                            False),
}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [ODDS] %(levelname)s %(message)s",
    handlers=[
        logging.FileHandler(LOG_DIR / "sports_odds.log"),
        logging.StreamHandler(),
    ],
)
log = logging.getLogger("odds")


@dataclass
class OddsOpportunity:
    polymarket_title: str
    polymarket_slug: str
    polymarket_outcome: str
    polymarket_price: float
    polymarket_implied_prob: float
    bookmaker_avg_prob: float
    edge_pct: float
    sport: str
    bookmaker_odds: dict
    recommended_side: str
    confidence: str


def decimal_to_prob(odds: float) -> float:
    return 1.0 / odds * 100 if odds > 0 else 0


def american_to_prob(ml: float) -> float:
    if ml > 0:
        return 100 / (ml + 100) * 100
    elif ml < 0:
        return abs(ml) / (abs(ml) + 100) * 100
    return 0


def _ml_to_decimal(ml: float) -> float:
    if ml > 0:
        return ml / 100 + 1
    elif ml < 0:
        return 100 / abs(ml) + 1
    return 1.0


# ─── Odds Sources ────────────────────────────────────────────────────────────

def fetch_odds(sport_key: str) -> list[dict]:
    """Fetch odds from The Odds API (optional, 500 req/month free)."""
    if not ODDS_API_KEY:
        return []
    try:
        r = requests.get(f"{ODDS_API}/sports/{sport_key}/odds", params={
            "apiKey": ODDS_API_KEY,
            "regions": "us,eu,uk",
            "markets": "h2h",
            "oddsFormat": "decimal",
        }, timeout=15)
        r.raise_for_status()
        remaining = r.headers.get("x-requests-remaining", "?")
        log.info(f"Odds API: {sport_key} | {len(r.json())} events | {remaining} requests remaining")
        return r.json()
    except Exception as e:
        log.error(f"Odds API error for {sport_key}: {e}")
        return []


SHARP_LEAGUES = {
    "basketball_nba": "nba", "basketball_ncaab": "ncaab",
    "icehockey_nhl": "nhl", "soccer_epl": "epl",
    "soccer_spain_la_liga": "la_liga", "soccer_germany_bundesliga": "bundesliga",
    "soccer_italy_serie_a": "serie_a", "soccer_france_ligue_one": "ligue_1",
    "soccer_uefa_champs_league": "champions_league",
    "soccer_uefa_europa_league": "europa_league",
    "soccer_usa_mls": "mls", "baseball_mlb": "mlb",
}


def fetch_sharp_odds(sport_key: str) -> list[dict]:
    """Fetch odds from SharpAPI (free tier, 12 req/min)."""
    if not SHARP_API_KEY:
        return []
    league = SHARP_LEAGUES.get(sport_key)
    if not league:
        return []
    try:
        r = requests.get(f"{SHARP_API}/odds", params={
            "league": league, "market": "moneyline", "limit": 200,
        }, headers={"X-API-Key": SHARP_API_KEY}, timeout=15)
        r.raise_for_status()
        data = r.json()
        if not data.get("success"):
            return []

        events_by_id = {}
        for odd in data.get("data", []):
            eid = odd.get("event_id", "")
            if eid not in events_by_id:
                events_by_id[eid] = {
                    "id": eid, "sport_key": sport_key,
                    "home_team": odd.get("home_team", ""),
                    "away_team": odd.get("away_team", ""),
                    "bookmakers": {},
                }
            ev = events_by_id[eid]
            book = odd.get("sportsbook", "unknown")
            if book not in ev["bookmakers"]:
                ev["bookmakers"][book] = []
            ev["bookmakers"][book].append({
                "name": odd.get("selection", ""),
                "price": odd.get("odds_decimal", 2.0),
            })

        events_out = []
        for ev in events_by_id.values():
            bookmakers = []
            for book_name, outcomes in ev["bookmakers"].items():
                bookmakers.append({
                    "key": book_name,
                    "title": book_name.replace("_", " ").title(),
                    "markets": [{"key": "h2h", "outcomes": outcomes}],
                })
            ev["bookmakers"] = bookmakers
            events_out.append(ev)

        log.info(f"SharpAPI: {sport_key} -> {len(events_out)} events with odds")
        return events_out
    except Exception as e:
        log.debug(f"SharpAPI error for {sport_key}: {e}")
        return []


def fetch_espn_odds(sport_key: str) -> list[dict]:
    """Fetch odds from ESPN hidden API (free, no auth, unlimited)."""
    espn_cfg = ESPN_SPORTS.get(sport_key)
    if not espn_cfg:
        return []
    scoreboard_path, odds_path, is_soccer = espn_cfg
    events_out = []
    today = datetime.now(timezone.utc)

    for day_offset in range(3):
        date_str = (today + timedelta(days=day_offset)).strftime("%Y%m%d")
        try:
            r = requests.get(
                f"{ESPN_BASE}/{scoreboard_path}/scoreboard",
                params={"dates": date_str, "limit": 100}, timeout=8,
            )
            r.raise_for_status()
            events = r.json().get("events", [])
        except Exception as e:
            log.debug(f"ESPN scoreboard error {sport_key} {date_str}: {e}")
            continue

        for event in events:
            eid = event.get("id", "")
            comps = event.get("competitions", [{}])
            if not comps:
                continue
            comp = comps[0]
            competitors = comp.get("competitors", [])
            if len(competitors) < 2:
                continue

            home_team = away_team = None
            for c in competitors:
                team_data = c.get("team", {})
                name = team_data.get("displayName", "")
                if c.get("homeAway") == "home":
                    home_team = name
                else:
                    away_team = name

            if not home_team or not away_team:
                continue

            status_type = comp.get("status", {}).get("type", {}).get("name", "")
            if status_type in ("STATUS_FINAL", "STATUS_POSTPONED", "STATUS_CANCELED"):
                continue

            try:
                r2 = requests.get(
                    f"{ESPN_ODDS_BASE}/{odds_path}/events/{eid}/competitions/{eid}/odds",
                    timeout=8,
                )
                if r2.status_code != 200:
                    continue
                odds_items = r2.json().get("items", [])
            except Exception:
                continue

            if not odds_items:
                continue

            bookmakers = []
            for item in odds_items:
                provider = item.get("provider", {}).get("name", "ESPN")
                home_odds = item.get("homeTeamOdds", {})
                away_odds = item.get("awayTeamOdds", {})
                home_ml = home_odds.get("moneyLine")
                away_ml = away_odds.get("moneyLine")
                if home_ml is None or away_ml is None:
                    continue

                outcomes = [
                    {"name": home_team, "price": _ml_to_decimal(home_ml)},
                    {"name": away_team, "price": _ml_to_decimal(away_ml)},
                ]
                if is_soccer:
                    draw_data = item.get("drawOdds", {})
                    draw_ml = draw_data.get("moneyLine")
                    if draw_ml is not None:
                        outcomes.append({"name": "Draw", "price": _ml_to_decimal(draw_ml)})

                bookmakers.append({
                    "key": provider.lower().replace(" ", "_"),
                    "title": provider,
                    "markets": [{"key": "h2h", "outcomes": outcomes}],
                })

            if bookmakers:
                events_out.append({
                    "id": eid, "sport_key": sport_key,
                    "home_team": home_team, "away_team": away_team,
                    "bookmakers": bookmakers,
                })

        time.sleep(0.3)

    log.info(f"ESPN: {sport_key} -> {len(events_out)} events with odds")
    return events_out


# ─── Polymarket Data ─────────────────────────────────────────────────────────

POLY_CACHE_FILE = Path(__file__).parent / ".poly_sports_cache.json"
POLY_CACHE_MAX_AGE = 7200  # 2 hours


def fetch_polymarket_sports() -> list[dict]:
    """Fetch all active sports h2h moneyline markets from Polymarket."""
    if POLY_CACHE_FILE.exists():
        age = time.time() - POLY_CACHE_FILE.stat().st_mtime
        if age < POLY_CACHE_MAX_AGE:
            try:
                cached = json.loads(POLY_CACHE_FILE.read_text())
                log.info(f"Using cached Polymarket data ({len(cached)} markets, {age:.0f}s old)")
                return cached
            except Exception:
                pass

    sports = []
    seen_slugs = set()
    offset = 0
    max_offset = 3000
    consecutive_empty = 0

    while offset < max_offset:
        try:
            r = requests.get(f"{GAMMA_API}/events", params={
                "tag": "Sports", "closed": "false", "limit": 100, "offset": offset,
            }, timeout=8)
            r.raise_for_status()
            batch = r.json()
        except Exception as e:
            log.error(f"Polymarket fetch error at offset {offset}: {e}")
            break

        if not batch:
            break

        batch_h2h_count = 0
        for event in batch:
            event_title = event.get("title", "") or ""
            for m in event.get("markets", []):
                q = (m.get("question", "") or "").lower()
                slug = (m.get("slug", "") or "").lower()

                if "vs" not in q and "vs" not in event_title.lower():
                    continue

                outcomes_raw = m.get("outcomes", "[]")
                if isinstance(outcomes_raw, str):
                    outcomes = json.loads(outcomes_raw) if outcomes_raw else []
                else:
                    outcomes = outcomes_raw

                if not outcomes:
                    continue

                first_outcome = str(outcomes[0]).lower()
                if first_outcome in ("yes", "no", "over", "under"):
                    continue

                if any(kw in slug for kw in ["spread", "total", "1h-", "-1h",
                                              "points-", "rebounds-", "assists-"]):
                    continue

                prices_raw = m.get("outcomePrices", "[]")
                if isinstance(prices_raw, str):
                    prices = json.loads(prices_raw) if prices_raw else []
                else:
                    prices = prices_raw
                if prices:
                    float_prices = [float(p) for p in prices[:len(outcomes)]]
                    if any(p >= 0.98 for p in float_prices):
                        continue

                if slug in seen_slugs:
                    continue
                seen_slugs.add(slug)

                m["_event_title"] = event_title
                sports.append(m)
                batch_h2h_count += 1

        if batch_h2h_count == 0:
            consecutive_empty += 1
            if consecutive_empty >= 5:
                log.info(f"  Early stop at offset {offset}: {consecutive_empty} consecutive empty batches")
                break
        else:
            consecutive_empty = 0

        if offset % 1000 == 0 and offset > 0:
            log.info(f"  ... scanned {offset} events, {len(sports)} h2h markets so far")
        offset += 100

    log.info(f"Found {len(sports)} Polymarket h2h moneyline markets (scanned {offset} events)")

    try:
        POLY_CACHE_FILE.write_text(json.dumps(sports, default=str))
    except Exception:
        pass

    return sports


# ─── Matching Engine ─────────────────────────────────────────────────────────

def normalize_team_name(name: str) -> str:
    name = name.lower().strip()
    abbrevs = {
        " st ": " state ", " st.": " state",
        "n.c. ": "north carolina ", "fla ": "florida ",
        "conn ": "connecticut ", "mass ": "massachusetts ",
    }
    for abbr, full in abbrevs.items():
        name = name.replace(abbr, full)

    replacements = {
        "fc barcelona": "barcelona", "real madrid cf": "real madrid",
        "manchester united": "man united", "manchester city": "man city",
        "tottenham hotspur": "tottenham",
        "los angeles lakers": "lakers", "golden state warriors": "warriors",
        "boston celtics": "celtics", "new york knicks": "knicks",
        "milwaukee bucks": "bucks", "miami heat": "heat",
        "denver nuggets": "nuggets", "nashville predators": "predators",
        "seattle kraken": "kraken",
    }
    for full_name, short in replacements.items():
        if full_name in name:
            return short
    for suffix in [" fc", " cf", " afc", " sc", " bc"]:
        name = name.replace(suffix, "")
    return name.strip()


def get_bookmaker_consensus(event: dict) -> dict:
    team_probs = {}
    team_counts = {}
    for bookmaker in event.get("bookmakers", []):
        for market in bookmaker.get("markets", []):
            if market.get("key") != "h2h":
                continue
            for outcome in market.get("outcomes", []):
                name = outcome.get("name", "")
                odds = outcome.get("price", 0)
                if odds <= 0:
                    continue
                prob = 1.0 / odds * 100
                if name.lower() == "draw":
                    key = "draw"
                else:
                    key = normalize_team_name(name)
                team_probs[key] = team_probs.get(key, 0) + prob
                team_counts[key] = team_counts.get(key, 0) + 1

    avg_probs = {k: team_probs[k] / team_counts[k] for k in team_probs}
    total = sum(avg_probs.values())
    if total > 0 and total != 100:
        avg_probs = {k: v / total * 100 for k, v in avg_probs.items()}
    return avg_probs


_COMMON_MASCOTS = {
    "tigers", "bulldogs", "eagles", "hawks", "bears", "wildcats", "lions",
    "panthers", "warriors", "knights", "cardinals", "cougars", "mustangs",
    "owls", "rams", "wolves", "hornets", "rebels", "aggies", "spartans",
    "pioneers", "bobcats", "falcons", "huskies",
}


def _team_in_text(team: str, text: str, pro_sport: bool = False) -> bool:
    if team in text:
        return True
    parts = team.split()
    if len(parts) >= 2:
        school = " ".join(parts[:-1])
        if school in text:
            return True
        nickname = parts[-1]
        if pro_sport or nickname not in _COMMON_MASCOTS:
            if nickname in text:
                return True
    elif len(parts) == 1:
        if team in text:
            return True
    for suffix in [" fc", " cf", " afc", " sc", " bc"]:
        stripped = team.replace(suffix, "").strip()
        if stripped and stripped in text:
            return True
    return False


def match_odds_to_polymarket(odds_events: list[dict], poly_markets: list[dict], sport_key: str = "") -> list[OddsOpportunity]:
    """Match sportsbook h2h odds to Polymarket and find mispriced markets."""
    is_pro = sport_key not in ("basketball_ncaab",) and "college" not in sport_key

    opportunities = []

    for event in odds_events:
        home = normalize_team_name(event.get("home_team", ""))
        away = normalize_team_name(event.get("away_team", ""))
        consensus = get_bookmaker_consensus(event)
        if not consensus:
            continue

        for m in poly_markets:
            q = (m.get("question", "") or "").lower()
            title = (m.get("_event_title", "") or "").lower()
            combined = q + " " + title

            if any(kw in combined for kw in ["spread:", "o/u ", "over/under", "handicap", "(-", "(+"]):
                continue

            # Skip esports (shared city names with real sports teams)
            esports_markers = ["call of duty", "valorant", "league of legends",
                               "counter-strike", "cs2", "dota", "overwatch",
                               "rocket league", "esports", "e-sports",
                               "(bo3)", "(bo5)", "(bo7)", "thieves", "sentinels",
                               "cloud9", "faze", "optic", "100 thieves", "koi"]
            if any(marker in combined for marker in esports_markers):
                continue

            if not (_team_in_text(home, combined, pro_sport=is_pro) and _team_in_text(away, combined, pro_sport=is_pro)):
                continue

            # Cross-sport guard
            slug = (m.get("slug", "") or "").lower()
            slug_sport_prefixes = {
                "basketball_nba": ["nba-"], "basketball_ncaab": ["ncaab-", "cbb-", "cwbb-"],
                "icehockey_nhl": ["nhl-"], "soccer_epl": ["epl-"],
                "soccer_spain_la_liga": ["lal-"], "soccer_germany_bundesliga": ["bun-"],
                "soccer_italy_serie_a": ["itc-", "sea-"], "soccer_france_ligue_one": ["fl1-"],
                "soccer_uefa_champs_league": ["ucl-"], "soccer_uefa_europa_league": ["uel-"],
                "soccer_usa_mls": ["mls-"], "baseball_mlb": ["mlb-"],
            }
            expected_prefixes = slug_sport_prefixes.get(sport_key, [])
            if expected_prefixes and slug:
                all_known = [p for prefixes in slug_sport_prefixes.values() for p in prefixes]
                all_known.extend(["euroleague-", "bkligend-", "bknbl-", "bkcl-", "bkseriea-",
                                  "bkfr1-", "bkarg-", "bkkbl-", "bkcba-",
                                  "khl-", "ahl-", "snhl-", "cehl-", "dehl-", "shl-",
                                  "atp-", "wta-", "ufc-", "zuffa-",
                                  "dota-", "cs2-", "lol-", "val-", "ow-", "rl-",
                                  "cricket-", "cricipl-", "t20-", "test-", "odi-",
                                  "cfb-", "cwbb-", "cbb-"])
                slug_prefix = slug.split("-")[0] + "-" if "-" in slug else ""
                if slug_prefix and slug_prefix in all_known and slug_prefix not in expected_prefixes:
                    continue

            prices = m.get("outcomePrices", "[]")
            if isinstance(prices, str):
                prices = json.loads(prices) if prices else []
            outcomes = m.get("outcomes", "[]")
            if isinstance(outcomes, str):
                outcomes = json.loads(outcomes) if outcomes else []
            if len(outcomes) < 2 or len(prices) < 2:
                continue

            float_prices = [float(p) for p in prices[:len(outcomes)]]
            if max(float_prices) >= 0.98:
                continue

            # Date freshness check
            today = datetime.now(timezone.utc)
            valid_dates = [(today + timedelta(days=d)).strftime("%Y-%m-%d") for d in range(-1, 4)]
            if slug:
                slug_date_match = re.search(r'(20\d{2}-\d{2}-\d{2})', slug)
                if slug_date_match and slug_date_match.group(1) not in valid_dates:
                    continue

            for i, poly_outcome in enumerate(outcomes):
                poly_name = normalize_team_name(str(poly_outcome))
                poly_price = float_prices[i]
                poly_prob = poly_price * 100

                book_prob = None
                for book_team, prob in consensus.items():
                    if book_team == poly_name or _team_in_text(book_team, poly_name, pro_sport=is_pro) or _team_in_text(poly_name, book_team, pro_sport=is_pro):
                        book_prob = prob
                        break

                if book_prob is None:
                    continue

                edge = book_prob - poly_prob
                if abs(edge) < MIN_EDGE_PCT:
                    continue

                side = "BUY" if edge > 0 else "SELL"
                abs_edge = abs(edge)

                opportunities.append(OddsOpportunity(
                    polymarket_title=m.get("question", ""),
                    polymarket_slug=m.get("slug", ""),
                    polymarket_outcome=str(poly_outcome),
                    polymarket_price=poly_price,
                    polymarket_implied_prob=poly_prob,
                    bookmaker_avg_prob=book_prob,
                    edge_pct=abs_edge,
                    sport=event.get("sport_key", ""),
                    bookmaker_odds={"consensus": round(book_prob, 1)},
                    recommended_side=side,
                    confidence="HIGH" if abs_edge >= 10 else "MEDIUM",
                ))

    seen = {}
    for opp in opportunities:
        key = (opp.polymarket_slug, opp.polymarket_outcome)
        if key not in seen or opp.edge_pct > seen[key].edge_pct:
            seen[key] = opp

    result = list(seen.values())
    result.sort(key=lambda x: x.edge_pct, reverse=True)
    return result


# ─── Telegram Alerts ─────────────────────────────────────────────────────────

def send_telegram(msg: str, chat_id: str = ""):
    chat_id = chat_id or TELEGRAM_CHAT_ID
    if not TELEGRAM_TOKEN or not chat_id:
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            json={"chat_id": chat_id, "text": msg, "parse_mode": "Markdown"},
            timeout=10,
        )
    except Exception:
        pass


def send_public_signal(msg: str):
    send_telegram(msg)
    if TELEGRAM_PUBLIC_CHANNEL:
        send_telegram(msg, TELEGRAM_PUBLIC_CHANNEL)


def format_signal_message(opp: OddsOpportunity) -> str:
    if opp.recommended_side == "SELL":
        action = f"BUY NO on {opp.polymarket_outcome}"
        entry = f"${1 - opp.polymarket_price:.3f}"
        implied = f"{100 - opp.polymarket_implied_prob:.1f}%"
        book = f"{100 - opp.bookmaker_avg_prob:.1f}%"
    else:
        action = f"BUY {opp.polymarket_outcome}"
        entry = f"${opp.polymarket_price:.3f}"
        implied = f"{opp.polymarket_implied_prob:.1f}%"
        book = f"{opp.bookmaker_avg_prob:.1f}%"

    return (
        f"*SIGNAL: {action}*\n"
        f"Market: {opp.polymarket_title}\n"
        f"Entry: {entry}\n"
        f"Polymarket: {implied} | Books: {book}\n"
        f"Edge: {opp.edge_pct:.1f}% | Confidence: {opp.confidence}\n"
        f"Sport: {opp.sport.replace('_', ' ').title()}\n"
        f"polymarket.com/event/{opp.polymarket_slug}"
    )


# ─── Signal Tracking DB ─────────────────────────────────────────────────────

def init_db():
    conn = sqlite3.connect(str(DB_FILE))
    conn.execute("""
        CREATE TABLE IF NOT EXISTS signals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT NOT NULL,
            sport TEXT, market_title TEXT, slug TEXT,
            outcome TEXT, side TEXT, entry_price REAL,
            poly_prob REAL, book_prob REAL, edge_pct REAL,
            confidence TEXT,
            resolved INTEGER DEFAULT 0, resolution_price REAL,
            won INTEGER, pnl REAL, resolved_at TEXT,
            telegram_sent INTEGER DEFAULT 0
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS scan_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT NOT NULL,
            n_markets_scanned INTEGER,
            n_opportunities INTEGER,
            sports_checked TEXT
        )
    """)
    conn.commit()
    return conn


def save_signals(opportunities: list[OddsOpportunity]):
    conn = init_db()
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    saved = 0
    for opp in opportunities:
        existing = conn.execute(
            "SELECT id FROM signals WHERE slug=? AND outcome=? AND timestamp LIKE ?",
            (opp.polymarket_slug, opp.polymarket_outcome, f"{today}%")
        ).fetchone()
        if existing:
            continue
        conn.execute("""
            INSERT INTO signals (timestamp, sport, market_title, slug, outcome, side,
                                 entry_price, poly_prob, book_prob, edge_pct, confidence)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            datetime.now(timezone.utc).isoformat(),
            opp.sport, opp.polymarket_title, opp.polymarket_slug,
            opp.polymarket_outcome, opp.recommended_side,
            opp.polymarket_price, opp.polymarket_implied_prob,
            opp.bookmaker_avg_prob, opp.edge_pct, opp.confidence,
        ))
        saved += 1
    conn.commit()
    conn.close()
    return saved


def check_resolved_signals():
    conn = init_db()
    pending = conn.execute(
        "SELECT id, slug, outcome, side, entry_price FROM signals WHERE resolved=0"
    ).fetchall()
    if not pending:
        return []

    resolved = []
    for sig_id, slug, outcome, side, entry_price in pending:
        try:
            r = requests.get(f"{GAMMA_API}/markets", params={"slug": slug, "limit": 1}, timeout=10)
            markets = r.json()
            if not markets:
                continue
            m = markets[0]
            if not m.get("closed", False):
                continue

            outcomes_raw = m.get("outcomes", "[]")
            prices_raw = m.get("outcomePrices", "[]")
            if isinstance(outcomes_raw, str):
                outcomes_list = json.loads(outcomes_raw) if outcomes_raw else []
            else:
                outcomes_list = outcomes_raw
            if isinstance(prices_raw, str):
                prices_list = json.loads(prices_raw) if prices_raw else []
            else:
                prices_list = prices_raw

            resolution_price = None
            for i, o in enumerate(outcomes_list):
                if str(o) == outcome and i < len(prices_list):
                    resolution_price = float(prices_list[i])
                    break

            if resolution_price is None:
                continue

            if side == "BUY":
                won = 1 if resolution_price >= 0.95 else 0
                pnl = (1.0 - entry_price) if won else -entry_price
            else:
                won = 1 if resolution_price <= 0.05 else 0
                pnl = entry_price if won else -(1.0 - entry_price)

            conn.execute("""
                UPDATE signals SET resolved=1, resolution_price=?, won=?, pnl=?, resolved_at=?
                WHERE id=?
            """, (resolution_price, won, round(pnl, 4),
                  datetime.now(timezone.utc).isoformat(), sig_id))

            resolved.append({
                "id": sig_id, "slug": slug, "outcome": outcome,
                "side": side, "entry": entry_price, "resolution": resolution_price,
                "won": won, "pnl": pnl,
            })
        except Exception:
            continue

    conn.commit()
    conn.close()
    return resolved


def format_results_message(results: list[dict]) -> str:
    if not results:
        return ""
    wins = sum(1 for r in results if r["won"])
    total = len(results)
    total_pnl = sum(r["pnl"] for r in results)
    msg = f"*Results Update* -- {wins}/{total} correct ({wins/total*100:.0f}%)\n"
    msg += f"Total P&L: ${total_pnl:+.2f} per $1 stake\n\n"
    for r in results:
        icon = "W" if r["won"] else "L"
        msg += f"[{icon}] {r['side']} {r['outcome']} -> ${r['pnl']:+.4f}\n"
    return msg


# ─── Commands ────────────────────────────────────────────────────────────────

def cmd_scan():
    """Scan all sports for odds edges."""
    log.info("Fetching Polymarket sports markets...")
    poly_markets = fetch_polymarket_sports()
    log.info(f"Found {len(poly_markets)} Polymarket sports markets")

    all_opportunities = []

    for sport_key in ESPN_SPORTS:
        odds = fetch_espn_odds(sport_key)
        sharp_odds = fetch_sharp_odds(sport_key)
        if sharp_odds:
            if odds:
                espn_teams = {(e.get("home_team", "").lower(), e.get("away_team", "").lower()) for e in odds}
                for se in sharp_odds:
                    key = (se.get("home_team", "").lower(), se.get("away_team", "").lower())
                    if key not in espn_teams:
                        odds.append(se)
                    else:
                        for eo in odds:
                            if eo.get("home_team", "").lower() == key[0] and eo.get("away_team", "").lower() == key[1]:
                                eo["bookmakers"].extend(se.get("bookmakers", []))
                                break
            else:
                odds = sharp_odds

        if not odds and ODDS_API_KEY and sport_key in SPORT_KEYS:
            log.info(f"  ESPN+Sharp empty for {sport_key}, trying Odds API fallback...")
            odds = fetch_odds(sport_key)

        if odds:
            opps = match_odds_to_polymarket(odds, poly_markets, sport_key)
            all_opportunities.extend(opps)
            log.info(f"  {sport_key}: {len(odds)} events, {len(opps)} opportunities")
        time.sleep(0.5)

    resolved = check_resolved_signals()
    if resolved:
        log.info(f"Resolved {len(resolved)} signals")
        results_msg = format_results_message(resolved)
        if results_msg:
            send_public_signal(results_msg)
            print(results_msg)

    conn = init_db()
    conn.execute("INSERT INTO scan_log (timestamp, n_markets_scanned, n_opportunities, sports_checked) VALUES (?,?,?,?)",
                 (datetime.now(timezone.utc).isoformat(), len(poly_markets), len(all_opportunities),
                  ",".join(SPORT_KEYS)))
    conn.commit()
    conn.close()

    if not all_opportunities:
        log.info("No opportunities found. Markets are efficiently priced right now.")
        return

    all_opportunities.sort(key=lambda x: x.edge_pct, reverse=True)
    saved = save_signals(all_opportunities)
    log.info(f"Saved {saved} new signals to DB")

    print(f"\n=== SPORTS ODDS OPPORTUNITIES ({len(all_opportunities)} found) ===\n")
    for opp in all_opportunities[:20]:
        print(
            f"  {opp.confidence:6s} | {opp.recommended_side:4s} {opp.polymarket_outcome} "
            f"@ ${opp.polymarket_price:.3f} (Poly: {opp.polymarket_implied_prob:.1f}% vs Book: {opp.bookmaker_avg_prob:.1f}%) "
            f"| Edge: {opp.edge_pct:.1f}%"
        )
        print(f"         {opp.polymarket_title[:80]}")
        print()

    for opp in all_opportunities[:5]:
        msg = format_signal_message(opp)
        send_public_signal(msg)


def cmd_no_api_scan():
    """Scan whale sports activity (no API key needed)."""
    log.info("Scanning whale sports activity...")
    try:
        r = requests.get(f"{DATA_API}/v1/leaderboard", params={
            "timePeriod": "MONTH", "orderBy": "PNL", "limit": 25
        }, timeout=15)
        lb = r.json()
    except Exception:
        lb = []

    market_counts = {}
    for entry in lb[:15]:
        wallet = entry.get("proxyWallet", "")
        name = entry.get("userName") or entry.get("pseudonym") or "???"
        try:
            r = requests.get(f"{DATA_API}/positions", params={
                "user": wallet, "sizeThreshold": 100, "limit": 20, "sortBy": "CURRENT"
            }, timeout=15)
            positions = r.json()
            for pos in positions:
                title = pos.get("title", "")
                slug = pos.get("slug", "")
                outcome = pos.get("outcome", "")
                size = pos.get("size", 0)
                tl = title.lower()
                if any(w in tl for w in ["vs.", "vs ", "win on", "spread", "o/u", "nba", "nhl", "epl"]):
                    key = (slug, outcome)
                    if key not in market_counts:
                        market_counts[key] = {"title": title, "outcome": outcome, "whales": [], "total_size": 0}
                    market_counts[key]["whales"].append(name)
                    market_counts[key]["total_size"] += size
        except Exception:
            pass
        time.sleep(0.5)

    markets = sorted(market_counts.values(), key=lambda x: len(x["whales"]), reverse=True)

    print(f"\n=== SPORTS MARKETS WITH WHALE ACTIVITY ===\n")
    for m in markets[:20]:
        whale_names = ", ".join(m["whales"][:3])
        more = f" +{len(m['whales'])-3}" if len(m["whales"]) > 3 else ""
        print(f"  [{len(m['whales'])} whales] {m['outcome']:15s} | {m['total_size']:>10,.0f} sh | {m['title'][:55]}")
        print(f"            {whale_names}{more}")
        print()


def cmd_results():
    """Show signal track record."""
    conn = init_db()

    total = conn.execute("SELECT COUNT(*) FROM signals").fetchone()[0]
    resolved_count = conn.execute("SELECT COUNT(*) FROM signals WHERE resolved=1").fetchone()[0]
    pending = total - resolved_count
    wins = conn.execute("SELECT COUNT(*) FROM signals WHERE resolved=1 AND won=1").fetchone()[0]
    losses = resolved_count - wins
    total_pnl = conn.execute("SELECT COALESCE(SUM(pnl), 0) FROM signals WHERE resolved=1").fetchone()[0]
    wr = (wins / resolved_count * 100) if resolved_count > 0 else 0

    print(f"\n=== SIGNAL TRACK RECORD ===\n")
    print(f"  Total signals: {total}")
    print(f"  Resolved: {resolved_count} ({wins}W / {losses}L = {wr:.0f}% WR)")
    print(f"  Pending: {pending}")
    print(f"  Total P&L: ${total_pnl:+.4f} per $1 stake")
    print()

    sports = conn.execute("""
        SELECT sport, COUNT(*) as total,
               SUM(CASE WHEN won=1 THEN 1 ELSE 0 END) as wins,
               SUM(pnl) as pnl
        FROM signals WHERE resolved=1 GROUP BY sport ORDER BY pnl DESC
    """).fetchall()
    if sports:
        print("  By Sport:")
        for sport, t, w, p in sports:
            print(f"    {sport:30s} | {w}/{t} ({w/t*100:.0f}% WR) | ${p:+.4f}")
        print()

    recent = conn.execute("""
        SELECT timestamp, side, outcome, market_title, edge_pct, entry_price, resolved, won, pnl
        FROM signals ORDER BY timestamp DESC LIMIT 15
    """).fetchall()
    if recent:
        print("  Recent Signals:")
        for ts, side, outcome, title, edge, entry, res, won, pnl in recent:
            date = ts[:10]
            if res:
                icon = "W" if won else "L"
                pnl_str = f"${pnl:+.3f}"
            else:
                icon = "?"
                pnl_str = "pending"
            print(f"    {date} [{icon}] {side:4s} {outcome[:20]:20s} @ ${entry:.3f} | {edge:.1f}% edge | {pnl_str} | {title[:35]}")

    conn.close()

    resolved = check_resolved_signals()
    if resolved:
        print(f"\n  Just resolved: {len(resolved)} signals")
        results_msg = format_results_message(resolved)
        if results_msg:
            send_public_signal(results_msg)
            print(results_msg)


def cmd_monitor():
    """Continuous monitoring -- scan every 30 minutes."""
    log.info("Starting sports odds monitor (30-min intervals)...")
    while True:
        try:
            cmd_scan()
        except Exception as e:
            log.error(f"Scan error: {e}")
        log.info("Next scan in 30 minutes...")
        time.sleep(1800)


def main():
    parser = argparse.ArgumentParser(description="Polymarket Sports Odds Scanner")
    parser.add_argument("--scan", action="store_true", help="Scan for odds edges (ESPN free, no key needed)")
    parser.add_argument("--whales", action="store_true", help="Scan whale sports activity (no API key needed)")
    parser.add_argument("--results", action="store_true", help="Show signal track record")
    parser.add_argument("--monitor", action="store_true", help="Continuous monitoring (30-min intervals)")
    args = parser.parse_args()

    if args.results:
        cmd_results()
    elif args.monitor:
        cmd_monitor()
    elif args.whales:
        cmd_no_api_scan()
    else:
        cmd_scan()


if __name__ == "__main__":
    main()
