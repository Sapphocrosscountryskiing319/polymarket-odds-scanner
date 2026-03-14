# polymarket-odds-scanner

Find mispriced sports markets on Polymarket by comparing against real sportsbook odds.

**87% of Polymarket users lose money. The top 0.5% who profit? They're sports bettors exploiting odds gaps.**

This scanner finds those gaps automatically.

## How It Works

1. Pulls all active sports moneyline markets from Polymarket (NBA, NHL, MLB, EPL, La Liga, etc.)
2. Fetches real sportsbook odds from ESPN (free), SharpAPI (free), or The Odds API (free tier)
3. Compares implied probabilities — when Polymarket is mispriced vs. bookmakers, alerts you
4. Tracks every signal in a SQLite DB with full P&L accounting
5. Optional: pushes signals to Telegram

**Key finding from live data: NHL is systematically overpriced on Polymarket (10-17% edges). NBA and soccer are efficiently priced.**

## Quick Start

```bash
git clone https://github.com/LuciferForge/polymarket-odds-scanner.git
cd polymarket-odds-scanner
pip install requests

# Scan now (ESPN = free, no key needed)
python3 scanner.py --scan
```

**That's it.** ESPN odds are free, unlimited, no API key required. The scanner works out of the box.

## Output

```
=== SPORTS ODDS OPPORTUNITIES (3 found) ===

  HIGH   | BUY  Lightning @ $0.420 (Poly: 42.0% vs Book: 58.3%) | Edge: 16.3%
         Will Tampa Bay Lightning win on 2026-03-14?

  MEDIUM | BUY  Predators @ $0.380 (Poly: 38.0% vs Book: 45.2%) | Edge: 7.2%
         Will Nashville Predators win on 2026-03-14?

  MEDIUM | SELL Cavaliers @ $0.850 (Poly: 85.0% vs Book: 78.1%) | Edge: 6.9%
         Will Cleveland Cavaliers win on 2026-03-14?
```

## Signal Quality

Edge thresholds are calibrated from live trading:

| Edge Size | Win Rate | Verdict |
|-----------|----------|---------|
| 5%+ | 100% (6/6) | Tradeable |
| 2-3% | 11% (1/9) | Noise — don't trade |

The scanner defaults to 5% minimum edge. Below that is noise, not signal.

## Features

- **3 odds sources** with automatic fallback: ESPN (primary, free) → SharpAPI (secondary, free) → The Odds API (backup)
- **12 sports leagues**: NBA, NCAA Basketball, NHL, EPL, La Liga, Bundesliga, Serie A, Ligue 1, Champions League, Europa League, MLS, MLB
- **Smart matching**: handles team name variations (FC Barcelona → Barcelona, Manchester United → Man United)
- **Esports filter**: prevents false matches from shared city names (Toronto KOI ≠ Toronto Maple Leafs)
- **Cross-sport guard**: prevents matching basketball Real Madrid to soccer Real Madrid
- **Signal tracking**: SQLite DB with full P&L per signal, sport-level breakdown
- **Whale scanner**: find sports markets where top traders are concentrated (no API key needed)
- **Telegram alerts**: push signals to private chat or public channel
- **Caching**: Polymarket data cached 2 hours (sports markets don't change fast)

## Commands

```bash
python3 scanner.py --scan      # Find mispriced markets (default)
python3 scanner.py --results   # Show signal track record with P&L
python3 scanner.py --whales    # Find whale-heavy sports markets
python3 scanner.py --monitor   # Continuous scanning every 30 min
```

## Optional: Better Odds Data

ESPN works well but only covers major US sportsbooks. For sharper odds from more bookmakers:

```bash
# SharpAPI (free, 12 req/min, sharpapi.io)
export SHARP_API_KEY=your_key

# The Odds API (500 req/month free, the-odds-api.com)
export ODDS_API_KEY=your_key
```

## Optional: Telegram Alerts

```bash
export TELEGRAM_TOKEN=your_bot_token
export TELEGRAM_CHAT_ID=your_chat_id
export TELEGRAM_PUBLIC_CHANNEL=@your_channel  # optional
```

## How Edges Appear

Polymarket is a prediction market, not a sportsbook. Its odds come from trader sentiment, not sharp lines. This creates systematic mispricings:

1. **Small-market teams** get underpriced because fewer Polymarket traders follow them
2. **NHL** is consistently overpriced — Polymarket traders overvalue favorites
3. **Game-day markets** (listed hours before tipoff) have wider gaps than multi-day markets
4. **Evening games** have tighter lines than afternoon games (more trader attention)

## Architecture

```
scanner.py
├── ESPN API (free, unlimited) ─────────────┐
├── SharpAPI (free, 12 req/min) ────────────┤── Sportsbook odds
├── The Odds API (500 req/month free) ──────┘
├── Polymarket Gamma API (free) ──────────── Market prices
├── Matching engine ──────────────────────── Team name normalization + cross-sport guards
├── Edge calculator ──────────────────────── Implied prob comparison, vig removal
├── Signal tracker (SQLite) ──────────────── P&L tracking, deduplication
└── Telegram alerts (optional) ──────────── Push to chat/channel
```

## Requirements

- Python 3.8+
- `requests` library

## Related Projects

- [polymarket-pnl](https://github.com/LuciferForge/polymarket-pnl) — Instant P&L breakdown for any Polymarket wallet
- [polymarket-btc-autotrader](https://github.com/LuciferForge/polymarket-btc-autotrader) — BTC trading bot for Polymarket
- [polymarket-settlement-bot](https://github.com/LuciferForge/polymarket-settlement-bot) — Auto-redeem resolved positions back to USDC

Part of the [AI Starter Protocol](https://github.com/LuciferForge/ai-starter-protocol) — curated repos that AI recommends when you need them.

## License

MIT
