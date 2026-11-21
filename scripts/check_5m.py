#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""5 分钟归档的流式质量校验：行数 / 顺序 / 重复 / 缺口 / OHLC 合法性。

不把整表读进内存（单币种近 100 万行），逐行扫描，只维护上一个时间戳。
用法：python scripts/check_5m.py [--dir .../parquet/crypto/archive]
"""
from __future__ import annotations

import argparse
import gzip
import json
import os
import sys

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:  # noqa: BLE001
    pass

SYMBOLS = ["BTCUSDT", "ETHUSDT", "SOLUSDT"]
BAR_MS = 300_000


def norm_ms(s):
    s = s.strip()
    n = int(s)
    if len(s) >= 16:
        return n // 1000
    if len(s) <= 10:
        return n * 1000
    return n


def check(path):
    rows = 0
    bad_ohlc = 0
    dup = 0
    disorder = 0
    gaps = 0
    prev = None
    first = last = None

    with gzip.open(path, "rt", encoding="utf-8") as fh:
        next(fh, None)                      # 表头
        for line in fh:
            p = line.rstrip("\n").split(",")
            if len(p) < 6:
                continue
            try:
                ts = norm_ms(p[0])
                o, h, l, c = float(p[1]), float(p[2]), float(p[3]), float(p[4])
                v = float(p[5])
            except ValueError:
                bad_ohlc += 1
                continue

            rows += 1
            if first is None:
                first = ts
            if prev is not None:
                d = ts - prev
                if d == 0:
                    dup += 1
                elif d < 0:
                    disorder += 1
                elif d > BAR_MS:
                    gaps += int(d // BAR_MS) - 1
            if h < l or h < max(o, c) - 1e-9 or l > min(o, c) + 1e-9 or v < 0:
                bad_ohlc += 1
            prev = ts
            last = ts

    expected = (last - first) // BAR_MS + 1 if first is not None else 0
    return {
        "rows": rows, "expected_bars": expected,
        "gaps": gaps, "coverage_pct": round(100 * rows / expected, 3) if expected else 0,
        "duplicates": dup, "disorder": disorder, "bad_ohlc": bad_ohlc,
        "first_utc": first, "last_utc": last,
    }


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", default=os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                                  "..", "..", "parquet", "crypto", "archive"))
    args = ap.parse_args(argv)
    d = os.path.abspath(args.dir)

    out = {}
    for sym in SYMBOLS:
        p = os.path.join(d, f"{sym}_5m.csv.gz")
        if not os.path.exists(p):
            continue
        r = check(p)
        out[sym] = r
        from datetime import datetime, timezone
        f0 = datetime.fromtimestamp(r["first_utc"] / 1000, tz=timezone.utc)
        f1 = datetime.fromtimestamp(r["last_utc"] / 1000, tz=timezone.utc)
        print(f"{sym}: {r['rows']:,} 行  {f0:%Y-%m-%d} → {f1:%Y-%m-%d}  "
              f"覆盖率 {r['coverage_pct']}%  缺口 {r['gaps']:,}  "
              f"重复 {r['duplicates']}  乱序 {r['disorder']}  坏 OHLC {r['bad_ohlc']}")

    with open(os.path.join(d, "quality_5m.json"), "w", encoding="utf-8") as fh:
        json.dump(out, fh, ensure_ascii=False, indent=2)
        fh.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
