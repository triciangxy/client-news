#!/usr/bin/env python3
"""
fetch_news.py — runs on GitHub Actions to fetch quotes + news for all clients,
writes consolidated data.json which the static dashboard reads.

Hybrid sourcing per client:
  - exchange: regulatory filings (HKEX, TDnet, KRX) where available
  - rss: official RSS feeds where the company publishes one
  - domain-news: Google News filtered to the company's own domain via site:
  - general-news: broad Google News search (last-resort fallback)

Output: data.json with shape:
  {
    "generated_at": "2026-05-24T13:55:00Z",
    "quotes": { "1299.HK": {price, change, changePct, ...}, ... },
    "news":   { "AIA": {source: {...}, items: [...]}, ... }
  }
"""

import json
import sys
import time
from datetime import datetime, timezone
from urllib.parse import quote_plus
import xml.etree.ElementTree as ET

import requests

UA = "Mozilla/5.0 (compatible; ClientPulse/1.0; +https://github.com)"
TIMEOUT = 20

# ── Client roster ─────────────────────────────────────────────────────────
CLIENTS = [
    {
        "name": "AIA Group", "short": "AIA", "type": "listed", "country": "Hong Kong",
        "ticker": "1299.HK", "aum": 328, "earnings": "2026-08-21",
        "blurb": "Largest independent pan-Asian life insurer; HKEX-listed, US$328bn total assets.",
        "newsroom": "https://www.aia.com/en/media-centre/press-releases",
        "sources": [
            {"type": "domain-news", "label": "aia.com", "kind": "google-news",
             "query": "site:aia.com"},
        ],
    },
    {
        "name": "Shinhan Financial Group", "short": "Shinhan", "type": "listed", "country": "South Korea",
        "ticker": "055550.KS", "aum": 500, "earnings": "2026-07-25",
        "blurb": "Korea's largest financial holding group by assets, KRX & NYSE dual-listed.",
        "newsroom": "https://www.shinhangroup.com/en/pr/news/list.do",
        "sources": [
            {"type": "domain-news", "label": "shinhangroup.com", "kind": "google-news",
             "query": "site:shinhangroup.com OR site:shinhan.com"},
        ],
    },
    {
        "name": "Sumitomo Mitsui Trust", "short": "SMTB", "type": "listed", "country": "Japan",
        "ticker": "8309.T", "aum": 624, "earnings": "2026-08-08",
        "blurb": "Japan's largest trust bank; ~US$1tn in total assets, manages Nikko Asset Mgmt.",
        "newsroom": "https://www.smtg.jp/english/news",
        "sources": [
            {"type": "domain-news", "label": "smtg.jp", "kind": "google-news",
             "query": "site:smtg.jp OR site:smtb.jp"},
        ],
    },
    {
        "name": "Japan Post Bank", "short": "JPB", "type": "listed", "country": "Japan",
        "ticker": "7182.T", "aum": 1750, "earnings": "2026-08-13",
        "blurb": "One of the world's largest deposit banks; \u00a5229tn AUM, partially gov-owned.",
        "newsroom": "https://www.jp-bank.japanpost.jp/en/news/en_news_index.html",
        "sources": [
            {"type": "domain-news", "label": "jp-bank.japanpost.jp", "kind": "google-news",
             "query": "site:jp-bank.japanpost.jp"},
        ],
    },
    {
        "name": "Temasek Holdings", "short": "Temasek", "type": "sovereign", "country": "Singapore",
        "aum": 320,
        "blurb": "Singapore state investment company; S$434bn net portfolio as of FY2025.",
        "newsroom": "https://www.temasek.com.sg/en/news-and-resources/news-room",
        "sources": [
            {"type": "domain-news", "label": "temasek.com.sg", "kind": "google-news",
             "query": "site:temasek.com.sg"},
        ],
    },
    {
        "name": "GIC", "short": "GIC", "type": "sovereign", "country": "Singapore",
        "aum": 770,
        "blurb": "Manages Singapore's foreign reserves; one of the world's largest SWFs.",
        "newsroom": "https://www.gic.com.sg/newsroom/",
        "sources": [
            {"type": "domain-news", "label": "gic.com.sg", "kind": "google-news",
             "query": "site:gic.com.sg"},
        ],
    },
    {
        "name": "China Investment Corp.", "short": "CIC", "type": "sovereign", "country": "China",
        "aum": 1330,
        "blurb": "China's sovereign wealth fund managing overseas reserves; ~US$1.3tn AUM.",
        "newsroom": "http://www.china-inv.cn/en/News/",
        "sources": [
            {"type": "domain-news", "label": "china-inv.cn", "kind": "google-news",
             "query": "site:china-inv.cn"},
        ],
    },
    {
        "name": "Korea Investment Corp.", "short": "KIC", "type": "sovereign", "country": "South Korea",
        "aum": 190,
        "blurb": "Korea's sovereign wealth fund; manages MOEF & BOK assets.",
        "newsroom": "https://www.kic.kr/en/cm/cntnts/cntntsView.do?mi=1175&cntntsId=1141",
        "sources": [
            {"type": "domain-news", "label": "kic.kr", "kind": "google-news",
             "query": "site:kic.kr"},
        ],
    },
    {
        "name": "National Pension Service", "short": "NPS", "type": "pension", "country": "South Korea",
        "aum": 870,
        "blurb": "World's 3rd-largest pension fund; ~US$870bn AUM.",
        "newsroom": "https://fund.nps.or.kr/jsppage/fund/mcs_e/mcs_e_03_01.jsp",
        "sources": [
            {"type": "domain-news", "label": "nps.or.kr", "kind": "google-news",
             "query": "site:nps.or.kr"},
        ],
    },
    {
        "name": "Hostplus", "short": "Hostplus", "type": "pension", "country": "Australia",
        "aum": 90,
        "blurb": "Australian industry super fund; A$130bn+ FUM.",
        "newsroom": "https://hostplus.com.au/about/media",
        "sources": [
            {"type": "domain-news", "label": "hostplus.com.au", "kind": "google-news",
             "query": "site:hostplus.com.au"},
        ],
    },
    {
        "name": "TCorp", "short": "TCorp", "type": "pension", "country": "Australia",
        "aum": 80,
        "blurb": "NSW Treasury Corp; central financing & investment arm for NSW gov.",
        "newsroom": "https://www.tcorp.nsw.gov.au/news",
        "sources": [
            {"type": "domain-news", "label": "tcorp.nsw.gov.au", "kind": "google-news",
             "query": "site:tcorp.nsw.gov.au"},
        ],
    },
    {
        "name": "Nippon Life Insurance", "short": "Nissay", "type": "insurance", "country": "Japan",
        "aum": 580,
        "blurb": "Japan's largest private life insurer (mutual); \u00a580tn+ in total assets.",
        "newsroom": "https://www.nissay.co.jp/english/news/",
        "sources": [
            {"type": "domain-news", "label": "nissay.co.jp", "kind": "google-news",
             "query": "site:nissay.co.jp"},
        ],
    },
    {
        "name": "FWD Group", "short": "FWD", "type": "insurance", "country": "Hong Kong",
        "aum": 80,
        "blurb": "Pan-Asian life insurer backed by Richard Li; 10+ markets, ~US$80bn AUM.",
        "newsroom": "https://www.fwd.com/about-fwd/newsroom/",
        "sources": [
            {"type": "domain-news", "label": "fwd.com", "kind": "google-news",
             "query": "site:fwd.com"},
        ],
    },
    {
        "name": "Norinchukin Bank", "short": "Nochu", "type": "bank", "country": "Japan",
        "aum": 690,
        "blurb": "Central cooperative bank for Japan's agriculture & fishery coops; \u00a5100tn+.",
        "newsroom": "https://www.nochubank.or.jp/en/news/index.html",
        "sources": [
            {"type": "domain-news", "label": "nochubank.or.jp", "kind": "google-news",
             "query": "site:nochubank.or.jp"},
        ],
    },
]

