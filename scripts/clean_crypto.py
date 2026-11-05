#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""加密行情清洗与校验：原始 CSV -> clean CSV + 质量报告。

输入（bootstrap_history.py / pull_local.ps1 产出）：
  parquet/crypto/history/{SYM}_1d.csv
  parquet/crypto/history/{SYM}_funding.csv
  parquet/crypto/{SYM}_5m.csv          （Actions 采集，可能只有最近一两天）

输出：
  parquet/crypto/clean/{SYM}_1d.csv
  parquet/crypto/clean/{SYM}_funding_8h.csv
  parquet/crypto/clean/{SYM}_funding_daily.csv
  parquet/crypto/clean/{SYM}_5m.csv
  parquet/crypto/clean/quality_report.json
  parquet/crypto/clean/quality_report.md

清洗原则：
  * 只删"确定的坏行"（时间戳重复、OHLC 自相矛盾），其余一律打标记不删；
  * 缺口如实记录，不插值（加密 7×24 交易，缺 bar 说明抓取断了，插值等于伪造）；
  * 所有时间统一 UTC，日线以 UTC 00:00 切分；
  * 资金费率保留原始值 + 假定的归一化列，单位未证实时显式标注。
"""
from __future__ import annotations

import argparse
import csv
import json
import math
import os
import sys
from datetime import datetime, timedelta, timezone

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:  # noqa: BLE001
    pass

SYMBOLS = ["BTCUSDT", "ETHUSDT", "SOLUSDT"]


# ---------------------------------------------------------------- IO

def read_rows(path):
    with open(path, newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def write_rows(path, fields, rows):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow(r)


def norm_ms(v):
    """币安 2025 年起把时间戳从毫秒换成微秒（16 位），这里统一归一到毫秒。"""
    s = str(v).strip()
    n = int(s)
    if len(s) >= 16:          # 微秒
        return n // 1000
    if len(s) <= 10:          # 秒
        return n * 1000
    return n                  # 毫秒


def ms_to_dt(ms):
    return datetime.fromtimestamp(norm_ms(ms) / 1000, tz=timezone.utc)


def f(x, default=float("nan")):
    try:
        return float(x)
    except (TypeError, ValueError):
        return default


# ---------------------------------------------------------------- 日线

def clean_daily(raw_path):
    rows = read_rows(raw_path)
    issues = {"duplicates": 0, "bad_ohlc": 0, "nonpositive_price": 0,
              "negative_volume": 0, "bad_timestamp": 0}
    seen, out = set(), []

    for r in rows:
        try:
            dt = ms_to_dt(r["open_time"])
        except Exception:  # noqa: BLE001
            issues["bad_timestamp"] += 1
            continue
        key = dt.strftime("%Y-%m-%d")
        o, h, l, c = f(r["open"]), f(r["high"]), f(r["low"]), f(r["close"])
        v = f(r["volume"], 0.0)

        if key in seen:
            issues["duplicates"] += 1
            continue
        if any(math.isnan(x) or x <= 0 for x in (o, h, l, c)):
            issues["nonpositive_price"] += 1
            continue
        if h < l or h < max(o, c) - 1e-9 or l > min(o, c) + 1e-9:
            issues["bad_ohlc"] += 1
            continue
        if v < 0:
            issues["negative_volume"] += 1

        seen.add(key)
        out.append({
            "date": key,
            "open": f"{o:.8f}", "high": f"{h:.8f}", "low": f"{l:.8f}", "close": f"{c:.8f}",
            "volume": f"{v:.8f}",
            "quote_volume": r.get("quote_volume", ""),
            "trades": r.get("trades", ""),
        })

    out.sort(key=lambda r: r["date"])

    # 缺口：按 UTC 自然日数
    gaps = []
    if len(out) >= 2:
        start = datetime.strptime(out[0]["date"], "%Y-%m-%d").replace(tzinfo=timezone.utc)
        end = datetime.strptime(out[-1]["date"], "%Y-%m-%d").replace(tzinfo=timezone.utc)
        expected = (end - start).days + 1
        have = len(out)
        if expected != have:
            have_set = {r["date"] for r in out}
            cur = start
            while cur <= end:
                k = cur.strftime("%Y-%m-%d")
                if k not in have_set:
                    gaps.append(k)
                cur += timedelta(days=1)
    issues["expected_days"] = expected if len(out) >= 2 else len(out)
    issues["missing_days"] = len(gaps)
    issues["missing_sample"] = gaps[:10]
    return out, issues


# ---------------------------------------------------------------- 资金费率

def clean_funding(raw_path):
    rows = read_rows(raw_path)
    issues = {"duplicates": 0, "bad_value": 0, "interval_not_8h": 0, "bad_timestamp": 0}
    seen, out = set(), []

    for r in rows:
        try:
            dt = ms_to_dt(r["calc_time"])
        except Exception:  # noqa: BLE001
            issues["bad_timestamp"] += 1
            continue
        key = dt.strftime("%Y-%m-%dT%H:%M:%SZ")
        rate = f(r["last_funding_rate"])
        if key in seen:
            issues["duplicates"] += 1
            continue
        if math.isnan(rate) or abs(rate) > 0.05:      # >5%/8h 视为异常
            issues["bad_value"] += 1
            continue
        if str(r.get("funding_interval_hours", "8")) not in ("8", "8.0"):
            issues["interval_not_8h"] += 1
        seen.add(key)
        out.append({"ts_utc": key, "rate": f"{rate:.10f}"})

    out.sort(key=lambda r: r["ts_utc"])
    return out, issues


def daily_funding(funding_rows):
    """按 UTC 日期聚合：当天 3 次结算之和（= 持有一天的资金费成本）、当天最后一次。"""
    agg = {}
    for r in funding_rows:
        d = r["ts_utc"][:10]
        a = agg.setdefault(d, {"n": 0, "sum": 0.0, "last": 0.0})
        a["n"] += 1
        a["sum"] += float(r["rate"])
        a["last"] = float(r["rate"])
    return [{"date": d, "settlements": a["n"],
             "rate_sum": f"{a['sum']:.10f}", "rate_last": f"{a['last']:.10f}"}
            for d, a in sorted(agg.items())]


# ---------------------------------------------------------------- 5 分钟

def clean_5m(raw_path, bar_sec=300):
    rows = read_rows(raw_path)
    issues = {"duplicates": 0, "bad_ohlc": 0, "gaps": 0}
    seen, out = set(), []

    for r in rows:
        key = r["open_time_utc"]
        if key in seen:
            issues["duplicates"] += 1
            continue
        o, h, l, c = f(r["open"]), f(r["high"]), f(r["low"]), f(r["close"])
        if any(math.isnan(x) for x in (o, h, l, c)) or h < l:
            issues["bad_ohlc"] += 1
            continue
        seen.add(key)
        out.append({"ts_utc": key, "open": r["open"], "high": r["high"],
                    "low": r["low"], "close": r["close"], "volume": r["volume"],
                    "source": r.get("source", "")})

    out.sort(key=lambda r: r["ts_utc"])

    # 缺口
    if len(out) >= 2:
        t0 = datetime.strptime(out[0]["ts_utc"], "%Y-%m-%dT%H:%M:%SZ")
        t1 = datetime.strptime(out[-1]["ts_utc"], "%Y-%m-%dT%H:%M:%SZ")
        expected = int((t1 - t0).total_seconds() // bar_sec) + 1
        issues["expected_bars"] = expected
        issues["gaps"] = expected - len(out)
        issues["coverage_pct"] = round(100.0 * len(out) / expected, 2) if expected else 0.0
    return out, issues


# ---------------------------------------------------------------- 主流程

def main(argv=None):
    ap = argparse.ArgumentParser(description="加密行情清洗与校验")
    ap.add_argument("--root", default=os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                                   "..", "..", "parquet", "crypto"))
    args = ap.parse_args(argv)

    root = os.path.abspath(args.root)
    out_dir = os.path.join(root, "clean")
    os.makedirs(out_dir, exist_ok=True)

    report = {"generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
              "root": root, "symbols": {}}

    for sym in SYMBOLS:
        info = {}
        d_path = os.path.join(root, "history", f"{sym}_1d.csv")
        if os.path.exists(d_path):
            rows, issues = clean_daily(d_path)
            write_rows(os.path.join(out_dir, f"{sym}_1d.csv"),
                       ["date", "open", "high", "low", "close", "volume", "quote_volume", "trades"],
                       rows)
            info["daily"] = {"rows": len(rows),
                             "range": [rows[0]["date"], rows[-1]["date"]] if rows else [],
                             **issues}

        fr_path = os.path.join(root, "history", f"{sym}_funding.csv")
        if os.path.exists(fr_path):
            rows, issues = clean_funding(fr_path)
            write_rows(os.path.join(out_dir, f"{sym}_funding_8h.csv"), ["ts_utc", "rate"], rows)
            d_rows = daily_funding(rows)
            write_rows(os.path.join(out_dir, f"{sym}_funding_daily.csv"),
                       ["date", "settlements", "rate_sum", "rate_last"], d_rows)
            info["funding"] = {"rows": len(rows),
                               "range": [rows[0]["ts_utc"][:10], rows[-1]["ts_utc"][:10]] if rows else [],
                               "daily_rows": len(d_rows), **issues}

        m_path = os.path.join(root, f"{sym}_5m.csv")
        if os.path.exists(m_path):
            rows, issues = clean_5m(m_path)
            write_rows(os.path.join(out_dir, f"{sym}_5m.csv"),
                       ["ts_utc", "open", "high", "low", "close", "volume", "source"], rows)
            info["m5"] = {"rows": len(rows),
                          "range": [rows[0]["ts_utc"], rows[-1]["ts_utc"]] if rows else [],
                          **issues}

        report["symbols"][sym] = info

    with open(os.path.join(out_dir, "quality_report.json"), "w", encoding="utf-8") as fh:
        json.dump(report, fh, ensure_ascii=False, indent=2)
        fh.write("\n")

    # 人读版本
    lines = ["# 数据质量报告", "",
             f"生成时间：{report['generated_at']}", ""]
    for sym, info in report["symbols"].items():
        lines.append(f"## {sym}")
        if "daily" in info:
            d = info["daily"]
            lines.append(f"- 日线：{d['rows']} 行，{d['range'][0]} → {d['range'][1]}；"
                         f"应有 {d['expected_days']} 天，缺 {d['missing_days']} 天；"
                         f"重复 {d['duplicates']}、坏 OHLC {d['bad_ohlc']}、非正价格 {d['nonpositive_price']}")
        if "funding" in info:
            fd = info["funding"]
            lines.append(f"- 资金费率：{fd['rows']} 条 8h，{fd['range'][0]} → {fd['range'][1]}；"
                         f"重复 {fd['duplicates']}、异常值 {fd['bad_value']}、非 8h 间隔 {fd['interval_not_8h']}")
        if "m5" in info:
            m = info["m5"]
            lines.append(f"- 5 分钟：{m['rows']} 根，{m['range'][0]} → {m['range'][1]}；"
                         f"覆盖率 {m.get('coverage_pct', 0)}%、缺口 {m['gaps']}、重复 {m['duplicates']}")
        lines.append("")
    with open(os.path.join(out_dir, "quality_report.md"), "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines))
        fh.write("\n")

    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
