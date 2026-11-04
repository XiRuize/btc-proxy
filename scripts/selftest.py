#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""collect.py 的离线自测：不联网，验证解析/去重/快照拼装三条逻辑。

运行：python scripts/selftest.py
通过时打印 OK 并返回 0；断言失败会抛 AssertionError（返回 1）。
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


def ms(minutes_ago):
    return int((time.time() - minutes_ago * 60) * 1000)


def kline(open_min_ago, close_min_ago, close_price):
    """构造一条 /api/v3/klines 记录（12 列）。"""
    return [
        ms(open_min_ago), "100.0", "110.0", "90.0", str(close_price), "12.5",
        ms(close_min_ago), "1250.0", 345, "6.0", "600.0", "0",
    ]


def build_fixtures():
    return {
        "klines:BTCUSDT": [
            kline(15, 10, 105.0),
            kline(10, 5, 106.5),
            kline(0, -5, 999.0),         # 还没收盘（close_time 在未来）—— 必须被丢弃
        ],
        "premium:BTCUSDT": {
            "symbol": "BTCUSDT", "markPrice": "106.4", "indexPrice": "106.3",
            "lastFundingRate": "0.00010000", "nextFundingTime": ms(-30),
        },
        "oi:BTCUSDT": {"symbol": "BTCUSDT", "openInterest": "12345.6", "time": ms(3)},
        "lsr:BTCUSDT": [
            {"symbol": "BTCUSDT", "longShortRatio": "1.85", "longAccount": "0.65",
             "shortAccount": "0.35", "timestamp": ms(3)}
        ],
    }


def main():
    tmp = os.path.abspath(os.path.join(HERE, "..", "_selftest"))
    shutil.rmtree(tmp, ignore_errors=True)
    os.makedirs(tmp, exist_ok=True)
    cfg_path = os.path.join(tmp, "config.json")
    with open(cfg_path, "w", encoding="utf-8") as fh:
        json.dump({
            "symbols": ["BTCUSDT"],
            "spot_base": "https://api.binance.com",
            "futures_base": "https://fapi.binance.com",
            "kline_interval": "5m",
            "backfill_days": 3,
        }, fh)

    fx_path = os.path.join(tmp, "responses.json")
    with open(fx_path, "w", encoding="utf-8") as fh:
        json.dump(build_fixtures(), fh)

    out = os.path.join(tmp, "data")

    # 第一轮：应写入 2 根已收盘 K 线 + 1 行快照
    rc = collect.main(["--config", cfg_path, "--out", out, "--fixtures", fx_path])
    assert rc == 0, f"第一轮应成功，实际 rc={rc}"

    krows = collect.read_csv(os.path.join(out, "BTCUSDT_5m.csv"))
    assert len(krows) == 2, f"应只保留 2 根已收盘 K 线，实际 {len(krows)}"
    assert krows[-1]["close"] == "106.5", "最后一根收盘价应为 106.5（未来 bar 被丢弃）"
    assert all(r["open_time_utc"].endswith("Z") for r in krows), "时间戳应为 UTC ISO 格式"

    srows = collect.read_csv(os.path.join(out, "BTCUSDT_snapshot_5m.csv"))
    assert len(srows) == 1, f"快照应有 1 行，实际 {len(srows)}"
    assert srows[0]["mark_price"] == "106.4", "标记价解析错误"
    assert srows[0]["open_interest"] == "12345.6", "持仓量解析错误"
    assert srows[0]["long_short_ratio"] == "1.85", "多空比解析错误"

    latest = json.load(open(os.path.join(out, "latest.json"), encoding="utf-8"))
    assert latest["symbols"]["BTCUSDT"]["last_close"] == "106.5", "latest.json 收盘价错误"

    status = json.load(open(os.path.join(out, "status.json"), encoding="utf-8"))
    assert status["ok"] == ["BTCUSDT"] and not status["errors"], f"status 异常: {status}"

    # 第二轮：同样输入，不应新增任何行（去重）
    collect.main(["--config", cfg_path, "--out", out, "--fixtures", fx_path])
    assert len(collect.read_csv(os.path.join(out, "BTCUSDT_5m.csv"))) == 2, "重复运行写入了重复行"
    assert len(collect.read_csv(os.path.join(out, "BTCUSDT_snapshot_5m.csv"))) == 1, "快照重复写入"

    # 合约接口全挂时，K 线仍要落盘（部分失败不影响主数据）
    broken = {"klines:BTCUSDT": [kline(20, 15, 101.0)]}
    broken_path = os.path.join(tmp, "broken.json")
    with open(broken_path, "w", encoding="utf-8") as fh:
        json.dump(broken, fh)
    rc = collect.main(["--config", cfg_path, "--out", out, "--fixtures", broken_path])
    assert rc == 0, "部分失败时仍应返回 0"
    assert len(collect.read_csv(os.path.join(out, "BTCUSDT_5m.csv"))) == 3, "K 线未追加"
    status = json.load(open(os.path.join(out, "status.json"), encoding="utf-8"))
    assert status["partially_failed"] is True and status["errors"], "部分失败未记录到 status"

    print(f"OK  自测通过（临时目录 {tmp}）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
