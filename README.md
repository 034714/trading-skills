# Trading Skills

面向 AI 助手的四个只读交易研究技能。输出条件化计划，不自动交易，也不保证收益。

- **pm-weather**：天气预测市场只读研究，优先最高温、最低温和逐日温度档位；核实当地日期、结算站与来源，比较 YES / NO 完整盘口和条件回报。其他天气按需研究，不承诺已有专用模型。
- **pm-liquid**：流动性优先的事件合约研究；同时读 Polymarket / Kalshi 两侧真实盘口，按价差与 20 份可成交深度筛选，默认挂被动限价，内置低价/高价屏幕与跨平台同事件比价。
- **trade**：BTC / ETH（可扩展到其他币）技术与期权研究；一条命令带现价、资金费率、OI、多空比快照、1H/4H 日内背景与山寨币筛选；回复第一句就给「现在做什么」的大白话结论；区分行情快照、模型假设和可执行条件，不把指标或历史波动率比较直接当作买卖信号。
- **stock**：Gate 股票类 USDT 永续（美股/港股/指数/未上市 AI 公司）条件研究；无默认标的，`--list` 查合约；一条命令带执行层快照（标记价 vs 指数锚溢价、按合约周期资金费、OI、多空账户数）、Yahoo 底层指标、1H 日内背景与美股时段状态；单一场所与财报未核查照直说。

## 表达风格

两个预测市场 skill 默认先说人话：第一段回答“有没有值得挂的单”，随后给一张四列表（买什么 / 挂单价 / 20 份成本 / 什么情况撤），盘口、预报、模型和费用细节放表后备注。用户问“为什么市场这么定价”时，用“官方预报多少、哪个模型偏暖或偏冷、市场在跟谁”解释，不堆百分比。

首次出现的术语会翻译成中文：YES/NO、bid/ask、价差、VWAP、longshot、maker/taker；Kalshi 指美国 CFTC 监管的预测市场交易所（美元计价、每份 $1），Polymarket 是加密 USDC 计价的预测市场，两者不是同一家。完整档位、压力情景、规则正文和费用公式只在用户追问或需要留证时展开；研究口径与风险边界不变。

## 安装

天气技能目录已改名为 `skills/pm-weather`。新名称安装包须实际发布后再下载，旧 Release 的 `pm-edge.zip` 不因此自动更名。安装请使用独立实体目录，不要建立指向开发项目的快捷方式或目录联接：删除开发仓库不应影响已安装技能。更新时重新导入，或按核对过的正式文件清单同步；安装副本不会自动跟随仓库修改。

