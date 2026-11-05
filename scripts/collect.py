#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""多交易所 5 分钟快照采集器 —— 在 GitHub Actions runner 上运行。

背景（2026-11 实测）：
  * Binance 对 GitHub 机房 IP 返回 HTTP 451（地区封锁），不能用；
  * 改用美国合规交易所：Coinbase（现货）、Kraken（现货）、Kraken Futures（资金费率/持仓量），
    它们在 Azure 机房可正常访问，且不需要 API Key。

设计：
  * 只用标准库（runner 上不装包）；
  * 每个币种第一次成功用哪个交易所就"钉住"（写进 data/meta.json），之后一直用它，
    避免同一份 CSV 里混入不同交易所的价格；
  * 只写已收盘的 bar；按时间戳去重，重复运行不产生重复行；
  * 单个币种/单个接口失败不拖垮整轮，状态写进 status.json。

用法：
  python scripts/collect.py --config config.json --out data
  python scripts/collect.py --config config.json --out data --fixtures fixtures/responses.json
  python scripts/collect.py --config config.json --out data --loop-minutes 28   # 持续采集
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import sys
import time
import urllib.request
from datetime import datetime, timezone

try:                       # Windows 控制台默认 GBK，避免中文日志乱码
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:          # noqa: BLE001
    pass

FIELDS_KLINE = [
    "open_time_utc", "open", "high", "low", "close", "volume", "quote_volume",
    "trades", "taker_buy_base", "taker_buy_quote", "close_time_utc", "source", "fetched_at",
]
FIELDS_SNAP = [
    "ts_utc", "symbol", "mark_price", "index_price", "funding_rate",
    "next_funding_time_utc", "open_interest", "long_short_ratio", "source", "fetched_at",
]
UA = "btc-proxy/2.0"


# ---------------------------------------------------------------- 基础工具

