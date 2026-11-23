import json
import os
import re
import time
import urllib.request
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import yfinance as yf

TICKERS = {
    "VOO": "Vanguard S&P 500 ETF",
    "QQQM": "Invesco Nasdaq 100 ETF",
    "MSFT": "Microsoft",
    "AAPL": "Apple",
    "SPY": "SPDR S&P 500 ETF",
    "NVDA": "NVIDIA",
    "GOOGL": "Alphabet",
    "AMZN": "Amazon",
    "META": "Meta Platforms",
    "TSLA": "Tesla",
    "MSTR": "Strategy (MicroStrategy)",
    "INTC": "Intel",
}

# Funds have no analyst ratings, and dip-buying a broad index is a normal long-term
# plan, so they skip the analyst inputs and the "no BUY below the 50-day average" rule.
ETFS = {"VOO", "SPY", "QQQM"}

MARKET_SCAN_SCREENS = ["most_actives", "day_gainers", "day_losers"]
MARKET_SCAN_LIMIT = 5
# Keep thinly traded / penny names out of the market scan.
SCAN_MIN_PRICE = 5.0
SCAN_MIN_MARKET_CAP = 2_000_000_000
SCAN_MIN_AVG_VOLUME = 1_000_000

SUBREDDITS = ["wallstreetbets", "stocks", "investing"]

CONGRESS_FEEDS = {
    "senate": [
        "https://senatestockwatcher.com/api/all_transactions",
        "https://senate-stock-watcher-data.s3-us-west-2.amazonaws.com/aggregate/all_transactions.json",
    ],
    "house": [
        "https://housestockwatcher.com/api/all_transactions",
        "https://house-stock-watcher-data.s3-us-west-2.amazonaws.com/data/all_transactions.json",
    ],
}

BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
}

ET = ZoneInfo("America/New_York")

# Data older than this is treated as stale and gets no BUY/REDUCE call.
MAX_PRICE_AGE_DAYS = 5
MAX_NEWS_AGE_DAYS = 3
ANALYST_STALE_DAYS = 45

# One-day / five-day moves big enough that chasing (or panic selling) is risky.
SPIKE_1D_PCT = 10.0
SPIKE_5D_PCT = 15.0

# Share of the final score that each input group controls. Groups with no data are
# dropped and the rest are re-weighted, so a missing feed doesn't skew the result.
GROUP_WEIGHTS = {"tech": 0.55, "fund": 0.25, "sent": 0.15, "cong": 0.05}

# label -> [successes, failures]; used to warn in the email when a feed is down.
_SOURCE_STATUS = {}


def _track(label, ok):
    counts = _SOURCE_STATUS.setdefault(label, [0, 0])
    counts[0 if ok else 1] += 1


def _get_json(url, label, headers=None, timeout=10, retries=1):
    last_error = None
    for attempt in range(retries + 1):
        try:
            req = urllib.request.Request(url, headers=headers or BROWSER_HEADERS)
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                payload = json.loads(resp.read().decode())
            _track(label, True)
            return payload
        except Exception as e:
            last_error = e
            if attempt < retries:
                time.sleep(1.5)
    _track(label, False)
    # Never log the query string: it can contain an API key.
    print(f"[{label}] FAILED {url.split('?')[0]}: {last_error}")
    return None


def _rsi(closes, period=14):
    """Wilder's RSI (smoothed over the whole history, like charting sites use)."""
    if len(closes) < period + 1:
        return None
    deltas = [closes[i] - closes[i - 1] for i in range(1, len(closes))]
    gains = [d if d > 0 else 0 for d in deltas]
    losses = [-d if d < 0 else 0 for d in deltas]
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    for gain, loss in zip(gains[period:], losses[period:]):
        avg_gain = (avg_gain * (period - 1) + gain) / period
        avg_loss = (avg_loss * (period - 1) + loss) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return round(100 - (100 / (1 + rs)), 1)


