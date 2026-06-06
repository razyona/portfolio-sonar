"""
Portfolio Sonar — AMPX & SIVE.ST  |  v4 — 15 sources + US Macro
──────────────────────────────────────────────────────────────────
Stock sources per run (every 5 min):
  📰 Google News (EN + SV + institutional queries)
  🏛  SEC EDGAR 8-K + 13D/13G (insider/institutional)
  📊 Yahoo Finance              Finnhub news + earnings
  🏦  Finnhub insider transactions (SEC Form 4)
  💬 StockTwits                 Reddit
  📣 PR Newswire + Business Wire
  🏢 Cision Sweden (SIVE.ST)
  💹 Price alert ≥3%           Volume spike ≥2× avg

US Macro sources (fire instantly on release):
  🏦 Federal Reserve RSS (FOMC decisions, speeches, minutes)
  📈 BEA RSS (GDP, PCE, Personal Income, Trade)
  📊 Finnhub Economic Calendar (CPI, NFP, PPI, Retail Sales...)
  🔍 Google News (macro keyword monitoring)

Institutional detection: JPMorgan, Goldman, BlackRock etc. → auto URGENT
Groq classifier → 🔴 URGENT pushed immediately | 🟡 WATCH hourly digest
"""

import os, json, hashlib, re, time
import requests, feedparser, yfinance as yf
from datetime import datetime, timezone, timedelta
from dateutil import parser as dateparse
import pytz

# ── Credentials ───────────────────────────────────────────────────────────────
TG_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
TG_CHAT  = os.environ["TELEGRAM_CHAT_ID"]
GROQ_KEY = os.environ["GROQ_API_KEY"]
FH_KEY   = os.environ["FINNHUB_API_KEY"]

IL  = pytz.timezone("Asia/Jerusalem")
DIR = os.path.dirname(os.path.abspath(__file__))

SEEN_FILE        = os.path.join(DIR, "seen_ids.json")
DIGEST_FILE      = os.path.join(DIR, "digest_queue.json")
PRICE_FILE       = os.path.join(DIR, "price_baseline.json")
LAST_DIGEST_FILE = os.path.join(DIR, "last_digest.json")
MACRO_SEEN_FILE  = os.path.join(DIR, "macro_seen.json")

PRICE_ALERT_PCT  = 3.0   # % move triggers URGENT
VOLUME_SPIKE_X   = 2.0   # × avg 20-day volume triggers URGENT

HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; PortfolioSonar/2.0)"}

# ── US Macro keywords → auto-URGENT ──────────────────────────────────────────
MACRO_URGENT_KW = [
    # Fed / rates
    "federal reserve", "fed rate", "rate hike", "rate cut", "rate decision",
    "fomc", "fed funds rate", "monetary policy", "interest rate decision",
    "jerome powell", "kevin warsh", "fed chair",
    # Inflation
    "consumer price index", " cpi ", "core cpi", "inflation report",
    "personal consumption expenditure", " pce ", "core pce",
    "producer price index", " ppi ",
    # Jobs
    "non-farm payroll", "nonfarm payroll", "jobs report", "jobs added",
    "unemployment rate", "initial jobless claims", "jobless claims",
    # GDP / growth
    " gdp ", "gross domestic product", "economic growth", "recession",
    "gdp growth", "gdp contraction", "gdp estimate",
    # Trade / consumer
    "retail sales", "consumer confidence", "ism manufacturing",
    "ism services", "trade deficit", "trade balance",
    # Market stress
    "10-year yield", "10-year treasury", "inverted yield curve",
    "treasury yield", "stagflation", "federal open market",
    # BEA/BLS releases
    "bureau of economic analysis", "bureau of labor statistics",
    "personal income", "personal outlays",
]

# ── Institutional investor names → auto-URGENT ────────────────────────────────
INSTITUTIONAL_NAMES = [
    "jpmorgan", "jp morgan", "goldman sachs", "blackrock", "vanguard",
    "morgan stanley", "fidelity", "citadel", "bridgewater", "renaissance",
    "two sigma", "point72", "millennium", "de shaw", "aqr", "tudor",
    "carlyle", "kkr", "apollo", "warburg", "sequoia", "andreessen",
    "state street", "norges bank", "swissnational", "pension fund",
    "flaggning", "flagging notification", "major shareholder",
    "stake acquisition", "acquired stake", "13d", "13g", "form 4",
    "insider buying", "insider purchase", "storägare", "kapitalandel",
    "institutional", "hedge fund", "asset management",
]

