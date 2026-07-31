#!/usr/bin/env python3
"""
fetch_twse.py — runs on GitHub Actions to pull public director disclosures from
the Taiwan Stock Exchange, writes twse.json which prospects.html reads.

Everything here comes from TWSE's own open data service. Three things matter for
sizing a prospect:

  - daily closing price + shares outstanding  -> what the company is worth
  - director & supervisor shareholdings       -> what their stake is worth
  - director & supervisor remuneration        -> what they take home each year

TWSE renames and renumbers its datasets periodically, so rather than hardcoding
dataset IDs this resolves them at runtime from the service's own index, matching
on the Chinese title. Preferred IDs are tried first; the keyword match is the
fallback that survives a renumbering. Run with --selftest to see exactly which
dataset answered for each role.

Output is split in two, because the whole market is ~1,000 boards:

  twse.json        a light index of every listed company — code, names, price,
                   market cap, director count. Prices move daily, so this is the
                   file that changes on every run. ~150 KB.
  twse/<code>.json one shard per company with its board: holdings and pay.
                   Holdings are disclosed monthly and pay annually, so these
                   shards rarely change and git only commits the ones that did.

Keeping price out of the shards is what stops a daily refresh from rewriting a
thousand files and bloating the repo.

Usage:
  python fetch_twse.py                 # fetch, write twse.json + twse/*.json
  python fetch_twse.py --selftest      # fetch, report resolution, write nothing
  python fetch_twse.py --fixture       # write demo data with no network
  python fetch_twse.py --limit 50      # first 50 companies only, for a quick run
  python fetch_twse.py --codes 2317,2330,2454
"""

import argparse
import json
import os
import re
import shutil
import sys
from datetime import datetime, timezone

import requests

BASE = "https://openapi.twse.com.tw/v1"
INDEX_URLS = [f"{BASE}/swagger.json", f"{BASE}/openapi.json", "https://openapi.twse.com.tw/swagger.json"]
UA = "Mozilla/5.0 (compatible; ClientPulse-SoW/1.0; +https://github.com)"
TIMEOUT = 40

# By default every company the price dataset returns is published. --codes
# narrows it; these are only the fallback when the price dataset is unavailable.
FALLBACK_CODES = ["2317", "2330", "2454", "2412", "1301", "2882", "2881", "1216"]

SHARD_DIR = "twse"

# t187ap03_L gives 產業別 as a numeric code, not a name.
INDUSTRY = {
    "01": "Cement", "02": "Food", "03": "Plastics", "04": "Textiles",
    "05": "Electric machinery", "06": "Electrical & cable", "08": "Glass & ceramics",
    "09": "Paper & pulp", "10": "Steel & iron", "11": "Rubber", "12": "Automobile",
    "14": "Building materials", "15": "Shipping & transport", "16": "Tourism",
    "17": "Financials", "18": "Trading & retail", "19": "Conglomerate", "20": "Other",
    "21": "Chemicals", "22": "Biotech & healthcare", "23": "Oil, gas & electricity",
    "24": "Semiconductors", "25": "Computers & peripherals", "26": "Optoelectronics",
    "27": "Communications & networking", "28": "Electronic components",
    "29": "Electronic distribution", "30": "Information services",
    "31": "Other electronics", "32": "Cultural & creative", "33": "Agri-technology",
    "34": "E-commerce", "35": "Green energy", "36": "Digital cloud",
    "37": "Sports & leisure", "38": "Household goods",
}

# 內部人 rows carry a role in 職稱. Three things matter:
#   - 法人代表人  a person REPRESENTING a corporate or state shareholder. The shares
#                 are the institution's, not theirs. Counting them as personal
#                 wealth is how a state fund ends up looking like the richest
#                 individual on the board.
#   - 本人        the person's own holding — what we actually want.
#   - the seniority implied by the title, used to rank a board.
REP_MARKERS = ("法人代表", "代表人")
BOARD_TITLES = ("董事長", "副董事長", "常務董事", "董事", "獨立董事", "監察人", "大股東")