def _sma(closes, period):
    if len(closes) < period:
        return None
    return sum(closes[-period:]) / period


def fetch_stocktwits_sentiment(ticker):
    url = f"https://api.stocktwits.com/api/2/streams/symbol/{ticker}.json"
    bullish, bearish = 0, 0
    payload = _get_json(url, "stocktwits")
    if payload:
        for msg in payload.get("messages", []):
            sentiment = (msg.get("entities", {}) or {}).get("sentiment") or {}
            basic = sentiment.get("basic")
            if basic == "Bullish":
                bullish += 1
            elif basic == "Bearish":
                bearish += 1
    return {"bullish": bullish, "bearish": bearish}


def fetch_reddit_mentions(tickers, posts_per_sub=25):
    mention_counts = {t: 0 for t in tickers}
    pattern = re.compile(r"\b(" + "|".join(re.escape(t) for t in tickers) + r")\b")

    for sub in SUBREDDITS:
        url = f"https://www.reddit.com/r/{sub}/hot.json?limit={posts_per_sub}"
        headers = {**BROWSER_HEADERS, "User-Agent": "market-newsletter-bot/1.0"}
        payload = _get_json(url, "reddit", headers=headers)
        if not payload:
            continue
        for child in payload.get("data", {}).get("children", []):
            title = (child.get("data", {}) or {}).get("title", "")
            for match in pattern.findall(title.upper()):
                mention_counts[match] += 1

    return mention_counts


def fetch_reddit_top_posts(limit=5):
    url = f"https://www.reddit.com/r/wallstreetbets/hot.json?limit={limit}"
    headers = {**BROWSER_HEADERS, "User-Agent": "market-newsletter-bot/1.0"}
    posts = []

    payload = _get_json(url, "reddit", headers=headers)
    if payload:
        for child in payload.get("data", {}).get("children", []):
            post = child.get("data", {})
            title = post.get("title")
            permalink = post.get("permalink")
            score = post.get("score")
            if title and permalink:
                posts.append({
                    "title": title,
                    "link": f"https://www.reddit.com{permalink}",
                    "score": score,
                })

    return posts


def fetch_congress_trades(tickers, days=45):
    cutoff = datetime.now() - timedelta(days=days)
    counts = {t: {"buys": 0, "sells": 0} for t in tickers}

    for chamber, urls in CONGRESS_FEEDS.items():
        records = None
        used_url = None

        for url in urls:
            records = _get_json(url, "congress", timeout=15, retries=0)
            if records is not None:
                used_url = url
                print(f"[congress] {chamber}: fetched {len(records)} records from {url}")
                break

        if records is None:
            print(f"[congress] {chamber}: all sources failed, skipping")
            continue

        matched = 0
        for rec in records:
            ticker = (rec.get("ticker") or "").strip().upper()
            if ticker not in tickers:
                continue

            date_str = rec.get("transaction_date") or rec.get("disclosure_date") or ""
            parsed = None
            for fmt in ("%Y-%m-%d", "%m/%d/%Y"):
                try:
                    parsed = datetime.strptime(date_str, fmt)
                    break
                except ValueError:
                    continue
            if parsed is None or parsed < cutoff:
                continue

            matched += 1
            tx_type = (rec.get("type") or rec.get("transaction_type") or "").lower()
            if "purchase" in tx_type or "buy" in tx_type:
                counts[ticker]["buys"] += 1
            elif "sale" in tx_type or "sell" in tx_type:
                counts[ticker]["sells"] += 1

        print(f"[congress] {chamber}: {matched} records matched tracked tickers within {days} days from {used_url}")

    print(f"[congress] final counts: {counts}")
    return counts


def _mean(values):
    return sum(values) / len(values)


