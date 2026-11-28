#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""本机拉取器：把 data 分支上的 CSV 同步到本地数据源目录。

本机连不上币安，但 raw.githubusercontent.com 可达，所以读的是仓库里的
data 分支（采集器每 5 分钟重写该分支）。

用法：
  python scripts/pull_local.py --config config.json
  python scripts/pull_local.py --config config.json --out ../parquet/crypto
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone

try:                       # Windows 控制台默认 GBK，避免中文日志乱码
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:          # noqa: BLE001
    pass

UA = "btc-proxy-pull/1.0"


def http_get(url, timeout=20):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def parse_ts(txt):
    return datetime.strptime(txt, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def main(argv=None):
    ap = argparse.ArgumentParser(description="从 GitHub raw 拉取采集结果")
    ap.add_argument("--config", default="config.json")
    ap.add_argument("--out", default=None)
    args = ap.parse_args(argv)

    cfg_path = args.config if os.path.isabs(args.config) else os.path.join(os.getcwd(), args.config)
    with open(cfg_path, encoding="utf-8") as fh:
        cfg = json.load(fh)

    repo = cfg["repo"]
    out = args.out or os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", cfg.get("local_output", "../parquet/crypto"))
    out = os.path.abspath(out)
    os.makedirs(out, exist_ok=True)

    base = f"https://raw.githubusercontent.com/{repo['owner']}/{repo['name']}/{repo['branch']}/data"
    print(f"仓库分支：{repo['owner']}/{repo['name']}@{repo['branch']}")
    print(f"输出目录：{out}\n")

    if repo["owner"] == "YOUR_GITHUB_NAME":
        print("!! config.json 里的 owner 还是占位符，先把仓库建好并改成真实用户名。")
        return 2

    now = datetime.now(timezone.utc)
    failed = []

    for sym in cfg["symbols"]:
        for suffix in ("_5m.csv", "_snapshot_5m.csv"):
            name = f"{sym}{suffix}"
            url = f"{base}/{name}"
            try:
                raw = http_get(url)
            except urllib.error.HTTPError as exc:
                print(f"  {name:28s} HTTP {exc.code}（仓库还没有这个文件？）")
                failed.append(name)
                continue
            except Exception as exc:  # noqa: BLE001
                print(f"  {name:28s} 失败：{exc}")
                failed.append(name)
                continue

            dst = os.path.join(out, name)
            with open(dst, "wb") as fh:
                fh.write(raw)

            text = raw.decode("utf-8", "replace")
            rows = list(csv.DictReader(text.splitlines()))
            if not rows:
                print(f"  {name:28s} 0 行")
                continue

            last = rows[-1]
            ts_field = "ts_utc" if "snapshot" in name else "open_time_utc"
            age = ""
            try:
                age_min = (now - parse_ts(last[ts_field])).total_seconds() / 60
                age = f"，滞后 {age_min:.0f} 分钟"
            except Exception:  # noqa: BLE001
                pass
            print(f"  {name:28s} {len(rows):>7} 行，最后 {last[ts_field]}{age}")

    try:
        latest = http_get(f"{base}/latest.json")
        with open(os.path.join(out, "latest.json"), "wb") as fh:
            fh.write(latest)
        doc = json.loads(latest)
        print(f"\n快照生成时间：{doc.get('generated_at')}")
        for sym, row in doc.get("symbols", {}).items():
            print(f"  {sym}: 最新收盘 {row.get('last_close','?')}  标记价 {row.get('mark_price','?')}  资金费率 {row.get('funding_rate','?')}")
    except Exception as exc:  # noqa: BLE001
        print(f"\nlatest.json 拉取失败：{exc}")

    if failed:
        print(f"\n缺失 {len(failed)} 个文件：{', '.join(failed)}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
