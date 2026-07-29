# Portfolio monitor

A transparent, rules-based news + price monitor for a stock watchlist.
It suggests buy / hold / sell per ticker based on a visible checklist —
it never places trades and is not financial advice.

## What it does

1. `monitor.py` pulls recent headlines (NewsAPI) and price/volume data
   (`yfinance`, no key required) for each ticker in your watchlist.
2. It scores each ticker against a checklist: news presence, keyword
   sentiment, price vs 20-day average, volume vs average, and a
   conflicting-signals flag.
3. Results are written to `data/results.json`.
4. A GitHub Actions workflow runs this on a schedule and commits the
   updated file.
5. `index.html`, hosted via GitHub Pages, reads `data/results.json` and
   renders the dashboard.

## One-time setup

### 1. Get a free NewsAPI key
Sign up at [newsapi.org](https://newsapi.org/register) (free tier: 100
requests/day, articles from the last month). Copy your API key.

> Note: the NewsAPI free tier only covers *developer* use and has a
> short article-history window. It's fine for a personal monitor like
> this; if you outgrow it, swap in Finnhub or Alpha Vantage news
> endpoints in `fetch_news()`.

### 2. Push this repo to GitHub
```
git init
git add .
git commit -m "Initial commit"
git remote add origin https://github.com/<your-username>/<repo-name>.git
git push -u origin main
```

### 3. Add your API key as a repo secret
In your GitHub repo: **Settings → Secrets and variables → Actions → New
repository secret**
- Name: `NEWSAPI_KEY`
- Value: (your key from step 1)

### 4. (Optional) Set your ticker list
By default the watchlist is `SPY,QQQ,VXUS,AMZN,GOOG`. To change it
without editing code:
**Settings → Secrets and variables → Actions → Variables tab → New
repository variable**
- Name: `TICKERS`
- Value: e.g. `SPY,QQQ,AAPL,MSFT,NVDA`

### 5. Enable GitHub Pages
**Settings → Pages → Source: Deploy from a branch → Branch: `main` /
`root`**. Your dashboard will be live at
`https://<your-username>.github.io/<repo-name>/`.

### 6. Run it
- It runs automatically on the schedule in
  `.github/workflows/monitor.yml` (default: weekday mornings before
  market open).
- To run it immediately: go to the **Actions** tab → **Portfolio
  monitor** → **Run workflow**.
- To run it locally: `pip install -r requirements.txt`, set
  `NEWSAPI_KEY` as an environment variable, then `python monitor.py`.

## Customizing the logic

Everything that drives a call is in `monitor.py`:
- `POSITIVE_WORDS` / `NEGATIVE_WORDS` — the keyword lists behind the
  sentiment score. Add, remove, or weight terms as you like.
- `build_checklist()` — the checklist itself. Add new checks here
  (e.g. an earnings-calendar proximity flag) the same way the
  existing ones are structured.
- `compute_call()` — the buy/hold/sell threshold logic.

Because it's all rules-based, every suggestion the dashboard shows can
be traced back to a specific, readable rule — nothing is a black box.

## Limitations, honestly

- Free NewsAPI has a small daily quota and limited historical depth.
- `yfinance` pulls from Yahoo Finance's public endpoints; these can
  occasionally be rate-limited or change format.
- This is a decision-support tool, not a trading bot. It does not
  execute trades, and nothing here should be treated as personalized
  financial advice.
