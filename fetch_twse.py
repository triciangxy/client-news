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

Usage:
  python fetch_twse.py                 # fetch and write twse.json
  python fetch_twse.py --selftest      # fetch, report resolution, write nothing
  python fetch_twse.py --fixture       # write a demo twse.json with no network
  python fetch_twse.py --codes 2317,2330,2454
"""

import argparse
import json
import re
import sys
from datetime import datetime, timezone

import requests

BASE = "https://openapi.twse.com.tw/v1"
INDEX_URLS = [f"{BASE}/swagger.json", f"{BASE}/openapi.json", "https://openapi.twse.com.tw/swagger.json"]
UA = "Mozilla/5.0 (compatible; ClientPulse-SoW/1.0; +https://github.com)"
TIMEOUT = 40

# Companies to publish. Codes are TWSE listing codes.
DEFAULT_CODES = ["2317", "2330", "2454", "2412", "1301", "2882", "2881", "1216"]

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
    "pay":      ["酬金總額", "領取報酬總額", "個人酬金", "酬金", "Remuneration", "總酬金"],
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

def build(codes):
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

    companies = []
    for code in codes:
        price_row = (prices.get(code) or [{}])[0]
        val_row = (vals.get(code) or [{}])[0]
        info_row = (infos.get(code) or [{}])[0]

        price = to_num(pick(price_row, "close"))
        capital = to_num(pick(info_row, "capital"))
        # Paid-in capital is NT$; TWSE ordinary shares carry a NT$10 par value,
        # so share count is capital / 10 unless the dataset gave a share count.
        shares_out = capital / 10 if capital and capital > 1e8 else capital

        directors = []
        for row in holds.get(code, []):
            person = pick(row, "person")
            if not person:
                continue
            directors.append({
                "name": str(person).strip(),
                "title": str(pick(row, "title") or "").strip(),
                "shares": to_num(pick(row, "shares")) or 0,
                "pay": None,
                "payBasis": None,
            })

        # Attach remuneration by name where the pay dataset covers this company.
        by_name = {d["name"]: d for d in directors}
        for row in pays.get(code, []):
            person = str(pick(row, "person") or "").strip()
            exact = to_num(pick(row, "pay"))
            band = band_midpoint(pick(row, "pay_band"))
            target = by_name.get(person)
            if target is None and person:
                target = {"name": person, "title": str(pick(row, "title") or "").strip(),
                          "shares": 0, "pay": None, "payBasis": None}
                directors.append(target)
                by_name[person] = target
            if target is None:
                continue
            if exact:
                target["pay"], target["payBasis"] = exact, "disclosed"
            elif band:
                target["pay"], target["payBasis"] = band, "band midpoint"

        companies.append({
            "code": code,
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
        d = companies[-1]
        log(f"  {code}: price={d['price']} shares={d['sharesOutstanding']} directors={len(d['directors'])}")

    return {
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "source": "Taiwan Stock Exchange open data (openapi.twse.com.tw)",
        "currency": "TWD",
        "resolution": how,
        "companies": companies,
    }


def fixture():
    """A small, obviously-synthetic payload so the UI is usable with no network.

    Figures are invented. Replace by running the fetcher for real.
    """
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


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--selftest", action="store_true", help="report dataset resolution, write nothing")
    ap.add_argument("--fixture", action="store_true", help="write synthetic sample data, no network")
    ap.add_argument("--codes", help="comma-separated TWSE codes")
    ap.add_argument("--out", default="twse.json")
    args = ap.parse_args()

    if args.fixture:
        payload = fixture()
        with open(args.out, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=1)
        print(f"wrote {args.out} (fixture)")
        return 0

    codes = [c.strip() for c in args.codes.split(",")] if args.codes else DEFAULT_CODES
    payload = build(codes)

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
        log("ERROR: nothing resolved — leaving the existing twse.json untouched")
        return 1

    with open(args.out, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=1)
    print(f"wrote {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
