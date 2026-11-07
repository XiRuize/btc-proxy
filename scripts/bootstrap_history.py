#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""历史回补：从 data.binance.vision 下载币安官方批量数据（本机可直连）。

产出（默认写到 ../../parquet/crypto/history/）：
  {SYM}_1d.csv.gz       日线，2017-08 起
  {SYM}_5m.csv.gz       5 分钟线，2017-08 起（体积大，默认 gzip）
  {SYM}_funding.csv     8 小时资金费率（U 本位合约）

实现要点：
  * 逐月下载、行级去重（不把整表读成对象，5m 一年 10.5 万行，9 年近百万行）；
  * 时间戳可能是毫秒也可能是微秒（币安 2025 年起改的），排序按整数时间戳，天然正确；
  * 月包之外补当月日包，保证"到昨天"。

用法：
  python scripts/bootstrap_history.py --symbols BTCUSDT --interval 5m
  python scripts/bootstrap_history.py --symbols BTCUSDT ETHUSDT SOLUSDT --interval 1d
"""
from __future__ import annotations

import argparse
import gzip
import io
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
UA = "btc-proxy-history/2.0"
KLINE_HEADER = ("open_time,open,high,low,close,volume,close_time,quote_volume,"
                "trades,taker_buy_base,taker_buy_quote,ignore")
FUNDING_HEADER = "calc_time,funding_interval_hours,last_funding_rate"

# 各币种在币安的上线月份，避免无谓的 404
LISTING = {"BTCUSDT": (2017, 8), "ETHUSDT": (2017, 8), "SOLUSDT": (2020, 8),
           "BNBUSDT": (2017, 11), "XRPUSDT": (2018, 5)}


def http_get(url, timeout=40, retries=3):
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
        time.sleep(1.2 * (i + 1))
    raise RuntimeError(f"{url} 失败: {last}")


def unzip_lines(raw):
    with zipfile.ZipFile(io.BytesIO(raw)) as z:
        return z.read(z.namelist()[0]).decode("utf-8", "replace").splitlines()


def month_iter(start, end):
    y, m = start
    while (y, m) <= (end.year, end.month):
        yield y, m
        m += 1
        if m == 13:
            y, m = y + 1, 1


def collect_klines(sym, interval, start, now):
    """返回 {时间戳字符串(原始): 整行}，按月包 + 当月日包。"""
    lines = {}
    last_month = (now.year, now.month - 1) if now.month > 1 else (now.year - 1, 12)

    for y, m in month_iter(start, datetime(last_month[0], last_month[1], 1, tzinfo=timezone.utc)):
        url = f"{BASE}/spot/monthly/klines/{sym}/{interval}/{sym}-{interval}-{y:04d}-{m:02d}.zip"
        raw = http_get(url)
        if raw is None:
            continue
        for line in unzip_lines(raw):
            key = line.split(",", 1)[0].strip()
            if key.isdigit():
                lines[key] = line.strip()

    day = datetime(now.year, now.month, 1, tzinfo=timezone.utc)
    while day <= now:
        d = day.strftime("%Y-%m-%d")
        url = f"{BASE}/spot/daily/klines/{sym}/{interval}/{sym}-{interval}-{d}.zip"
        raw = http_get(url)
        if raw:
            for line in unzip_lines(raw):
                key = line.split(",", 1)[0].strip()
                if key.isdigit():
                    lines[key] = line.strip()
        day += timedelta(days=1)
    return lines


def write_lines(path, header, mapping):
    gz = path.endswith(".gz")
    opener = gzip.open if gz else open
    with opener(path, "wt", encoding="utf-8", newline="\n") as fh:
        fh.write(header + "\n")
        for k in sorted(mapping, key=int):
            fh.write(mapping[k] + "\n")
    return len(mapping)


def collect_funding(sym, start, now):
    rows = {}
    last_month = (now.year, now.month - 1) if now.month > 1 else (now.year - 1, 12)
    for y, m in month_iter(start, datetime(last_month[0], last_month[1], 1, tzinfo=timezone.utc)):
        url = f"{BASE}/futures/um/monthly/fundingRate/{sym}/{sym}-fundingRate-{y:04d}-{m:02d}.zip"
        raw = http_get(url)
        if raw is None:
            continue
        for line in unzip_lines(raw):
            p = line.split(",")
            if p and p[0].strip().isdigit():
                rows[p[0].strip()] = line.strip()

    day = datetime(now.year, now.month, 1, tzinfo=timezone.utc)
    while day <= now:
        d = day.strftime("%Y-%m-%d")
        url = f"{BASE}/futures/um/daily/fundingRate/{sym}/{sym}-fundingRate-{d}.zip"
        raw = http_get(url)
        if raw:
            for line in unzip_lines(raw):
                p = line.split(",")
                if p and p[0].strip().isdigit():
                    rows[p[0].strip()] = line.strip()
        day += timedelta(days=1)
    return rows


def main(argv=None):
    ap = argparse.ArgumentParser(description="币安官方批量数据回补")
    ap.add_argument("--symbols", nargs="+", default=["BTCUSDT"])
    ap.add_argument("--interval", default="1d", choices=["1d", "1h", "5m", "1m"])
    ap.add_argument("--start", default=None, help="起始年月 YYYY-MM，默认按上市月份")
    ap.add_argument("--no-gzip", action="store_true", help="输出未压缩 CSV（体积大）")
    ap.add_argument("--skip-funding", action="store_true")
    ap.add_argument("--out", default=os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                                  "..", "..", "parquet", "crypto", "history"))
    args = ap.parse_args(argv)

    out_dir = os.path.abspath(args.out)
    os.makedirs(out_dir, exist_ok=True)
    now = datetime.now(timezone.utc)
    suffix = ".csv" if args.no_gzip else ".csv.gz"

    for sym in args.symbols:
        start = (tuple(int(x) for x in args.start.split("-")) if args.start
                 else LISTING.get(sym, (2017, 8)))
        print(f"  {sym} {args.interval}（自 {start[0]}-{start[1]:02d}）...", flush=True)
        t0 = time.time()
        mapping = collect_klines(sym, args.interval, start, now)
        path = os.path.join(out_dir, f"{sym}_{args.interval}{suffix}")
        n = write_lines(path, KLINE_HEADER, mapping)
        size = os.path.getsize(path) / 1e6
        print(f"    -> {path}  {n} 行  {size:.1f} MB  {time.time()-t0:.0f}s")

        if not args.skip_funding:
            print(f"  {sym} funding ...", flush=True)
            t0 = time.time()
            fmap = collect_funding(sym, (2020, 1), now)
            fpath = os.path.join(out_dir, f"{sym}_funding.csv")
            fn = write_lines(fpath, FUNDING_HEADER, fmap)
            print(f"    -> {fpath}  {fn} 行  {os.path.getsize(fpath)/1e3:.0f} KB  {time.time()-t0:.0f}s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
