#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""历史回补：从 data.binance.vision 下载币安官方批量数据（本机可直连）。

为什么需要它：Actions 采集器只从"现在"往后攒，做研究要 2017 年以来的底仓。
data.binance.vision 是币安的公开数据桶，按 月/日 打包 CSV，不受 451 影响。

产出（默认写到 ../../parquet/crypto/history/）：
  {SYM}_1d.csv       日线，2017-08 起
  {SYM}_1h.csv       小时线（可选，--with-1h）
  {SYM}_funding.csv  8 小时资金费率（U 本位合约）

用法：
  python scripts/bootstrap_history.py --symbols BTCUSDT --kind 1d
  python scripts/bootstrap_history.py --symbols BTCUSDT ETHUSDT --kind all --with-1h
"""
from __future__ import annotations

import argparse
import csv
import io
import json
import os
import sys
import time
import urllib.error
import urllib.request
import zipfile
from datetime import datetime, timedelta, timezone

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:  # noqa: BLE001
    pass

BASE = "https://data.binance.vision/data"
UA = "btc-proxy-history/1.0"
KLINE_FIELDS = ["open_time", "open", "high", "low", "close", "volume", "close_time",
                "quote_volume", "trades", "taker_buy_base", "taker_buy_quote", "ignore"]


def http_get(url, timeout=30, retries=3):
    last = None
    for i in range(retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.read()
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                return None
            last = exc
        except Exception as exc:  # noqa: BLE001
            last = exc
        time.sleep(1.5 * (i + 1))
    raise RuntimeError(f"{url} 失败: {last}")


def unzip_csv(raw):
    with zipfile.ZipFile(io.BytesIO(raw)) as z:
        name = z.namelist()[0]
        return z.read(name).decode("utf-8", "replace")


def months_between(start_ym, end_dt):
    y, m = start_ym
    while (y, m) <= (end_dt.year, end_dt.month):
        yield y, m
        m += 1
        if m == 13:
            y, m = y + 1, 1


def fetch_klines(sym, interval, start_ym, out_dir, now):
    """月包 + 当月日包，合并去重后写 CSV。"""
    rows = {}
    last_month = (now.year, now.month - 1) if now.month > 1 else (now.year - 1, 12)

    for y, m in months_between(start_ym, datetime(last_month[0], last_month[1], 1, tzinfo=timezone.utc)):
        url = f"{BASE}/spot/monthly/klines/{sym}/{interval}/{sym}-{interval}-{y:04d}-{m:02d}.zip"
        raw = http_get(url)
        if raw is None:
            print(f"    缺月包 {y}-{m:02d}（跳过）")
            continue
        rows.update(parse_kline_csv(unzip_csv(raw)))

    # 当月：按日补齐
    day = datetime(now.year, now.month, 1, tzinfo=timezone.utc)
    while day <= now:
        d = day.strftime("%Y-%m-%d")
        url = f"{BASE}/spot/daily/klines/{sym}/{interval}/{sym}-{interval}-{d}.zip"
        raw = http_get(url)
        if raw:
            rows.update(parse_kline_csv(unzip_csv(raw)))
        day += timedelta(days=1)

    path = os.path.join(out_dir, f"{sym}_{interval}.csv")
    ordered = [rows[k] for k in sorted(rows)]
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=KLINE_FIELDS, extrasaction="ignore")
        w.writeheader()
        for r in ordered:
            w.writerow(r)
    return path, len(ordered)


def parse_kline_csv(text):
    out = {}
    for line in text.strip().splitlines():
        parts = line.split(",")
        if len(parts) < 12 or not parts[0].strip().isdigit():
            continue                      # 表头或异常行
        out[parts[0]] = {k: v for k, v in zip(KLINE_FIELDS, parts)}
    return out


def fetch_funding(sym, start_ym, out_dir, now):
    rows = {}
    last_month = (now.year, now.month - 1) if now.month > 1 else (now.year - 1, 12)
    for y, m in months_between(start_ym, datetime(last_month[0], last_month[1], 1, tzinfo=timezone.utc)):
        url = f"{BASE}/futures/um/monthly/fundingRate/{sym}/{sym}-fundingRate-{y:04d}-{m:02d}.zip"
        raw = http_get(url)
        if raw is None:
            continue
        text = unzip_csv(raw)
        for line in text.strip().splitlines():
            parts = line.split(",")
            if len(parts) < 3 or not parts[0].strip().isdigit():
                continue
            rows[parts[0]] = {"calc_time": parts[0], "funding_interval_hours": parts[1],
                              "last_funding_rate": parts[2]}
    path = os.path.join(out_dir, f"{sym}_funding.csv")
    ordered = [rows[k] for k in sorted(rows, key=int)]
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=["calc_time", "funding_interval_hours", "last_funding_rate"])
        w.writeheader()
        for r in ordered:
            w.writerow(r)
    return path, len(ordered)


def main(argv=None):
    ap = argparse.ArgumentParser(description="币安官方批量数据回补")
    ap.add_argument("--symbols", nargs="+", default=["BTCUSDT"])
    ap.add_argument("--kind", default="1d", choices=["1d", "1h", "all"])
    ap.add_argument("--with-1h", action="store_true", help="额外拉 1 小时线")
    ap.add_argument("--start", default="2017-08", help="起始年月，YYYY-MM")
    ap.add_argument("--out", default=os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                                  "..", "..", "parquet", "crypto", "history"))
    args = ap.parse_args(argv)

    out_dir = os.path.abspath(args.out)
    os.makedirs(out_dir, exist_ok=True)
    start_ym = tuple(int(x) for x in args.start.split("-"))
    now = datetime.now(timezone.utc)

    intervals = []
    if args.kind in ("1d", "all"):
        intervals.append("1d")
    if args.kind in ("1h", "all") or args.with_1h:
        intervals.append("1h")

    for sym in args.symbols:
        for interval in intervals:
            print(f"  {sym} {interval} ...", flush=True)
            path, n = fetch_klines(sym, interval, start_ym, out_dir, now)
            print(f"    -> {path}  {n} 行")
        print(f"  {sym} funding ...", flush=True)
        path, n = fetch_funding(sym, start_ym, out_dir, now)
        print(f"    -> {path}  {n} 行")
    return 0


if __name__ == "__main__":
    sys.exit(main())
