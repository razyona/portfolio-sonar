"""
Portfolio Sonar — AMPX & SIVE.ST  |  v5 — מיקוד + חבילה אחת
──────────────────────────────────────────────────────────────────
מקורות מניות (כל 5 דקות):
  📰 Google News (EN + SV)       📊 Yahoo Finance + Finnhub
  🏛  SEC EDGAR 8-K + 13D/13G    🏦 Finnhub Form 4 (insider)
  🏢 Cision Sweden (SIVE.ST)     🔍 Google News מוסדי

מקורות מאקרו (מיידי בעת פרסום):
  🏦 Federal Reserve RSS         📈 BEA (GDP / PCE / Trade)
  📊 Finnhub Economic Calendar   🔍 Google News macro

פילוסופיה:
  • כל האירועים הדחופים מריצה אחת → הודעה מקובצת אחת בלבד
  • ניתוח איכותי: מודל 70b מסכם את כל האירועים בבת אחת
  • ללא התראות מחיר/נפח
  • ללא Reddit / StockTwits / PR Newswire / Business Wire
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
LAST_DIGEST_FILE = os.path.join(DIR, "last_digest.json")
MACRO_SEEN_FILE  = os.path.join(DIR, "macro_seen.json")

HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; PortfolioSonar/5.0)"}

# ── US Macro keywords → auto-URGENT ──────────────────────────────────────────
MACRO_URGENT_KW = [
    "federal reserve", "fed rate", "rate hike", "rate cut", "rate decision",
    "fomc", "fed funds rate", "monetary policy", "interest rate decision",
    "jerome powell", "kevin warsh", "fed chair",
    "consumer price index", " cpi ", "core cpi", "inflation report",
    "personal consumption expenditure", " pce ", "core pce",
    "producer price index", " ppi ",
    "non-farm payroll", "nonfarm payroll", "jobs report", "jobs added",
    "unemployment rate", "initial jobless claims", "jobless claims",
    " gdp ", "gross domestic product", "economic growth", "recession",
    "retail sales", "consumer confidence", "ism manufacturing", "ism services",
    "10-year yield", "treasury yield", "stagflation", "federal open market",
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
        "finnhub":     "AMPX",
        "keywords":    ["ampx", "amprius", "silicon anode", "battery energy", "amprius technologies"],
        "google_q_en": "AMPX Amprius Technologies",
        "google_q_sv": None,
        "cision_slug": None,
        "sec":         True,
        "is_swedish":  False,
    },
    "SIVE.ST": {
        "name":        "Sivers Semiconductors",
        "yahoo":       "SIVE.ST",
        "finnhub":     "SIVE:STO",
        "keywords":    ["sive", "sivers", "sivers semiconductors", "sivers ima", "mmwave", "5g chip"],
        "google_q_en": "Sivers Semiconductors SIVE Stockholm",
        "google_q_sv": "Sivers Semiconductors",
        "cision_slug": "sivers-semiconductors",
        "sec":         False,
        "is_swedish":  True,
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

def title_id(title: str) -> str:
    """Title-based dedup key — strips noise, catches same story from different sources."""
    normalized = re.sub(r'[^a-z0-9א-ת]', '', title.lower())
    return "t:" + mid(normalized[:70])

def clean(text: str) -> str:
    return re.sub(r"<[^>]+>", "", text or "").strip()

def is_recent(pub: str, hours: int = 24) -> bool:
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
        "tid":       title_id(title),
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

# ── Source: Yahoo Finance ─────────────────────────────────────────────────────
def fetch_yahoo(symbol: str, yahoo_sym: str) -> list:
    try:
        news = yf.Ticker(yahoo_sym).news or []
        out = []
        for n in news[:15]:
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

# ── Source: Finnhub company news ──────────────────────────────────────────────
def fetch_finnhub(fh_sym: str) -> list:
    try:
        today    = datetime.now().strftime("%Y-%m-%d")
        week_ago = (datetime.now() - timedelta(days=5)).strftime("%Y-%m-%d")
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

# ── Source: Finnhub insider transactions (SEC Form 4) ────────────────────────
def fetch_finnhub_insider(fh_sym: str) -> list:
    try:
        r = requests.get(
            "https://finnhub.io/api/v1/stock/insider-transactions",
            params={"symbol": fh_sym, "token": FH_KEY},
            timeout=10
        )
        out = []
        for tx in r.json().get("data", [])[:10]:
            name   = tx.get("name", "Unknown")
            chg    = tx.get("change", 0)
            price  = tx.get("transactionPrice", 0)
            date   = tx.get("transactionDate", "")
            shares = tx.get("share", 0)
            action = "רכישה" if chg > 0 else "מכירה"
            arrow  = "📈" if chg > 0 else "📉"
            title  = f"{arrow} Insider {action}: {name} — {chg:+,} מניות @ ${price:.2f} ({date})"
            uid    = tx.get("id", f"{name}{date}{chg}")
            out.append(make_item(
                str(uid), title, "SEC Form 4 (Insider)",
                f"https://finance.yahoo.com/quote/{fh_sym}/insider-transactions",
                date, f"סך מניות: {shares:,}", "Regulatory"
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
            out.extend(fetch_google(q, lang=lang, region=region))
        except Exception as e:
            print(f"    [Institutional news] {e}")
    return out

# ── Source: Cision Sweden ────────────────────────────────────────────────────
def fetch_cision(slug: str) -> list:
    try:
        feed = feedparser.parse(f"https://news.cision.com/{slug}/r/")
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
                date    = ev.get("date","")
                eps_est = ev.get("epsEstimate","")
                iid     = mid(f"earnings_{sym}_{date}")
                alerts.append({
                    "id":        iid,
                    "tid":       iid,
                    "title":     f"📅 דוח רווחים {date} — EPS צפוי: {eps_est}",
                    "source":    "Finnhub Earnings Calendar",
                    "link":      f"https://finance.yahoo.com/quote/{cfg['yahoo']}",
                    "published": datetime.now(timezone.utc).isoformat(),
                    "summary":   f"דוח רווחים מתוכנן ל-{sym}",
                    "layer":     "Regulatory",
                    "ticker":    sym,
                    "reason":    "דוח רווחים מתקרב",
                })
        except Exception as e:
            print(f"    [Earnings] {sym}: {e}")
    return alerts

# ── Source: Federal Reserve RSS ──────────────────────────────────────────────
def fetch_fed_rss() -> list:
    try:
        feed = feedparser.parse("https://www.federalreserve.gov/feeds/press_all.xml")
        out = []
        for e in feed.entries[:15]:
            out.append(make_item(
                e.get("id", e.get("link", e.get("title",""))),
                e.get("title",""), "Federal Reserve",
                e.get("link",""), e.get("published",""),
                e.get("summary",""), "Macro"
            ))
        return out
    except Exception as ex:
        print(f"    [Fed RSS] {ex}")
        return []

# ── Source: BEA (GDP / PCE / Trade) ──────────────────────────────────────────
def fetch_bea_rss() -> list:
    try:
        feed = feedparser.parse("https://apps.bea.gov/rss/rss.xml")
        out = []
        for e in feed.entries[:15]:
            out.append(make_item(
                e.get("link", e.get("title","")),
                e.get("title",""), "BEA (GDP/PCE/Trade)",
                e.get("link",""), e.get("published",""),
                e.get("summary",""), "Macro"
            ))
        return out
    except Exception as ex:
        print(f"    [BEA RSS] {ex}")
        return []

# ── Source: Finnhub Economic Calendar ────────────────────────────────────────
def fetch_finnhub_macro() -> list:
    try:
        r = requests.get(
            "https://finnhub.io/api/v1/calendar/economic",
            params={"token": FH_KEY},
            timeout=12
        )
        events = r.json().get("economicCalendar", [])
        out = []
        for ev in events[:80]:
            if not ev.get("actual"):
                continue
            if ev.get("country","") != "US":
                continue
            event_name = ev.get("event","")
            actual     = ev.get("actual","")
            estimate   = ev.get("estimate","")
            prev       = ev.get("prev","")
            event_time = ev.get("time","")

            beat_miss = ""
            try:
                def _num(s):
                    return float(str(s).replace("%","").replace("K","e3")
                                       .replace("M","e6").replace("B","e9"))
                if estimate:
                    diff = _num(actual) - _num(estimate)
                    beat_miss = "✅ עבר ציפיות" if diff > 0 else ("❌ מתחת לציפיות" if diff < 0 else "〰 בהתאם לציפיות")
            except:
                pass

            title = f"📊 {event_name}: {actual}"
            if beat_miss:
                title += f"  {beat_miss}"
            if estimate:
                title += f"  (צפוי: {estimate})"
            if prev:
                title += f"  | קודם: {prev}"

            out.append(make_item(
                mid(f"macro_{event_name}_{event_time}"),
                title, "Finnhub Economic Calendar",
                "https://finnhub.io/calendar/economic",
                event_time,
                f"בפועל: {actual} | צפוי: {estimate} | קודם: {prev}",
                "Macro"
            ))
        return out
    except Exception as ex:
        print(f"    [Finnhub Macro] {ex}")
        return []

# ── Source: Google News — macro keywords ─────────────────────────────────────
def fetch_macro_google() -> list:
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

# ── Groq ──────────────────────────────────────────────────────────────────────
def groq_post(model: str, prompt: str, max_tokens: int) -> str:
    r = requests.post(
        "https://api.groq.com/openai/v1/chat/completions",
        headers={"Authorization": f"Bearer {GROQ_KEY}", "Content-Type": "application/json"},
        json={"model": model,
              "messages": [{"role": "user", "content": prompt}],
              "max_tokens": max_tokens},
        timeout=25
    )
    return r.json()["choices"][0]["message"]["content"].strip()

def is_institutional_event(item: dict) -> bool:
    blob = (item.get("title","") + " " + item.get("summary","")).lower()
    return any(inst in blob for inst in INSTITUTIONAL_NAMES)

def classify(item: dict, sym: str, name: str) -> dict:
    if is_institutional_event(item):
        inst_matches = [i for i in INSTITUTIONAL_NAMES if i in
                        (item.get("title","") + item.get("summary","")).lower()]
        return {
            "urgency": "URGENT",
            "reason":  f"זוהתה פעילות מוסדית: {', '.join(inst_matches[:3])}"
        }
    prompt = (
        f"מסווג חדשות פיננסיות עבור {sym} ({name}).\n"
        f"כותרת: {item['title']}\nמקור: {item['source']} | שכבה: {item['layer']}\n"
        f"תקציר: {item.get('summary','')[:120]}\n\n"
        f"השב אך ורק ב-JSON: {{\"urgency\":\"URGENT|WATCH|FYI|IGNORE\",\"reason\":\"משפט אחד בעברית\"}}\n\n"
        f"URGENT = SEC / רווחים / מיזוג/רכישה / פשיטת רגל / שותפות מהותית / כניסת משקיע מוסדי\n"
        f"WATCH  = המלצת אנליסט, חדשות מוצר, שינוי הנהלה, מגמה רלוונטית\n"
        f"FYI    = ציוצים חברתיים, אזכור כללי\n"
        f"IGNORE = לא רלוונטי / ספאם"
    )
    try:
        raw = groq_post("llama-3.3-70b-versatile", prompt, 80)
        m = re.search(r'\{.*\}', raw, re.DOTALL)
        if m:
            return json.loads(m.group())
    except Exception as e:
        print(f"    [classify] {e}")
    return {"urgency": "WATCH", "reason": "סיווג לא זמין"}

def analyze_batch(items: list, sym: str, name: str) -> str:
    """ניתוח אחד מקיף לכל האירועים הדחופים של טיקר אחד."""
    items_text = "\n".join([
        f"- [{item['source']}] {item['title']}"
        for item in items[:6]
    ])
    prompt = (
        f"אנליסט buy-side. סכם את האירועים הבאים עבור {sym} ({name}) בעברית תמציתית ומקצועית.\n"
        f"מקסימום 450 תווים, טקסט רגיל.\n\n"
        f"אירועים חדשים:\n{items_text}\n\n"
        f"פורמט:\n"
        f"מה קרה: [משפט אחד מסכם את העיקר]\n"
        f"השפעה: שורי/דובי/ניטרלי — [הסבר קצר]\n"
        f"לעקוב: [נקודת מעקב אחת]\n\n"
        f"כתוב הכל בעברית. מונחים מקצועיים (SEC, EPS, M&A, CPI וכד') — השאר באנגלית."
    )
    try:
        return groq_post("llama-3.3-70b-versatile", prompt, 280)
    except Exception as e:
        return f"שגיאת ניתוח: {e}"

def analyze_macro_batch(items: list) -> str:
    """ניתוח מאקרו מקיף לכל הנתונים שפורסמו בריצה."""
    items_text = "\n".join([f"- {item['title']}" for item in items[:5]])
    prompt = (
        f"אנליסט buy-side. סכם את נתוני המאקרו הבאים בעברית תמציתית ומקצועית.\n"
        f"מקסימום 450 תווים.\n\n"
        f"נתונים שפורסמו:\n{items_text}\n\n"
        f"פורמט:\n"
        f"נתון: [משפט אחד מסכם]\n"
        f"השפעה: שורי/דובי/ניטרלי למניות — [הסבר קצר]\n"
        f"לעקוב: [מה לנטר הלאה]\n\n"
        f"כתוב בעברית. CPI, GDP, Fed, FOMC, PCE, NFP — השאר באנגלית."
    )
    try:
        return groq_post("llama-3.3-70b-versatile", prompt, 280)
    except Exception as e:
        return f"ניתוח לא זמין: {e}"

# ── Formatters ────────────────────────────────────────────────────────────────
LAYER_ICON = {"Regulatory": "🏛", "News": "📰", "Macro": "📊"}

def fmt_bundled_urgent(by_ticker: dict, now_str: str) -> str:
    """הודעה מקובצת אחת לכל האירועים הדחופים מהריצה הנוכחית."""
    total = sum(len(v["items"]) for v in by_ticker.values())
    lines = [f"🔴 <b>עדכון דחוף  {now_str} ישראל</b>  ({total} אירועים)\n"]
    for sym, data in sorted(by_ticker.items()):
        name  = TICKERS.get(sym, {}).get("name", "")
        items = data["items"]
        analysis = data["analysis"]
        lines.append(f"<b>━━ {sym} — {name} ━━</b>")
        for item in items[:4]:
            icon = LAYER_ICON.get(item.get("layer",""), "📡")
            lines.append(f"{icon} {item['title'][:120]}")
            if item.get("reason"):
                lines.append(f"<i>   {item['reason'][:80]}</i>")
        lines.append("")
        lines.append(analysis)
        lines.append("")
    return "\n".join(lines)

def fmt_bundled_macro(items: list, analysis: str, now_str: str) -> str:
    """הודעה מקובצת אחת לכל נתוני המאקרו מהריצה הנוכחית."""
    lines = [f"📊 <b>התראת מאקרו — כלכלת ארה\"ב  {now_str} ישראל</b>\n"]
    for item in items[:5]:
        lines.append(f"• <b>{item['title'][:150]}</b>")
        lines.append(f"  <i>{item['source']}</i>")
    lines.append("")
    lines.append(analysis)
    return "\n".join(lines)

def fmt_digest(items: list, now_str: str) -> str:
    if not items:
        return ""
    lines = [f"🟡 <b>תקציר שעתי  {now_str} ישראל</b>\n"]
    by_ticker: dict = {}
    for it in items:
        by_ticker.setdefault(it.get("ticker","?"), []).append(it)
    for sym, its in sorted(by_ticker.items()):
        name = TICKERS.get(sym,{}).get("name","")
        lines.append(f"<b>── {sym}  {name} ──</b>")
        for it in its[:5]:
            icon = LAYER_ICON.get(it.get("layer",""),"•")
            lines.append(f"{icon} {it['title'][:100]}")
            if it.get("reason"):
                lines.append(f"   <i>{it['reason']}</i>")
        lines.append("")
    lines.append(f"<i>{len(items)} פריטים מהתקציר האחרון</i>")
    return "\n".join(lines)

# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    now_il  = datetime.now(IL)
    now_str = now_il.strftime("%d/%m/%Y %H:%M")
    print(f"\n[{now_str}] ── Sonar v5 scan start ──")

    seen         = set(load_json(SEEN_FILE, []))
    digest_queue = load_json(DIGEST_FILE, [])
    last_digest  = load_json(LAST_DIGEST_FILE, {"hour_key": ""})
    new_seen     = set()
    urgent_by_ticker: dict = {}
    new_watch    = []

    # ── Per-ticker sources ────────────────────────────────────────────
    for sym, cfg in TICKERS.items():
        print(f"\n  [{sym}] fetching...")
        raw: list = []

        # מקורות ראשיים
        raw += fetch_google(cfg["google_q_en"])
        raw += fetch_yahoo(sym, cfg["yahoo"])
        raw += fetch_finnhub(cfg["finnhub"])

        # רגולטורי / מוסדי
        if cfg.get("sec"):
            raw += fetch_sec(sym)
            raw += fetch_sec_institutional(sym, cfg["name"])
            raw += fetch_finnhub_insider(cfg["finnhub"])
        raw += fetch_institutional_news(sym, cfg["name"], is_swedish=cfg.get("is_swedish", False))

        # שוודי
        if cfg.get("google_q_sv"):
            raw += fetch_google(cfg["google_q_sv"], lang="sv", region="SE")
        if cfg.get("cision_slug"):
            raw += fetch_cision(cfg["cision_slug"])

        print(f"  [{sym}] {len(raw)} raw → dedup + filter...")
        all_kw = cfg["keywords"] + [sym.split(".")[0].lower(),
                                    cfg["name"].lower().split()[0]]
        urgent_items = []
        new_cnt = 0

        for item in raw:
            iid = item["id"]
            tid = item.get("tid","")
            # dedup: גם לפי URL וגם לפי כותרת
            if iid in seen or iid in new_seen or tid in seen or tid in new_seen:
                continue
            new_seen.add(iid)
            if tid:
                new_seen.add(tid)
            if not is_recent(item.get("published",""), hours=24):
                continue
            if not kw_hit(item, all_kw):
                continue

            item["ticker"] = sym
            new_cnt += 1

            clf            = classify(item, sym, cfg["name"])
            urgency        = clf.get("urgency","WATCH")
            item["reason"] = clf.get("reason","")

            if urgency == "URGENT":
                urgent_items.append(item)
            elif urgency == "WATCH":
                new_watch.append(item)

        # ניתוח אחד מקיף לכל האירועים הדחופים של הטיקר
        if urgent_items:
            analysis = analyze_batch(urgent_items, sym, cfg["name"])
            urgent_by_ticker[sym] = {"items": urgent_items, "analysis": analysis}

        print(f"  [{sym}] {new_cnt} חדשים → {len(urgent_items)} דחופים")

    # ── Earnings ──────────────────────────────────────────────────────
    for item in fetch_earnings_alerts():
        iid = item["id"]
        if iid in seen or iid in new_seen:
            continue
        new_seen.add(iid)
        sym = item.get("ticker", list(TICKERS.keys())[0])
        if sym not in urgent_by_ticker:
            analysis = analyze_batch([item], sym, TICKERS.get(sym,{}).get("name",""))
            urgent_by_ticker[sym] = {"items": [item], "analysis": analysis}
        else:
            urgent_by_ticker[sym]["items"].append(item)

    # ── US Macro ──────────────────────────────────────────────────────
    print("\n  [MACRO] scanning...")
    macro_seen     = set(load_json(MACRO_SEEN_FILE, []))
    new_macro_seen = set()
    macro_urgent   = []

    for item in fetch_fed_rss() + fetch_bea_rss() + fetch_finnhub_macro() + fetch_macro_google():
        iid = item["id"]
        tid = item.get("tid","")
        if iid in macro_seen or iid in new_macro_seen or tid in macro_seen or tid in new_macro_seen:
            continue
        new_macro_seen.add(iid)
        if tid:
            new_macro_seen.add(tid)
        if not is_recent(item.get("published",""), hours=36):
            continue
        if not is_macro_event(item):
            continue
        macro_urgent.append(item)

    save_json(MACRO_SEEN_FILE, list((macro_seen | new_macro_seen))[-3000:])
    print(f"  [MACRO] {len(macro_urgent)} נתוני מאקרו חדשים")

    # ── שליחה: הודעה מקובצת אחת לדחופים ────────────────────────────
    if urgent_by_ticker:
        tg_send(fmt_bundled_urgent(urgent_by_ticker, now_il.strftime("%d/%m %H:%M")))
        total = sum(len(v["items"]) for v in urgent_by_ticker.values())
        print(f"  🔴 הודעה מקובצת → Telegram ({total} אירועים ב-{len(urgent_by_ticker)} טיקרים)")

    if macro_urgent:
        analysis = analyze_macro_batch(macro_urgent)
        tg_send(fmt_bundled_macro(macro_urgent, analysis, now_il.strftime("%d/%m %H:%M")))
        print(f"  📊 מאקרו מקובץ → Telegram ({len(macro_urgent)} נתונים)")

    # ── תקציר שעתי ───────────────────────────────────────────────────
    digest_queue.extend(new_watch)
    digest_queue = digest_queue[-200:]

    hour_key = now_il.strftime("%Y-%m-%d-%H")
    if (now_il.minute <= 8
            and last_digest.get("hour_key") != hour_key
            and len(digest_queue) > 0):
        msg = fmt_digest(digest_queue, now_il.strftime("%d/%m %H:%M"))
        if msg:
            tg_send(msg)
            print(f"  🟡 תקציר שעתי → Telegram ({len(digest_queue)} פריטים)")
        digest_queue = []
        save_json(LAST_DIGEST_FILE, {"hour_key": hour_key})
    elif not urgent_by_ticker and not macro_urgent:
        print("  ✓ אין פריטים חדשים — שקט")

    # ── שמירת מצב ────────────────────────────────────────────────────
    save_json(SEEN_FILE,   list((seen | new_seen))[-6000:])
    save_json(DIGEST_FILE, digest_queue)
    print(f"\n[{now_str}] סיום")

if __name__ == "__main__":
    main()