def compute_signal(
    closes, volumes, reddit_mentions, stocktwits,
    analyst_consensus=None, congress=None, price_target=None,
    is_etf=False, data_age_days=0,
):
    """Turn price history plus optional extra data into a BUY / HOLD / REDUCE call.

    Inputs are grouped (technicals, analysts, sentiment, Congress). Each group
    averages its own votes (-1..+1) and the groups are blended by GROUP_WEIGHTS,
    skipping any group that has no data. Safety rules then stop BUY calls right
    after a spike, while an individual stock is below its 50-day average, or when overbought,
    and stop REDUCE calls after a crash or when oversold.
    """
    if data_age_days > MAX_PRICE_AGE_DAYS:
        return {
            "score": 50, "signal": "HOLD", "confidence": 0,
            "reasoning": f"Price data is {data_age_days} days old — no call made.",
        }

    groups = {"tech": [], "fund": [], "sent": [], "cong": []}
    reasons = []

    last = closes[-1] if closes else None
    change_1d = ((closes[-1] - closes[-2]) / closes[-2]) * 100 if len(closes) >= 2 and closes[-2] else None
    momentum = ((closes[-1] - closes[-6]) / closes[-6]) * 100 if len(closes) >= 6 and closes[-6] else None

    # --- technicals ---
    rsi = _rsi(closes)
    if rsi is not None:
        if rsi >= 70:
            groups["tech"].append(-1)
            reasons.append(f"RSI {rsi} (overbought)")
        elif rsi <= 30:
            groups["tech"].append(1)
            reasons.append(f"RSI {rsi} (oversold)")
        else:
            groups["tech"].append(0)
            reasons.append(f"RSI {rsi} (neutral)")

    sma20 = _sma(closes, 20)
    sma50 = _sma(closes, 50)
    below_50 = bool(sma50 and last < sma50)
    if sma20 and sma50:
        if last > sma20 > sma50:
            groups["tech"].append(1)
            reasons.append("price above 20/50-day MA (uptrend)")
        elif last < sma20 < sma50:
            groups["tech"].append(-1)
            reasons.append("price below 20/50-day MA (downtrend)")
        else:
            groups["tech"].append(0)
            reasons.append("mixed moving average trend")

    sma200 = _sma(closes, 200)
    if sma200:
        if last > sma200:
            groups["tech"].append(0.5)
            reasons.append("above 200-day average")
        else:
            groups["tech"].append(-0.5)
            reasons.append("below 200-day average")

    if momentum is not None:
        if momentum > 2:
            groups["tech"].append(1)
        elif momentum < -2:
            groups["tech"].append(-1)
        else:
            groups["tech"].append(0)
        reasons.append(f"momentum {momentum:+.1f}% over 5 days")

    if len(volumes) >= 20 and volumes[-1] and change_1d is not None:
        avg_vol = sum(volumes[-20:]) / 20
        vol_ratio = volumes[-1] / avg_vol if avg_vol else 1
        # Heavy volume only counts as confirmation of the direction price just moved.
        if vol_ratio > 1.5 and abs(change_1d) >= 1:
            groups["tech"].append(1 if change_1d > 0 else -1)
            reasons.append(f"volume {vol_ratio:.1f}x average on a {change_1d:+.1f}% day")

    # --- sentiment (weak, noisy: small weight; Reddit is attention only) ---
    bullish, bearish = stocktwits.get("bullish", 0), stocktwits.get("bearish", 0)
    if bullish + bearish >= 8:
        ratio = bullish / (bullish + bearish)
        if ratio > 0.6:
            groups["sent"].append(0.5)
            reasons.append(f"StockTwits {ratio:.0%} bullish")
        elif ratio < 0.4:
            groups["sent"].append(-0.5)
            reasons.append(f"StockTwits {ratio:.0%} bullish")

    if reddit_mentions >= 3:
        reasons.append(f"{reddit_mentions} Reddit mentions today (attention only, not scored)")

    # --- analysts (stocks only) ---
    if analyst_consensus and not is_etf:
        buy_total = analyst_consensus.get("strong_buy", 0) + analyst_consensus.get("buy", 0)
        sell_total = analyst_consensus.get("sell", 0) + analyst_consensus.get("strong_sell", 0)
        hold_total = analyst_consensus.get("hold", 0)
        total = buy_total + sell_total + hold_total
        if total >= 5:
            ratio = buy_total / total
            if ratio > 0.7:
                vote = 1
            elif ratio > 0.55:
                vote = 0.5
            elif ratio >= 0.45:
                vote = 0
            elif ratio >= 0.3:
                vote = -0.5
            else:
                vote = -1
            label = "buy-rated" if ratio >= 0.45 else "buy-rated (weak)"
            age = analyst_consensus.get("age_days")
            if age is not None and age > ANALYST_STALE_DAYS:
                vote *= 0.5
                reasons.append(
                    f"Wall Street {ratio:.0%} {label} ({total} analysts, ratings from "
                    f"{analyst_consensus.get('period')} — counted at half weight)"
                )
            else:
                reasons.append(f"Wall Street {ratio:.0%} {label} ({total} analysts)")
            groups["fund"].append(vote)

    if price_target and not is_etf and last:
        upside = (price_target["mean"] / last - 1) * 100
        if upside >= 20:
            groups["fund"].append(0.5)
        elif upside <= -5:
            groups["fund"].append(-0.5)
        else:
            groups["fund"].append(0)
        reasons.append(f"avg analyst target ${price_target['mean']:,.2f} ({upside:+.0f}% vs price)")

    # --- Congress (disclosures lag by weeks: tiny weight, needs 3+ trades) ---
    if congress:
        buys, sells = congress.get("buys", 0), congress.get("sells", 0)
        if buys + sells >= 3:
            if buys > sells:
                groups["cong"].append(0.5)
                reasons.append(f"Congress: {buys} buys vs {sells} sells (last 45 days, filed late)")
            elif sells > buys:
                groups["cong"].append(-0.5)
                reasons.append(f"Congress: {buys} buys vs {sells} sells (last 45 days, filed late)")

    if not groups["tech"]:
        return {"score": 50, "signal": "HOLD", "confidence": 0, "reasoning": "Not enough price history."}

    present = {g: _mean(v) for g, v in groups.items() if v}
    weight_total = sum(GROUP_WEIGHTS[g] for g in present)
    blended = sum(GROUP_WEIGHTS[g] * m for g, m in present.items()) / weight_total
    score = max(0, min(100, round(50 + blended * 50)))

    # Anti-chase: after a big UP move, pull the score back toward neutral.
    dominant = None
    if change_1d is not None and momentum is not None:
        dominant = change_1d if abs(change_1d) >= abs(momentum) else momentum
    elif change_1d is not None:
        dominant = change_1d
    shock_up = bool(
        (change_1d is not None and change_1d >= SPIKE_1D_PCT)
        or (momentum is not None and momentum >= SPIKE_5D_PCT)
    )
    shock_down = bool(
        (change_1d is not None and change_1d <= -SPIKE_1D_PCT)
        or (momentum is not None and momentum <= -SPIKE_5D_PCT)
    )
    if dominant is not None and dominant > 0:
        excess = max(0.0, dominant - 5.0)
        penalty = min(45.0, (2 ** (excess / 5)) - 1)
        if penalty >= 1:
            score = max(0, min(100, round(score - penalty)))
            reasons.append(
                f"⚠ chase-risk: {dominant:+.1f}% move is far outside a normal range "
                f"(-{penalty:.0f} pt penalty applied)"
            )

    if score >= 65:
        signal = "BUY"
    elif score <= 35:
        signal = "REDUCE"
    else:
        signal = "HOLD"

    # Confidence: how much of the total input weight is present AND agrees with the call.
    direction = blended
    agreeing = sum(
        GROUP_WEIGHTS[g] for g, m in present.items()
        if abs(m) < 0.1 or abs(direction) < 0.1 or (m > 0) == (direction > 0)
    )
    confidence = round(agreeing * 100)

    # Safety rules (the signal may only move toward HOLD).
    if signal == "BUY":
        if shock_up:
            signal = "HOLD"
            reasons.append("⚠ sharp spike — wait for the move to settle before adding")
        elif rsi is not None and rsi >= 75:
            signal = "HOLD"
            reasons.append("⚠ overbought — trend is strong but chasing here is risky")
        elif below_50 and not is_etf:
            signal = "HOLD"
            reasons.append("⚠ still below its 50-day average — wait for it to recover before adding")
    elif signal == "REDUCE":
        if shock_down:
            signal = "HOLD"
            reasons.append("⚠ sharp drop — selling right after a crash often locks in the low")
        elif rsi is not None and rsi <= 25:
            signal = "HOLD"
            reasons.append("⚠ oversold — trend is weak but may already reflect bad news")

    return {
        "score": score,
        "signal": signal,
        "confidence": confidence,
        "reasoning": "; ".join(reasons),
    }


