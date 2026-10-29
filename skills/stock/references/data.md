# 数据层：Gate / Yahoo 接口事实与局限

## Gate（执行层）

- `/futures/usdt/contracts`：全量合约。**股票类判定用 `contract_type == "stocks"`**，不要靠名字猜。关键字段：
  - `mark_price`（标记价）、`index_price`（指数锚，溢价的分母）、`last_price`
  - `funding_rate` / `funding_rate_indicative`、`funding_interval`（秒；每份合约逐次读取，不预设1h或8h）
  - `leverage_min/max`、`maker_fee_rate`/`taker_fee_rate`、`quanto_multiplier`（张×乘数=股数，未逐个核验）、`order_size_min`
  - `position_size`（OI 张数；名义≈张数×乘数×标记价）、`long_users`/`short_users`（账户数口径）
  - `create_time`（上市时间，K线深度的上界）、`enable_circuit_breaker`（股票类多为 false）、`in_delisting`、`status`
- `/futures/usdt/candlesticks?contract=&interval=1d|4h|1h&limit=`：**字典格式** `{o,h,l,c,v,t,sum}`，`t` 为**秒**且是K线起点；日线按 00:00 UTC 对齐；未收盘K线用 `t+step > now` 过滤。与现货接口的数组+毫秒格式不同。
- `/futures/usdt/tickers`：`volume_24h_quote` 与K线 `sum` 口径对不上（乘数与统计窗差异，未核验），只作量级参考，成交额不是深度。`--list` 默认不截断，返回全部交易中且未退市的 stocks 合约；`--sort volume` 降序，缺失排末尾；`--top N` 显式限行，`--min-volume` 过滤缺失成交额，`--underlying-only` 仅保留已映射的美元底层。这两次批量公开请求不是全市场K线扫描。
- 历史：Gate 股票永续上线晚（AAPL 等自 2026-09-03，CXMT 2026-07-15，ZHIPU 2026-04-13），EMA200 通常算不出。

## Yahoo（底层，仅公开 chart 接口）

- `query1.finance.yahoo.com/v8/finance/chart/{symbol}?interval=1d|1h&range=2y|60d&includePrePost=false`：无 key，必须带浏览器 UA。
- `includePrePost=false` 时 **1H 只含正股时段**（每交易日约 7 根，周末无K线）；日线含未收盘K线，须按纽约16:00过滤；US日线 close_time 修正为当日常规收盘，不再误用1小时。末根1H 15:30–16:00仅30分钟。提前收盘仍需官方核查；非美股日线保守按起点+24h过滤，仅作背景。
- `quoteSummary`/calendarEvents 需要 crumb（401）：**财报日拿不到**，只能人工联网核查后注明来源。
- `v1/finance/search?q=` 可查代码（无 key）。

## 底层映射表（脚本内 YAHOO_MAP）

- 美股主上市：同代码（AAPL→AAPL 等，USD），作为**指标层**（长历史 EMA/RSI/ATR/RV，RV 年化用 **252** 个交易日，与 trade 的 365 不同）。
- 非美元主上市：MINIMAX→0100.HK、SMIC→0981.HK、SKHYNIX→000660.KS（已验证存在）。**仅作背景**（货币不同、Gate 为 USD 折算价，指标一律用 Gate 层），不算 Gate vs 底层溢价。
- 无映射（CXMT/UNITREE/ZHIPU/OPENAI/ANTHROPIC 等）：Yahoo 上的 `XXX-USD (CCC)` 是场外衍生报价，**不是主上市**，不采用；标"单一场所定价"。

## 美股时段函数（ny_session）

- DST：3月第二个周日07:00 UTC至11月第一个周日06:00 UTC为EDT（UTC-4），其余EST（UTC-5），按现行美国规则。
- pre_market 04:00–09:30、regular 09:30–16:00、after_hours 16:00–20:00（当地时间），周六日 weekend_closed。**美股假期未内置**，节日前后结论要人工核对。

## 环境事实

- 本机走 fake-IP 代理（域名全部解析进 198.18.0.0/15），`get()` 只做 https + 域名白名单（api.gateio.ws / query1.finance.yahoo.com）+ 字面 IP 私网检查，**不做 DNS 解析后的 IP 判定**——做了会拦掉一切外联。

## 时间质量、情景与兼容性

- 保留 `stock_ctx_v1` 与旧键；增补 `data_quality`（日线/4H/1H源、起止毫秒、年龄、陈旧状态）、`session_assessment`、`event_research`，不引入自动新闻服务。
- Gate完成K线与查询时钟差超过1.5个周期标陈旧；Yahoo按最近工作日16:00或常规时段1H检查，跳过周末而非完整交易日历。假期/提前收盘可能报 `stale_or_calendar_exception`，无异常也不代表日历已核验。电脑时钟和缺失中间K线仍需复核，本次未实现完整缺口扫描。
- 底层 `price_time_unix` 来自 regularMarketTime，缺失则未知；`fetched_at_utc` 不是源价格时间。Gate合约快照也仅有本地查询时间，不冒充撮合事件时间。
- `multi_timeframe["4h"]` 新增Gate已完成K线RSI14、EMA20/50和状态；不足50根写 unavailable。最近摆点距离与自动卡入场改用Gate标记价参考，日线趋势比较仍用同源日线，不混用价格序列。
- 条件卡补 `structural_targets`（历史摆点候选及时间/确认规则）、`valid_until_utc`（下一UTC 4H边界）、`holding_period`、未确认 `entry_trigger`、`event_review_status` 与 `data_quality_blocked`。摆点左右各2根完成K线，有确认滞后，可能已反复触及，不是未来必达价。日内默认草案不能自动变成多日方案。
- `gross_R_targets` 保留原均价风险算术，结构目标单列。自动止损/0.5ATR分批仍是原有启发式示意，日线ATR不可直接证明日内合理性。每100美元风险示例单位为底层单位，不是合约张数；张数需再除合约乘数。缺乘数时不计算名义OI。
- 新闻/财报/宏观的实际联网证据按 SKILL.md 记录；脚本输出 not_checked 不是无事件。
