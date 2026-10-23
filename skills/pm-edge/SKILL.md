---
name: pm-edge
description: 预测市场研究（Polymarket / Kalshi / Gate）。默认先看天气与文化，区分当天与明天，同时评估 YES 和 NO，先审结算规则再读订单簿，输出「方向 + 条件挂单价 + 单位风险 + 失效条件」的可执行计划。Use when the user asks 预测市场、盘口、赔率、胜率、扫一下有什么机会、这个盘该不该买、买 YES 还是 NO、天气盘、气温盘、票房、奖项、推文计数、Polymarket、Kalshi、挂单价。Do NOT use for 加密永续/期权方向（用 trade），spot 长期投资、税务。
---

# 预测市场研究

只读分析，不代下单。结论是条件计划，不是保证。

## 输出顺序（先给能执行的，再给理由）

```
## 挂单：<市场> <日期>
方向：买 YES / 买 NO（哪个档）
限价：____（盘口 ____）   数量：____ 份 ≈ $____
风险：全损 = $____（占本金 __%）
失效：<一句话触发条件>
```

后面只写 2–3 行理由，不复述分析过程。数据缺失时直接写缺什么，不要编数字。

## 三个必答问题

**1. 结算源是什么？** 一句话写出"当且仅当 ___ 时结算为 YES"，并给出结算页面、日期时区、精度、修订规则。写不清 → PASS。口径细节见 `references/weather.md`（温度）与 `references/crypto.md`（价格盘）。

**2. 是当天还是明天？** 当天有观测，可以做单调排除；明天只有预报，概率是模型近似。两者不能混：**不能用今天的观测去判明天的盘**。

**3. 费用与 NO 报价拿到了吗？** 费率未知就只报毛收益；NO 必须读真实 NO 订单簿。见 `references/mechanics.md`。

## 工作流

### 第 1 步：扫描候选

```bash
# 天气（当天/明天的盘都在这个 tag 里）
python scripts/pm_scan.py --tag weather --top 12 --books 3

# 文化 / 奖项 / 影视 / 音乐
python scripts/pm_scan.py --tag awards,movies,music,pop-culture --top 12

# Kalshi 日高温系列（关键字搜不到，必须用 series）
python scripts/pm_scan.py --platform kalshi --series KXHIGHNY,KXHIGHLAX --top 10
```

### 第 2 步：当天盘做单调排除

```bash
python scripts/pm_temp_brackets.py --limit 60 --top 12
```

只输出三种状态：`IMPOSSIBLE_ABOVE` / `IMPOSSIBLE_BELOW` / `STILL_POSSIBLE`。**当天结束前的极值仍可能改变，没有"锁定档"。** 买"赢的那一档"缺乏可证明边际；能证明的只有已死档位的 NO，而它们通常已经在 0.99 附近。

### 第 3 步：明天的盘用模型

```bash
python scripts/pm_forecast.py --date 2026-10-24 --top 12
```

`p_model` 是**模型正态近似**，不是校准胜率。四到五个模式常常共享初始场，一致不代表准。只把偏离当"值得打开规则读一遍"的线索。

### 第 4 步：加密价格盘

```bash
python scripts/pm_barrier.py --spot 82000 --barrier 85000 \
  --deadline "2026-11-01T03:59:00+00:00" --iv 0.40 --mode touch --ask 0.61 --fee-rate 0.07
```

输出是风险中性近似，离散 1 分钟采样会让真实触发概率偏离。计算口径的坑见 `references/crypto.md`。

### 第 5 步：算净边际与单位风险

```bash
python scripts/pm_edge.py --p 0.55 --price 0.42 --fee-rate 0.02 --equity 1000 --risk-pct 1
```

费率未知时省掉 `--fee-rate`，脚本会拒绝给净边际。没有本金与风险比例时只给"每 $100 风险对应多少份"。

### 第 6 步：记录与校准

```bash
python scripts/pm_calibration.py log --question "..." --p 0.55 --price 0.42
python scripts/pm_calibration.py resolve --id <id> --outcome yes
python scripts/pm_calibration.py score
```

写入本机 `~/.prediction-market/ledger.jsonl`，不写仓库。样本少于 30 条时差异当噪音。

## 判据（可按证据偏离，但要写理由）

| 项 | 参考值 |
|---|---|
| 毛收益（对了赚多少） | ≥8% 才值得进一步看 |
| 价差 | ≤3¢；>5¢ 基本放弃 |
| 深度 | 2¢ 内 ≥3× 计划金额 |
| 剩余时间 | ≤90 天；当日盘注意收盘时间 |
| 结论依据 | 结算源已核实的口径，不是价格高低 |

## 没机会时逐级放宽

1. 放宽毛收益到 5%、价差到 5¢。
2. 放宽成交量到 $500，允许薄盘小仓。
3. 换类别：天气 → 文化 → 加密。
4. 换方向：YES 没肉就找 NO；单档没肉就看多档结构。
5. 仍然没有：说"今天不做"，给触发价和观察清单，不要为了有单而下单。

放宽是"多看几个地方"，不是把不可成交的东西算成机会。

## 默认 PASS

- 结算规则含糊、来源打不开、平局/取消/缺失数据兜底不明。
- 你已经确定要买的一侧，拿不到可成交价或费率。
- 唯一理由是"已经 95% 了"。
- 你的概率与价格差不到 2pp，且没有独立依据。
- 想加仓挽回上一笔亏损。
- 只在同一个结算源上堆多个"看起来分散"的仓位。

## 参考文件

- `references/weather.md` — 温度盘口径、站点/精度/时区、单调排除、香港陷阱、预报的边界。
- `references/crypto.md` — 加密价格盘三类、Binance 1 分钟 K 线口径、费用、模型能做什么。
- `references/mechanics.md` — 平台字段、费用、订单簿、negRisk、限价与成交。
- `references/risk.md` — 概率写法、净边际、仓位收缩、校准与自欺清单。
