"""
Portfolio News Monitor
-----------------------
Pulls recent news (NewsAPI) and price/volume/candlestick data (yfinance)
for a watchlist of tickers, scores each against a transparent,
rules-based checklist geared toward SWING TRADES (roughly a 2-6 week
holding window, not long-term buy-and-hold), and writes the results to
data/results.json for the dashboard to display.

Signal smoothing: each run's raw call is combined with recent history
so a single noisy day can't flip the displayed call back and forth.
A call only changes once it's been confirmed across multiple runs.

This tool NEVER places trades. It only produces suggestions for a
human to review. Nothing here should be treated as financial advice.
"""

import json
import os
from datetime import datetime, timedelta, timezone

import requests
import yfinance as yf

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

HERE = os.path.dirname(__file__)
TICKERS_FILE = os.path.join(HERE, "tickers.json")
OUTPUT_PATH = os.path.join(HERE, "data", "results.json")

NEWSAPI_KEY = os.environ.get("NEWSAPI_KEY", "")
NEWSAPI_URL = "https://newsapi.org/v2/everything"

# Swing-trade window: this tool is built for ~2-6 week holds, not
# long-term investing. The trend average below is intentionally shorter
# than a classic 20/50-day investing window.
SWING_WINDOW_LABEL = "2-6 weeks"
TREND_MA_DAYS = 15          # ~3 trading weeks, mid-point of the swing window
BREAKOUT_1D_PCT = 4.0       # a single-day move at/above this % is flagged as a breakout candidate
BREAKOUT_VOLUME_RATIO = 1.5 # breakout must be confirmed by above-average volume

# How many of the last N runs must agree before the displayed call changes.
# This is the anti-flip-flop filter.
SMOOTHING_WINDOW = 3
SMOOTHING_REQUIRED_AGREEMENT = 2  # e.g. 2 of the last 3 runs must agree

