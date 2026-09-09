# VPM — Valorant Prediction Model & Automated Trading Agent

An end-to-end system that predicts Valorant esports (VCT) match outcomes and
trades those predictions against real-money markets on Polymarket. It covers
the full pipeline: scraping match/player data, engineering features and a
player-Elo rating system, training a calibrated win-probability model, and
running a live agent that finds mispriced markets and manages positions
automatically.

## What it does

1. **Scrapes** VCT match results, per-player stats, and round-by-round
   economy data from [vlr.gg](https://www.vlr.gg) (3,000+ maps, 600+ players
   across VCT 2023–2026).
2. **Builds features**, including a custom player-level Elo rating system
   (role-normalized composite of ACS/KAST/FK-FD, margin- and
   opponent-strength-scaled updates) plus trailing form stats (map win rate,
   first-kill/first-death diff) over 5/10-map windows.
3. **Trains** a `HistGradientBoostingClassifier` on a chronological
   train/test split to output a calibrated win probability for a given
   matchup and map.
4. **Watches Polymarket** for live Valorant markets, compares the model's
   probability to market price, and when the edge clears a threshold, sizes
   and places an order (quarter-Kelly, confidence-capped).
5. **Manages the position** with a trailing-floor exit ladder tuned against
   backtested price paths, rescraping and re-entering automatically between
   maps for Bo3/Bo5 series.
6. **Serves predictions** via a Streamlit app for manual lookups and a
   FastAPI + Next.js dashboard for live agent monitoring.

## Architecture

```
Scraper/  ──scrapes──▶  Retraining/  ──trains──▶  Predict/
(vlr.gg data)          (Elo + features)         (win probability model)
                                                        │
                                                        ▼
                              Agent/  ◀──prices/orders──▶  PolyInt/
                        (match polling, edge detection,      (Polymarket
                         entry/exit orchestration)            price feed +
                                                               order execution)
                                                        │
                                                        ▼
                                          Dashboard/ (FastAPI + Next.js)
                                          Web App/ (Streamlit manual lookup)
```

| Component | Purpose |
|---|---|
| [`Scraper/`](Scraper/) | VLR.gg scraper — match, player, and round-by-round economy data into a 3-sheet Excel dataset. Rate-limited, resumable, CLI-driven. |
| [`Retraining/`](Retraining/) | Feature engineering and player-Elo system; model training with time-series CV, calibration, and feature-importance analysis. |
| [`Predict/`](Predict/) | Inference layer — loads the trained model and returns a win probability + feature breakdown for any matchup. |
| [`PolyInt/`](PolyInt/) | Polymarket integration — fetches live market prices (Gamma API) and executes orders (CLOB API via `py-clob-client`). |
| [`Agent/`](Agent/) | The trading agent — polls vlr.gg for match state, detects entry edges, sizes and places trades, manages exits with a trailing-floor ladder, handles Bo3/Bo5 series and multi-match concurrency, persists state, and posts Discord alerts. |
| [`Dashboard/`](Dashboard/) | FastAPI backend + Next.js frontend for live monitoring of agent activity, trade history, and performance. |
| [`Web App/`](Web%20App/) | Streamlit app for ad-hoc, manual matchup predictions. |

## Model

- **Type:** `HistGradientBoostingClassifier` (scikit-learn), trained on a
  chronological 80/20 split (train: Mar 2023–Aug 2025, test: Aug 2025–Mar
  2026) with `TimeSeriesSplit` hyperparameter search.
- **Features:** map win rate (trailing 10), first-kill/first-death diff
  (trailing 5), and two player-Elo signals (rating sum diff, rating trend
  diff) between the two teams — kept intentionally small to avoid overfitting
  on a noisy, low-signal domain.
- **Held-out performance:** 59.8% accuracy, 0.596 ROC-AUC, 0.680 log loss on
  493 unseen matches — modest edges are the norm in esports match prediction,
  and the trading strategy is built around exploiting *calibration* gaps
  rather than raw accuracy.

## Trading agent

The agent runs 24/7 against VCT Americas/EMEA/Pacific, and for each detected
match:

- Waits for the map veto, computes model probability vs. Polymarket's live
  ask price, and enters when edge exceeds a 5% threshold (model confidence
  capped at 70% either direction).
- Sizes positions with quarter-Kelly.
- Exits via a trailing-floor ladder retuned against backtested historical
  price paths (activates once a position is up ~38%, then trails in ~25%
  steps) rather than a fixed stop-loss/take-profit.
- Handles both Bo3 and Bo5 series, including moneyline vs. per-map market
  selection on the series-deciding map, cooldowns between maps, and
  re-entry after a rescrape.
- Supports paper-trading and live modes, persists state for crash recovery,
  and sends Discord notifications on fills/exits.

## Tech stack

Python (pandas, scikit-learn, BeautifulSoup, `py-clob-client`), FastAPI,
Next.js/TypeScript, Streamlit, SQLite, Discord webhooks.

## Status

Actively developed. The model and agent logic are backtested and paper/live
trade-tested on real VCT matches; strategy parameters (edge threshold, sizing,
exit ladder) are periodically retuned as more live trade data comes in.

---

*This is a personal research/trading project. Nothing here is financial
advice, and prediction-market trading carries real financial risk.*