# ── Ticker config ─────────────────────────────────────────────────────────────
TICKERS = {
    "AMPX": {
        "name":        "Amprius Technologies",
        "yahoo":       "AMPX",
        "stocktwits":  "AMPX",
        "finnhub":     "AMPX",
        "keywords":    ["ampx", "amprius", "silicon anode", "battery energy", "amprius technologies"],
        "google_q_en": "AMPX Amprius Technologies",
        "google_q_sv": None,
        "cision_slug": None,
        "reddit_q":    "AMPX OR Amprius Technologies",
        "sec":         True,
        "is_swedish":  False,
        "prnw_kw":     ["amprius"],
        "bw_kw":       ["amprius"],
    },
    "SIVE.ST": {
        "name":        "Sivers Semiconductors",
        "yahoo":       "SIVE.ST",
        "stocktwits":  None,
        "finnhub":     "SIVE:STO",
        "keywords":    ["sive", "sivers", "sivers semiconductors", "sivers ima", "mmwave", "5g chip"],
        "google_q_en": "Sivers Semiconductors SIVE Stockholm",
        "google_q_sv": "Sivers Semiconductors",
        "cision_slug": "sivers-semiconductors",
        "reddit_q":    "Sivers Semiconductors SIVE",
        "sec":         False,
        "is_swedish":  True,
        "prnw_kw":     ["sivers"],
        "bw_kw":       ["sivers"],
    },
}

# ── State helpers ─────────────────────────────────────────────────────────────
def tg_send(text: str):
    url = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
    for chunk in [text[i:i+4000] for i in range(0, len(text), 4000)]:
        try:
            requests.post(url, json={"chat_id": TG_CHAT, "text": chunk,
                                     "parse_mode": "HTML"}, timeout=15)
        except Exception as e:
            print(f"  [TG] {e}")

def load_json(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except:
        return default

def save_json(path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, default=str)

def mid(text: str) -> str:
    return hashlib.md5(text.encode()).hexdigest()[:14]

def clean(text: str) -> str:
    return re.sub(r"<[^>]+>", "", text or "").strip()

def is_recent(pub: str, hours: int = 48) -> bool:
    if not pub:
        return True
    try:
        dt = dateparse.parse(pub)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - dt) < timedelta(hours=hours)
    except:
        return True

def kw_hit(item: dict, keywords: list) -> bool:
    blob = (item.get("title","") + " " + item.get("summary","")).lower()
    return any(k in blob for k in keywords)

def make_item(id_src, title, source, link, published, summary, layer) -> dict:
    return {
        "id":        mid(id_src),
        "title":     clean(title)[:180],
        "source":    source,
        "link":      link or "",
        "published": published or "",
        "summary":   clean(summary)[:250],
        "layer":     layer,
    }

# ── Source: Google News RSS ───────────────────────────────────────────────────
def fetch_google(query: str, lang: str = "en-US", region: str = "US") -> list:
    url = (f"https://news.google.com/rss/search"
           f"?q={requests.utils.quote(query)}"
           f"&hl={lang}&gl={region}&ceid={region}:{lang[:2]}")
    feed = feedparser.parse(url)
    out = []
    for e in feed.entries[:12]:
        out.append(make_item(
            e.get("link", e.get("title","")),
            e.get("title",""),
            e.get("source", {}).get("title","Google News"),
            e.get("link",""),
            e.get("published",""),
            e.get("summary",""),
            "News"
        ))
    return out

# ── Source: SEC EDGAR 8-K ─────────────────────────────────────────────────────
def fetch_sec(ticker: str) -> list:
    url = (f"https://www.sec.gov/cgi-bin/browse-edgar"
           f"?action=getcompany&CIK={ticker}&type=8-K"
           f"&dateb=&owner=include&count=5&output=atom")
    feed = feedparser.parse(url)
    out = []
    for e in feed.entries[:5]:
        uid = e.get("id", e.get("link", e.get("title","")))
        out.append(make_item(uid, e.get("title",""), "SEC EDGAR 8-K",
                             e.get("link",""), e.get("published",""),
                             e.get("summary",""), "Regulatory"))
    return out

# ── Source: Yahoo Finance ─────────────────────────────────────────────────────
def fetch_yahoo(symbol: str, yahoo_sym: str) -> list:
    try:
        news = yf.Ticker(yahoo_sym).news or []
        out = []
        for n in news[:12]:
            ts = n.get("providerPublishTime", 0)
            pub = datetime.fromtimestamp(ts, tz=timezone.utc).isoformat() if ts else ""
            out.append(make_item(
                n.get("link", n.get("title","")),
                n.get("title",""),
                n.get("publisher","Yahoo Finance"),
                n.get("link",""), pub, "", "News"
            ))
        return out
    except Exception as e:
        print(f"    [Yahoo] {symbol}: {e}")
        return []

