#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""币安 5 分钟快照采集器 —— 在 GitHub Actions runner 上运行。

为什么放 Actions 里跑：本机到 api.binance.com 的 DNS 被污染、直连不通，
而 GitHub 的 runner 在境外可以正常调用；runner 把结果 commit 回仓库，
本机再通过 raw.githubusercontent.com 读取（该域名已验证可达）。

设计约束：
  * 只用标准库（runner 上不装任何包，减少失败面）
  * 增量追加 + 按时间戳去重，重复运行不会写重复行
  * 单个币种/单个接口失败不拖垮整轮，状态写进 status.json
  * 只写"已收盘"的 K 线，避免半根 bar 混进数据

用法：
  python scripts/collect.py --config config.json --out data
  python scripts/collect.py --config config.json --out data --fixtures fixtures/responses.json  # 离线自测
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

try:                       # Windows 控制台默认 GBK，避免中文日志乱码
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:          # noqa: BLE001
    pass

FIELDS_KLINE = [
    "open_time_utc", "open", "high", "low", "close", "volume", "quote_volume",
    "trades", "taker_buy_base", "taker_buy_quote", "close_time_utc", "fetched_at",
]
FIELDS_SNAP = [
    "ts_utc", "symbol", "mark_price", "index_price", "funding_rate",
    "next_funding_time_utc", "open_interest", "long_short_ratio", "fetched_at",
]
UA = "btc-proxy/1.0 (+https://github.com/)"


# ---------------------------------------------------------------- 基础工具