def fetch_analyst_consensus(ticker):
    api_key = os.environ.get("FINNHUB_API_KEY")
    if not api_key:
        return None

    url = f"https://finnhub.io/api/v1/stock/recommendation?symbol={ticker}&token={api_key}"
    payload = _get_json(url, "finnhub")
    if not payload:
        return None

    latest = max(payload, key=lambda r: r.get("period") or "")
    age_days = None
    try:
        period_date = datetime.strptime(latest.get("period"), "%Y-%m-%d").date()
        age_days = (datetime.now(ET).date() - period_date).days
    except (TypeError, ValueError):
        pass
    return {
        "period": latest.get("period"),
        "age_days": age_days,
        "strong_buy": latest.get("strongBuy", 0),
        "buy": latest.get("buy", 0),
        "hold": latest.get("hold", 0),
        "sell": latest.get("sell", 0),
        "strong_sell": latest.get("strongSell", 0),
    }


def fetch_price_target(ticker_obj):
    try:
        targets = ticker_obj.analyst_price_targets
        if targets and targets.get("mean"):
            return {
                "mean": float(targets["mean"]),
                "low": float(targets["low"]) if targets.get("low") else None,
                "high": float(targets["high"]) if targets.get("high") else None,
            }
    except Exception as e:
        print(f"[price_target] unavailable: {e}")
    return None