session = requests.Session()
session.headers.update({"User-Agent": UA})


def fetch_quote_chart(symbol):
    """Yahoo Finance chart endpoint. Public, no auth needed.
    Returns dict matching the prior v7 shape, computed from meta + prev close."""
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
    r = session.get(url, timeout=TIMEOUT)
    r.raise_for_status()
    data = r.json()
    results = data.get("chart", {}).get("result")
    if not results:
        return None
    meta = results[0].get("meta", {})
    price = meta.get("regularMarketPrice")
    prev = meta.get("chartPreviousClose") or meta.get("previousClose")
    change = (price - prev) if (price is not None and prev is not None) else None
    change_pct = (change / prev * 100) if (change is not None and prev) else None
    return {
        "price": price,
        "change": change,
        "changePct": change_pct,
        "currency": meta.get("currency"),
        "prevClose": prev,
        "dayHigh": meta.get("regularMarketDayHigh"),
        "dayLow": meta.get("regularMarketDayLow"),
        "marketState": meta.get("marketState") or "",
        "exchange": meta.get("fullExchangeName") or meta.get("exchangeName"),
    }


def fetch_quotes(symbols):
    """Iterate per-symbol via the chart endpoint (no auth required)."""
    out = {}
    for sym in symbols:
        try:
            q = fetch_quote_chart(sym)
            if q:
                out[sym] = q
        except Exception as e:
            print(f"  ! quote fetch failed for {sym}: {e}", file=sys.stderr)
        time.sleep(0.2)
    return out


