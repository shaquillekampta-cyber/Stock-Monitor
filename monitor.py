"""
Portfolio News Monitor
-----------------------
Pulls recent news (NewsAPI) and price/volume data (yfinance) for a
watchlist of tickers, scores each against a transparent, rules-based
checklist, and writes the results to data/results.json for the
dashboard to display.

This tool NEVER places trades. It only produces suggestions for a
human to review. Nothing here should be treated as financial advice.
"""

import json
import os
import re
from datetime import datetime, timedelta, timezone

import requests
import yfinance as yf

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

TICKERS = os.environ.get("TICKERS", "SPY,QQQ,VXUS,AMZN,GOOG").split(",")
NEWSAPI_KEY = os.environ.get("NEWSAPI_KEY", "")
NEWSAPI_URL = "https://newsapi.org/v2/everything"
OUTPUT_PATH = os.path.join(os.path.dirname(__file__), "data", "results.json")

# Simple, transparent keyword lists. Tune these freely -- the point of a
# rules-based system is that every decision is inspectable and editable.
POSITIVE_WORDS = [
    "beat", "beats", "surge", "surges", "rally", "rallies", "upgrade",
    "upgraded", "outperform", "record", "strong", "growth", "bullish",
    "raises guidance", "raised guidance", "buy rating", "inflow", "inflows",
    "expansion", "profit rise", "exceeds", "exceeded", "tops estimates",
]
NEGATIVE_WORDS = [
    "miss", "misses", "plunge", "plunges", "downgrade", "downgraded",
    "underperform", "weak", "decline", "bearish", "cuts guidance",
    "cut guidance", "sell rating", "outflow", "outflows", "lawsuit",
    "investigation", "recall", "layoffs", "warns", "warning", "fraud",
    "delisted", "bankruptcy", "probe",
]

NEWS_LOOKBACK_DAYS = 2


# ---------------------------------------------------------------------------
# Data fetching
# ---------------------------------------------------------------------------

def fetch_news(ticker: str) -> list[dict]:
    """Fetch recent headlines for a ticker via NewsAPI. Returns [] on any
    failure so a missing/invalid key never crashes the whole run."""
    if not NEWSAPI_KEY:
        return []

    since = (datetime.now(timezone.utc) - timedelta(days=NEWS_LOOKBACK_DAYS)).strftime("%Y-%m-%d")
    params = {
        "q": ticker,
        "from": since,
        "sortBy": "publishedAt",
        "language": "en",
        "pageSize": 20,
        "apiKey": NEWSAPI_KEY,
    }
    try:
        resp = requests.get(NEWSAPI_URL, params=params, timeout=15)
        resp.raise_for_status()
        articles = resp.json().get("articles", [])
    except requests.RequestException as exc:
        print(f"[warn] news fetch failed for {ticker}: {exc}")
        return []

    return [
        {
            "title": a.get("title") or "",
            "description": a.get("description") or "",
            "source": (a.get("source") or {}).get("name", "unknown"),
            "url": a.get("url"),
            "publishedAt": a.get("publishedAt"),
        }
        for a in articles
    ]


def fetch_price_data(ticker: str) -> dict:
    """Fetch current price, change, and volume vs average via yfinance.
    yfinance needs no API key."""
    try:
        t = yf.Ticker(ticker)
        hist = t.history(period="1mo")
        if hist.empty:
            return {}

        last = hist.iloc[-1]
        prev = hist.iloc[-2] if len(hist) > 1 else last
        avg_volume = hist["Volume"].mean()
        ma_20 = hist["Close"].tail(20).mean()

        price = float(last["Close"])
        change_pct = ((price - float(prev["Close"])) / float(prev["Close"])) * 100 if prev["Close"] else 0.0

        return {
            "price": round(price, 2),
            "change_pct": round(change_pct, 2),
            "volume": int(last["Volume"]),
            "avg_volume": int(avg_volume),
            "volume_ratio": round(float(last["Volume"]) / avg_volume, 2) if avg_volume else None,
            "above_20d_ma": bool(price > ma_20),
            "ma_20": round(float(ma_20), 2),
        }
    except Exception as exc:  # yfinance can raise a variety of errors
        print(f"[warn] price fetch failed for {ticker}: {exc}")
        return {}


# ---------------------------------------------------------------------------
# Scoring -- rules-based, transparent, editable
# ---------------------------------------------------------------------------

def score_news_sentiment(articles: list[dict]) -> dict:
    """Counts positive/negative keyword hits across headlines+descriptions.
    Returns a simple tally, not a black-box score."""
    pos_hits, neg_hits = 0, 0
    matched_positive, matched_negative = [], []

    for a in articles:
        text = f"{a['title']} {a['description']}".lower()
        for word in POSITIVE_WORDS:
            if word in text:
                pos_hits += 1
                matched_positive.append((word, a["title"]))
        for word in NEGATIVE_WORDS:
            if word in text:
                neg_hits += 1
                matched_negative.append((word, a["title"]))

    if pos_hits > neg_hits:
        direction = "positive"
    elif neg_hits > pos_hits:
        direction = "negative"
    else:
        direction = "neutral"

    return {
        "direction": direction,
        "positive_hits": pos_hits,
        "negative_hits": neg_hits,
        "article_count": len(articles),
        "sample_positive": matched_positive[:3],
        "sample_negative": matched_negative[:3],
    }