从 [Releases](https://github.com/034714/trading-skills/releases) 的 **Assets** 下载所需的 `pm-weather.zip`、`pm-liquid.zip`、`trade.zip` 或 `stock.zip`，解压后将对应 skill 目录导入客户端（若支持目录导入），或复制到下述技能目录。每个 skill 根目录应直接包含 `SKILL.md`，避免多套一层目录。GitHub 自动生成的 **Source code (zip/tar.gz)** 是整个仓库源码，不是单个 skill 安装包；若下载源码，请使用其中 `skills/` 下对应目录。

将 `skills/pm-weather`、`skills/pm-liquid`、`skills/trade` 和 `skills/stock` 复制到助手支持的 skill 目录，例如支持该路径的客户端使用 `~/.agents/skills/`；MiMo Desktop 可使用 `~/.config/mimocode/skills/`。具体路径以客户端文档为准，安装后确认已发现技能，不要同时安装同名的多份副本。

直接运行脚本需要 Python 3.10+；脚本使用标准库，离线测试需安装 `pytest`。从仓库根目录运行下方命令；参数以脚本 `--help` 为准。取数需要访问各公开接口，不需要交易账户密钥。

## 怎么问

- `/pm-weather 扫描明天的天气盘，看看有没有值得做的。`
- `/pm-weather 只看纽约、东京、香港，稳健但也要有合理赚头。`
- `/pm-weather 北京为什么押 7°C 最多？直接告诉我挂哪、挂多少。`
- `/pm-liquid 这周有什么流动性好的热门事件合约值得看？不看天气。`
- `/pm-liquid 扫一下今晚和这周的深盘，优先能挂单的。`
- `/pm-liquid 湖人这场两边盘口怎么样？能不能挂限价？`
- `/trade BTC、ETH 现在什么结构？我想 79500 接多、止损 77800，给分批计划和期权适配。`
- `/trade 今天有没有日内级别的机会？`
- `/trade 顺带扫一下山寨币有什么值得细看的。`
- `/stock 长鑫、宇树、智谱我现在该怎么办？我有多单。`
- `/stock Gate 上有哪些股票合约可以交易？`

## pm-liquid：流动性优先的事件合约

`pm-liquid` 只做“当下真的能成交”的盘：默认扫体育、宏观数据、政治、金融，读两侧真实 ask/bid、20 份完整成交、费用与收盘时间；24h 成交只作参考（Kalshi 按 $1 名义、串关膨胀；临开赛/刚结束都会失真）。默认不主动买 <10¢、不追 >90¢，入场默认挂被动限价。

```bash
python skills/pm-liquid/scripts/scan.py --top 8                 # both，首次约 40s，缓存命中约 20s
python skills/pm-liquid/scripts/scan.py --query "Lakers"        # 指定事件同轮读两边
python skills/pm-liquid/scripts/scan.py --tail                  # 高赔率模式，放开 10–90c
python skills/pm-liquid/scripts/scan.py --platform kalshi --series KXCPI,KXPAYROLLS,KXFED
```

输出每行：平台、对象·周期（UTC 收盘）、两侧 ask/bid、20 份 VWAP、费用、24h 量、状态（Y/N 可挂、深度不足、价差超限、价格屏）。Longshot、maker/taker、成交量口径与跨平台比价见 `skills/pm-liquid/references/playbook.md`。

## pm-weather：温度盘优先的天气研究

`pm-weather` 专注天气，优先最高温、最低温与逐日温度档位；降水、风速等仅按需核实规则、数据与证据，不承诺已有专用模型或自动扫描支持。非天气事件不属本 skill：体育、宏观、政治、加密价格等流动性合约转 `pm-liquid` 的已有流程；推文、奖项、票房不再由天气 skill 承接，也不据此承诺其他 skill 有专用模型。现有通用脚本保留。

首次没有偏好时只问一个选择题：稳健有赚头（推荐）/均衡/高赔率；已有偏好不重复问，直接扫描或不能交互时默认稳健且重视条件回报，不保证收益。默认展示 3–6 个候选，不是上限，不凑数；按证据稳健性、条件回报、流动性与不确定性安排行序，不另设排序列。

天气扫描、结算与诊断统一用 `pm_weather.py`：

```bash
python skills/pm-weather/scripts/pm_weather.py --date YYYY-MM-DD --mode both --top 10  # 核实目标当地日；费用另核实
python skills/pm-weather/scripts/pm_weather.py --settle YYYY-MM-DD  # 仅已结束且指定源已发布的当地日
python skills/pm-weather/scripts/pm_weather.py --bias               # 可信样本诊断，不等于交易胜率
```

先核实实际时间、城市时区与目标当地日，区分预报发布/有效时刻、观测时间、关盘和结算发布时间。核对市场 ID、YES/NO token 归属、标题与日期；两侧完整 book 排序后计算足额成交 VWAP，不把 shortlist 当全集，完整档位分布另行展示。日期或 ID 不匹配的响应拒用，缺失数据明确报告。

主表样式：

> | 状态 | 城市·日期·最高/最低·档位 | 方向 | 市场/天气 | 挂单/失效 | 胜率依据 |

- **市场/天气**：本侧真实 ask、bid、价差、容量，以及预报中心/实测；报价附实际时间，部分成交均价不冒充整笔可成交价。
- **胜率依据**：集合比例、滚动偏差、平滑与压力测试都只是模型情景，不是已验证真实概率；有依据才写范围，无可靠估计就直说，不能从市场 ask 倒造胜率。
- **挂单/失效**：以真实盘口、tick 与条件价值给有依据的近端限价；远端价须实际历史或连续盘口支持，否则写无触达依据、成交概率未知。价格到达仍重核证据与费用。
- **状态**：当前可考虑/等待限价条件/不参与，纯观察另列；来源未核实与硬筛未通过不冒充合格项，`--risky` 仅作诊断。

天气与事件合约均逐市场核实费用，未知只报毛值。相同二元事件等份 YES+NO 须两侧足额 ASK、全部费用及结算兑换一致；`YES bid + NO ask < 1` 不算锁价。非原子成交仍有单腿、滑点与结算风险，不称无风险。挂单占比不代表人数、信心或胜率。

天气默认只在本次进程内存中复用数据，结束即释放，不保存下载缓存或研究记录。仅 `--cache-mode disk` 开启外置磁盘缓存（默认上限256MB/168小时）；仅 `--save-records` 保存预测与结算记录，二者独立。显式写盘路径可配置，默认 Windows `%LOCALAPPDATA%/trading-skills/pm-weather/{cache,data}`。pm-liquid盘口仅本轮内存复用；stock仅内存合约表；trade不增加磁盘行情缓存，可用 `--no-options` 跳过期权取数。所有技能收尾清理自己创建的临时产物与后台任务，不关闭用户已有程序、不删除已有记录；安装包不含运行数据。


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

stock专项离线回归现为28项（含3个真实提示的规则评估，不是fresh会话模型评测）；pm-liquid 离线回归 9 项。

## 重要边界

- 高市场价格不等于已校准的高胜率；不同模型也可能高度相关。
- 当天最高温和最低温在当天结束之前仍可能改变；观测站、日期、精度和修订规则必须匹配合约。
- 未核实的费用、NO 侧报价、深度或期权结算规格，不能用于声称存在可执行套利。
- 限价不保证成交或 maker 身份；止损不保证成交价。部分成交、滑点、跳空和结算争议都可能造成损失。
- 报价快照和条件计划不是实时指令。没有资金规模与风险预算时，不输出假装适合用户的实际数量。
- 数据接口失败时必须报告缺失，不能填造价格。
- 不包含自动下单、账户密钥、个人账本或对话记录。

## English overview

Four read-only research skills for AI assistants: **pm-weather** for weather prediction markets, prioritizing daily high/low temperature bins (other weather events are researched on demand, not promised dedicated model support), **pm-liquid** for liquidity-first event contracts on Polymarket / Kalshi, **trade** for crypto market structure and options candidates, and **stock** for Gate stock-linked USDT perpetuals. No automated orders, account keys or profit guarantees.

Copy the desired `skills/<name>` directory into your client's supported skills directory; avoid duplicate names. CLI scripts require Python 3.10+ and use the standard library. Run commands above from the repository root and check each script's `--help`. Dates are local to the contract, not necessarily your computer.

Quotes are snapshots, not live instructions. Verify timestamps, executable depth, fees and settlement rules. Stock news, macro events, earnings, holidays and early closes require manual verification; “not checked” never means “no event”. Weather ensemble proportions are model scenarios, not measured probabilities. Equal-size YES+NO pairing uses both executable asks plus all costs, not a bid/ask mix, and is not risk-free. IV versus historical RV is descriptive, not a mispricing or selling signal.

## 验证范围

离线回归测试验证公式边界、日期处理和失败路径（各技能独立运行，以实际测试输出为准），不代表已回测出盈利策略；公开接口冒烟测试只说明当时能读到数据，不证明模型准确。历史上的实盘教训（预报漂移击穿相邻档 NO、批量请求站点错配等）已转化为内置风控与校验，但过往验证不构成对未来胜率的保证。使用前应查阅对应平台官方文件和具体合约条款。