# ── Dataset roles ─────────────────────────────────────────────────────────
# `prefer` is tried first; `keywords` (all must appear in the dataset's title or
# path) is how we re-find a dataset that has been renumbered.
ROLES = {
    "price": {
        "prefer": ["/exchangeReport/STOCK_DAY_ALL"],
        "keywords": [["每日收盤"], ["收盤行情"]],
    },
    "valuation": {
        "prefer": ["/exchangeReport/BWIBBU_ALL"],
        "keywords": [["本益比"], ["殖利率"]],
    },
    "company": {
        "prefer": ["/opendata/t187ap03_L"],
        "keywords": [["公司", "基本資料"]],
    },
    "holdings": {
        "prefer": ["/opendata/t187ap11_L", "/opendata/t187ap10_L"],
        "keywords": [["董事", "持股"], ["董監", "持股"], ["內部人", "持股"]],
    },
    "remuneration": {
        "prefer": ["/opendata/t187ap28_L", "/opendata/t187ap29_L"],
        "keywords": [["董事", "酬金"], ["董監", "酬金"], ["酬金"]],
    },
    # Dividends turn a paper stake into cash actually received, which is the
    # distinction that matters most for a private bank.
    "dividend": {
        "prefer": ["/opendata/t187ap45_L", "/exchangeReport/TWT49U"],
        "keywords": [["股利", "分派"], ["除權息"], ["股利"]],
    },
}

# TWSE column names drift between datasets and over time; map by alias.
ALIASES = {
    "code":     ["Code", "公司代號", "證券代號", "股票代號", "SecuritiesCompanyCode"],
    "name":     ["Name", "公司名稱", "證券名稱", "CompanyName"],
    "name_en":  ["公司英文簡稱", "CompanyNameEn", "英文簡稱"],
    "close":    ["ClosingPrice", "收盤價", "Close"],
    "capital":  ["實收資本額", "PaidInCapital", "已發行普通股數或TDR原發行股數"],
    "pe":       ["PEratio", "本益比"],
    "pb":       ["PBratio", "股價淨值比"],
    "yield":    ["DividendYield", "殖利率(%)", "殖利率"],
    "person":   ["姓名", "Name", "職稱姓名", "PersonName"],
    "title":    ["職稱", "Title", "身分別"],
    "shares":   ["目前持股", "持股數", "本月持有股數", "所持股數", "Shares", "選任時持股"],
    # Tenure is disclosed — no need to assume it. 初次選任 (first elected) is the
    # one that matters; 選任日期 is the current term and understates a long-serving
    # director who has been re-elected.
    "first_elected": ["初次選任日期", "初次當選日期", "初次選任日", "FirstElectedDate"],
    "elected":  ["選任日期", "當選日期", "到職日期", "就任日期", "ElectedDate", "任期起始日"],
    "pay":      ["酬金總額", "領取報酬總額", "個人酬金", "酬金", "Remuneration", "總酬金"],
    "industry": ["產業別", "industry", "公司產業別", "Industry"],
    "dps":      ["股利", "每股股利", "現金股利", "每股配息", "DividendPerShare"],
    "pay_band": ["酬金級距", "級距", "RemunerationRange"],
}

# MOPS discloses most individual pay as a band rather than a figure. Midpoints
# in NT$; the top band is open-ended so it is floored at its lower bound.
BANDS = [
    (r"未達?\s*1[,，]?000", 500_000),
    (r"1[,，]?000.*?2[,，]?000", 1_500_000),
    (r"2[,，]?000.*?3[,，]?500", 2_750_000),
    (r"3[,，]?500.*?5[,，]?000", 4_250_000),
    (r"5[,，]?000.*?1[,，]?0000", 7_500_000),
    (r"1[,，]?0000.*?1[,，]?5000", 12_500_000),
    (r"1[,，]?5000.*?3[,，]?0000", 22_500_000),
    (r"3[,，]?0000.*?5[,，]?0000", 40_000_000),
    (r"5[,，]?0000.*?1[,，]?00000", 75_000_000),
    (r"1[,，]?00000\s*以上", 100_000_000),
]