# ── Source: StockTwits ────────────────────────────────────────────────────────
def fetch_stocktwits(symbol: str) -> list:
    try:
        r = requests.get(
            f"https://api.stocktwits.com/api/2/streams/symbol/{symbol}.json",
            headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36", "Accept": "application/json"},
            timeout=10
        )
        if r.status_code != 200 or not r.text.strip():
            return []
        messages = r.json().get("messages", [])
        out = []
        for m in messages[:20]:
            sent = ((m.get("entities") or {}).get("sentiment") or {}).get("basic","")
            body = m.get("body","")[:140]
            uid = str(m.get("id", body))
            title = f"[{sent}] {body}" if sent else body
            out.append(make_item(uid, title,
                                 f"StockTwits @{m.get('user',{}).get('username','?')}",
                                 f"https://stocktwits.com/symbol/{symbol}",
                                 m.get("created_at",""), body, "Social"))
        return out
    except Exception as e:
        print(f"    [StockTwits] {symbol}: {e}")
        return []

# ── Source: Finnhub insider transactions (SEC Form 4) ────────────────────────
def fetch_finnhub_insider(fh_sym: str) -> list:
    try:
        r = requests.get(
            "https://finnhub.io/api/v1/stock/insider-transactions",
            params={"symbol": fh_sym, "token": FH_KEY},
            timeout=10
        )
        out = []
        for tx in r.json().get("data", [])[:15]:
            name  = tx.get("name", "Unknown")
            chg   = tx.get("change", 0)
            price = tx.get("transactionPrice", 0)
            code  = tx.get("transactionCode", "")
            date  = tx.get("transactionDate", "")
            shares = tx.get("share", 0)
            action = "BUY" if chg > 0 else "SELL"
            arrow  = "📈" if chg > 0 else "📉"
            title  = f"{arrow} Insider {action}: {name} — {chg:+,} shares @ ${price:.2f} ({date})"
            uid    = tx.get("id", f"{name}{date}{chg}")
            out.append(make_item(
                str(uid), title, "SEC Form 4 (Insider)",
                f"https://finance.yahoo.com/quote/{fh_sym}/insider-transactions",
                date, f"Total shares held: {shares:,}", "Regulatory"
            ))
        return out
    except Exception as e:
        print(f"    [Insider] {fh_sym}: {e}")
        return []

# ── Source: SEC 13D/13G (institutional investors) ────────────────────────────
def fetch_sec_institutional(ticker: str, company_name: str) -> list:
    try:
        headers_edgar = {"User-Agent": "PortfolioSonar contact@portfolio.research"}
        url = (f"https://www.sec.gov/cgi-bin/browse-edgar"
               f"?action=getcompany&CIK={ticker}&type=SC+13"
               f"&dateb=&owner=include&count=5&output=atom")
        feed = feedparser.parse(url, request_headers=headers_edgar)
        out = []
        for e in feed.entries[:5]:
            uid = e.get("id", e.get("link", ""))
            out.append(make_item(
                uid, e.get("title", ""), "SEC 13D/13G (Institutional)",
                e.get("link", ""), e.get("published", ""),
                e.get("summary", ""), "Regulatory"
            ))
        return out
    except Exception as e:
        print(f"    [SEC 13D/13G] {ticker}: {e}")
        return []

# ── Source: Dedicated institutional Google News ───────────────────────────────
def fetch_institutional_news(ticker: str, name: str, is_swedish: bool = False) -> list:
    queries = [
        f'"{name}" JPMorgan OR "Goldman Sachs" OR BlackRock OR Vanguard OR stake',
        f'"{ticker}" institutional investor OR "major shareholder" OR "hedge fund"',
    ]
    if is_swedish:
        queries.append(f'"{name}" flaggning OR storägare OR kapitalandel')

    out = []
    for q in queries:
        try:
            lang   = "sv" if is_swedish and "flagg" in q else "en-US"
            region = "SE" if is_swedish and "flagg" in q else "US"
            items  = fetch_google(q, lang=lang, region=region)
            out.extend(items)
        except Exception as e:
            print(f"    [Institutional news] {e}")
    return out

