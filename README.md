# btc-proxy — 用 GitHub Actions 当"云代理"的加密行情采集器

> 仓库：<https://github.com/XiRuize/btc-proxy>（public）
> 本地归档：`量化研究/数据源/btc-proxy/`

## 1. 为什么需要它

本机（境内）到交易所 API 的链路是断的：`api.binance.com`、`api.okx.com`、`api.coingecko.com` 的 DNS 被污染，
浏览器和 shell 都直连不上；shell 的 TLS 还受沙箱限制（`schannel SEC_E_NO_CREDENTIALS`）。

GitHub 的 runner 在境外、网络正常，于是让它在云端取数、把结果 commit 回仓库，本机再从仓库读。

```text
GitHub runner                      仓库 data 分支               本机
──────────────                     ─────────────              ────────────────────
collect.py                          BTCUSDT_5m.csv             pull_local.ps1（SSH）
  ├─ Coinbase 现货 5m K 线    ──►   ETHUSDT_5m.csv       ──►   数据源/parquet/crypto/
  └─ Kraken Futures 资金费率        ..._snapshot_5m.csv
                                     latest.json / status.json
```

## 2. 踩过的坑（2026-11 实测）

| 尝试 | 结果 |
|---|---|
| 直连 `api.binance.com`（本机） | DNS 污染，超时 |
| 直连 `data.binance.vision`（本机） | ✅ 可下载 2017 至今全量历史（`bootstrap_history.py`） |
| **Coinbase / Kraken（GitHub runner）** | ✅ **可用，已在跑** |
| Binance（GitHub runner） | ❌ `HTTP 451` —— 币安按地区封锁，机房 IP 同样在黑名单 |
| OKX / Bybit / CoinGecko（本机） | ❌ DNS 污染或超时 |
| 公共 CORS 代理中转币安 | ❌ 被币安按出口地区拒绝 |

结论：**现货用 Coinbase，合约情绪用 Kraken Futures**，两者都不封机房 IP、都不需要 API Key。

## 3. 目录

```text
btc-proxy/
  config.json                    # 币种/交易所映射、采集参数
  .github/workflows/collect.yml  # 定时 + push 触发；发布到 data 分支
  scripts/collect.py             # 采集（跑在 runner 上，纯标准库）
  scripts/selftest.py            # 离线自测（不联网）
  scripts/pull_local.ps1         # 本机拉取（走 SSH，推荐）
  scripts/pull_local.py          # 本机拉取（走 HTTPS raw，备用；本机 shell 的 TLS 受限时用不了）
  scripts/bootstrap_history.py   # 历史回补（data.binance.vision，本机直连）
  scripts/build_archive.py       # 瘦身打包（59 MB -> 可上传 GitHub）
  scripts/check_5m.py            # 5 分钟归档的流式质量校验
  scripts/clean_crypto.py        # 清洗/校验（产出 clean/ 与质量报告）
  fixtures/responses.json        # 离线样例
  data/                          # 运行产物，被推到 data 分支
```

## 3.5 仓库的三个分支

| 分支 | 放什么 | 更新方式 | 体积 |
|---|---|---|---|
| `main` | 采集器代码、workflow、文档 | `git push` | 小 |
| `data` | 最近 1–2 天的 5 分钟增量 + 合约快照 | Actions 每 5 分钟重写（孤儿提交） | 小 |
| `history` | **全量历史归档**：5m / 1d / 资金费率 | 手动，按月 | **59 MB** |

`history` 分支内容：

| 文件 | 行数 |
|---|---|
| `BTCUSDT_5m.csv.gz` / `ETHUSDT_5m.csv.gz` | 各 967,945 |
| `SOLUSDT_5m.csv.gz` | 655,421 |
| `{SYM}_1d.csv.gz` | 3,367 / 3,367 / 2,277 |
| `{SYM}_funding.csv.gz` | 7,488 / 7,488 / 6,793 |

为什么 5 分钟要瘦身：9 年 97 万行，原始 CSV.gz 有 50 MB/币种；只保留 7 列、价格 2 位小数后降到 23 MB。
`open_time` 保留原始值（**2025 年后是微秒**），读取时要按位数归一。