session = requests.Session()
session.headers.update({"User-Agent": UA, "Accept": "application/json"})

log_lines = []


def log(msg):
    print(msg, file=sys.stderr)
    log_lines.append(msg)


def get_json(url):
    try:
        r = session.get(url, timeout=TIMEOUT)
        r.raise_for_status()
        return r.json()
    except Exception as exc:                       # noqa: BLE001 - report, never crash the run
        log(f"  ! {url} -> {type(exc).__name__}: {exc}")
        return None


def pick(row, key):
    """Read a value from a TWSE row by whichever alias that dataset happens to use."""
    for alias in ALIASES[key]:
        if alias in row and row[alias] not in (None, "", "-"):
            return row[alias]
    return None


def to_num(v):
    if v is None:
        return None
    s = re.sub(r"[,\s%元]", "", str(v))
    try:
        return float(s)
    except ValueError:
        return None


def parse_date(v):
    """Normalise a TWSE date to YYYY-MM-DD.

    Taiwan filings mix the ROC calendar with the Gregorian one: '1090615' is ROC
    year 109 = 2020. Anything that lands before 1911 is therefore an ROC year and
    gets shifted; a real Gregorian date passes through untouched.
    """
    if v is None:
        return None
    s = str(v).strip()
    if not s or s in ("-", "0"):
        return None

    parts = re.findall(r"\d+", s)
    if len(parts) >= 3:
        y, m, d = int(parts[0]), int(parts[1]), int(parts[2])
    elif len(parts) == 1:
        digits = parts[0]
        if len(digits) == 8:                      # 20200615
            y, m, d = int(digits[:4]), int(digits[4:6]), int(digits[6:])
        elif len(digits) == 7:                    # 1090615 (ROC)
            y, m, d = int(digits[:3]), int(digits[3:5]), int(digits[5:])
        elif len(digits) == 6:                    # 990101 (ROC, 2-digit year)
            y, m, d = int(digits[:2]), int(digits[2:4]), int(digits[4:])
        elif len(digits) in (3, 4):               # year only
            y, m, d = int(digits), 1, 1
        else:
            return None
    else:
        return None

    if y < 1911:
        y += 1911                                 # ROC -> Gregorian
    if not (1900 <= y <= 2100 and 1 <= m <= 12 and 1 <= d <= 31):
        return None
    return f"{y:04d}-{m:02d}-{d:02d}"


def band_midpoint(text):
    """Turn a MOPS remuneration band into a usable NT$ figure."""
    if not text:
        return None
    s = str(text)
    for pattern, midpoint in BANDS:
        if re.search(pattern, s):
            return midpoint
    nums = [to_num(n) for n in re.findall(r"[\d,]{4,}", s)]
    nums = [n for n in nums if n]
    if len(nums) >= 2:
        return (nums[0] + nums[1]) / 2 * 1000       # bands are quoted in NT$ thousands
    return nums[0] * 1000 if nums else None


# ── dataset resolution ────────────────────────────────────────────────────

def load_index():
    """TWSE's own API index, used to re-find datasets that have been renumbered."""
    for url in INDEX_URLS:
        doc = get_json(url)
        if doc and isinstance(doc, dict) and doc.get("paths"):
            log(f"  index: {url} ({len(doc['paths'])} paths)")
            return doc
    log("  index: unavailable — falling back to preferred IDs only")
    return None


def resolve(role, spec, index):
    """Return (path, how) for a role, preferring known IDs then keyword match."""
    paths = (index or {}).get("paths", {})
    for path in spec["prefer"]:
        if not paths or path in paths:
            return path, ("preferred" if not paths else "preferred+confirmed")
    for keyset in spec["keywords"]:
        for path, node in paths.items():
            blob = json.dumps(node, ensure_ascii=False) + path
            if all(k in blob for k in keyset):
                return path, f"matched {'+'.join(keyset)}"
    return (spec["prefer"][0], "preferred (unconfirmed)") if spec["prefer"] else (None, "unresolved")