def iso(ms):
    """毫秒时间戳 -> ISO8601 UTC 字符串。"""
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def read_csv(path):
    if not os.path.exists(path):
        return []
    with open(path, newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def write_csv_atomic(path, fields, rows):
    """先写临时文件再替换，避免 runner 中途被 kill 留下半截文件。"""
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow(row)
    os.replace(tmp, path)


def upsert(path, fields, new_rows, key):
    """按 key 合并去重后排序写回，返回 (总行数, 新增行数)。"""
    existing = read_csv(path)
    merged = {r[key]: r for r in existing}
    before = len(merged)
    for row in new_rows:
        merged[row[key]] = row
    rows = sorted(merged.values(), key=lambda r: r[key])
    write_csv_atomic(path, fields, rows)
    return len(rows), len(rows) - before


# ---------------------------------------------------------------- 取数

class Fetcher:
    """带重试的 JSON GET；fixtures 提供时完全走离线数据。"""

    def __init__(self, timeout=10, retries=3, fixtures=None):
        self.timeout = timeout
        self.retries = retries
        self.fixtures = fixtures or {}
        self.calls = 0

    def get(self, key, url):
        self.calls += 1
        if self.fixtures:
            if key not in self.fixtures:
                raise KeyError(f"fixture 缺少 key: {key}")
            return self.fixtures[key]

        last = None
        for attempt in range(self.retries):
            try:
                req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    body = resp.read().decode("utf-8")
                return json.loads(body)
            except Exception as exc:  # noqa: BLE001 - 网络错误类型很多，统一重试
                last = exc
                if attempt < self.retries - 1:
                    time.sleep(1.5 * (attempt + 1))
        raise RuntimeError(f"{url} 连续 {self.retries} 次失败: {last}")


def parse_klines(payload, symbol, fetched_at):
    """把 /api/v3/klines 的返回转成行，丢掉尚未收盘的最后一根。"""
    rows = []
    now_ms = int(time.time() * 1000)
    for k in payload:
        open_time, close_time = int(k[0]), int(k[6])
        if close_time > now_ms:      # 仍在走的那根，跳过
            continue
        rows.append({
            "open_time_utc": iso(open_time),
            "open": k[1], "high": k[2], "low": k[3], "close": k[4],
            "volume": k[5], "quote_volume": k[7], "trades": k[8],
            "taker_buy_base": k[9], "taker_buy_quote": k[10],
            "close_time_utc": iso(close_time),
            "fetched_at": fetched_at,
        })
    return rows


def build_snapshot(symbol, premium, oi, lsr, fetched_at):
    """把三个合约接口拼成一行快照；任一缺失就留空，不整行丢弃。"""
    def pick(obj, *names):
        if not isinstance(obj, dict):
            return ""
        for n in names:
            if n in obj and obj[n] not in (None, ""):
                return obj[n]
        return ""

    ts = ""
    if isinstance(lsr, list) and lsr:
        ts = iso(int(lsr[0].get("timestamp", 0)) or int(time.time() * 1000))
    if not ts:
        ts = iso(int(oi.get("time", 0)) or int(time.time() * 1000))

    return {
        "ts_utc": ts,
        "symbol": symbol,
        "mark_price": pick(premium, "markPrice"),
        "index_price": pick(premium, "indexPrice"),
        "funding_rate": pick(premium, "lastFundingRate"),
        "next_funding_time_utc": iso(int(premium["nextFundingTime"])) if premium.get("nextFundingTime") else "",
        "open_interest": pick(oi, "openInterest"),
        "long_short_ratio": pick(lsr[0], "longShortRatio") if isinstance(lsr, list) and lsr else "",
        "fetched_at": fetched_at,
    }


# ---------------------------------------------------------------- 主流程

def collect_one(sym, cfg, fetcher, out_dir, status):
    fetched_at = now_iso()
    spot = cfg["spot_base"].rstrip("/")
    fut = cfg["futures_base"].rstrip("/")
    interval = cfg.get("kline_interval", "5m")

    kline_path = os.path.join(out_dir, f"{sym}_5m.csv")
    snap_path = os.path.join(out_dir, f"{sym}_snapshot_5m.csv")
    info = {"symbol": sym}

    # 1) K 线：首次运行回补若干天，之后只取最新 2 根
    have_rows = len(read_csv(kline_path))
    limit = 1000 if have_rows == 0 else 2
    if have_rows == 0:
        days = int(cfg.get("backfill_days", 3))
        limit = min(1000, max(3, days * 288))
    try:
        payload = fetcher.get(f"klines:{sym}", f"{spot}/api/v3/klines?symbol={sym}&interval={interval}&limit={limit}")
        rows = parse_klines(payload, sym, fetched_at)
        total, added = upsert(kline_path, FIELDS_KLINE, rows, "open_time_utc")
        info.update(kline_rows=total, kline_added=added, kline_last=rows[-1]["open_time_utc"] if rows else "")
    except Exception as exc:  # noqa: BLE001
        info["kline_error"] = str(exc)
        status["errors"].append(f"{sym} kline: {exc}")

    # 2) 合约快照：标记价 / 资金费率 / 持仓量 / 大户多空比
    try:
        premium = fetcher.get(f"premium:{sym}", f"{fut}/fapi/v1/premiumIndex?symbol={sym}")
        oi = fetcher.get(f"oi:{sym}", f"{fut}/fapi/v1/openInterest?symbol={sym}")
        lsr_raw = fetcher.get(
            f"lsr:{sym}",
            f"{fut}/futures/data/globalLongShortAccountRatio?symbol={sym}&period=5m&limit=1",
        )
        if isinstance(lsr_raw, dict):          # 接口报错时返回 {"code":..., "msg":...}
            lsr_raw = []
        row = build_snapshot(sym, premium, oi, lsr_raw, fetched_at)
        total, added = upsert(snap_path, FIELDS_SNAP, [row], "ts_utc")
        info.update(snapshot_rows=total, snapshot_added=added, snapshot_last=row["ts_utc"])
    except Exception as exc:  # noqa: BLE001
        info["snapshot_error"] = str(exc)
        status["errors"].append(f"{sym} snapshot: {exc}")

    return info


def main(argv=None):
    ap = argparse.ArgumentParser(description="币安 5 分钟快照采集器")
    ap.add_argument("--config", default="config.json")
    ap.add_argument("--out", default="data")
    ap.add_argument("--fixtures", help="离线自测用的 JSON 响应包")
    args = ap.parse_args(argv)

    with open(args.config, encoding="utf-8") as fh:
        cfg = json.load(fh)

    fixtures = None
    if args.fixtures:
        with open(args.fixtures, encoding="utf-8") as fh:
            fixtures = json.load(fh)

    os.makedirs(args.out, exist_ok=True)
    fetcher = Fetcher(
        timeout=cfg.get("timeout_sec", 10),
        retries=cfg.get("retries", 3),
        fixtures=fixtures,
    )

    started = now_iso()
    status = {"started_at": started, "ok": [], "errors": [], "symbols": {}}

    for sym in cfg["symbols"]:
        info = collect_one(sym, cfg, fetcher, args.out, status)
        status["symbols"][sym] = info
        status["ok"].append(sym)

    status["finished_at"] = now_iso()
    status["http_calls"] = fetcher.calls
    status["partially_failed"] = bool(status["errors"])

    # latest.json：给本机一个"一眼看当前价"的入口
    latest = {
        "generated_at": status["finished_at"],
        "symbols": {},
    }
    for sym in cfg["symbols"]:
        rows = read_csv(os.path.join(args.out, f"{sym}_snapshot_5m.csv"))
        if rows:
            latest["symbols"][sym] = rows[-1]
            krows = read_csv(os.path.join(args.out, f"{sym}_5m.csv"))
            if krows:
                latest["symbols"][sym]["last_close"] = krows[-1]["close"]
                latest["symbols"][sym]["last_bar"] = krows[-1]["open_time_utc"]

    for name, obj in (("status.json", status), ("latest.json", latest)):
        with open(os.path.join(args.out, name), "w", encoding="utf-8") as fh:
            json.dump(obj, fh, ensure_ascii=False, indent=2)
            fh.write("\n")

    print(json.dumps(status, ensure_ascii=False, indent=2))
    # 全失败才返回非 0，让 workflow 变红但仍保留已抓到的部分
    return 0 if status["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