# Simple, transparent keyword lists. Tune these freely -- the point of a
# rules-based system is that every decision is inspectable and editable.
POSITIVE_WORDS = [
    "beat", "beats", "surge", "surges", "rally", "rallies", "upgrade",
    "upgraded", "outperform", "record", "strong", "growth", "bullish",
    "raises guidance", "raised guidance", "buy rating", "inflow", "inflows",
    "expansion", "profit rise", "exceeds", "exceeded", "tops estimates",
    "breakout", "momentum",
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
# Ticker list -- editable via tickers.json, no code or workflow changes needed
# ---------------------------------------------------------------------------

DEFAULT_TICKERS = ["SPY", "QQQ", "VXUS", "AMZN", "GOOG"]


def load_tickers() -> list[str]:
    """Reads tickers.json if present, else falls back to the TICKERS env
    var, else the built-in default. tickers.json is the recommended way
    to add/remove tickers -- just edit the file in the repo."""
    if os.path.exists(TICKERS_FILE):
        try:
            with open(TICKERS_FILE) as f:
                data = json.load(f)
            tickers = [t.strip().upper() for t in data.get("tickers", []) if t.strip()]
            if tickers:
                return tickers
        except (json.JSONDecodeError, OSError) as exc:
            print(f"[warn] could not read tickers.json ({exc}), falling back")

    env_tickers = os.environ.get("TICKERS", "")
    if env_tickers.strip():
        return [t.strip().upper() for t in env_tickers.split(",") if t.strip()]

    return DEFAULT_TICKERS


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
    """Fetch price, change, volume, trend, and raw OHLC candles via
    yfinance. yfinance needs no API key. Includes enough history for
    both the swing-trade trend average and candlestick pattern checks."""
    try:
        t = yf.Ticker(ticker)
        hist = t.history(period="2mo")
        if hist.empty or len(hist) < 2:
            return {}

        last = hist.iloc[-1]
        prev = hist.iloc[-2]
        avg_volume = hist["Volume"].mean()
        trend_ma = hist["Close"].tail(TREND_MA_DAYS).mean()

        price = float(last["Close"])
        change_pct = ((price - float(prev["Close"])) / float(prev["Close"])) * 100 if prev["Close"] else 0.0
        volume_ratio = round(float(last["Volume"]) / avg_volume, 2) if avg_volume else None

        return {
            "price": round(price, 2),
            "change_pct": round(change_pct, 2),
            "volume": int(last["Volume"]),
            "avg_volume": int(avg_volume),
            "volume_ratio": volume_ratio,
            "above_trend_ma": bool(price > trend_ma),
            "trend_ma": round(float(trend_ma), 2),
            "trend_ma_days": TREND_MA_DAYS,
            "candles": {
                "open": round(float(last["Open"]), 2),
                "high": round(float(last["High"]), 2),
                "low": round(float(last["Low"]), 2),
                "close": round(float(last["Close"]), 2),
                "prev_open": round(float(prev["Open"]), 2),
                "prev_close": round(float(prev["Close"]), 2),
            },
        }
    except Exception as exc:  # yfinance can raise a variety of errors
        print(f"[warn] price fetch failed for {ticker}: {exc}")
        return {}


# ---------------------------------------------------------------------------
# Candlestick / breakout detection
# ---------------------------------------------------------------------------

def detect_candlestick_signal(price: dict):
    """Looks at the most recent candle (plus the one before it) for simple,
    well-known patterns, and separately flags large one-day jumps
    confirmed by volume -- the 'highest one-day jump' breakout case.
    Returns None if there's not enough data."""
    candles = price.get("candles")
    if not candles:
        return None

    o, h, l, c = candles["open"], candles["high"], candles["low"], candles["close"]
    prev_o, prev_c = candles["prev_open"], candles["prev_close"]
    body = abs(c - o)
    full_range = max(h - l, 0.0001)  # avoid divide-by-zero on halted/no-range days

    # 1. Breakout candidate: big one-day % jump + volume confirmation.
    # This is the "highest one-day stock jumps" signal specifically requested.
    change_pct = price.get("change_pct", 0.0)
    volume_ratio = price.get("volume_ratio") or 0.0
    if change_pct >= BREAKOUT_1D_PCT and volume_ratio >= BREAKOUT_VOLUME_RATIO:
        return {
            "pattern": "breakout",
            "detail": (
                f"Up {change_pct}% in one day on {volume_ratio}x average volume -- "
                f"a volume-confirmed breakout, the kind of single-day jump worth "
                f"watching for a swing entry."
            ),
            "signal": "pos",
        }

    # 2. Bullish engulfing: today's green body fully engulfs yesterday's
    # red body -- a classic reversal-to-the-upside pattern.
    if c > o and prev_c < prev_o and c >= prev_o and o <= prev_c:
        return {
            "pattern": "bullish engulfing",
            "detail": "Today's candle fully engulfs yesterday's decline -- a common bullish reversal signal.",
            "signal": "pos",
        }

    # 3. Bearish engulfing: the mirror image.
    if c < o and prev_c > prev_o and o >= prev_c and c <= prev_o:
        return {
            "pattern": "bearish engulfing",
            "detail": "Today's decline fully engulfs yesterday's gain -- a common bearish reversal signal.",
            "signal": "neg",
        }

    # 4. Hammer: small body near the top of the day's range with a long
    # lower wick -- often marks a bottom after a decline.
    lower_wick = min(o, c) - l
    upper_wick = h - max(o, c)
    if lower_wick >= 2 * body and upper_wick <= body * 0.5 and body / full_range < 0.35:
        return {
            "pattern": "hammer",
            "detail": "Long lower wick with a small body near the day's high -- often signals a bottom forming.",
            "signal": "pos",
        }

    return None


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

    # 3. Price/trend context (swing-trade window, not long-term)
    if price.get("above_trend_ma") is True:
        checklist.append((
            f"Trend ({price.get('trend_ma_days', TREND_MA_DAYS)}-day, swing window)",
            f"Price ${price['price']} is above its {price['trend_ma_days']}-day average (${price['trend_ma']}).",
            "pos",
        ))
    elif price.get("above_trend_ma") is False:
        checklist.append((
            f"Trend ({price.get('trend_ma_days', TREND_MA_DAYS)}-day, swing window)",
            f"Price ${price['price']} is below its {price['trend_ma_days']}-day average (${price['trend_ma']}).",
            "neg",
        ))
    else:
        checklist.append(("Trend", "Price data unavailable.", "neutral"))

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

    # 5. Candlestick / breakout pattern
    candle_signal = detect_candlestick_signal(price)
    if candle_signal:
        checklist.append((
            f"Candlestick: {candle_signal['pattern']}",
            candle_signal["detail"],
            candle_signal["signal"],
        ))
    else:
        checklist.append((
            "Candlestick",
            "No notable single/two-day candlestick pattern or volume-confirmed breakout today.",
            "neutral",
        ))

    # 6. Conflicting signal flag
    price_bullish = price.get("above_trend_ma") is True
    price_bearish = price.get("above_trend_ma") is False
    if (sentiment["direction"] == "positive" and price_bearish) or (
        sentiment["direction"] == "negative" and price_bullish
    ):
        checklist.append((
            "Conflicting signals",
            "News sentiment and price trend disagree -- treat with extra caution.",
            "neg",
        ))

    return [{"label": c[0], "detail": c[1], "signal": c[2]} for c in checklist]


def compute_raw_call(checklist: list[dict]) -> dict:
    """Turns the checklist into a raw buy/hold/sell suggestion for THIS run
    only. This gets smoothed against history before being shown -- see
    apply_signal_smoothing()."""
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
# Signal smoothing -- the anti-flip-flop filter
# ---------------------------------------------------------------------------

def load_previous_results() -> dict:
    """Loads the last results.json (if any) so we can read each ticker's
    recent call history. Returns {} if there's no prior file."""
    if not os.path.exists(OUTPUT_PATH):
        return {}
    try:
        with open(OUTPUT_PATH) as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}

    return {t["ticker"]: t for t in data.get("tickers", []) if "ticker" in t}