def fetch_role(role, spec, index):
    path, how = resolve(role, spec, index)
    if not path:
        log(f"  {role:<13} UNRESOLVED")
        return [], how
    rows = get_json(BASE + path if path.startswith("/") else path)
    n = len(rows) if isinstance(rows, list) else 0
    log(f"  {role:<13} {path}  [{how}]  {n} rows")
    return (rows if isinstance(rows, list) else []), how


def index_by_code(rows):
    out = {}
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        code = str(pick(row, "code") or "").strip()
        if code:
            out.setdefault(code, []).append(row)
    return out


# ── build ─────────────────────────────────────────────────────────────────

def build(codes=None, limit=None):
    log("Resolving TWSE datasets:")
    index = load_index()
    data, how = {}, {}
    for role, spec in ROLES.items():
        data[role], how[role] = fetch_role(role, spec, index)

    prices = index_by_code(data["price"])
    vals = index_by_code(data["valuation"])
    infos = index_by_code(data["company"])
    holds = index_by_code(data["holdings"])
    pays = index_by_code(data["remuneration"])
    divs = index_by_code(data["dividend"])

    # Publish the whole market by default — the search box needs every company,
    # not a hand-kept roster.
    if codes:
        universe = list(codes)
    else:
        universe = sorted(set(prices) | set(infos)) or list(FALLBACK_CODES)
    if limit:
        universe = universe[:limit]
    log(f"\nBuilding {len(universe)} companies:")

    companies = []
    for code in universe:
        price_row = (prices.get(code) or [{}])[0]
        val_row = (vals.get(code) or [{}])[0]
        info_row = (infos.get(code) or [{}])[0]

        price = to_num(pick(price_row, "close"))
        capital = to_num(pick(info_row, "capital"))
        # Paid-in capital is NT$; TWSE ordinary shares carry a NT$10 par value,
        # so share count is capital / 10 unless the dataset gave a share count.
        shares_out = capital / 10 if capital and capital > 1e8 else capital

        # One person can appear several times — 魏哲家 is filed as both 董事長本人
        # and 總經理本人 with the same shares. Summing the rows double-counts, so
        # merge by name and keep the largest holding.
        merged = {}
        for row in holds.get(code, []):
            person = pick(row, "person")
            if not person:
                continue
            name = str(person).strip()
            title = str(pick(row, "title") or "").strip()
            rec = merged.get(name)
            if rec is None:
                rec = {
                    "name": name, "title": title, "titles": [],
                    "shares": 0, "since": None, "pay": None, "payBasis": None,
                    "isRep": any(m in title for m in REP_MARKERS),
                    "onBoard": any(t in title for t in BOARD_TITLES),
                }
                merged[name] = rec
            if title and title not in rec["titles"]:
                rec["titles"].append(title)
            rec["shares"] = max(rec["shares"], to_num(pick(row, "shares")) or 0)
            rec["isRep"] = rec["isRep"] or any(m in title for m in REP_MARKERS)
            rec["onBoard"] = rec["onBoard"] or any(t in title for t in BOARD_TITLES)
            rec["since"] = rec["since"] or parse_date(pick(row, "first_elected")) \
                                        or parse_date(pick(row, "elected"))

        directors = []
        for rec in merged.values():
            rec["title"] = " / ".join(rec["titles"]) or rec["title"]
            rec.pop("titles", None)
            directors.append(rec)

        # Attach remuneration by name where the pay dataset covers this company.
        by_name = {d["name"]: d for d in directors}
        for row in pays.get(code, []):
            person = str(pick(row, "person") or "").strip()
            exact = to_num(pick(row, "pay"))
            band = band_midpoint(pick(row, "pay_band"))
            target = by_name.get(person)
            if target is None and person:
                target = {"name": person, "title": str(pick(row, "title") or "").strip(),
                          "shares": 0, "since": None, "pay": None, "payBasis": None}
                directors.append(target)
                by_name[person] = target
            if target is None:
                continue
            # The pay dataset sometimes carries an election date the holdings
            # dataset omits.
            if not target.get("since"):
                target["since"] = (parse_date(pick(row, "first_elected"))
                                   or parse_date(pick(row, "elected")))
            if exact:
                target["pay"], target["payBasis"] = exact, "disclosed"
            elif band:
                target["pay"], target["payBasis"] = band, "band midpoint"

        div_row = (divs.get(code) or [{}])[0]
        people = [d for d in directors if not d["isRep"]]
        reps = [d for d in directors if d["isRep"]]
        with_holdings = sum(1 for d in people if (d["shares"] or 0) > 0)
        top = max(people, key=lambda d: d["shares"] or 0, default=None)

        companies.append({
            "code": code,
            "industry": INDUSTRY.get(str(pick(info_row, "industry") or "").strip().zfill(2)),
            "industryCode": (pick(info_row, "industry") or None),
            # Institutional representatives are excluded from personal wealth but
            # counted, so the page can say how many were set aside and why.
            "repCount": len(reps),
            "dps": to_num(pick(div_row, "dps")),
            # Coverage denominator: how many of the board actually disclose a holding.
            "boardSize": len(people),
            "withHoldings": with_holdings,
            "topHolder": ({"name": top["name"], "title": top["title"], "shares": top["shares"]}
                          if top and (top["shares"] or 0) > 0 else None),
            "name": (pick(info_row, "name") or pick(price_row, "name") or code),
            "nameEn": pick(info_row, "name_en"),
            "price": price,
            "sharesOutstanding": shares_out,
            "marketCap": (price * shares_out) if (price and shares_out) else None,
            "pe": to_num(pick(val_row, "pe")),
            "pb": to_num(pick(val_row, "pb")),
            "yield": to_num(pick(val_row, "yield")),
            "directors": sorted(directors, key=lambda d: -(d["shares"] or 0)),
        })

    log(f"  built {len(companies)} companies, "
        f"{sum(len(c['directors']) for c in companies)} director rows")

    return {
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "source": "Taiwan Stock Exchange open data (openapi.twse.com.tw)",
        "currency": "TWD",
        "resolution": how,
        "companies": companies,
    }