def iso(ms):
    return datetime.fromtimestamp(int(ms) / 1000, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def read_csv(path):
    if not os.path.exists(path):
        return []
    with open(path, newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def write_csv_atomic(path, fields, rows):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow(row)
    os.replace(tmp, path)


def upsert(path, fields, new_rows, key):
    existing = read_csv(path)
    merged = {r[key]: r for r in existing}
    before = len(merged)
    for row in new_rows:
        merged[row[key]] = row
    rows = sorted(merged.values(), key=lambda r: r[key])
    write_csv_atomic(path, fields, rows)
    return len(rows), len(rows) - before


def load_json(path, default):
    if not os.path.exists(path):
        return default
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:  # noqa: BLE001
        return default


# ---------------------------------------------------------------- 取数

class Fetcher:
    def __init__(self, timeout=15, retries=3, fixtures=None):
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
            except Exception as exc:  # noqa: BLE001
                last = exc
                if attempt < self.retries - 1:
                    time.sleep(1.5 * (attempt + 1))
        raise RuntimeError(f"{url} 连续 {self.retries} 次失败: {last}")


# ---------------------------------------------------------------- 现货适配

def spot_coinbase(cfg, fetcher, bar_sec, backfill):
    """Coinbase Exchange 公开 K 线：[[time, low, high, open, close, volume], ...]"""
    product = cfg["coinbase"]
    url = (f"https://api.exchange.coinbase.com/products/{product}/candles"
           f"?granularity={bar_sec}")
    data = fetcher.get(f"coinbase:{product}", url)
    if not isinstance(data, list):
        raise RuntimeError(f"Coinbase 返回异常: {str(data)[:120]}")
    now = int(time.time())
    rows = []
    for t, low, high, open_, close, vol in data:
        t = int(t)
        if t + bar_sec > now:          # 未收盘
            continue
        rows.append({
            "open_time_utc": iso(t * 1000),
            "open": open_, "high": high, "low": low, "close": close,
            "volume": vol, "quote_volume": "", "trades": "",
            "taker_buy_base": "", "taker_buy_quote": "",
            "close_time_utc": iso((t + bar_sec) * 1000 - 1000),
            "source": "coinbase",
        })
    rows.sort(key=lambda r: r["open_time_utc"])
    return rows[-backfill:] if backfill else rows


def spot_kraken(cfg, fetcher, bar_sec, backfill):
    """Kraken OHLC：result[pair] = [[time, open, high, low, close, vwap, volume, count], ...]"""
    pair = cfg["kraken"]
    url = f"https://api.kraken.com/0/public/OHLC?pair={pair}&interval={bar_sec // 60}"
    data = fetcher.get(f"kraken:{pair}", url)
    if not isinstance(data, dict) or data.get("error"):
        raise RuntimeError(f"Kraken 返回异常: {str(data)[:120]}")
    series = next((v for k, v in data["result"].items() if k != "last"), None)
    if not series:
        raise RuntimeError("Kraken 结果为空")
    now = int(time.time())
    rows = []
    for item in series:
        t = int(item[0])
        if t + bar_sec > now:
            continue
        rows.append({
            "open_time_utc": iso(t * 1000),
            "open": item[1], "high": item[2], "low": item[3], "close": item[4],
            "volume": item[6], "quote_volume": "", "trades": item[7],
            "taker_buy_base": "", "taker_buy_quote": "",
            "close_time_utc": iso((t + bar_sec) * 1000 - 1000),
            "source": "kraken",
        })
    rows.sort(key=lambda r: r["open_time_utc"])
    return rows[-backfill:] if backfill else rows


SPOT = {"coinbase": spot_coinbase, "kraken": spot_kraken}


# ---------------------------------------------------------------- 合约适配

def deriv_kraken_futures(cfg, fetcher):
    """Kraken Futures tickers 含 markPrice / indexPrice / fundingRate / openInterest。"""
    sym = cfg["kraken_fut"]
    data = fetcher.get("kraken_futures:tickers", "https://futures.kraken.com/derivatives/api/v3/tickers")
    tickers = data.get("tickers") if isinstance(data, dict) else None
    if not tickers:
        raise RuntimeError(f"Kraken Futures 返回异常: {str(data)[:120]}")
    hit = next((t for t in tickers if t.get("symbol") == sym), None)
    if not hit:
        raise RuntimeError(f"Kraken Futures 找不到 {sym}")
    return {
        "ts_utc": now_iso()[:17] + "00Z",          # 对齐到分钟
        "symbol": cfg["name"],
        "mark_price": hit.get("markPrice", ""),
        "index_price": hit.get("indexPrice", ""),
        "funding_rate": hit.get("fundingRate", ""),
        "next_funding_time_utc": "",
        "open_interest": hit.get("openInterest", ""),
        "long_short_ratio": "",
        "source": "kraken_futures",
    }


DERIV = {"kraken_futures": deriv_kraken_futures}


def derive_snapshot_from_spot(row_spot, name):
    """合约接口全挂时的兜底：用现货收盘价当 mark，保证时间轴不断。"""
    if not row_spot:
        return None
    return {
        "ts_utc": row_spot["open_time_utc"],
        "symbol": name,
        "mark_price": row_spot["close"],
        "index_price": "",
        "funding_rate": "",
        "next_funding_time_utc": "",
        "open_interest": "",
        "long_short_ratio": "",
        "source": "spot_fallback",
    }


# ---------------------------------------------------------------- 主流程

def collect_one(cfg, venues, fetcher, out_dir, meta, status):
    name = cfg["name"]
    bar_sec = int(cfg.get("_bar_seconds", 300))
    backfill = 0 if meta["venue"].get(name) else int(cfg.get("_backfill_bars", 288))

    info = {"symbol": name}
    kline_path = os.path.join(out_dir, f"{name}_5m.csv")
    snap_path = os.path.join(out_dir, f"{name}_snapshot_5m.csv")

    pinned = meta["venue"].get(name)
    # 已钉住就只试这一家：宁可留缺口，也不让同一份 CSV 混入两家交易所的价格
    order = [pinned] if pinned else list(venues["spot"])
    rows, used = [], None
    for venue in order:
        try:
            rows = SPOT[venue](cfg, fetcher, bar_sec, backfill)
            if rows:
                used = venue
                break
            info.setdefault("spot_notes", []).append(f"{venue}: 返回 0 行")
        except Exception as exc:  # noqa: BLE001
            info.setdefault("spot_errors", []).append(f"{venue}: {exc}")
            status["errors"].append(f"{name} spot/{venue}: {exc}")

    if not used:
        info["kline_error"] = "所有现货源均失败"
        status["errors"].append(f"{name}: 所有现货源均失败")
        return info

    meta["venue"][name] = used
    total, added = upsert(kline_path, FIELDS_KLINE, rows, "open_time_utc")
    info.update(kline_rows=total, kline_added=added, kline_source=used,
                kline_last=rows[-1]["open_time_utc"])

    made = False
    for venue in venues["deriv"]:
        try:
            row = DERIV[venue](cfg, fetcher)
            total_s, added_s = upsert(snap_path, FIELDS_SNAP, [row], "ts_utc")
            info.update(snapshot_rows=total_s, snapshot_added=added_s,
                        snapshot_source=venue, snapshot_last=row["ts_utc"])
            made = True
            break
        except Exception as exc:  # noqa: BLE001
            info.setdefault("deriv_errors", []).append(f"{venue}: {exc}")
            status["errors"].append(f"{name} deriv/{venue}: {exc}")

    if not made:
        row = derive_snapshot_from_spot(rows[-1], name)
        if row:
            total_s, added_s = upsert(snap_path, FIELDS_SNAP, [row], "ts_utc")
            info.update(snapshot_rows=total_s, snapshot_added=added_s,
                        snapshot_source="spot_fallback", snapshot_last=row["ts_utc"])
    return info


def main(argv=None):
    ap = argparse.ArgumentParser(description="多交易所 5 分钟快照采集器")
    ap.add_argument("--config", default="config.json")
    ap.add_argument("--out", default="data")
    ap.add_argument("--fixtures", help="离线自测用的 JSON 响应包")
    ap.add_argument("--loop-minutes", type=float, default=None, help="持续采集多少分钟（0=只跑一轮）")
    args = ap.parse_args(argv)

    with open(args.config, encoding="utf-8") as fh:
        cfg = json.load(fh)

    fixtures = None
    if args.fixtures:
        with open(args.fixtures, encoding="utf-8") as fh:
            fixtures = json.load(fh)

    os.makedirs(args.out, exist_ok=True)
    fetcher = Fetcher(
        timeout=cfg.get("timeout_sec", 15),
        retries=cfg.get("retries", 3),
        fixtures=fixtures,
    )

    bar_seconds = int(cfg.get("bar_seconds", 300))
    for s in cfg["symbols"]:
        s["_bar_seconds"] = bar_seconds
        s["_backfill_bars"] = int(cfg.get("backfill_bars", 288))

    meta_path = os.path.join(args.out, "meta.json")
    meta = load_json(meta_path, {"venue": {}})
    meta.setdefault("venue", {})

    loop_minutes = args.loop_minutes if args.loop_minutes is not None else float(cfg.get("loop_minutes", 0))
    deadline = time.time() + loop_minutes * 60

    rounds = 0
    status = {}
    while True:
        rounds += 1
        status = {"round": rounds, "started_at": now_iso(), "errors": [], "symbols": {}}
        for sym_cfg in cfg["symbols"]:
            status["symbols"][sym_cfg["name"]] = collect_one(
                sym_cfg, cfg["venues"], fetcher, args.out, meta, status)

        status["finished_at"] = now_iso()
        status["http_calls"] = fetcher.calls
        status["partially_failed"] = bool(status["errors"])
        status["venue"] = meta["venue"]

        latest = {"generated_at": status["finished_at"], "symbols": {}}
        for sym_cfg in cfg["symbols"]:
            name = sym_cfg["name"]
            krows = read_csv(os.path.join(args.out, f"{name}_5m.csv"))
            srows = read_csv(os.path.join(args.out, f"{name}_snapshot_5m.csv"))
            entry = {}
            if krows:
                entry.update(last_close=krows[-1]["close"], last_bar=krows[-1]["open_time_utc"],
                             source=krows[-1]["source"])
            if srows:
                entry.update(mark_price=srows[-1]["mark_price"],
                             funding_rate=srows[-1]["funding_rate"],
                             open_interest=srows[-1]["open_interest"])
            if entry:
                latest["symbols"][name] = entry

        for fname, obj in (("status.json", status), ("latest.json", latest), ("meta.json", meta)):
            with open(os.path.join(args.out, fname), "w", encoding="utf-8") as fh:
                json.dump(obj, fh, ensure_ascii=False, indent=2)
                fh.write("\n")

        print(json.dumps(status, ensure_ascii=False, indent=2))

        if time.time() + bar_seconds > deadline:
            break
        sleep_for = bar_seconds - (time.time() % bar_seconds) + 5
        if time.time() + sleep_for > deadline:
            break
        time.sleep(sleep_for)

    return 0 if all(s.get("kline_rows") for s in status.get("symbols", {}).values()) else 1


if __name__ == "__main__":
    sys.exit(main())