def apply_signal_smoothing(ticker: str, raw_call: str, previous: dict) -> dict:
    """Prevents the displayed call from flipping on a single noisy run.
    Keeps a rolling history of raw calls per ticker; the DISPLAYED call
    only changes once SMOOTHING_REQUIRED_AGREEMENT of the last
    SMOOTHING_WINDOW raw calls agree on something different from the
    current displayed call.
    """
    prev_entry = previous.get(ticker, {})
    history = prev_entry.get("call_history", [])
    displayed_call = prev_entry.get("displayed_call", raw_call)

    history = (history + [raw_call])[-SMOOTHING_WINDOW:]

    # Count how many of the recent raw calls agree on each candidate.
    candidate_calls = {c for c in history if c != displayed_call}
    changed = False
    for candidate in candidate_calls:
        if history.count(candidate) >= SMOOTHING_REQUIRED_AGREEMENT:
            displayed_call = candidate
            changed = True
            break

    return {
        "displayed_call": displayed_call,
        "call_history": history,
        "changed_this_run": changed,
    }


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    tickers = load_tickers()
    previous = load_previous_results()

    results = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "swing_window": SWING_WINDOW_LABEL,
        "tickers": [],
    }

    for ticker in tickers:
        print(f"Processing {ticker}...")
        articles = fetch_news(ticker)
        price = fetch_price_data(ticker)
        checklist = build_checklist(ticker, articles, price)
        raw = compute_raw_call(checklist)
        smoothing = apply_signal_smoothing(ticker, raw["call"], previous)

        results["tickers"].append({
            "ticker": ticker,
            "price": price,
            "call": {**raw, "displayed_call": smoothing["displayed_call"]},
            "call_history": smoothing["call_history"],
            "signal_changed_this_run": smoothing["changed_this_run"],
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
'Add candlestick detection and smoothing'
