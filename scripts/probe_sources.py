#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""数据源连通性探测：本机或 GitHub runner 都能跑。

为什么需要：本机受 DNS 污染/地区限制，很多源在浏览器里能看、在脚本里连不上；
而 GitHub runner 在境外，能连的又不一样。所以把候选源列成一张表，实测后才知道哪条管道能用。

用法：
  python scripts/probe_sources.py                 # 探测全部
  python scripts/probe_sources.py --only onchain  # 只测某一类
  python scripts/probe_sources.py --out result.json
"""
from __future__ import annotations

import argparse
import json
import os
import socket
import ssl
import sys
import time
import urllib.error
import urllib.request

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:  # noqa: BLE001
    pass

UA = "btc-research-probe/1.0"

# kind: onchain / deriv / etf / macro / sentiment / news / academic / venue
SOURCES = [
    # ---- 链上 ----
    ("mempool.space", "https://mempool.space/api/blocks/tip/height", "onchain", "区块高度，无需 key"),
    ("blockchain.info", "https://api.blockchain.info/stats", "onchain", "链上统计（算力/手续费/交易数）"),
    ("blockchair", "https://api.blockchair.com/bitcoin/stats", "onchain", "地址/交易统计"),
    ("coinmetrics-community", "https://community-api.coinmetrics.io/v4/timeseries/asset-metrics?assets=btc&metrics=PriceUSD,AdrActCnt&limit=2", "onchain", "免费社区版，含活跃地址"),
    ("blockstream-esplora", "https://blockstream.info/api/blocks/tip/height", "onchain", "Esplora API"),
    ("mempool-fees", "https://mempool.space/api/v1/fees/recommended", "onchain", "手续费档位"),

    # ---- 衍生品/合约 ----
    ("deribit", "https://www.deribit.com/api/v2/public/get_index_price?index_name=btc_usd", "deriv", "期权/永续，IV 与持仓"),
    ("kraken-futures", "https://futures.kraken.com/derivatives/api/v3/tickers", "deriv", "已在用：资金费率/持仓量"),
    ("bybit", "https://api.bybit.com/v5/market/tickers?category=linear&symbol=BTCUSDT", "deriv", "常封美国 IP"),
    ("okx", "https://www.okx.com/api/v5/public/time", "deriv", "常封美国 IP"),
    ("binance-fapi", "https://fapi.binance.com/fapi/v1/premiumIndex?symbol=BTCUSDT", "deriv", "已知 451"),
    ("coinglass", "https://open-api.coinglass.com/public/v2/liquidation_history?symbol=BTC", "deriv", "清算数据，需 key"),

    # ---- ETF 资金流 ----
    ("farside-uk", "https://farside.co.uk/btc/", "etf", "BTC 现货 ETF 每日净流入（HTML 表格）"),
    ("sosovalue", "https://api.sosovalue.com/api/etf/bitcoin/historical", "etf", "ETF 流量 API"),

    # ---- 宏观 ----
    ("fred", "https://fred.stlouisfed.org/graph/fredgraph.csv?id=DGS10", "macro", "美债收益率等（CSV）"),
    ("bis", "https://stats.bis.org/api/v1/data/WS_LONG_CPI/M.US/all", "macro", "国际清算银行"),
    ("worldbank", "https://api.worldbank.org/v2/country/US/indicator/NY.GDP.MKTP.CD?format=json&per_page=1", "macro", "世界银行"),

    # ---- 情绪 ----
    ("alternative-me-fng", "https://api.alternative.me/fng/?limit=2", "sentiment", "恐慌贪婪指数"),
    ("cryptopanic", "https://cryptopanic.com/api/v1/posts/?auth_token=demo&public=true", "sentiment", "新闻聚合，需 key"),

    # ---- 新闻/研究 ----
    ("gdelt", "https://api.gdeltproject.org/api/v2/doc/doc?query=bitcoin&mode=artlist&maxrecords=3&format=json", "news", "全球新闻事件库"),
    ("arxiv-qfin", "https://export.arxiv.org/api/query?search_query=cat:q-fin.TR&max_results=2", "academic", "论文预印本"),
    ("ssrn", "https://papers.ssrn.com/sol3/results.cfm", "academic", "工作论文（常反爬）"),

    # ---- 行情/聚合 ----
    ("coingecko", "https://api.coingecko.com/api/v3/ping", "venue", "已知 DNS 污染"),
    ("coinpaprika", "https://api.coinpaprika.com/v1/tickers/btc-bitcoin", "venue", "免费聚合"),
    ("binance-vision", "https://data.binance.vision/", "venue", "已在用：历史批量"),
    ("coinbase-exchange", "https://api.exchange.coinbase.com/products/BTC-USD/candles?granularity=300", "venue", "已在用：5m 实时"),

    # ---- 境内 ----
    ("eastmoney", "https://push2.eastmoney.com/api/qt/stock/get?secid=1.000001&fields=f43", "cn", "境内财经（浏览器可达）"),
    ("jin10", "https://www.jin10.com/", "cn", "快讯"),
    ("wallstreetcn", "https://api-one.wallstcn.com/apiv1/content/lives?channel=global-channel&limit=2", "cn", "华尔街见闻快讯"),
]


def probe(url, timeout=12):
    t0 = time.time()
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "*/*"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read(4096)
            return {
                "ok": True, "status": resp.status,
                "ms": int((time.time() - t0) * 1000),
                "ctype": resp.headers.get("Content-Type", ""),
                "sample": body[:160].decode("utf-8", "replace").replace("\n", " ")[:160],
            }
    except urllib.error.HTTPError as exc:
        return {"ok": False, "status": exc.code, "ms": int((time.time() - t0) * 1000),
                "error": f"HTTP {exc.code}"}
    except (urllib.error.URLError, socket.timeout, ssl.SSLError, ConnectionError) as exc:
        return {"ok": False, "status": 0, "ms": int((time.time() - t0) * 1000),
                "error": str(getattr(exc, "reason", exc))[:120]}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "status": 0, "ms": int((time.time() - t0) * 1000),
                "error": f"{type(exc).__name__}: {exc}"[:120]}


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", help="只测某一类（onchain/deriv/etf/macro/sentiment/news/academic/venue/cn）")
    ap.add_argument("--out", default=os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                                  "..", "..", "crypto-sources-probe.json"))
    args = ap.parse_args(argv)

    rows = [s for s in SOURCES if not args.only or s[2] == args.only]
    results = {}
    for sid, url, kind, note in rows:
        r = probe(url)
        r.update(kind=kind, url=url, note=note)
        results[sid] = r
        flag = "OK  " if r["ok"] else "FAIL"
        detail = f"{r['status']}" if r["ok"] else r.get("error", "")
        print(f"  [{flag}] {sid:22s} {kind:10s} {r['ms']:5d}ms  {detail}")

    out = os.path.abspath(args.out)
    os.makedirs(os.path.dirname(out), exist_ok=True)
    payload = {"probed_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "results": results}
    with open(out, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=2)
        fh.write("\n")

    ok = sum(1 for r in results.values() if r["ok"])
    print(f"\n可达 {ok}/{len(results)}，结果写入 {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