def parse_rss(xml_bytes):
    """Parse RSS or Atom; return list of {title, source, url, pubDate}."""
    items = []
    try:
        # Strip BOM if present
        text = xml_bytes.decode("utf-8", errors="replace").lstrip("\ufeff")
        root = ET.fromstring(text)
    except Exception as e:
        print(f"  ! XML parse failed: {e}", file=sys.stderr)
        return items

    # RSS 2.0: items under channel
    for it in root.iter("item"):
        title_raw = (it.findtext("title") or "").strip()
        link = (it.findtext("link") or "").strip()
        pub = (it.findtext("pubDate") or "").strip()
        # Google News titles end with " - Publisher Name"
        source = ""
        title = title_raw
        if " - " in title_raw:
            parts = title_raw.rsplit(" - ", 1)
            if len(parts) == 2 and len(parts[1]) < 80:
                title, source = parts[0].strip(), parts[1].strip()
        if title:
            items.append({"title": title, "source": source, "url": link, "pubDate": pub})

    # Atom fallback
    if not items:
        ns = {"a": "http://www.w3.org/2005/Atom"}
        for entry in root.iter("{http://www.w3.org/2005/Atom}entry"):
            title = (entry.findtext("a:title", namespaces=ns) or "").strip()
            link_el = entry.find("a:link", namespaces=ns)
            link = link_el.attrib.get("href", "") if link_el is not None else ""
            pub = (entry.findtext("a:updated", namespaces=ns)
                   or entry.findtext("a:published", namespaces=ns) or "").strip()
            if title:
                items.append({"title": title, "source": "", "url": link, "pubDate": pub})

    return items[:10]


def fetch_google_news(query):
    url = (f"https://news.google.com/rss/search?q={quote_plus(query)}"
           "&hl=en-US&gl=US&ceid=US:en")
    r = session.get(url, timeout=TIMEOUT)
    r.raise_for_status()
    return parse_rss(r.content)


def fetch_rss(url):
    r = session.get(url, timeout=TIMEOUT)
    r.raise_for_status()
    return parse_rss(r.content)


def fetch_client_news(client):
    """Try each source in order until one returns items."""
    for src in client["sources"]:
        try:
            if src["kind"] == "google-news":
                items = fetch_google_news(src["query"])
            elif src["kind"] == "rss":
                items = fetch_rss(src["url"])
            else:
                continue
            if items:
                return {"source": src, "items": items}
        except Exception as e:
            print(f"  ! {client['short']} / {src['label']} failed: {e}", file=sys.stderr)
            continue
        # be polite between attempts
        time.sleep(0.2)
    return {"source": None, "items": []}


def main():
    print(f"Starting fetch at {datetime.now(timezone.utc).isoformat()}")

    tickers = [c["ticker"] for c in CLIENTS if c.get("ticker")]
    print(f"Fetching {len(tickers)} quotes...")
    quotes = fetch_quotes(tickers)
    print(f"  → got {len(quotes)} quotes")

    news = {}
    for c in CLIENTS:
        print(f"Fetching news for {c['short']} ...")
        news[c["short"]] = fetch_client_news(c)
        n = len(news[c["short"]]["items"])
        src = news[c["short"]]["source"]
        src_label = src["label"] if src else "NONE"
        print(f"  → {n} items from {src_label}")
        time.sleep(0.5)  # rate-limit politeness for Google News

    output = {
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "clients": CLIENTS,  # ship the roster with the data so the frontend stays simple
        "quotes": quotes,
        "news": news,
    }

    with open("data.json", "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)

    print(f"Wrote data.json ({sum(len(v['items']) for v in news.values())} total headlines)")


if __name__ == "__main__":
    main()
