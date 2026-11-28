#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""collect.py 的离线自测：不联网，验证解析 / 去重 / 交易所钉住 / 失败隔离。

运行：python scripts/selftest.py
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import collect  # noqa: E402


def sec(minutes_ago):
    return int(time.time() - minutes_ago * 60)


def candle(open_min_ago, vol="12.5", close="105.0"):
    """Coinbase 格式：[time, low, high, open, close, volume]"""
    t = sec(open_min_ago)
    return [t, "100.0", "110.0", "101.0", close, vol]


def build_fixtures():
    return {
        "coinbase:BTC-USD": [
            candle(15, close="105.0"),
            candle(10, close="106.5"),
            candle(-5, close="999.0"),        # 还没收盘 —— 必须丢弃
        ],
        "kraken_futures:tickers": {
            "result": "success",
            "tickers": [
                {"symbol": "PF_ETHUSD", "markPrice": "1.0", "fundingRate": "0.0"},
                {"symbol": "PF_XBTUSD", "markPrice": "106.4", "indexPrice": "106.3",
                 "fundingRate": "0.00010000", "openInterest": "12345.6"},
            ],
        },
    }


def make_config(tmp):
    cfg = {
        "repo": {"owner": "t", "name": "t", "branch": "data"},
        "symbols": [{"name": "BTCUSDT", "coinbase": "BTC-USD", "kraken": "XBTUSD",
                     "kraken_fut": "PF_XBTUSD"}],
        "venues": {"spot": ["coinbase", "kraken"], "deriv": ["kraken_futures"]},
        "bar_seconds": 300,
        "backfill_bars": 288,
    }
    path = os.path.join(tmp, "config.json")
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(cfg, fh)
    return path


def main():
    tmp = os.path.abspath(os.path.join(HERE, "..", "_selftest"))
    shutil.rmtree(tmp, ignore_errors=True)
    os.makedirs(tmp, exist_ok=True)

    cfg_path = make_config(tmp)
    fx_path = os.path.join(tmp, "responses.json")
    with open(fx_path, "w", encoding="utf-8") as fh:
        json.dump(build_fixtures(), fh)
    out = os.path.join(tmp, "data")

    # 第一轮
    collect.main(["--config", cfg_path, "--out", out, "--fixtures", fx_path,
                  "--loop-minutes", "0"])
    krows = collect.read_csv(os.path.join(out, "BTCUSDT_5m.csv"))
    assert len(krows) == 2, f"应只保留 2 根已收盘 K 线，实际 {len(krows)}"
    assert krows[-1]["close"] == "106.5", "未来 bar 未被丢弃"
    assert krows[-1]["source"] == "coinbase", "source 列未写对"

    srows = collect.read_csv(os.path.join(out, "BTCUSDT_snapshot_5m.csv"))
    assert len(srows) == 1, f"快照应有 1 行，实际 {len(srows)}"
    assert srows[0]["mark_price"] == "106.4", "标记价解析错误"
    assert srows[0]["open_interest"] == "12345.6", "持仓量解析错误"
    assert srows[0]["source"] == "kraken_futures", "快照来源未记录"

    meta = json.load(open(os.path.join(out, "meta.json"), encoding="utf-8"))
    assert meta["venue"]["BTCUSDT"] == "coinbase", "交易所未钉住"
    print("  第一轮 OK：2 根 K 线 + 1 行合约快照，钉住 coinbase")

    # 第二轮：同样输入，不应新增
    collect.main(["--config", cfg_path, "--out", out, "--fixtures", fx_path,
                  "--loop-minutes", "0"])
    assert len(collect.read_csv(os.path.join(out, "BTCUSDT_5m.csv"))) == 2, "重复写入 K 线"
    assert len(collect.read_csv(os.path.join(out, "BTCUSDT_snapshot_5m.csv"))) == 1, "重复写入快照"
    print("  第二轮 OK：去重生效，无新增行")

    # 第三轮：现货源挂掉 —— 不能偷偷切到 kraken，必须报错并保持数据干净
    broken = {"kraken_futures:tickers": build_fixtures()["kraken_futures:tickers"]}
    broken_path = os.path.join(tmp, "broken.json")
    with open(broken_path, "w", encoding="utf-8") as fh:
        json.dump(broken, fh)
    rc = collect.main(["--config", cfg_path, "--out", out, "--fixtures", broken_path,
                       "--loop-minutes", "0"])
    assert rc == 1, f"取数全失败时应返回 1，实际 {rc}"
    assert len(collect.read_csv(os.path.join(out, "BTCUSDT_5m.csv"))) == 2, "失败轮次污染了数据"
    status = json.load(open(os.path.join(out, "status.json"), encoding="utf-8"))
    assert status["errors"], "失败未记录到 status"
    assert status["symbols"]["BTCUSDT"].get("kline_error"), "缺少 kline_error"
    print("  第三轮 OK：现货源挂掉时报错、不换源、不污染数据")

    print(f"OK  全部自测通过（{tmp}）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
