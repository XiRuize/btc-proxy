#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把 history/ 里的原始 CSV 整理成"可上传 GitHub"的归档包。

为什么要瘦身：5 分钟线 9 年近 97 万行，原始 CSV.gz 有 50 MB；价格保留 2 位小数、
只保留研究常用的 7 列后降到 23 MB，三个币种合计 ~70 MB，才适合放进 git。

输出（archive/）：
  {SYM}_5m.csv.gz      7 列：open_time,open,high,low,close,volume,taker_buy_base
  {SYM}_1d.csv.gz      日线，完整列
  {SYM}_funding.csv    8h 资金费率
  manifest.json         行数/区间/sha256/大小，便于校验

用法：python scripts/build_archive.py
"""
from __future__ import annotations

import gzip
import hashlib
import json
import os
import shutil
import sys

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:  # noqa: BLE001
    pass

SYMBOLS = ["BTCUSDT", "ETHUSDT", "SOLUSDT"]
SLIM_HEADER = "open_time,open,high,low,close,volume,taker_buy_base"


def sha256(path, chunk=1 << 20):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        while True:
            b = fh.read(chunk)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


def slim_5m(src, dst):
    rows = []
    with gzip.open(src, "rt", encoding="utf-8") as fh:
        next(fh, None)
        for line in fh:
            p = line.rstrip("\n").split(",")
            if len(p) < 11:
                continue
            try:
                rows.append((
                    int(p[0]),
                    ",".join([p[0]] + [f"{float(x):.2f}" for x in p[1:5]]
                             + [f"{float(p[5]):.4f}", f"{float(p[9]):.4f}"]),
                ))
            except ValueError:
                continue
    rows.sort(key=lambda r: r[0])
    with gzip.open(dst, "wb", compresslevel=9) as fh:
        fh.write((SLIM_HEADER + "\n").encode())
        fh.write(("\n".join(r[1] for r in rows) + "\n").encode())
    return len(rows), rows[0][0], rows[-1][0]


def copy_gz(src, dst):
    """已经是 .gz 的直接拷（不二次压缩），未压缩的再压。"""
    if src.endswith(".gz"):
        shutil.copyfile(src, dst)
    else:
        with open(src, "rb") as i, gzip.open(dst, "wb", compresslevel=9) as o:
            o.write(i.read())
    with gzip.open(dst, "rt", encoding="utf-8") as f:
        n = sum(1 for _ in f) - 1
    return n


def main():
    here = os.path.dirname(os.path.abspath(__file__))
    root = os.path.abspath(os.path.join(here, "..", "..", "parquet", "crypto"))
    src_dir = os.path.join(root, "history")
    out_dir = os.path.abspath(os.path.join(here, "..", "..", "parquet", "crypto", "archive"))
    os.makedirs(out_dir, exist_ok=True)

    manifest = {"generated_from": src_dir, "files": {}}
    for sym in SYMBOLS:
        m5 = os.path.join(src_dir, f"{sym}_5m.csv.gz")
        if os.path.exists(m5):
            dst = os.path.join(out_dir, f"{sym}_5m.csv.gz")
            n, t0, t1 = slim_5m(m5, dst)
            manifest["files"][f"{sym}_5m.csv.gz"] = {
                "rows": n, "bytes": os.path.getsize(dst), "sha256": sha256(dst),
                "first_ts": t0, "last_ts": t1,
                "columns": SLIM_HEADER.split(","),
            }
            print(f"  {sym}_5m.csv.gz  {n} 行  {os.path.getsize(dst)/1e6:.1f} MB")

        d1 = os.path.join(src_dir, f"{sym}_1d.csv.gz")
        if os.path.exists(d1):
            dst = os.path.join(out_dir, f"{sym}_1d.csv.gz")
            n = copy_gz(d1, dst)
            manifest["files"][f"{sym}_1d.csv.gz"] = {
                "rows": n, "bytes": os.path.getsize(dst), "sha256": sha256(dst),
            }
            print(f"  {sym}_1d.csv.gz  {n} 行  {os.path.getsize(dst)/1e3:.0f} KB")

        fd = os.path.join(src_dir, f"{sym}_funding.csv")
        if os.path.exists(fd):
            dst = os.path.join(out_dir, f"{sym}_funding.csv.gz")
            n = copy_gz(fd, dst)
            manifest["files"][f"{sym}_funding.csv.gz"] = {
                "rows": n, "bytes": os.path.getsize(dst), "sha256": sha256(dst),
            }
            print(f"  {sym}_funding.csv.gz  {n} 行")

    mpath = os.path.join(out_dir, "manifest.json")
    with open(mpath, "w", encoding="utf-8") as fh:
        json.dump(manifest, fh, ensure_ascii=False, indent=2)
        fh.write("\n")
    total = sum(v["bytes"] for v in manifest["files"].values())
    print(f"\n合计 {total/1e6:.1f} MB，manifest 已写入 {mpath}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