def _history(ticker_obj, period="1y", tries=3):
    """Yahoo occasionally returns nothing under rate limiting; retry before giving up."""
    for attempt in range(tries):
        try:
            hist = ticker_obj.history(period=period)
            if hist is not None and not hist.empty:
                return hist
        except Exception as e:
            print(f"[history] attempt {attempt + 1} failed: {e}")
        time.sleep(2 * (attempt + 1))
    return None


def _fresh_news(ticker_obj, limit=3, max_age_days=MAX_NEWS_AGE_DAYS):
    """Only headlines with a publish date inside the last few days."""
    items = []
    try:
        raw = ticker_obj.news or []
    except Exception:
        return items

    now = datetime.now(timezone.utc)
    for item in raw[:15]:
        content = item.get("content", item)
        title = content.get("title") or item.get("title")
        link = (content.get("clickThroughUrl") or {}).get("url") or item.get("link")
        published = content.get("pubDate")
        if not (title and link and published):
            continue
        try:
            when = datetime.fromisoformat(str(published).replace("Z", "+00:00"))
        except ValueError:
            continue
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
        if now - when > timedelta(days=max_age_days):
            continue
        items.append({"title": title, "link": link, "published": when.astimezone(ET).strftime("%b %d")})
        if len(items) >= limit:
            break
    return items