# ── output: light index + one shard per company ───────────────────────────

def write_output(payload, out_path, shard_dir):
    """Split the payload into a searchable index and per-company board shards."""
    companies = payload["companies"]

    if os.path.isdir(shard_dir):
        shutil.rmtree(shard_dir)                 # drop delisted companies
    os.makedirs(shard_dir, exist_ok=True)

    written = 0
    for c in companies:
        if not c["directors"]:
            continue
        # Price deliberately stays out of the shard: it moves daily, and a shard
        # that changes daily would rewrite ~1,000 files on every run.
        shard = {
            "code": c["code"], "name": c["name"], "nameEn": c["nameEn"],
            "sharesOutstanding": c["sharesOutstanding"],
            "dps": c.get("dps"), "boardSize": c.get("boardSize"),
            "withHoldings": c.get("withHoldings"), "repCount": c.get("repCount"),
            "directors": c["directors"],
        }
        with open(os.path.join(shard_dir, f"{c['code']}.json"), "w", encoding="utf-8") as fh:
            json.dump(shard, fh, ensure_ascii=False, separators=(",", ":"))
        written += 1

    index = {
        "generated_at": payload["generated_at"],
        "source": payload["source"],
        "currency": payload["currency"],
        "fixture": payload.get("fixture", False),
        "resolution": payload.get("resolution", {}),
        "shardDir": shard_dir,
        "companies": [{
            "code": c["code"], "name": c["name"], "nameEn": c["nameEn"],
            "price": c["price"], "marketCap": c["marketCap"], "pe": c["pe"],
            "sharesOutstanding": c["sharesOutstanding"],
            "industry": c.get("industry"), "dps": c.get("dps"),
            "boardSize": c.get("boardSize"), "withHoldings": c.get("withHoldings"),
            "repCount": c.get("repCount"),
            "topHolder": c.get("topHolder"),
            "directors": len(c["directors"]),
        } for c in companies],
    }
    with open(out_path, "w", encoding="utf-8") as fh:
        json.dump(index, fh, ensure_ascii=False, separators=(",", ":"))

    size_kb = os.path.getsize(out_path) / 1024
    log(f"  wrote {out_path} ({len(index['companies'])} companies, {size_kb:.0f} KB) "
        f"and {written} shards in {shard_dir}/")
    return written