## 4. 数据契约

**`{SYMBOL}_5m.csv`** —— 5 分钟 K 线，只收已收盘的 bar

| 字段 | 含义 | 备注 |
|---|---|---|
| `open_time_utc` / `close_time_utc` | UTC ISO8601 | **主键** |
| `open/high/low/close` | 成交价（USD 计价） | Coinbase 的 `BTC-USD` |
| `volume` | 成交量（币） | |
| `quote_volume` / `taker_buy_base` / `taker_buy_quote` | 成交额 / 主动买入 | Coinbase 不提供，**留空** |
| `trades` | 成交笔数 | Kraken 提供，Coinbase 留空 |
| `source` | 数据来源（`coinbase` / `kraken`） | **同一文件只会出现一个 source** |
| `fetched_at` | 抓取时刻 | 用于审计延迟 |

**`{SYMBOL}_snapshot_5m.csv`** —— 合约情绪，每 5 分钟一行

| 字段 | 来源 | 含义 |
|---|---|---|
| `ts_utc` | 抓取时刻（对齐到分钟） | 主键 |
| `mark_price` / `index_price` | Kraken Futures | 标记价 / 指数价，两者之差≈基差 |
| `funding_rate` | Kraken Futures | 资金费率，**原始值**；按 Kraken 文档为"每 8 小时百分比"（`1.74` ≈ 1.74%/8h），使用前建议用一次已知结算值校准 |
| `next_funding_time_utc` | — | Kraken 不返回，留空 |
| `open_interest` | Kraken Futures | 未平仓合约量 |
| `long_short_ratio` | — | Kraken 不返回，留空（币安版本有，但已不可用） |
| `source` | — | `kraken_futures`；合约挂掉时退化为 `spot_fallback`（仅 mark_price） |

**`latest.json`** 各币种最新一行；**`status.json`** 每轮成功/失败明细；**`meta.json`** 记录每个币种钉住的交易所。

## 5. 调度设计（为什么不是 cron `*/5`）

GitHub 的定时器对 5 分钟粒度极不准：实测 15 小时只触发了 2 次。所以改成：

- cron `*/30` 每 30 分钟启动一次；
- 每次 job **在进程内连续采 28 分钟**（每 5 分钟一轮，对齐 bar 收盘后 5 秒），随后退出；
- `push` 触发时只跑一轮，用于快速验证代码改动。

覆盖率约 93%，代价是单次 job 运行 28 分钟。public 仓库的 Actions 分钟数**免费无限**。

## 6. 本机拉取

```powershell
cd "C:\Users\生升\Desktop\未来\量化研究\数据源\btc-proxy"
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\pull_local.ps1
```

走 SSH（`git fetch` + `git show`），输出到 `数据源/parquet/crypto/`，并打印每份文件的最后时间戳与滞后分钟数。

> 为什么不用 `pull_local.py`：它读 `raw.githubusercontent.com`，而本机 shell 的 TLS 拿不到凭据，
> 只有在能正常访问 HTTPS 的环境才用得上。

## 7. 排错

| 现象 | 原因 | 处理 |
|---|---|---|
| `status.json` 里全是 HTTP 451 | 用了币安 | 换成 Coinbase / Kraken（本仓库已改） |
| Actions 红，`kline_error` | 交易所偶发限流 | 看 `errors`，下一轮会自愈；连续失败再查 |
| `data` 分支几小时不更新 | 定时器排队 / 仓库 60 天无活动被停 | 手动 `workflow_dispatch`；本仓库每次 push 数据都算活动 |
| `pull_local.ps1` 报 "missing terminator" | .ps1 被存成无 BOM 的 UTF-8，PowerShell 5.1 按 GBK 解析 | 用 UTF-8 **带 BOM** 保存 |
| 私有仓库读不到 | `raw` 需要 token | 用 `pull_local.ps1`（SSH），或转 public |

## 8. 合规

只抓交易所公开行情，不含密钥、不含个人信息。仓库 public，任何人都能读。