# ── Source: Finnhub company news ──────────────────────────────────────────────
def fetch_finnhub(fh_sym: str) -> list:
    try:
        today    = datetime.now().strftime("%Y-%m-%d")
        week_ago = (datetime.now() - timedelta(days=7)).strftime("%Y-%m-%d")
        r = requests.get(
            "https://finnhub.io/api/v1/company-news",
            params={"symbol": fh_sym, "from": week_ago, "to": today, "token": FH_KEY},
            timeout=10
        )
        out = []
        for n in r.json()[:12]:
            ts = n.get("datetime",0)
            pub = datetime.fromtimestamp(ts, tz=timezone.utc).isoformat() if ts else ""
            out.append(make_item(
                str(n.get("id", n.get("headline",""))),
                n.get("headline",""), n.get("source","Finnhub"),
                n.get("url",""), pub, n.get("summary",""), "News"
            ))
        return out
    except Exception as e:
        print(f"    [Finnhub] {fh_sym}: {e}")
        return []

# ── Source: Reddit (no auth) ──────────────────────────────────────────────────
def fetch_reddit(query: str) -> list:
    out = []
    for sub in ["stocks", "investing", "wallstreetbets"]:
        try:
            r = requests.get(
                f"https://old.reddit.com/r/{sub}/search.json",
                params={"q": query, "sort": "new", "limit": 10, "t": "week"},
                headers={"User-Agent": "PortfolioSonar:v2.0 (by /u/portfolio_scanner)"},
                timeout=10
            )
            for post in r.json().get("data",{}).get("children",[]):
                d = post.get("data",{})
                ts = d.get("created_utc",0)
                pub = datetime.fromtimestamp(ts, tz=timezone.utc).isoformat() if ts else ""
                title = d.get("title","")[:160]
                link  = f"https://reddit.com{d.get('permalink','')}"
                score = d.get("score",0)
                out.append(make_item(
                    d.get("id", title),
                    f"[{score}▲] {title}",
                    f"r/{sub}",
                    link, pub, d.get("selftext","")[:200], "Social"
                ))
            time.sleep(0.5)   # polite rate limit
        except Exception as e:
            print(f"    [Reddit] r/{sub}: {e}")
    return out

# ── Source: Cision Sweden (SIVE.ST) ──────────────────────────────────────────
def fetch_cision(slug: str) -> list:
    try:
        url  = f"https://news.cision.com/{slug}/r/"
        feed = feedparser.parse(url)
        out = []
        for e in feed.entries[:8]:
            out.append(make_item(
                e.get("link", e.get("title","")),
                e.get("title",""), "Cision Sweden",
                e.get("link",""), e.get("published",""),
                e.get("summary",""), "Regulatory"
            ))
        return out
    except Exception as e:
        print(f"    [Cision] {slug}: {e}")
        return []

# ── Source: PR Newswire + Business Wire ───────────────────────────────────────
def fetch_wire(url: str, source_name: str, keywords: list) -> list:
    try:
        feed = feedparser.parse(url)
        out = []
        for e in feed.entries[:30]:
            title   = clean(e.get("title",""))
            summary = clean(e.get("summary",""))
            blob    = (title + " " + summary).lower()
            if not any(k in blob for k in keywords):
                continue
            out.append(make_item(
                e.get("link", title),
                title, source_name,
                e.get("link",""), e.get("published",""),
                summary, "Regulatory"
            ))
        return out
    except Exception as e:
        print(f"    [{source_name}]: {e}")
        return []

# ── Source: Finnhub earnings calendar ────────────────────────────────────────
def fetch_earnings_alerts() -> list:
    alerts = []
    today  = datetime.now().strftime("%Y-%m-%d")
    ahead  = (datetime.now() + timedelta(days=7)).strftime("%Y-%m-%d")
    for sym, cfg in TICKERS.items():
        fh_sym = cfg.get("finnhub","")
        if not fh_sym:
            continue
        try:
            r = requests.get(
                "https://finnhub.io/api/v1/calendar/earnings",
                params={"from": today, "to": ahead, "symbol": fh_sym, "token": FH_KEY},
                timeout=10
            )
            for ev in r.json().get("earningsCalendar",[]):
                date = ev.get("date","")
                eps_est = ev.get("epsEstimate","")
                iid = mid(f"earnings_{sym}_{date}")
                alerts.append({
                    "id":           iid,
                    "title":        f"📅 Earnings due {date} — EPS est: {eps_est}",
                    "source":       "Finnhub Earnings Calendar",
                    "link":         f"https://finance.yahoo.com/quote/{cfg['yahoo']}",
                    "published":    datetime.now(timezone.utc).isoformat(),
                    "summary":      f"{sym} earnings report scheduled",
                    "layer":        "Regulatory",
                    "ticker":       sym,
                    "force_urgent": True,
                })
        except Exception as e:
            print(f"    [Earnings] {sym}: {e}")
    return alerts