def fixture(count=60):
    """An obviously-synthetic market so search and drill-in work with no network.

    Every name and figure is invented. Replace by running the fetcher for real.
    """
    import random
    rng = random.Random(20260730)

    sectors = ["Precision", "Electronics", "Semiconductor", "Chemical", "Financial",
               "Textile", "Steel", "Optical", "Telecom", "Marine"]
    zh = ["精密", "電子", "半導體", "化學", "金融", "紡織", "鋼鐵", "光電", "電信", "海運"]
    titles = [("董事長", 0.07), ("副董事長", 0.03), ("董事兼總經理", 0.01),
              ("董事", 0.004), ("董事", 0.002), ("監察人", 0.001), ("獨立董事", 0.0)]

    companies = []
    for i in range(count):
        s = i % len(sectors)
        code = str(9100 + i)
        price = round(rng.uniform(18, 640), 2)
        shares = rng.randrange(200, 4000) * 1_000_000
        directors = []
        for n, (title, frac) in enumerate(titles):
            if n >= 4 and rng.random() < 0.3:
                continue
            held = int(shares * frac * rng.uniform(0.5, 1.6))
            pay = rng.choice([3_200_000, 4_800_000, 9_500_000, 21_000_000,
                              28_500_000, 38_000_000, 62_000_000])
            # Founders sit longer than independent directors. One in six rows is
            # left without a date, so the "not disclosed" path stays visible.
            first_year = rng.randint(1985, 2005) if n < 3 else rng.randint(2008, 2023)
            since = None if rng.random() < 0.16 else \
                f"{first_year}-{rng.randint(1, 12):02d}-{rng.randint(1, 28):02d}"
            directors.append({
                "name": f"示範-{code}-{n + 1}", "title": title, "shares": held,
                "since": since,
                "pay": pay, "payBasis": rng.choice(["band midpoint", "disclosed"]),
            })
        top = max(directors, key=lambda d: d["shares"], default=None)
        companies.append({
            "code": code,
            "industry": sectors[s],
            "dps": round(rng.uniform(0, 9), 2) if rng.random() > 0.25 else None,
            "boardSize": len(directors) + rng.randint(0, 3),
            "withHoldings": sum(1 for d in directors if d["shares"] > 0),
            "topHolder": ({"name": top["name"], "title": top["title"], "shares": top["shares"]}
                          if top and top["shares"] > 0 else None),
            "name": f"示範{zh[s]}股份有限公司 {code}",
            "nameEn": f"Demo {sectors[s]} Co {code}",
            "price": price, "sharesOutstanding": shares,
            "marketCap": price * shares,
            "pe": round(rng.uniform(7, 32), 1), "pb": round(rng.uniform(0.6, 4.2), 1),
            "yield": round(rng.uniform(0, 7), 1),
            "directors": sorted(directors, key=lambda d: -d["shares"]),
        })

    return {
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "source": "FIXTURE — synthetic sample data, not from TWSE",
        "currency": "TWD",
        "fixture": True,
        "resolution": {},
        "companies": companies,
    }