def build_checklist(ticker: str, articles: list[dict], price: dict) -> list[dict]:
    """Builds the visible, line-item checklist used to justify the call.
    Each item is (label, detail, signal) where signal in {pos, neg, neutral}.
    """
    checklist = []
    sentiment = score_news_sentiment(articles)

    # 1. News catalyst presence
    if sentiment["article_count"] == 0:
        checklist.append((
            "News catalyst",
            "No recent articles found (check API key/quota, or ticker is quiet).",
            "neutral",
        ))
    else:
        checklist.append((
            "News catalyst",
            f"{sentiment['article_count']} articles in the last {NEWS_LOOKBACK_DAYS} days.",
            "neutral",
        ))

    # 2. Sentiment direction + magnitude
    if sentiment["direction"] == "positive":
        checklist.append((
            "Sentiment",
            f"{sentiment['positive_hits']} positive keyword hits vs {sentiment['negative_hits']} negative.",
            "pos",
        ))
    elif sentiment["direction"] == "negative":
        checklist.append((
            "Sentiment",
            f"{sentiment['negative_hits']} negative keyword hits vs {sentiment['positive_hits']} positive.",
            "neg",
        ))
    else:
        checklist.append((
            "Sentiment",
            "No clear lean -- positive and negative keyword hits are balanced or absent.",
            "neutral",
        ))

    # 3. Price/trend context
    if price.get("above_20d_ma") is True:
        checklist.append((
            "Trend context",
            f"Price ${price['price']} is above its 20-day average (${price['ma_20']}).",
            "pos",
        ))
    elif price.get("above_20d_ma") is False:
        checklist.append((
            "Trend context",
            f"Price ${price['price']} is below its 20-day average (${price['ma_20']}).",
            "neg",
        ))
    else:
        checklist.append(("Trend context", "Price data unavailable.", "neutral"))

    # 4. Volume anomaly
    ratio = price.get("volume_ratio")
    if ratio is not None:
        if ratio >= 1.5:
            checklist.append((
                "Volume",
                f"Volume is {ratio}x the recent average -- unusually high, news is likely being acted on.",
                "pos" if sentiment["direction"] != "negative" else "neg",
            ))
        elif ratio <= 0.6:
            checklist.append((
                "Volume",
                f"Volume is {ratio}x the recent average -- unusually light conviction.",
                "neutral",
            ))
        else:
            checklist.append((
                "Volume",
                f"Volume is {ratio}x the recent average -- roughly normal.",
                "neutral",
            ))
    else:
        checklist.append(("Volume", "Volume data unavailable.", "neutral"))

    # 5. Conflicting signal flag
    price_bullish = price.get("above_20d_ma") is True
    price_bearish = price.get("above_20d_ma") is False
    if (sentiment["direction"] == "positive" and price_bearish) or (
        sentiment["direction"] == "negative" and price_bullish
    ):
        checklist.append((
            "Conflicting signals",
            "News sentiment and price trend disagree -- treat with extra caution.",
            "neg",
        ))

    return [{"label": c[0], "detail": c[1], "signal": c[2]} for c in checklist]


def compute_call(checklist: list[dict]) -> dict:
    """Turns the checklist into a buy/hold/sell suggestion. Simple majority
    of pos/neg line items, conflicting-signal flag suppresses a firm buy/sell."""
    pos = sum(1 for c in checklist if c["signal"] == "pos")
    neg = sum(1 for c in checklist if c["signal"] == "neg")
    has_conflict = any(c["label"] == "Conflicting signals" for c in checklist)

    if has_conflict:
        call = "hold"
    elif pos >= neg + 2:
        call = "buy"
    elif neg >= pos + 2:
        call = "sell"
    else:
        call = "hold"

    return {"call": call, "positive_signals": pos, "negative_signals": neg, "total_signals": len(checklist)}


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    results = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "tickers": [],
    }

    for raw_ticker in TICKERS:
        ticker = raw_ticker.strip().upper()
        if not ticker:
            continue

        print(f"Processing {ticker}...")
        articles = fetch_news(ticker)
        price = fetch_price_data(ticker)
        checklist = build_checklist(ticker, articles, price)
        call = compute_call(checklist)

        results["tickers"].append({
            "ticker": ticker,
            "price": price,
            "call": call,
            "checklist": checklist,
            "headlines": [
                {"title": a["title"], "source": a["source"], "url": a["url"]}
                for a in articles[:5]
            ],
        })

    os.makedirs(os.path.dirname(OUTPUT_PATH), exist_ok=True)
    with open(OUTPUT_PATH, "w") as f:
        json.dump(results, f, indent=2)

    print(f"Wrote {OUTPUT_PATH}")


if __name__ == "__main__":
    main()
