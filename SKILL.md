---
name: context-cfo
display_name: "上下文账本"
display_name_en: "Context Ledger"
description: "把 AI 的钱花在哪算成一张能拿给老板看的账单。扫描一个 agent 配置目录（skills、mcp、配置文件），算出每次加载多少 token、占用多少上下文窗口、一次会话多少钱、一个月烧多少钱，找出最烧钱的文件和可删的死重（超长/重复/触发模糊），给出按风险分级的删减清单——低风险可删、高风险合规项不许删只能压缩。给「被 token 账单吓到、想搞明白钱花在哪」的人用。何时不用：没有配置目录时；只问单个文件多大时（用 wc 就够，不必上这套）。"
description_zh: "月底账单出来才知道贵，但不知道贵在哪。扫一遍技能与配置目录，算清每次加载多少 token、一次会话多少钱、一个月烧多少，指出最烧钱的文件和能删的死重，按风险分级给删减清单——合规项不许删只能压措辞。"
description_en: "Scan a skills and config directory, quantify tokens loaded per call, cost per session and per month, name the most expensive files, and rank removable dead weight by risk."
examples_zh:
  - 我每个月 token 到底烧多少钱
  - 哪个 skill 最占上下文
  - 帮我把配置目录精简一下，哪些能删
examples_en:
  - How much do my tokens cost each month
  - Which skill eats the most context
  - Slim down my config directory and tell me what is safe to delete
version: 1.0.1
author: 王天霖
agent_created: true
---

# context-cfo — AI 上下文成本审计师

用一句话说清钱花在哪，而不是给一堆「少说点话」的建议。

## 你会得到什么

跑一次，得到四样东西：

1. **总量**：这个目录每次加载多少 token，占上下文窗口百分之几。
2. **成本**：一次会话多少钱，一个月烧多少钱（月度投影）。
3. **构成**：哪个文件最烧钱，从大到小排好。
4. **可砍清单**：哪些能删、能省多少、哪些不许碰（合规项）。

每一个数字都能手算复核，不藏误差。

## 执行前

先跑 `python --version`（Windows）或 `python3 --version` 确认解释器可用。不可用时**不要中断任务**：改用 `wc -m` 数出每个文件的字符数，按下面「换算方式」的系数折算区间，再乘单价得出单次成本，并在结论第一行声明「本次为手工核算，未运行脚本」。脚本只是省时间，公式才是本体。

## 流程

### 1. 算账单

```bash
python scripts/estimate.py <目录> \
  --price-per-1m 10 \
  --context-window 200000 \
  --sessions-per-month 500
```

- `--price-per-1m`：单价（元/百万 token）。价格会变，别写死，按你用的模型填。
- `--context-window`：上下文窗口大小，算「窗口占用率」，回答「会不会爆窗口」。
- `--sessions-per-month`：每月会话数，把单次成本放大成月度账单——这是老板最想看的那行。

### 2. 找死重

```bash
python scripts/audit.py <目录> --price-per-1m 10 --sessions-per-month 500
```

识别三类死重，并给「可砍金额」提示：

- **超长/主导文件**：单文件 token 中点 > 8000，或占总量 ≥ 30%——账单里最大的一块，优先看。
- **跨文件重复**：两个文件共享 ≥ 40 字符的重复片段。
- **触发条件模糊**：SKILL.md 的 description < 20 字符，模型根本不知道何时该调它。

### 3. 看风险分级

每一项死重带风险分级：

- **低**：背景叙述、重复规则——可删可压缩，损失小。
- **中**：触发条件模糊的 skill——删了可能少一个能力，需人工确认。
- **高**：安全/合规硬约束（命中 secret/key/token/密评/商用密码/身份/社保/工资等）——**不许删，只能压缩措辞**。

## 换算方式（为什么可信）

中文和英文用不同系数，不混在一起（混了会低估中文一半以上）：

- CJK 字符（中文/假名/谚文/全角标点）＝ 0.9 ~ 1.3 token/字符
- 其余字符（ASCII/空白/符号）＝ 0.25 ~ 0.35 token/字符
- 每个文件 +2 token 固定开销

**给区间不给单点。** 任何数字都带 lo~hi，你或你的评审能拿字符数按上面的公式反推核对。公式见 `references/conversion.md`，一次完整运行的输出留档在 `tests/bill-config-demo.txt`。

## 硬约束

1. **只读。** 不修改被扫描目录。
2. **可砍 ≠ 已省。** 账单里绝不写「已省 X 元」，只写「可砍 X」。承诺尚未发生的节省，是这类工具最容易被识破的谎言。
3. **盲区必须标明。** 不读使用日志，无法判断某个 skill 是否真的从未被使用。对这类对象只输出 `unknown`，不推测「这个可能没人用」。
4. **加载行为可见。** 用 `--load-mode auto|full|meta` 声明账单是基于哪种加载假设算的——不同 harness 对 skill 的加载行为不同，藏起来会低估一倍。

## 何时不用

- 没有配置目录时——先向用户要目录。
- 只问单个文件多大时——`wc -c` 或 `wc -m` 就够，不必上估算。
- 要精确到「真实 tokenizer 逐字对照」时——本工具不联网、不引第三方依赖，只给区间，不给假精度。