def _legacy_fixture():
    return {
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "source": "FIXTURE — synthetic sample data, not from TWSE",
        "currency": "TWD",
        "fixture": True,
        "resolution": {},
        "companies": [
            {
                "code": "9101", "name": "示範精密股份有限公司", "nameEn": "Demo Precision Co Ltd",
                "price": 212.5, "sharesOutstanding": 1_380_000_000,
                "marketCap": 293_250_000_000, "pe": 14.8, "pb": 2.1, "yield": 3.4,
                "directors": [
                    {"name": "示範-董事長", "title": "董事長", "shares": 96_600_000,
                     "pay": 62_000_000, "payBasis": "band midpoint"},
                    {"name": "示範-副董事長", "title": "副董事長", "shares": 41_400_000,
                     "pay": 38_000_000, "payBasis": "band midpoint"},
                    {"name": "示範-總經理", "title": "董事兼總經理", "shares": 13_800_000,
                     "pay": 28_500_000, "payBasis": "disclosed"},
                    {"name": "示範-董事", "title": "董事", "shares": 4_140_000,
                     "pay": 9_500_000, "payBasis": "band midpoint"},
                    {"name": "示範-獨立董事", "title": "獨立董事", "shares": 0,
                     "pay": 3_200_000, "payBasis": "band midpoint"},
                ],
            },
            {
                "code": "9102", "name": "示範電子股份有限公司", "nameEn": "Demo Electronics Co Ltd",
                "price": 88.0, "sharesOutstanding": 3_200_000_000,
                "marketCap": 281_600_000_000, "pe": 11.2, "pb": 1.3, "yield": 5.1,
                "directors": [
                    {"name": "示範-創辦人", "title": "董事長", "shares": 224_000_000,
                     "pay": 48_000_000, "payBasis": "band midpoint"},
                    {"name": "示範-執行副總", "title": "董事", "shares": 32_000_000,
                     "pay": 21_000_000, "payBasis": "disclosed"},
                    {"name": "示範-監察人", "title": "監察人", "shares": 6_400_000,
                     "pay": 4_800_000, "payBasis": "band midpoint"},
                ],
            },
        ],
    }


def columns():
    """Print the real field names each dataset returns.

    The alias tables are guesses until this has been run once against live TWSE;
    election date, pay and dividend-per-share all came back empty on the first
    real fetch, which means the column is named something not in ALIASES.
    """
    index = load_index()
    for role, spec in ROLES.items():
        rows, _ = fetch_role(role, spec, index)
        print(f"\n=== {role} ({len(rows)} rows)")
        if not rows:
            print("   (no rows)")
            continue
        print("   columns:", ", ".join(sorted(rows[0].keys())))
        for sample in rows[:2]:
            print("   sample:", json.dumps(sample, ensure_ascii=False)[:400])
    return 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--selftest", action="store_true", help="report dataset resolution, write nothing")
    ap.add_argument("--columns", action="store_true", help="print each dataset's real field names, write nothing")
    ap.add_argument("--fixture", action="store_true", help="write synthetic sample data, no network")
    ap.add_argument("--codes", help="comma-separated TWSE codes (default: whole market)")
    ap.add_argument("--limit", type=int, help="cap the number of companies, for a quick run")
    ap.add_argument("--out", default="twse.json")
    ap.add_argument("--shard-dir", default=SHARD_DIR)
    args = ap.parse_args()

    if args.columns:
        return columns()

    if args.fixture:
        payload = fixture(args.limit or 60)
        write_output(payload, args.out, args.shard_dir)
        print(f"wrote {args.out} + {args.shard_dir}/ (fixture)")
        return 0

    codes = [c.strip() for c in args.codes.split(",")] if args.codes else None
    payload = build(codes, args.limit)

    filled = sum(1 for c in payload["companies"] if c["directors"])
    priced = sum(1 for c in payload["companies"] if c["price"])
    paid = sum(1 for c in payload["companies"] for d in c["directors"] if d["pay"])
    log("")
    log(f"SUMMARY  companies={len(payload['companies'])} priced={priced} "
        f"with_directors={filled} directors_with_pay={paid}")

    if args.selftest:
        print(json.dumps({"resolution": payload["resolution"],
                          "priced": priced, "with_directors": filled,
                          "directors_with_pay": paid}, ensure_ascii=False, indent=1))
        # A resolution report is the point of --selftest; a thin result is a
        # finding to act on, not a reason to fail the step.
        return 0

    if not priced and not filled:
        log("ERROR: nothing resolved — leaving the existing data untouched")
        return 1

    write_output(payload, args.out, args.shard_dir)
    print(f"wrote {args.out} + {args.shard_dir}/")
    return 0


if __name__ == "__main__":
    sys.exit(main())