def _no_data(name, reason="No data."):
    return {
        "name": name, "price": None, "change_pct": None, "news": [],
        "signal": {"score": 50, "signal": "HOLD", "confidence": 0, "reasoning": reason},
        "analyst_consensus": None, "price_target": None, "congress": None,
        "as_of": None, "stale": True,
    }


def _analyze_ticker(ticker, name, reddit_mentions, congress_trades):
    t = yf.Ticker(ticker)
    hist = _history(t)

    if hist is None:
        return _no_data(name)

    closes = hist["Close"].tolist()
    volumes = hist["Volume"].tolist()
    last_close = float(closes[-1])
    prev_close = float(closes[-2]) if len(closes) > 1 else last_close
    change_pct = ((last_close - prev_close) / prev_close) * 100 if prev_close else 0.0

    # How fresh is the newest price bar, and is it a full close or a live intraday print?
    now_et = datetime.now(ET)
    last_bar_date = hist.index[-1].date()
    age_days = (now_et.date() - last_bar_date).days
    live = (
        last_bar_date == now_et.date()
        and now_et.weekday() < 5
        and (now_et.hour, now_et.minute) < (16, 5)
    )
    as_of = f"{last_bar_date:%b %d} {'(intraday)' if live else 'close'}"

    is_etf = ticker in ETFS
    stocktwits = fetch_stocktwits_sentiment(ticker)
    analyst_consensus = None if is_etf else fetch_analyst_consensus(ticker)
    price_target = None if is_etf else fetch_price_target(t)
    congress = congress_trades.get(ticker)
    signal = compute_signal(
        closes, volumes, reddit_mentions.get(ticker, 0), stocktwits,
        analyst_consensus=analyst_consensus, congress=congress,
        price_target=price_target, is_etf=is_etf, data_age_days=age_days,
    )

    return {
        "name": name,
        "price": round(last_close, 2),
        "change_pct": round(change_pct, 2),
        "news": _fresh_news(t),
        "signal": signal,
        "analyst_consensus": analyst_consensus,
        "price_target": price_target,
        "congress": congress,
        "as_of": as_of,
        "stale": age_days > MAX_PRICE_AGE_DAYS,
    }


def fetch_market_scan(exclude_tickers, limit=MARKET_SCAN_LIMIT):
    candidates = {}

    for screen_id in MARKET_SCAN_SCREENS:
        try:
            resp = yf.screen(screen_id, size=25)
            quotes = resp.get("quotes", [])
            print(f"[market_scan] {screen_id}: {len(quotes)} quotes")
        except Exception as e:
            print(f"[market_scan] FAILED {screen_id}: {e}")
            continue

        for q in quotes:
            symbol = q.get("symbol")
            if not symbol or symbol in exclude_tickers:
                continue

            price = q.get("regularMarketPrice")
            market_cap = q.get("marketCap")
            avg_volume = q.get("averageDailyVolume3Month") or 0
            # Filter only on fields Yahoo actually returned.
            if price is not None and price < SCAN_MIN_PRICE:
                continue
            if market_cap is not None and market_cap < SCAN_MIN_MARKET_CAP:
                continue
            if avg_volume and avg_volume < SCAN_MIN_AVG_VOLUME:
                continue

            change = q.get("regularMarketChangePercent") or 0
            volume = q.get("regularMarketVolume") or 0
            name = q.get("shortName") or q.get("longName") or symbol

            score = min(abs(change), 30)
            if avg_volume and volume:
                score += min(volume / avg_volume, 5)

            if symbol not in candidates or score > candidates[symbol]["score"]:
                candidates[symbol] = {
                    "symbol": symbol, "name": name, "change": change,
                    "score": score, "source": screen_id,
                }

    ranked = sorted(candidates.values(), key=lambda c: c["score"], reverse=True)
    top = ranked[:limit]
    print(f"[market_scan] surfaced: {[c['symbol'] for c in top]}")
    return top


