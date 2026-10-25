# 数据层：Gate / Yahoo 接口事实与局限

## Gate（执行层）

- `/futures/usdt/contracts`：全量合约。**股票类判定用 `contract_type == "stocks"`**，不要靠名字猜。关键字段：
  - `mark_price`（标记价）、`index_price`（指数锚，溢价的分母）、`last_price`
  - `funding_rate` / `funding_rate_indicative`、`funding_interval`（秒；股票类常见 **3600=每小时**，与加密 8h 不同）
  - `leverage_min/max`、`maker_fee_rate`/`taker_fee_rate`、`quanto_multiplier`（张×乘数=股数，未逐个核验）、`order_size_min`
  - `position_size`（OI 张数；名义≈张数×乘数×标记价）、`long_users`/`short_users`（账户数口径）
  - `create_time`（上市时间，K线深度的上界）、`enable_circuit_breaker`（股票类多为 false）、`in_delisting`、`status`
- `/futures/usdt/candlesticks?contract=&interval=1d|4h|1h&limit=`：**字典格式** `{o,h,l,c,v,t,sum}`，`t` 为**秒**且是K线起点；日线按 00:00 UTC 对齐；未收盘K线用 `t+step > now` 过滤。与现货接口的数组+毫秒格式不同。
- `/futures/usdt/tickers`：`volume_24h_quote` 与K线 `sum` 口径对不上（乘数与统计窗差异，未核验），只作量级参考。
- 历史：Gate 股票永续上线晚（AAPL 等自 2026-09-03，CXMT 2026-07-15，ZHIPU 2026-04-13），EMA200 通常算不出。

## Yahoo（底层，仅公开 chart 接口）

- `query1.finance.yahoo.com/v8/finance/chart/{symbol}?interval=1d|1h&range=2y|60d&includePrePost=false`：无 key，必须带浏览器 UA。
- `includePrePost=false` 时 **1H 只含正股时段**（每交易日约 7 根，周末无K线）；日线含未收盘K线，须按 NY 时段过滤。
- `quoteSummary`/calendarEvents 需要 crumb（401）：**财报日拿不到**，只能人工联网核查后注明来源。
- `v1/finance/search?q=` 可查代码（无 key）。

## 底层映射表（脚本内 YAHOO_MAP）

- 美股主上市：同代码（AAPL→AAPL 等，USD），作为**指标层**（长历史 EMA/RSI/ATR/RV，RV 年化用 **252** 个交易日，与 trade 的 365 不同）。
- 非美元主上市：MINIMAX→0100.HK、SMIC→0981.HK、SKHYNIX→000660.KS（已验证存在）。**仅作背景**（货币不同、Gate 为 USD 折算价，指标一律用 Gate 层），不算 Gate vs 底层溢价。
- 无映射（CXMT/UNITREE/ZHIPU/OPENAI/ANTHROPIC 等）：Yahoo 上的 `XXX-USD (CCC)` 是场外衍生报价，**不是主上市**，不采用；标"单一场所定价"。

## 美股时段函数（ny_session）

- DST 近似：3 月第二个周日至 11 月第一个周日为 EDT（UTC-4），其余 EST（UTC-5）；忽略 02:00 切换瞬间。
- pre_market 04:00–09:30、regular 09:30–16:00、after_hours 16:00–20:00（当地时间），周六日 weekend_closed。**美股假期未内置**，节日前后结论要人工核对。

## 环境事实

- 本机走 fake-IP 代理（域名全部解析进 198.18.0.0/15），`get()` 只做 https + 域名白名单（api.gateio.ws / query1.finance.yahoo.com）+ 字面 IP 私网检查，**不做 DNS 解析后的 IP 判定**——做了会拦掉一切外联。
