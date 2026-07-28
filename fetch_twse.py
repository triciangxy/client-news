#!/usr/bin/env python3
"""
fetch_twse.py — builds prospects/data.json for the Taiwan Prospect Radar.

Runs on GitHub Actions. Two layers are combined:

  SLOW layer  prospects/roster.json — curated: who controls which TWSE issuer,
              roughly what share they hold, and how they should be approached.
  LIVE layer  TWSE open data — closing prices, shares outstanding, valuation
              ratios, monthly revenue; plus USD/TWD from the Yahoo chart endpoint.

The live layer is multiplied against the curated stakes to produce a wealth
estimate per subject, aggregated across every line that subject holds.

Sources (all public, no auth):
  openapi.twse.com.tw/v1/exchangeReport/STOCK_DAY_ALL   daily OHLC, all listed
  openapi.twse.com.tw/v1/opendata/t187ap03_L            company profile + capital
  openapi.twse.com.tw/v1/exchangeReport/BWIBBU_ALL      PE / PB / dividend yield
  openapi.twse.com.tw/v1/opendata/t187ap05_L            monthly revenue + YoY
  openapi.twse.com.tw/v1/exchangeReport/TWT48U_ALL      ex-dividend results
  mopsov.twse.com.tw  (best effort)                     insider holdings + pledges
  query1.finance.yahoo.com                              TWD=X

Every source is isolated: if one fails the run still produces a file, and the
failure is recorded in data.json under "diagnostics" so the UI can show it.

Usage:
  python fetch_twse.py                      # normal run, writes prospects/data.json
  python fetch_twse.py --out /tmp/x.json    # write elsewhere
  python fetch_twse.py --synthetic          # no network; deterministic fake prices
                                            # for local layout work. Never commit.
"""

import argparse
import json
import math
import os
import re
import sys
import time
from datetime import date, datetime, timedelta, timezone

import requests

UA = "Mozilla/5.0 (compatible; TWProspectRadar/1.0; +https://github.com)"
TIMEOUT = 30
ROSTER_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "prospects", "roster.json")
DEFAULT_OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "prospects", "data.json")

TWSE_API = "https://openapi.twse.com.tw/v1"

session = requests.Session()
session.headers.update({"User-Agent": UA, "Accept": "application/json"})

diagnostics = {"sources": {}, "warnings": []}


def note_source(name, ok, detail=""):
    diagnostics["sources"][name] = {"ok": bool(ok), "detail": detail}
    mark = "ok " if ok else "FAIL"
    print(f"  [{mark}] {name} {detail}", file=sys.stderr)


def warn(msg):
    diagnostics["warnings"].append(msg)
    print(f"  ! {msg}", file=sys.stderr)


# ── generic helpers ───────────────────────────────────────────────────────

def get_json(url, name, params=None):
    """GET returning parsed JSON, or None. Never raises."""
    try:
        r = session.get(url, params=params, timeout=TIMEOUT)
        r.raise_for_status()
        data = r.json()
        note_source(name, True, f"{len(data) if isinstance(data, list) else 'obj'} rows")
        return data
    except Exception as e:
        note_source(name, False, str(e)[:160])
        return None


def pick(row, *keys):
    """First present, non-empty value among candidate keys. TWSE renames fields
    between dataset revisions, so every read goes through here."""
    for k in keys:
        if k in row:
            v = row[k]
            if v not in (None, "", "-", "--"):
                return v
    return None


def to_float(v):
    """TWSE numbers arrive as strings with commas, and sometimes as '--'."""
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).strip().replace(",", "").replace("%", "")
    if s in ("", "-", "--", "N/A"):
        return None
    try:
        return float(s)
    except ValueError:
        return None


def logscale(value, floor, ceiling):
    """0..1 on a log ramp. Below floor -> 0, above ceiling -> 1."""
    if value is None or value <= 0:
        return 0.0
    if value <= floor:
        return 0.0
    if value >= ceiling:
        return 1.0
    return (math.log(value) - math.log(floor)) / (math.log(ceiling) - math.log(floor))


# ── TWSE sources ──────────────────────────────────────────────────────────