def fetch_stock_data():
    data = {}
    reddit_mentions = fetch_reddit_mentions(list(TICKERS.keys()))
    congress_trades = fetch_congress_trades(list(TICKERS.keys()))

    for ticker, name in TICKERS.items():
        data[ticker] = _analyze_ticker(ticker, name, reddit_mentions, congress_trades)

    return data


def fetch_market_scan_data():
    scan_hits = fetch_market_scan(set(TICKERS.keys()))
    if not scan_hits:
        return {}

    scan_tickers = [c["symbol"] for c in scan_hits]
    reddit_mentions = fetch_reddit_mentions(scan_tickers)
    congress_trades = fetch_congress_trades(scan_tickers)

    data = {}
    for c in scan_hits:
        ticker = c["symbol"]
        analyzed = _analyze_ticker(ticker, c["name"], reddit_mentions, congress_trades)
        analyzed["scan_source"] = c["source"]
        data[ticker] = analyzed

    return data


def _latest_close(symbol):
    hist = _history(yf.Ticker(symbol), period="5d")
    if hist is None:
        return None
    return round(float(hist["Close"].iloc[-1]), 2)


def fetch_macro_data():
    return {"vix": _latest_close("^VIX"), "treasury_10y": _latest_close("^TNX")}


def fetch_fear_greed():
    url = "https://production.dataviz.cnn.io/index/fearandgreed/graphdata"
    headers = {
        **BROWSER_HEADERS,
        "Referer": "https://www.cnn.com/markets/fear-and-greed",
        "Origin": "https://www.cnn.com",
    }

    payload = _get_json(url, "cnn-fear-greed", headers=headers)
    if payload:
        fg = payload.get("fear_and_greed", {})
        score = fg.get("score")
        rating = fg.get("rating")
        if score is not None:
            return {"score": round(float(score)), "rating": rating, "estimated": False}

    return _estimate_fear_greed()


def _estimate_fear_greed():
    try:
        vix_hist = yf.Ticker("^VIX").history(period="1y")
        if vix_hist.empty:
            return {"score": None, "rating": None, "estimated": True}
        current_vix = float(vix_hist["Close"].iloc[-1])
        vix_low = float(vix_hist["Close"].min())
        vix_high = float(vix_hist["Close"].max())
        if vix_high == vix_low:
            return {"score": None, "rating": None, "estimated": True}
        normalized = (current_vix - vix_low) / (vix_high - vix_low)
        score = round((1 - normalized) * 100)
        if score >= 75:
            rating = "Extreme Greed"
        elif score >= 55:
            rating = "Greed"
        elif score >= 45:
            rating = "Neutral"
        elif score >= 25:
            rating = "Fear"
        else:
            rating = "Extreme Fear"
        return {"score": score, "rating": rating, "estimated": True}
    except Exception:
        return {"score": None, "rating": None, "estimated": True}


def _data_warnings(stocks):
    warnings = []
    stale = [t for t, info in stocks.items() if info.get("stale")]
    if stale:
        warnings.append(f"No fresh price data for: {', '.join(stale)} — no signal given.")
    for label, (ok, failed) in _SOURCE_STATUS.items():
        if failed and not ok:
            warnings.append(f"{label} was unreachable this run — signals were computed without it.")
    if not os.environ.get("FINNHUB_API_KEY"):
        warnings.append("FINNHUB_API_KEY is not set — analyst ratings are missing.")
    return warnings


def fetch_all():
    stocks = fetch_stock_data()
    market_scan = fetch_market_scan_data()
    macro = fetch_macro_data()
    fear_greed = fetch_fear_greed()
    reddit = fetch_reddit_top_posts()
    return {
        "stocks": stocks,
        "market_scan": market_scan,
        "macro": macro,
        "fear_greed": fear_greed,
        "reddit": reddit,
        "generated_at": datetime.now(ET).strftime("%a %b %d, %I:%M %p ET"),
        "warnings": _data_warnings(stocks),
    }
