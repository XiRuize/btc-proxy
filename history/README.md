# btc-history — 加密历史数据归档

> 这个分支只放**全量历史基线**，不参与每 5 分钟的实时更新。
> 实时增量在 `data` 分支；两者都是 `XiRuize/btc-proxy` 仓库的一部分。

## 内容

| 文件 | 内容 | 覆盖 |
|---|---|---|
| `data/{SYM}_5m.csv.gz` | 5 分钟 K 线，7 列 | BTC/ETH 2017-08 起，SOL 2020-08 起 → 2026-11-05 |
| `data/{SYM}_1d.csv.gz` | 日线，完整 12 列 | 同上 |
| `data/{SYM}_funding.csv.gz` | 8 小时资金费率 | 2020-01 起 |
| `manifest.json` | 每个文件的行数、字节数、sha256、首末时间戳 | — |

`SYM` ∈ `BTCUSDT` / `ETHUSDT` / `SOLUSDT`。数据源为币安官方公开数据桶
[`data.binance.vision`](https://data.binance.vision)，无需 API Key。

## 为什么是这种格式

5 分钟线 9 年近 97 万行，原始 CSV.gz 约 50 MB/币种。为了让仓库不至于过大：

- 只保留研究常用的 7 列：`open_time,open,high,low,close,volume,taker_buy_base`；
- 价格保留 **2 位小数**（BTC 在 8 万美元级别，0.01 的精度足够），成交量保留 4 位；
- 丢掉的列可以推导：`quote_volume ≈ close × volume`；`trades`、`taker_buy_quote` 未保留。

压完约 23 MB/币种，三个币种合计 ~70 MB。

## 读法

```python
import pandas as pd
df = pd.read_csv("data/BTCUSDT_5m.csv.gz")
df["ts"] = pd.to_datetime(df["open_time"].astype("int64") // 1000, unit="ms", utc=True)
```

**注意 `open_time` 的单位**：币安 2025 年起把时间戳从毫秒（13 位）改成微秒（16 位），
所以同一个文件里两种长度混存。按整数排序仍然正确，但转 datetime 前要按位数归一
（`clean_crypto.py` 里的 `norm_ms` 已经处理）。

## 怎么更新

```powershell
cd 量化研究\数据源\btc-proxy
python scripts\bootstrap_history.py --symbols BTCUSDT --interval 5m   # 拉最新月包
python scripts\build_archive.py                                        # 瘦身 + 生成 manifest
# 然后把 archive/ 的内容复制到本分支 data/ 下，提交推送
```

## 与 `data` 分支的分工

| 分支 | 内容 | 更新频率 | 体积 |
|---|---|---|---|
| `main` | 采集器代码、workflow | 改代码时 | 小 |
| `data` | 最近 1–2 天的 5 分钟增量 + 合约快照 | 每 5 分钟 | 小（每天重写） |
| `history` | 全量历史基线 | 手动，按月 | ~70 MB |

`data` 分支每次用孤儿提交重写，所以不能放历史；历史放这里，避免每 5 分钟重传 70 MB。