def fetch_quotes():
    """Latest close for every listed stock. Keyed by 4-digit code."""
    rows = get_json(f"{TWSE_API}/exchangeReport/STOCK_DAY_ALL", "twse:STOCK_DAY_ALL")
    out = {}
    for r in rows or []:
        code = pick(r, "Code", "證券代號")
        close = to_float(pick(r, "ClosingPrice", "收盤價"))
        if not code or close is None:
            continue
        change = to_float(pick(r, "Change", "漲跌價差"))
        prev = close - change if change is not None else None
        out[str(code).strip()] = {
            "price": close,
            "change": change,
            "changePct": (change / prev * 100) if (change is not None and prev) else None,
            "open": to_float(pick(r, "OpeningPrice", "開盤價")),
            "high": to_float(pick(r, "HighestPrice", "最高價")),
            "low": to_float(pick(r, "LowestPrice", "最低價")),
            "volume": to_float(pick(r, "TradeVolume", "成交股數")),
            "turnover": to_float(pick(r, "TradeValue", "成交金額")),
        }
    return out


def fetch_company_info():
    """Company profile incl. paid-in capital, from which shares outstanding is
    derived (paid-in capital / par value). Keyed by 4-digit code."""
    rows = get_json(f"{TWSE_API}/opendata/t187ap03_L", "twse:t187ap03_L (company profile)")
    out = {}
    for r in rows or []:
        code = pick(r, "公司代號", "Code")
        if not code:
            continue
        capital = to_float(pick(r, "實收資本額", "PaidInCapital"))
        par_raw = pick(r, "普通股每股面額", "ParValue") or ""
        m = re.search(r"(\d+(?:\.\d+)?)", str(par_raw))
        par = float(m.group(1)) if m else 10.0
        if par <= 0:
            par = 10.0
        shares = (capital / par) if capital else None
        out[str(code).strip()] = {
            "name_zh": pick(r, "公司名稱", "CompanyName"),
            "short_zh": pick(r, "公司簡稱", "Abbreviation"),
            "name_en": pick(r, "英文簡稱", "EnglishAbbreviation", "英文公司名稱"),
            "industry": pick(r, "產業別", "Industry"),
            "chairman": pick(r, "董事長", "Chairman"),
            "president": pick(r, "總經理", "President"),
            "spokesperson": pick(r, "發言人", "Spokesperson"),
            "listed_date": pick(r, "上市日期", "ListingDate"),
            "website": pick(r, "網址", "WebSite"),
            "paid_in_capital": capital,
            "par_value": par,
            "shares_out": shares,
        }
    return out


def fetch_ratios():
    """PE / PB / dividend yield per stock. Yield is what we need — trailing DPS
    is recovered as price * yield / 100."""
    rows = get_json(f"{TWSE_API}/exchangeReport/BWIBBU_ALL", "twse:BWIBBU_ALL (valuation)")
    out = {}
    for r in rows or []:
        code = pick(r, "Code", "證券代號")
        if not code:
            continue
        out[str(code).strip()] = {
            "pe": to_float(pick(r, "PEratio", "本益比")),
            "pb": to_float(pick(r, "PBratio", "股價淨值比")),
            "divYield": to_float(pick(r, "DividendYield", "殖利率(%)", "YieldRatio")),
        }
    return out


def fetch_monthly_revenue():
    """Taiwan uniquely publishes company revenue monthly, on the 10th. The YoY
    number is the freshest fundamental signal available anywhere on this market."""
    rows = get_json(f"{TWSE_API}/opendata/t187ap05_L", "twse:t187ap05_L (monthly revenue)")
    out = {}
    for r in rows or []:
        code = pick(r, "公司代號", "Code")
        if not code:
            continue
        out[str(code).strip()] = {
            "period": pick(r, "資料年月", "Period"),
            "revenue": to_float(pick(r, "營業收入-當月營收", "Revenue")),
            "yoy": to_float(pick(r, "營業收入-去年同月增減(%)", "YoY")),
            "mom": to_float(pick(r, "營業收入-上月比較增減(%)", "MoM")),
            "cum_yoy": to_float(pick(r, "累計營業收入-前期比較增減(%)", "CumulativeYoY")),
        }
    return out


