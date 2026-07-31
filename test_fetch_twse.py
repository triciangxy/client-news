#!/usr/bin/env python3
"""
test_fetch_twse.py — exercises build() against stub datasets, no network.

Three market-wide runs died in a row on NameError/KeyError because build() is
only reachable with live TWSE, and this sandbox cannot reach it. Stubbing the
fetch layer makes the whole path testable in a second instead of four minutes.

    python test_fetch_twse.py
"""

import os
import sys

import fetch_twse as ft

# Rows shaped exactly like the live column dump, including the trailing space
# TWSE puts in "選任時持股 ".
HOLDINGS = [
    # A corporate director with a natural person representing it.
    {"公司代號": "1101", "職稱": "董事長本人", "姓名": "嘉利實業股份有限公司",
     "目前持股": "3835997", "設質股數": "0", "內部人關係人目前持股合計": "0", "選任時持股 ": "3335997"},
    {"公司代號": "1101", "職稱": "董事長之法人代表人", "姓名": "張安平",
     "目前持股": "4624351", "設質股數": "1000000", "內部人關係人目前持股合計": "9311403"},
    # A natural person filed twice under different roles — must merge to one.
    {"公司代號": "1101", "職稱": "董事本人", "姓名": "王小明",
     "目前持股": "5000000", "設質股數": "2500000", "內部人關係人目前持股合計": "1200000"},
    {"公司代號": "1101", "職稱": "總經理本人", "姓名": "王小明",
     "目前持股": "5000000", "設質股數": "2500000", "內部人關係人目前持股合計": "1200000"},
    {"公司代號": "1101", "職稱": "獨立董事本人", "姓名": "李四",
     "目前持股": "0", "設質股數": "0", "內部人關係人目前持股合計": "0"},
]
PRICE = [{"Code": "1101", "Name": "台泥", "ClosingPrice": "35.5"}]
VALUATION = [{"Code": "1101", "PEratio": "11.14", "PBratio": "0.66", "DividendYield": "3.33"}]
COMPANY = [{"公司代號": "1101", "公司名稱": "臺灣水泥股份有限公司", "英文簡稱": "TCC",
            "產業別": "01", "實收資本額": "77231817420", "董事長": "張安平",
            "總經理": "程耀輝", "上市日期": "19620209", "成立日期": "19501229"}]
REMUNERATION = [{"公司代號": "1101", "董事酬金-合計": "29518258",
                 "平均每位董事酬金-董事酬金": "2012151"}]
DIVIDEND = [
    {"公司代號": "1101", "股利年度": "113", "股東配發-盈餘分配之現金股利(元/股)": "1.0"},
    {"公司代號": "1101", "股利年度": "114", "股東配發-盈餘分配之現金股利(元/股)": "2.0",
     "股東配發-法定盈餘公積發放之現金(元/股)": "0.5", "股東配發-資本公積發放之現金(元/股)": "0.25"},
]
STUBS = {"price": PRICE, "valuation": VALUATION, "company": COMPANY,
         "holdings": HOLDINGS, "remuneration": REMUNERATION, "dividend": DIVIDEND}

failures = []


def check(label, got, want):
    ok = got == want
    print(f"   {'ok  ' if ok else 'FAIL'} {label}: {got!r}" + ("" if ok else f"  (want {want!r})"))
    if not ok:
        failures.append(label)


def main():
    ft.load_index = lambda: None
    ft.fetch_role = lambda role, spec, index: (STUBS[role], "stub")

    payload = ft.build()
    co = payload["companies"][0]
    board = {d["name"]: d for d in co["directors"]}

    print("build() over stub datasets:")
    check("one company built", len(payload["companies"]), 1)
    check("price parsed", co["price"], 35.5)
    check("industry decoded", co["industry"], "Cement")
    check("english name", co["nameEn"], "TCC")
    check("chairman", co["chairman"], "張安平")
    check("listing date normalised from ROC-era Gregorian", co["listedOn"], "1962-02-09")
    check("board average pay", co["avgBoardPay"], 2012151.0)
    # 2.0 + 0.5 + 0.25, taken from the LATER dividend year only.
    check("dps sums cash components of latest year", co["dps"], 2.75)
    check("dividend year is the most recent", co["dividendYear"], "114")

    print("\ninsider classification:")
    check("duplicate roles merged to one row", len(board), 4)
    check("merged titles joined", board["王小明"]["title"], "董事本人 / 總經理本人")
    check("shares not double counted", board["王小明"]["shares"], 5000000.0)
    check("pledged captured", board["王小明"]["pledged"], 2500000.0)
    check("related parties captured", board["張安平"]["related"], 9311403.0)
    check("nominee flagged", board["張安平"]["isRep"], True)
    check("corporate director flagged", board["嘉利實業股份有限公司"]["isCorporate"], True)
    check("natural person not flagged", board["王小明"]["isRep"], False)

    print("\ncoverage counts:")
    # 王小明 and 李四 are the only natural non-nominee people; one holds shares.
    check("board size excludes nominees and companies", co["boardSize"], 2)
    check("withHoldings counts only holders", co["withHoldings"], 1)
    check("repCount counts what was set aside", co["repCount"], 2)
    check("top holder is a natural person", co["topHolder"]["name"], "王小明")

    print("\nentity vs person classification:")
    for n in ["嘉利實業股份有限公司", "Pearl Place Holdings Limited", "Monster Holding Co.,Ltd.",
              "SVIC No.45 New Technology Business Investment L.L.P.", "國軍退除役官兵輔導委員會",
              "AGI Holding Co.,Ltd."]:
        check(f"entity: {n[:34]}", ft.is_corporate(n), True)
    # Real filings carry parenthetical rare-character notations and romanised
    # names; those are people and must not be swept up.
    for n in ["張安平", "王小明", "許（清爭）心", "洪麗(宓冉)", "張良（人予）",
              "Ｃｈｕｎｇ　Ｃｈｉｅｈ　Ｋｕｏ", "CHCHIN JONG HWA秦榮華", "Cooper Hsu"]:
        check(f"person: {n[:30]}", ft.is_corporate(n), False)

    print("\nedge cases:")
    check("ROC date", ft.parse_date("1090615"), "2020-06-15")
    check("Gregorian date", ft.parse_date("20200615"), "2020-06-15")
    check("junk date rejected", ft.parse_date("abc"), None)
    check("unknown alias does not raise", ft.pick({"x": 1}, "no_such_key"), None)

    # Run the real CLI entrypoint too. build() passing is not enough: a summary
    # line in main() referencing a removed field crashed a market-wide run that
    # build()-only tests waved through.
    print("\nCLI entrypoint:")
    import tempfile, io, contextlib
    with tempfile.TemporaryDirectory() as tmp:
        out = os.path.join(tmp, "twse.json")
        shard_dir = os.path.join(tmp, "twse")
        for argv in (["fetch_twse.py", "--selftest"],
                     ["fetch_twse.py", "--out", out, "--shard-dir", shard_dir]):
            saved = sys.argv
            sys.argv = argv
            try:
                with contextlib.redirect_stdout(io.StringIO()):
                    rc = ft.main()
            finally:
                sys.argv = saved
            check(f"main() {argv[1]} exits 0", rc, 0)
        check("index written", os.path.exists(out), True)
        check("shard written", os.path.exists(os.path.join(shard_dir, "1101.json")), True)

    print()
    if failures:
        print(f"FAILED: {len(failures)} check(s): {', '.join(failures)}")
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