# ── Source: Federal Reserve RSS ──────────────────────────────────────────────
def fetch_fed_rss() -> list:
    """Official Fed press releases: rate decisions, FOMC minutes, speeches."""
    try:
        feed = feedparser.parse("https://www.federalreserve.gov/feeds/press_all.xml")
        out = []
        for e in feed.entries[:15]:
            out.append(make_item(
                e.get("id", e.get("link", e.get("title",""))),
                e.get("title",""),
                "Federal Reserve",
                e.get("link",""),
                e.get("published",""),
                e.get("summary",""),
                "Macro"
            ))
        return out
    except Exception as ex:
        print(f"    [Fed RSS] {ex}")
        return []

# ── Source: BEA (GDP / PCE / Trade) ──────────────────────────────────────────
def fetch_bea_rss() -> list:
    """Bureau of Economic Analysis: GDP, PCE, Personal Income, Trade Balance."""
    try:
        feed = feedparser.parse("https://apps.bea.gov/rss/rss.xml")
        out = []
        for e in feed.entries[:15]:
            out.append(make_item(
                e.get("link", e.get("title","")),
                e.get("title",""),
                "BEA (GDP/PCE/Trade)",
                e.get("link",""),
                e.get("published",""),
                e.get("summary",""),
                "Macro"
            ))
        return out
    except Exception as ex:
        print(f"    [BEA RSS] {ex}")
        return []

# ── Source: Finnhub Economic Calendar ────────────────────────────────────────
def fetch_finnhub_macro() -> list:
    """Finnhub econ calendar — fires only when actual value is newly posted."""
    try:
        r = requests.get(
            "https://finnhub.io/api/v1/calendar/economic",
            params={"token": FH_KEY},
            timeout=12
        )
        events = r.json().get("economicCalendar", [])
        out = []
        for ev in events[:80]:
            if not ev.get("actual"):           # not yet released → skip
                continue
            if ev.get("country","") != "US":   # US only
                continue
            event_name = ev.get("event","")
            actual     = ev.get("actual","")
            estimate   = ev.get("estimate","")
            prev       = ev.get("prev","")
            event_time = ev.get("time","")

            # Beat / miss vs estimate
            beat_miss = ""
            try:
                def _num(s):
                    return float(str(s).replace("%","").replace("K","e3")
                                       .replace("M","e6").replace("B","e9"))
                if estimate:
                    diff = _num(actual) - _num(estimate)
                    beat_miss = "✅ BEAT" if diff > 0 else ("❌ MISS" if diff < 0 else "〰 IN LINE")
            except:
                pass

            title = f"📊 {event_name}: {actual}"
            if beat_miss:
                title += f"  {beat_miss}"
            if estimate:
                title += f"  (est: {estimate})"
            if prev:
                title += f"  | prev: {prev}"

            out.append(make_item(
                mid(f"macro_{event_name}_{event_time}"),
                title,
                "Finnhub Economic Calendar",
                "https://finnhub.io/calendar/economic",
                event_time,
                f"Actual: {actual} | Estimate: {estimate} | Previous: {prev}",
                "Macro"
            ))
        return out
    except Exception as ex:
        print(f"    [Finnhub Macro] {ex}")
        return []

# ── Source: Google News — macro keywords ─────────────────────────────────────
def fetch_macro_google() -> list:
    """Targeted Google News queries for high-impact US macro releases."""
    queries = [
        "Federal Reserve rate decision OR FOMC statement",
        "CPI inflation report \"consumer price index\" US",
        "\"non-farm payrolls\" OR \"jobs report\" OR unemployment US",
        "US GDP \"gross domestic product\" report",
        "PCE inflation OR \"retail sales\" OR PPI US report",
    ]
    out = []
    for q in queries:
        try:
            out.extend(fetch_google(q))
        except Exception as ex:
            print(f"    [Macro Google] {ex}")
    return out

# ── Macro helpers ─────────────────────────────────────────────────────────────
def is_macro_event(item: dict) -> bool:
    blob = (" " + (item.get("title","") + " " + item.get("summary","")).lower() + " ")
    return any(k in blob for k in MACRO_URGENT_KW)

def analyze_macro(item: dict) -> str:
    prompt = (
        "Buy-side analyst. Write a concise Telegram alert for this US macro release. Max 380 chars, plain text.\n"
        f"Event: {item['title']}\nSource: {item['source']}\nDetails: {item.get('summary','')}\n\n"
        "Format:\n"
        "Data: [one sentence on what was released]\n"
        "Impact: Bullish/Bearish/Neutral for stocks — [why in 1 line]\n"
        "Watch: [key thing to monitor next]"
    )
    try:
        return groq_post("llama-3.3-70b-versatile", prompt, 220)
    except Exception as e:
        return f"Analysis unavailable: {e}"