def fetch_ex_dividend():
    """Recent ex-dividend events. Taiwan concentrates almost all cash dividends
    into Jul-Sep, so this is the cash-in-hand calendar for the whole market."""
    rows = get_json(f"{TWSE_API}/exchangeReport/TWT48U_ALL", "twse:TWT48U_ALL (ex-dividend)")
    out = {}
    for r in rows or []:
        code = pick(r, "Code", "股票代號", "SecuritiesCompanyCode")
        if not code:
            continue
        out[str(code).strip()] = {
            "date": pick(r, "Date", "資料日期"),
            "cash": to_float(pick(r, "CashDividend", "息值", "權值+息值")),
        }
    return out


def fetch_fx():
    """USD/TWD from the Yahoo chart endpoint — same endpoint the sibling
    dashboard already relies on."""
    try:
        r = session.get("https://query1.finance.yahoo.com/v8/finance/chart/TWD=X", timeout=TIMEOUT)
        r.raise_for_status()
        meta = r.json()["chart"]["result"][0]["meta"]
        rate = meta.get("regularMarketPrice")
        if rate and rate > 1:
            note_source("yahoo:TWD=X", True, f"{rate:.3f}")
            return float(rate)
        raise ValueError(f"implausible rate {rate}")
    except Exception as e:
        note_source("yahoo:TWD=X", False, str(e)[:160])
        return None


MOPS_INSIDER_URL = "https://mopsov.twse.com.tw/mops/web/ajax_stapap1"


