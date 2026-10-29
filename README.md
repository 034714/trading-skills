# Trading Skills

面向 AI 助手的三个只读交易研究技能。输出条件化计划，不自动交易，也不保证收益。

- **pm-edge**：预测市场研究，优先天气与文化；明确事件日期，比较 YES / NO，先审计结算规则，再核对报价与费用。
- **trade**：BTC / ETH（可扩展到其他币）技术与期权研究；一条命令带现价、资金费率、OI、多空比快照、1H/4H 日内背景与山寨币筛选；回复第一句就给「现在做什么」的大白话结论；区分行情快照、模型假设和可执行条件，不把指标或历史波动率比较直接当作买卖信号。
- **stock**：Gate 股票类 USDT 永续（美股/港股/指数/未上市 AI 公司）条件研究；无默认标的，`--list` 查合约；一条命令带执行层快照（标记价 vs 指数锚溢价、按合约周期资金费、OI、多空账户数）、Yahoo 底层指标、1H 日内背景与美股时段状态；单一场所与财报未核查照直说。

## 安装

将 `skills/pm-edge`、`skills/trade` 和 `skills/stock` 复制到助手支持的 skill 目录，例如支持该路径的客户端使用 `~/.agents/skills/`；MiMo Desktop 可使用 `~/.config/mimocode/skills/`。具体路径以客户端文档为准，安装后确认已发现技能，不要同时安装同名的多份副本。

直接运行脚本需要 Python 3.10+；脚本使用标准库，离线测试需安装 `pytest`。从仓库根目录运行下方命令；参数以脚本 `--help` 为准。取数需要访问各公开接口，不需要交易账户密钥。

## 怎么问

- `/pm-edge 扫描明天的天气盘，看看有没有值得做的。`
- `/pm-edge 只看纽约、东京、香港。`
- `/pm-edge 这周有什么热门盘值得看？不看天气。`
- `/trade BTC、ETH 现在什么结构？我想 79500 接多、止损 77800，给分批计划和期权适配。`
- `/trade 今天有没有日内级别的机会？`
- `/trade 顺带扫一下山寨币有什么值得细看的。`
- `/stock 长鑫、宇树、智谱我现在该怎么办？我有多单。`
- `/stock Gate 上有哪些股票合约可以交易？`

## 预测市场：通用发现 + 天气专用模型

`pm-edge` 不只做天气。通用的盘（奖项、票房、体育、政治、推文计数）用发现工具看盘面事实：

```bash
python skills/pm-edge/scripts/pm_scan.py --tag awards --top 8 --books 8
python skills/pm-edge/scripts/pm_scan.py --query "tweet,posts" --top 8
```

输出每行的本侧买价/卖价、价差、成交量、费用状态，以及两个**挂单流动性占比**（买盘 NO 占比 / 卖盘 NO 占比）。这两个数字是"最优价 2¢ 内的美元挂单占比"，用来判断流动性厚薄和挂单拥挤，**不是人数、信心或胜率**。

天气盘额外有概率模型（三模式集合成员 + 滚动偏差修正），但仍标"未校准情景"：

```bash
python skills/pm-edge/scripts/pm_weather.py --date YYYY-MM-DD --top 10  # 替换为目标当地日期；费用另核实
python skills/pm-edge/scripts/pm_weather.py --settle YYYY-MM-DD        # 仅已结束且结果已发布的当地日
python skills/pm-edge/scripts/pm_weather.py --bias                      # 累计偏差/中心档命中率
```

回答的表长这样（列会随盘面变化）：

> | 盘面 | 市场 | 档位 | 概率（依据） | 现价可成交 | 等待限价 | 赔率 | 压力后胜率 | 状态 |

- **概率（依据）** 明确写出来源：模型成员比例（未校准）、独立基准率、或"无独立估计"。
- **现价可成交** 是本侧真实 ask 走单均价，且已验证能覆盖计划份数；深度不够会标"深度不足"，不给价格。
- **等待限价** 是低于现价若干 tick 的被动买单情景，不是报价，也不保证成交。
- **压力后胜率**（天气 NO）对每个档都施加 ±1.5°C 的全体成员平移；中心落在卖档内的只能观察。

两边都遵守：费用必须逐市场核实，未知就只报毛价差；`YES bid + NO ask < 1` 不算锁价。相同二元事件等份两侧足额 ASK 的每份条件净价差为 `1 - YES成交成本 - NO成交成本 - 全部费用`；结算、兑换与执行条件须一致。非原子成交仍有单腿、滑点与结算风险，不称“无风险”。通用发现支持 Polymarket / Kalshi，不代表所有事件类别都有独立概率模型；盘口占比不应升级成“强信号”或胜率。


## trade：一句话出 BTC/ETH 全景

问「BTC、ETH 什么结构」，回答开头是一张「现在的位置」快照——现价、已跌破/踩着的关键位、上方面对的阻力、资金费率、多空比、OI、临近事件，一眼看清处境：

<img width="1153" height="751" alt="回答开头的「现在的位置」快照表" src="https://github.com/user-attachments/assets/fdd41015-a990-4821-9278-17de0a653367" />

快照之后是结构判断和条件计划：