def fmt_macro_urgent(item: dict) -> str:
    now = datetime.now(IL).strftime("%d/%m %H:%M")
    return (
        f"📊 <b>MACRO ALERT — US Economy</b>  {now} IL\n"
        f"<b>{item['title'][:180]}</b>\n"
        f"<i>{item['source']}</i>\n\n"
        f"{item.get('analysis','')}\n\n"
        f"🔗 {item.get('link','')}"
    )

# ── Source: Price alert ───────────────────────────────────────────────────────
def check_price() -> list:
    alerts   = []
    baseline = load_json(PRICE_FILE, {})
    new_base = {}
    for sym, cfg in TICKERS.items():
        try:
            hist = yf.Ticker(cfg["yahoo"]).history(period="2d")
            if hist.empty:
                continue
            price = float(hist["Close"].iloc[-1])
            new_base[sym] = price
            if sym in baseline:
                prev = baseline[sym]
                pct  = ((price - prev) / prev) * 100
                if abs(pct) >= PRICE_ALERT_PCT:
                    arrow = "📈" if pct > 0 else "📉"
                    alerts.append({
                        "id":           mid(f"price_{sym}_{datetime.now().strftime('%Y%m%d%H')}"),
                        "title":        f"{arrow} {sym} moved {pct:+.1f}% → ${price:.3f}",
                        "source":       "Price Alert",
                        "link":         f"https://finance.yahoo.com/quote/{cfg['yahoo']}",
                        "published":    datetime.now(timezone.utc).isoformat(),
                        "summary":      f"From ${prev:.3f} to ${price:.3f}",
                        "layer":        "Price",
                        "ticker":       sym,
                        "force_urgent": True,
                    })
        except Exception as e:
            print(f"    [Price] {sym}: {e}")
    save_json(PRICE_FILE, new_base)
    return alerts

# ── Source: Volume anomaly ────────────────────────────────────────────────────
def check_volume() -> list:
    alerts = []
    for sym, cfg in TICKERS.items():
        try:
            hist = yf.Ticker(cfg["yahoo"]).history(period="30d")
            if len(hist) < 5:
                continue
            avg_vol  = float(hist["Volume"].iloc[:-1].tail(20).mean())
            cur_vol  = float(hist["Volume"].iloc[-1])
            if avg_vol > 0 and cur_vol >= avg_vol * VOLUME_SPIKE_X:
                mult = cur_vol / avg_vol
                alerts.append({
                    "id":           mid(f"vol_{sym}_{datetime.now().strftime('%Y%m%d%H')}"),
                    "title":        f"🔊 {sym} volume spike {mult:.1f}× avg ({int(cur_vol/1e6):.1f}M vs {int(avg_vol/1e6):.1f}M avg)",
                    "source":       "Volume Alert",
                    "link":         f"https://finance.yahoo.com/quote/{cfg['yahoo']}",
                    "published":    datetime.now(timezone.utc).isoformat(),
                    "summary":      f"Current: {int(cur_vol):,} | 20d avg: {int(avg_vol):,}",
                    "layer":        "Price",
                    "ticker":       sym,
                    "force_urgent": True,
                })
        except Exception as e:
            print(f"    [Volume] {sym}: {e}")
    return alerts

# ── Groq ──────────────────────────────────────────────────────────────────────
def groq_post(model: str, prompt: str, max_tokens: int) -> str:
    r = requests.post(
        "https://api.groq.com/openai/v1/chat/completions",
        headers={"Authorization": f"Bearer {GROQ_KEY}", "Content-Type": "application/json"},
        json={"model": model,
              "messages": [{"role": "user", "content": prompt}],
              "max_tokens": max_tokens},
        timeout=20
    )
    return r.json()["choices"][0]["message"]["content"].strip()

def is_institutional_event(item: dict) -> bool:
    """Fast local check — no API call needed."""
    blob = (item.get("title","") + " " + item.get("summary","")).lower()
    return any(inst in blob for inst in INSTITUTIONAL_NAMES)