def fetch_insiders(code, roc_year, month):
    """Best effort: MOPS monthly insider holding report (內部人持股餘額).

    This is the one source that gives filings-grade per-person share counts and
    pledge ratios. It is an HTML endpoint with no stable contract, so it is
    treated as enrichment only — when it works the curated stake is replaced and
    the row is marked filings-sourced; when it does not, the curated estimate
    stands and the row stays marked as an estimate.
    """
    try:
        r = session.post(
            MOPS_INSIDER_URL,
            data={
                "encodeURIComponent": "1", "step": "1", "firstin": "1", "off": "1",
                "TYPEK": "sii", "year": str(roc_year), "month": f"{month:02d}", "co_id": code,
            },
            headers={"Content-Type": "application/x-www-form-urlencoded",
                     "Accept": "text/html,application/xhtml+xml"},
            timeout=TIMEOUT,
        )
        r.raise_for_status()
        html = r.text
        rows = []
        for tr in re.findall(r"<tr[^>]*>(.*?)</tr>", html, re.S):
            cells = [re.sub(r"<[^>]+>", "", c).replace("&nbsp;", " ").strip()
                     for c in re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>", tr, re.S)]
            if len(cells) < 5:
                continue
            title, name = cells[0], cells[1]
            if not name or "姓名" in name or "職稱" in title:
                continue
            held = to_float(cells[3]) if len(cells) > 3 else None
            pledged = to_float(cells[5]) if len(cells) > 5 else None
            if held is None:
                continue
            rows.append({
                "title": title, "name": name, "shares": held, "pledged": pledged,
                "pledge_pct": (pledged / held * 100) if (pledged and held) else 0.0,
            })
        return rows
    except Exception as e:
        warn(f"MOPS insiders unavailable for {code}: {str(e)[:100]}")
        return []


# ── wealth model ──────────────────────────────────────────────────────────

def build(roster, live, fx_rate):
    a = roster["assumptions"]
    sc = roster["scoring"]
    band = a["range_band_pct"] / 100.0
    twd_per_usd = fx_rate or a["twd_per_usd_fallback"]

    quotes = live["quotes"]
    info = live["info"]
    ratios = live["ratios"]
    revenue = live["revenue"]
    exdiv = live["exdiv"]
    insiders = live["insiders"]

    def usd_m(ntd):
        return (ntd / twd_per_usd / 1e6) if ntd else 0.0

    # ── companies ──
    companies = []
    by_ticker = {}
    for c in roster["companies"]:
        t = c["ticker"]
        q = quotes.get(t, {})
        i = info.get(t, {})
        r = ratios.get(t, {})
        rev = revenue.get(t, {})
        price = q.get("price")
        shares = i.get("shares_out")
        mcap = price * shares if (price and shares) else None
        dy = r.get("divYield")
        dps = (price * dy / 100.0) if (price and dy) else None
        row = {
            "ticker": t,
            "name_zh": c["name_zh"],
            "name_en": c["name_en"],
            "sector": c["sector"],
            "industry": i.get("industry"),
            "chairman": i.get("chairman"),
            "president": i.get("president"),
            "website": i.get("website"),
            "price": price,
            "change": q.get("change"),
            "changePct": q.get("changePct"),
            "turnover": q.get("turnover"),
            "shares_out": shares,
            "market_cap_ntd": mcap,
            "market_cap_usd_m": usd_m(mcap),
            "pe": r.get("pe"),
            "pb": r.get("pb"),
            "div_yield": dy,
            "dps": dps,
            "revenue_period": rev.get("period"),
            "revenue_yoy": rev.get("yoy"),
            "ex_dividend": exdiv.get(t),
            "principal_count": len(c.get("principals", [])),
            "has_live_price": price is not None,
            "has_share_count": shares is not None,
        }
        companies.append(row)
        by_ticker[t] = row

    # ── subjects: aggregate every holding a person or family controls ──
    subjects = {}
    for c in roster["companies"]:
        t = c["ticker"]
        comp = by_ticker[t]
        for p in c.get("principals", []):
            sid = p["subject_id"]
            s = subjects.setdefault(sid, {
                "subject_id": sid,
                "name_zh": p["name_zh"], "name_en": p["name_en"],
                "subject_type": p["subject_type"],
                "role_zh": p["role_zh"], "role_en": p["role_en"],
                "archetype": p["archetype"], "base": p.get("base"),
                "vehicles": [], "tags": [], "notes": [], "holdings": [],
                "confidence": p.get("confidence", "low"),
            })
            for v in p.get("vehicles", []):
                if v not in s["vehicles"]:
                    s["vehicles"].append(v)
            for tag in p.get("tags", []):
                if tag not in s["tags"]:
                    s["tags"].append(tag)
            if p.get("note"):
                s["notes"].append({"ticker": t, "text": p["note"]})

            stake = p["stake_pct_est"]
            stake_source = "curated-estimate"
            pledge_pct = None

            # MOPS override, when the filings scrape produced something usable
            for ins in insiders.get(t, []):
                if p["name_zh"] and p["name_zh"] in ins["name"] and comp["shares_out"]:
                    filed = ins["shares"] / comp["shares_out"] * 100.0
                    if 0 < filed < 100:
                        stake = filed
                        stake_source = "mops-filing"
                        pledge_pct = ins["pledge_pct"]
                    break

            price = comp["price"]
            shares_out = comp["shares_out"]
            shares_held = shares_out * stake / 100.0 if shares_out else None
            value = shares_held * price if (shares_held and price) else None
            dps = comp["dps"]
            div = shares_held * dps if (shares_held and dps) else None
            day_move = shares_held * comp["change"] if (shares_held and comp["change"] is not None) else None

            s["holdings"].append({
                "ticker": t,
                "name_zh": comp["name_zh"], "name_en": comp["name_en"],
                "sector": comp["sector"],
                "stake_pct": stake,
                "stake_basis": p.get("stake_basis"),
                "stake_source": stake_source,
                "as_of": p.get("as_of"),
                "confidence": p.get("confidence", "low"),
                "shares_held": shares_held,
                "value_ntd": value,
                "value_usd_m": usd_m(value),
                "dividend_ntd": div,
                "day_move_ntd": day_move,
                "pledge_pct": pledge_pct,
                "price": price,
                "changePct": comp["changePct"],
                "div_yield": comp["div_yield"],
                "revenue_yoy": comp["revenue_yoy"],
                "priced": value is not None,
            })

    # ── derive wealth, signals, score ──
    out = []
    today = date.today()
    dividend_season = today.month in (6, 7, 8, 9)

    for s in subjects.values():
        holdings = sorted(s["holdings"], key=lambda h: h["value_ntd"] or 0, reverse=True)
        s["holdings"] = holdings
        priced = [h for h in holdings if h["priced"]]

        stake_value = sum(h["value_ntd"] for h in priced)
        gross_div = sum(h["dividend_ntd"] or 0 for h in priced)
        day_move = sum(h["day_move_ntd"] or 0 for h in priced)

        # archetype of the largest priced line drives the uplift
        archetype = (priced[0]["ticker"] and s["archetype"]) if priced else s["archetype"]
        uplift = a["unlisted_uplift"].get(archetype, 1.4)

        mid = stake_value * uplift
        net_div = gross_div * (1 - a["dividend_tax_rate"])
        bankable = net_div * a["bankable_share_of_dividend"]

        pledges = [h["pledge_pct"] for h in holdings if h["pledge_pct"] is not None]
        pledge_pct = max(pledges) if pledges else None

        top_share = (priced[0]["value_ntd"] / stake_value) if (priced and stake_value) else None

        nw_usd_m = usd_m(mid)
        bank_usd_m = usd_m(bankable)

        # ── signals: what a banker would actually open the conversation with ──
        signals = []
        if dividend_season and gross_div > 0:
            signals.append({
                "kind": "dividend", "weight": 1.0,
                "text_en": f"Dividend season — NT${gross_div/1e9:,.2f}bn gross across {len(priced)} line(s), "
                           f"roughly US${bank_usd_m:,.0f}m redeployable after tax",
                "text_zh": f"除息旺季 — {len(priced)} 檔合計股利約 NT${gross_div/1e8:,.1f} 億元，"
                           f"稅後可再配置約 US${bank_usd_m:,.0f} 百萬",
            })
        elif gross_div > 0:
            signals.append({
                "kind": "dividend", "weight": 0.45,
                "text_en": f"Annual dividend run-rate NT${gross_div/1e9:,.2f}bn gross "
                           f"(US${bank_usd_m:,.0f}m redeployable after tax)",
                "text_zh": f"年度股利水準約 NT${gross_div/1e8:,.1f} 億元（稅後可再配置 US${bank_usd_m:,.0f} 百萬）",
            })

        if abs(day_move) > 5e8:  # NT$500m paper move in a single session
            direction_en = "gained" if day_move > 0 else "lost"
            direction_zh = "增加" if day_move > 0 else "減少"
            signals.append({
                "kind": "move", "weight": 0.6,
                "text_en": f"Paper wealth {direction_en} NT${abs(day_move)/1e9:,.2f}bn in the latest session",
                "text_zh": f"最近交易日紙上財富{direction_zh} NT${abs(day_move)/1e8:,.1f} 億元",
            })

        hot = [h for h in holdings if (h["revenue_yoy"] or 0) > 25]
        if hot:
            signals.append({
                "kind": "fundamental", "weight": 0.5,
                "text_en": f"{hot[0]['name_en']} monthly revenue +{hot[0]['revenue_yoy']:.0f}% YoY — "
                           f"earnings momentum ahead of the next dividend cycle",
                "text_zh": f"{hot[0]['name_zh']} 月營收年增 {hot[0]['revenue_yoy']:.0f}%，"
                           f"下一個配息循環的獲利動能",
            })

        if pledge_pct and pledge_pct > 20:
            signals.append({
                "kind": "pledge", "weight": 0.7,
                "text_en": f"{pledge_pct:.0f}% of filed holdings pledged — an existing lending "
                           f"relationship sits elsewhere; refinancing is the wedge",
                "text_zh": f"申報持股質押比 {pledge_pct:.0f}%，既有融資關係在他行，可從再融資切入",
            })

        if top_share and top_share > 0.85 and len(priced) >= 1 and nw_usd_m > 200:
            signals.append({
                "kind": "concentration", "weight": 0.55,
                "text_en": f"{top_share*100:.0f}% of measured wealth sits in a single listed line — "
                           f"concentration and hedging conversation",
                "text_zh": f"可衡量財富有 {top_share*100:.0f}% 集中於單一上市持股，集中度與避險議題",
            })

        if "liquidity-event" in s["tags"]:
            signals.append({"kind": "event", "weight": 0.8,
                            "text_en": "Recent corporate action created a realised liquidity event",
                            "text_zh": "近期公司行動帶來實現的流動性事件"})
        if "succession-open" in s["tags"] or "succession-litigated" in s["tags"]:
            signals.append({"kind": "succession", "weight": 0.6,
                            "text_en": "Succession unresolved or contested — governance and trust structuring need",
                            "text_zh": "接班未定或有爭議，家族治理與信託架構需求"})
        if "family-dispute-history" in s["tags"]:
            signals.append({"kind": "governance", "weight": 0.5,
                            "text_en": "Documented intra-family dispute — demonstrated need for formal family governance",
                            "text_zh": "有公開的家族爭議紀錄，對正式家族治理有明確需求"})

        catalyst = min(1.0, sum(x["weight"] for x in signals) / 2.5)

        # ── accessibility: how winnable is this relationship ──
        acc = 0.5
        if "conflicted-onshore" in s["tags"]:
            acc -= 0.35   # they own a competing private bank
        if "under-covered" in s["tags"] or "low-profile" in s["tags"]:
            acc += 0.20
        if "offshore-comfortable" in s["tags"] or "already-offshore" in s["tags"] or "ky-structure" in s["tags"]:
            acc += 0.20
        if "structure-limited" in s["tags"] or "foundation-controlled" in s["tags"]:
            acc -= 0.20
        if s["confidence"] == "medium":
            acc += 0.05
        acc = max(0.0, min(1.0, acc))

        w = sc["weights"]
        score = (
            logscale(nw_usd_m, sc["wealth_log_floor_usd_m"], sc["wealth_log_ceiling_usd_m"]) * w["wealth"]
            + logscale(bank_usd_m, sc["liquidity_log_floor_usd_m"], sc["liquidity_log_ceiling_usd_m"]) * w["liquidity"]
            + catalyst * w["catalyst"]
            + acc * w["accessibility"]
        )

        flags = []
        if not priced:
            flags.append("no-live-price")
        if any(h["stake_source"] == "mops-filing" for h in holdings):
            flags.append("filings-verified")
        else:
            flags.append("estimate-only")
        if nw_usd_m < a["min_net_worth_usd_m"]:
            flags.append("sub-threshold")
        if "conflicted-onshore" in s["tags"]:
            flags.append("competitor-owner")
        if "structure-limited" in s["tags"] or "foundation-controlled" in s["tags"]:
            flags.append("attribution-weak")

        out.append({
            **s,
            "holdings": holdings,
            "uplift": uplift,
            "stake_value_ntd": stake_value,
            "stake_value_usd_m": usd_m(stake_value),
            "net_worth_mid_ntd": mid,
            "net_worth_low_usd_m": usd_m(mid * (1 - band)),
            "net_worth_mid_usd_m": nw_usd_m,
            "net_worth_high_usd_m": usd_m(mid * (1 + band)),
            "dividend_gross_ntd": gross_div,
            "dividend_gross_usd_m": usd_m(gross_div),
            "dividend_net_usd_m": usd_m(net_div),
            "bankable_usd_m": bank_usd_m,
            "day_move_ntd": day_move,
            "day_move_usd_m": usd_m(day_move),
            "pledge_pct": pledge_pct,
            "top_line_share": top_share,
            "line_count": len(holdings),
            "flags": flags,
            "signals": sorted(signals, key=lambda x: -x["weight"]),
            "catalyst_score": round(catalyst * 100),
            "accessibility_score": round(acc * 100),
            "score": round(score, 1),
        })

    out.sort(key=lambda s: -s["score"])
    return companies, out


# ── synthetic mode (local layout work only) ───────────────────────────────

def synthetic_live(roster):
    """Deterministic pseudo-market so the page can be styled without network
    access. Explicitly opt-in; never written by the scheduled job."""
    quotes, info, ratios, revenue = {}, {}, {}, {}
    for c in roster["companies"]:
        t = c["ticker"]
        seed = int(t)
        price = 40 + (seed % 900) * 0.9
        chg = ((seed % 17) - 8) * price * 0.004
        quotes[t] = {"price": round(price, 1), "change": round(chg, 2),
                     "changePct": round(chg / price * 100, 2), "open": price, "high": price,
                     "low": price, "volume": 1e6, "turnover": 1e9}
        info[t] = {"name_zh": c["name_zh"], "industry": c["sector"], "chairman": "—",
                   "president": "—", "website": None, "paid_in_capital": None,
                   "par_value": 10.0, "shares_out": (0.4 + (seed % 50) / 10.0) * 1e9}
        ratios[t] = {"pe": 15 + seed % 20, "pb": 1.5 + (seed % 40) / 10,
                     "divYield": 1.5 + (seed % 45) / 10}
        revenue[t] = {"period": "11506", "yoy": ((seed % 90) - 25), "mom": 0, "cum_yoy": 0}
    note_source("SYNTHETIC", True, "deterministic fake market — not for publication")
    return {"quotes": quotes, "info": info, "ratios": ratios, "revenue": revenue,
            "exdiv": {}, "insiders": {}}


# ── main ──────────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=DEFAULT_OUT)
    ap.add_argument("--synthetic", action="store_true",
                    help="no network; deterministic fake prices for local layout work")
    ap.add_argument("--skip-mops", action="store_true",
                    help="skip the MOPS insider scrape (slow, best-effort)")
    args = ap.parse_args()

    print(f"Starting TWSE prospect build at {datetime.now(timezone.utc).isoformat()}", file=sys.stderr)

    with open(ROSTER_PATH, encoding="utf-8") as f:
        roster = json.load(f)
    tickers = [c["ticker"] for c in roster["companies"]]
    print(f"Roster: {len(tickers)} issuers", file=sys.stderr)

    if args.synthetic:
        live = synthetic_live(roster)
        fx_rate = None
    else:
        live = {
            "quotes": fetch_quotes(),
            "info": fetch_company_info(),
            "ratios": fetch_ratios(),
            "revenue": fetch_monthly_revenue(),
            "exdiv": fetch_ex_dividend(),
            "insiders": {},
        }
        fx_rate = fetch_fx()

        if not args.skip_mops:
            # ROC year, previous month — MOPS publishes with a lag
            prev = date.today().replace(day=1) - timedelta(days=1)
            roc_year, month = prev.year - 1911, prev.month
            hit = 0
            for t in tickers:
                rows = fetch_insiders(t, roc_year, month)
                if rows:
                    live["insiders"][t] = rows
                    hit += 1
                time.sleep(0.4)  # be a good citizen
            note_source("mops:insider-holdings", hit > 0,
                        f"{hit}/{len(tickers)} issuers with parsed insider rows "
                        f"(ROC {roc_year}/{month:02d})")

    companies, prospects = build(roster, live, fx_rate)

    priced = [p for p in prospects if p["stake_value_ntd"] > 0]
    total_nw = sum(p["net_worth_mid_usd_m"] for p in prospects)
    total_bankable = sum(p["bankable_usd_m"] for p in prospects)

    output = {
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "mode": "synthetic" if args.synthetic else "live",
        "fx": {"twd_per_usd": fx_rate or roster["assumptions"]["twd_per_usd_fallback"],
               "live": fx_rate is not None},
        "assumptions": roster["assumptions"],
        "scoring": roster["scoring"],
        "roster_meta": roster["meta"],
        "totals": {
            "issuers": len(companies),
            "subjects": len(prospects),
            "subjects_priced": len(priced),
            "net_worth_usd_m": total_nw,
            "bankable_usd_m": total_bankable,
            "above_threshold": len([p for p in prospects
                                    if p["net_worth_mid_usd_m"] >= roster["assumptions"]["min_net_worth_usd_m"]]),
        },
        "companies": companies,
        "prospects": prospects,
        "diagnostics": diagnostics,
    }

    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)

    ok = sum(1 for v in diagnostics["sources"].values() if v["ok"])
    print(f"Wrote {args.out}: {len(prospects)} subjects, {len(priced)} priced, "
          f"{ok}/{len(diagnostics['sources'])} sources ok, "
          f"US${total_nw/1000:,.1f}bn estimated wealth pool", file=sys.stderr)

    if not args.synthetic and not priced:
        print("ERROR: no subject could be priced — every TWSE source failed", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