- **一句话结论 + 综合建议**：回复第一句就用大白话说现在做什么（等回调 / 挂条件单 / 观望），卡片之后附 3–6 句白话综合建议——多空两边证据、关键事件、两个条件分支，以及「现在不动也是一种建议」
- **结构判断**：日线指标 + 4H 摆点，顺势/区间/转折写清依据
- **日内背景**：1H/4H 的 RSI 与均线状态、距最近 4H 摆点的距离；日内条件卡有效期到下一根 4H 收盘
- **分批阶梯**：给入场价和止损后，批次按 0.5×ATR 排布、距止损太近的自动剔除，R 目标按均价算
- **期权适配**：限定期限内的真实 call/put 候选与 ATM IV / 30 日 RV 描述性比较；期限、偏斜与事件风险不同，不能据此判断错价或买卖方向，不输出可执行卖方组合
- 加一句「扫一下山寨币」会追加 8 个主流币的「值得细看」名单

哪项数据拉不到会明说，不填数。想直接跑脚本：

```bash
python skills/trade/scripts/crypto_ctx.py --json            # BTC+ETH 全景
python skills/trade/scripts/crypto_ctx.py --json --scan     # 加山寨币筛选
```

## stock：Gate 股票永续，先看溢价和时段

Gate 上能交易股票类 USDT 永续：美股主流（NVDA、TSLA、MSTR…）、指数、港股与未上市 AI 公司（长鑫 CXMT、宇树 UNITREE、智谱 ZHIPU、OpenAI、Anthropic…）。`/stock` 回答的是同一套问题——现在什么位置、该等还是该动——但多了两个股票特有的坐标：

- **溢价**：永续标记价对比 Gate 自己的指数锚（以及美股标的的 Yahoo 底层现价），一眼看出这个 24/7 市场此刻偏离真实股价多远——尤其周末和盘后，全靠情绪定价。
- **时段与锚定状态**：美股时段内数据有底层撮合支撑；闭市时明确提示"单一场所情绪定价"；长鑫/宇树/智谱这类只有 Gate 一家定价的标的，会直接说明无外部锚定、历史有多短、指标降级到了什么程度。

另外照直说：财报日历拿不到自动数据，谈计划前必须人工核查财报日；资金费率周期逐合约读取 funding_interval，不能预设每小时或8小时。想直接跑脚本：

```bash
python skills/stock/scripts/stock_ctx.py --list            # 查可交易的股票合约
python skills/stock/scripts/stock_ctx.py --symbols CXMT,NVDA --json   # 双层全景
```

无默认推荐名单。未点名时可主动从交易中合约按成交量级、底层与数据完整性筛选，再选最多4个深看；不是固定复查旧持仓。

```bash
python -B skills/stock/scripts/stock_ctx.py --list --sort volume --top 12 --underlying-only --json
```

`--list` 已移除前120行硬截断，默认完整输出；可加 `--min-volume`。日线/4H/1H附源K线时间与过期标记，假期/提前收盘仍须查官方日历。新闻、宏观局势与财报采用明确人工联网工作流：官方优先，记录发布/事件/查询时间，事实与解读及情景分开，未查不说无事件。卡片将历史摆点结构目标与算术R目标分列，附触发待确认、持有周期与到期；不保证到价。

stock专项离线回归现为28项（含3个真实提示的规则评估，不是fresh会话模型评测）。

## 重要边界

- 高市场价格不等于已校准的高胜率；不同模型也可能高度相关。
- 当天最高温和最低温在当天结束之前仍可能改变；观测站、日期、精度和修订规则必须匹配合约。
- 未核实的费用、NO 侧报价、深度或期权结算规格，不能用于声称存在可执行套利。
- 限价不保证成交或 maker 身份；止损不保证成交价。部分成交、滑点、跳空和结算争议都可能造成损失。
- 报价快照和条件计划不是实时指令。没有资金规模与风险预算时，不输出假装适合用户的实际数量。
- 数据接口失败时必须报告缺失，不能填造价格。
- 不包含自动下单、账户密钥、个人账本或对话记录。

## English overview

Three read-only research skills for AI assistants: **pm-edge** for prediction markets (Polymarket / Kalshi discovery and weather-specific models), **trade** for crypto market structure and options candidates, and **stock** for Gate stock-linked USDT perpetuals. No automated orders, account keys or profit guarantees.

Copy the desired `skills/<name>` directory into your client's supported skills directory; avoid duplicate names. CLI scripts require Python 3.10+ and use the standard library. Run commands above from the repository root and check each script's `--help`. Dates are local to the contract, not necessarily your computer.

Quotes are snapshots, not live instructions. Verify timestamps, executable depth, fees and settlement rules. Stock news, macro events, earnings, holidays and early closes require manual verification; “not checked” never means “no event”. Weather ensemble proportions are uncalibrated scenarios. Equal-size YES+NO pairing uses both executable asks plus all costs, not a bid/ask mix, and is not risk-free. IV versus historical RV is descriptive, not a mispricing or selling signal.

## 验证范围

离线回归测试验证公式边界、日期处理和失败路径（各技能独立运行，以实际测试输出为准），不代表已回测出盈利策略；公开接口冒烟测试只说明当时能读到数据，不证明模型准确。历史上的实盘教训（预报漂移击穿相邻档 NO、批量请求站点错配等）已转化为内置风控与校验，但过往验证不构成对未来胜率的保证。使用前应查阅对应平台官方文件和具体合约条款。