def classify(item: dict, sym: str, name: str) -> dict:
    # ── Fast local override: institutional entry = always URGENT ──────────────
    if is_institutional_event(item):
        inst_matches = [i for i in INSTITUTIONAL_NAMES if i in
                        (item.get("title","") + item.get("summary","")).lower()]
        return {
            "urgency": "URGENT",
            "reason":  f"Institutional activity detected: {', '.join(inst_matches[:3])}"
        }

    # ── Groq classification for everything else ───────────────────────────────
    prompt = (
        f"Financial news classifier for {sym} ({name}).\n"
        f"Title: {item['title']}\nSource: {item['source']} | Layer: {item['layer']}\n"
        f"Summary: {item.get('summary','')[:120]}\n\n"
        f"Reply ONLY with JSON: {{\"urgency\":\"URGENT|WATCH|FYI|IGNORE\",\"reason\":\"one sentence\"}}\n\n"
        f"URGENT=SEC/earnings/M&A/bankruptcy/major partnership/price spike/institutional investor entry\n"
        f"WATCH=analyst note, product news, exec change, relevant sector\n"
        f"FYI=social chatter, vague mention  IGNORE=unrelated/spam"
    )
    try:
        raw = groq_post("llama-3.1-8b-instant", prompt, 80)
        m = re.search(r'\{.*\}', raw, re.DOTALL)
        if m:
            return json.loads(m.group())
    except Exception as e:
        print(f"    [classify] {e}")
    return {"urgency": "WATCH", "reason": "classification unavailable"}

def analyze(item: dict, sym: str, name: str) -> str:
    prompt = (
        f"Buy-side analyst. Write Telegram analysis for {sym} ({name}). Max 350 chars, plain text.\n"
        f"Title: {item['title']}\nSource: {item['source']}\nSummary: {item.get('summary','')}\n\n"
        f"Format:\nWhat: [1 sentence]\nImpact: Bullish/Bearish/Neutral — [why]\nWatch: [key monitor point]"
    )
    try:
        return groq_post("llama-3.3-70b-versatile", prompt, 200)
    except Exception as e:
        return f"Analysis error: {e}"

# ── Formatters ────────────────────────────────────────────────────────────────
LAYER_ICON = {"Regulatory": "🏛", "Price": "💹", "News": "📰", "Social": "💬", "Macro": "📊"}

def fmt_urgent(item: dict, sym: str, analysis: str) -> str:
    now  = datetime.now(IL).strftime("%d/%m %H:%M")
    icon = LAYER_ICON.get(item.get("layer",""), "📡")
    return (
        f"🔴 <b>URGENT — {sym}</b>  {now} IL\n"
        f"{icon} <b>{item['title'][:140]}</b>\n"
        f"<i>{item['source']}</i>\n\n"
        f"{analysis}\n\n"
        f"🔗 {item.get('link','')}"
    )

def fmt_digest(items: list, now_str: str) -> str:
    if not items:
        return ""
    lines = [f"🟡 <b>Watch Digest</b>  {now_str} IL\n"]
    by_ticker: dict = {}
    for it in items:
        by_ticker.setdefault(it.get("ticker","?"), []).append(it)
    for sym, its in sorted(by_ticker.items()):
        name = TICKERS.get(sym,{}).get("name","")
        lines.append(f"<b>── {sym}  {name} ──</b>")
        for it in its[:6]:
            icon = LAYER_ICON.get(it.get("layer",""),"•")
            lines.append(f"{icon} {it['title'][:100]}")
            if it.get("reason"):
                lines.append(f"   <i>{it['reason']}</i>")
        lines.append("")
    lines.append(f"<i>{len(items)} items since last digest</i>")
    return "\n".join(lines)

# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    now_il  = datetime.now(IL)
    now_str = now_il.strftime("%d/%m/%Y %H:%M")
    print(f"\n[{now_str}] ── Sonar scan start ──")

    seen         = set(load_json(SEEN_FILE, []))
    digest_queue = load_json(DIGEST_FILE, [])
    last_digest  = load_json(LAST_DIGEST_FILE, {"hour_key": ""})
    new_seen     = set()
    urgent_msgs  = []
    new_watch    = []

    # ── Per-ticker sources ────────────────────────────────────────────
    for sym, cfg in TICKERS.items():
        print(f"\n  [{sym}] fetching...")
        raw: list = []

        raw += fetch_google(cfg["google_q_en"])
        raw += fetch_yahoo(sym, cfg["yahoo"])
        if cfg.get("sec"):
            raw += fetch_sec(sym)
            raw += fetch_sec_institutional(sym, cfg["name"])
            raw += fetch_finnhub_insider(cfg["finnhub"])
        if cfg.get("stocktwits"):
            raw += fetch_stocktwits(cfg["stocktwits"])
        raw += fetch_finnhub(cfg["finnhub"])
        raw += fetch_reddit(cfg["reddit_q"])
        raw += fetch_institutional_news(sym, cfg["name"], is_swedish=cfg.get("is_swedish", False))
        if cfg.get("google_q_sv"):
            raw += fetch_google(cfg["google_q_sv"], lang="sv", region="SE")
        if cfg.get("cision_slug"):
            raw += fetch_cision(cfg["cision_slug"])
        raw += fetch_wire(
            "https://www.prnewswire.com/rss/news-releases-list.rss",
            "PR Newswire", cfg["prnw_kw"]
        )
        raw += fetch_wire(
            "https://feed.businesswire.com/rss/home/?rss=G1",
            "Business Wire", cfg["bw_kw"]
        )

        print(f"  [{sym}] {len(raw)} raw → dedup + filter...")
        all_kw = cfg["keywords"] + [sym.split(".")[0].lower(),
                                    cfg["name"].lower().split()[0]]
        new_cnt = 0
        for item in raw:
            iid = item["id"]
            if iid in seen or iid in new_seen:
                continue
            new_seen.add(iid)
            if not is_recent(item.get("published",""), hours=48):
                continue
            if not kw_hit(item, all_kw):
                continue

            item["ticker"] = sym
            new_cnt += 1

            clf            = classify(item, sym, cfg["name"])
            urgency        = clf.get("urgency","WATCH")
            item["reason"] = clf.get("reason","")

            if urgency == "URGENT":
                analysis = analyze(item, sym, cfg["name"])
                urgent_msgs.append(fmt_urgent(item, sym, analysis))
            elif urgency == "WATCH":
                new_watch.append(item)

        print(f"  [{sym}] {new_cnt} new items processed")

    # ── Cross-ticker sources (price, volume, earnings) ────────────────
    print("\n  [MARKET] checking price, volume, earnings...")
    for forced_item in check_price() + check_volume() + fetch_earnings_alerts():
        iid = forced_item["id"]
        if iid in seen or iid in new_seen:
            continue
        new_seen.add(iid)
        sym = forced_item.get("ticker", list(TICKERS.keys())[0])
        cfg = TICKERS.get(sym, {})
        analysis = analyze(forced_item, sym, cfg.get("name",""))
        urgent_msgs.append(fmt_urgent(forced_item, sym, analysis))

    # ── US Macro sources ──────────────────────────────────────────────
    print("\n  [MACRO] scanning US economic releases...")
    macro_seen     = set(load_json(MACRO_SEEN_FILE, []))
    new_macro_seen = set()
    macro_items    = []

    macro_items += fetch_fed_rss()
    macro_items += fetch_bea_rss()
    macro_items += fetch_finnhub_macro()
    macro_items += fetch_macro_google()

    macro_cnt = 0
    for item in macro_items:
        iid = item["id"]
        if iid in macro_seen or iid in new_macro_seen:
            continue
        new_macro_seen.add(iid)
        if not is_recent(item.get("published",""), hours=36):
            continue
        if not is_macro_event(item):
            continue
        item["analysis"] = analyze_macro(item)
        tg_send(fmt_macro_urgent(item))
        macro_cnt += 1
        print(f"  📊 MACRO URGENT → Telegram: {item['title'][:60]}")

    save_json(MACRO_SEEN_FILE, list((macro_seen | new_macro_seen))[-3000:])
    print(f"  [MACRO] {macro_cnt} macro alerts sent, {len(macro_items)} items scanned")

    # ── Send URGENT immediately ───────────────────────────────────────
    for msg in urgent_msgs:
        tg_send(msg)
        print("  🔴 URGENT → Telegram")

    # ── Digest queue ──────────────────────────────────────────────────
    digest_queue.extend(new_watch)
    digest_queue = digest_queue[-200:]

    hour_key = now_il.strftime("%Y-%m-%d-%H")
    if (now_il.minute <= 8
            and last_digest.get("hour_key") != hour_key
            and len(digest_queue) > 0):
        msg = fmt_digest(digest_queue, now_il.strftime("%d/%m %H:%M"))
        if msg:
            tg_send(msg)
            print(f"  🟡 Digest → Telegram ({len(digest_queue)} items)")
        digest_queue = []
        save_json(LAST_DIGEST_FILE, {"hour_key": hour_key})
    elif len(digest_queue) == 0 and not urgent_msgs:
        print("  ✓ No new items this run — silent (no Telegram noise)")

    # ── Persist ───────────────────────────────────────────────────────
    save_json(SEEN_FILE,   list((seen | new_seen))[-6000:])
    save_json(DIGEST_FILE, digest_queue)
    print(f"\n[{now_str}] Done — {len(urgent_msgs)} urgent, {len(new_watch)} queued")

if __name__ == "__main__":
    main()
