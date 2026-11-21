#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""context-cfo 死重判定：在估算结果上标出「可砍」对象与风险分级。

只做识别与标注，不做删除。判定维度（详见 ../DESIGN-NOTES.md）：
- 超长文件：单文件 token 区间中点超过阈值。
- 跨文件重复：两个文件间长文本片段重复（滑动窗口哈希）。
- 触发条件模糊：SKILL.md 的 description 过短或缺少可判定的触发词。

风险分级：
- 低：背景叙述、重复规则（可删可压缩，损失小）。
- 中：触发条件模糊的 skill（删了可能少一个能力，需人工确认）。
- 高：安全/合规硬约束（不许删，只能压缩措辞）。本脚本按文件名/关键词保守标记，
  无法确证时标 unknown，不推测。

用法：
    python audit.py <目录> [--price-per-1m 价格] [--json]

输出：一个可砍清单，每项带 token 区间、可省金额区间、风险分级。
"""

import argparse
import json
import os
import sys
import hashlib

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import estimate as E

# 阈值
LONG_FILE_MID = 8000       # 绝对超长：单文件 token 中点超过此值（对应臃肿正文）
DOMINANT_RATIO = 0.30      # 相对判据：单文件占总量比例超过此值即标记为「主导文件」
DUP_MIN_CHARS = 40         # 重复片段最少字符数
DUP_MIN_WINDOW = 20        # 滑动窗口大小（字符）

# 风险关键词（保守标记，只用于「疑似高」提示，不替代人工判断）
HIGH_RISK_KEYWORDS = [
    "secret", "key", "token", "credential", "password", "安全", "合规",
    "密评", "商用密码", "身份", "社保", "工资", "审计", "红线",
]

# 触发条件模糊：SKILL.md description 长度（字符）低于此值视为模糊
DESC_MIN_CHARS = 20


def find_long_files(records):
    """双判据：绝对超长（>LONG_FILE_MID）或主导文件（占总量 >DOMINANT_RATIO）。"""
    total_mid = sum((r["lo"] + r["hi"]) / 2 for r in records if not r.get("error"))
    out = []
    for r in records:
        if r.get("error"):
            continue
        mid = (r["lo"] + r["hi"]) / 2
        dominant = (total_mid > 0 and mid / total_mid >= DOMINANT_RATIO)
        if mid >= LONG_FILE_MID or dominant:
            out.append({
                "path": r["path"], "mid": mid, "lo": r["lo"], "hi": r["hi"],
                "reason": "绝对超长" if mid >= LONG_FILE_MID else "主导文件(占比%.0f%%)" % (mid / total_mid * 100 if total_mid else 0),
            })
    return out


def _windows(text, size):
    for i in range(0, len(text) - size + 1):
        yield text[i:i + size]


def _win_set(text, size):
    """返回 text 中所有非纯空白窗口的集合。"""
    s = set()
    for i in range(0, len(text) - size + 1):
        w = text[i:i + size]
        if w.strip():
            s.add(w)
    return s


def find_duplicates(root, files):
    """跨文件重复：对每对文件，用两个文件的窗口集合求交集。

    只有当两个不同文件共享至少一个长度为 DUP_MIN_WINDOW 的窗口时才记为重复。
    重复字符数估计 = 交集窗口数 × 窗口大小（下界估计，因为共享窗口可能重叠）。
    """
    dups = []
    contents = {}
    win_sets = {}
    for rel in files:
        full = os.path.join(root, rel)
        try:
            text = E.read_text(full)
        except OSError:
            text = ""
        contents[rel] = text
        win_sets[rel] = _win_set(text, DUP_MIN_WINDOW)
    rels = sorted(contents.keys())
    for i in range(len(rels)):
        for j in range(i + 1, len(rels)):
            a, b = rels[i], rels[j]
            wa, wb = win_sets[a], win_sets[b]
            common_windows = wa & wb
            if not common_windows:
                continue
            # 用不同窗口去重后的数量估计重复字符（下界）
            dup_chars = len(common_windows) * DUP_MIN_WINDOW
            if dup_chars >= DUP_MIN_CHARS:
                dups.append({
                    "a": a, "b": b,
                    "dup_chars_est": dup_chars,
                    "shared_windows": len(common_windows),
                })
    return dups


def fuzzy_trigger_files(root, files):
    """扫描 SKILL.md 的 description 是否过短。"""
    out = []
    for rel in files:
        if not rel.endswith("SKILL.md") and not rel.endswith(".md"):
            continue
        full = os.path.join(root, rel)
        try:
            text = E.read_text(full)
        except OSError:
            continue
        # 提取 frontmatter 的 description
        desc = _extract_description(text)
        if desc is not None and len(desc) < DESC_MIN_CHARS:
            out.append({"path": rel, "description_len": len(desc)})
    return out


def _extract_description(text):
    if not text.startswith("---"):
        return None
    end = text.find("---", 3)
    if end < 0:
        return None
    fm = text[3:end]
    for line in fm.splitlines():
        if line.startswith("description:"):
            return line[len("description:"):].strip().strip('"').strip("'")
    return None


def risk_grade(path, text_hint=""):
    low = path.lower()
    for kw in HIGH_RISK_KEYWORDS:
        if kw.lower() in low or kw in text_hint:
            return "高"
    if path.endswith("SKILL.md") or ".mcp" in low or "mcp.json" in low:
        return "中"
    return "低"


def main(argv):
    ap = argparse.ArgumentParser(description="context-cfo 死重判定")
    ap.add_argument("root")
    ap.add_argument("--price-per-1m", type=float, default=10.0)
    ap.add_argument("--sessions-per-month", type=int, default=0,
                    help="每月会话次数，用于可砍金额的月度投影")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    if not os.path.isdir(args.root):
        print("错误：目录不存在：%s" % args.root, file=sys.stderr)
        return 2

    records, _effective = E.scan(args.root, load_mode="full")
    files = [r["path"] for r in records]

    long_files = find_long_files(records)
    dups = find_duplicates(args.root, files)
    fuzzy = fuzzy_trigger_files(args.root, files)

    price = args.price_per_1m
    items = []

    for lf in long_files:
        mid = lf["mid"]
        items.append({
            "type": "超长/主导文件",
            "path": lf["path"],
            "lo": lf["lo"], "hi": lf["hi"],
            "savable": "部分",
            "risk": risk_grade(lf["path"]),
            "note": lf.get("reason", ""),
        })

    for d in dups:
        items.append({
            "type": "跨文件重复",
            "path": "%s <-> %s" % (d["a"], d["b"]),
            "lo": 0, "hi": 0,
            "savable": "部分",
            "risk": "低",
            "note": "估计重复约 %d 字符" % d["dup_chars_est"],
        })

    for f in fuzzy:
        items.append({
            "type": "触发条件模糊",
            "path": f["path"],
            "lo": 0, "hi": 0,
            "savable": "待人工确认",
            "risk": "中",
            "note": "description 仅 %d 字符" % f["description_len"],
        })

    # 计算可砍金额：只有「低/中」风险才计入可砍，「高」风险不计（不许删）。
    # 可砍 token 取该对象 token 区间的中点；重复/模糊对象无独立 token，只计其可省比例提示。
    total_savable_lo = 0.0
    total_savable_hi = 0.0
    for it in items:
        if it["risk"] == "高":
            continue
        lo, hi = it["lo"], it["hi"]
        # 跨文件重复与触发模糊未独立计算 token，仅作提示，不计入金额
        if lo <= 0 and hi <= 0:
            continue
        total_savable_lo += lo
        total_savable_hi += hi
    # 按「部分可砍」粗估：超长/主导文件通常可压缩 30%~70%，取 50% 中线作为提示量级
    savable_lo_money = total_savable_lo * 0.5 / 1e6 * price
    savable_hi_money = total_savable_hi * 0.5 / 1e6 * price
    monthly = None
    if args.sessions_per_month > 0:
        monthly = {
            "lo": savable_lo_money * args.sessions_per_month,
            "hi": savable_hi_money * args.sessions_per_month,
        }

    result = {
        "root": args.root,
        "price_per_1m": price,
        "items": items,
        "savable": {
            "token_lo": total_savable_lo,
            "token_hi": total_savable_hi,
            "money_lo": savable_lo_money,
            "money_hi": savable_hi_money,
            "monthly": monthly,
        },
        "summary": {
            "long_files": len(long_files),
            "duplicates": len(dups),
            "fuzzy_triggers": len(fuzzy),
            "total_flags": len(items),
        },
    }

    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print("=" * 72)
        print("AI 上下文死重清单（只标注，不删除）")
        print("=" * 72)
        print("")
        if total_savable_lo > 0:
            print("可砍提示：粗估每次可省 %.4f ~ %.4f 元（按可压缩 50%% 中线）"
                  % (savable_lo_money, savable_hi_money))
            if monthly:
                print("          月度可省 %.2f ~ %.2f 元（每月 %d 次会话）"
                      % (monthly["lo"], monthly["hi"], args.sessions_per_month))
            print("          注：这是「可砍」，不是「已省」。高风险的合规项未计入。")
        else:
            print("可砍提示：未发现可量化可砍金额的对象（重复/模糊类需人工确认）。")
        print("")
        print("阈值: 超长 > %.0f token(中点) / 重复片段 > %d 字符 / description < %d 字符"
              % (LONG_FILE_MID, DUP_MIN_CHARS, DESC_MIN_CHARS))
        print("")
        if not items:
            print("未发现可标注死重。")
        for it in items:
            tag = "[" + it["risk"] + "]"
            print("  %-10s %s  %s" % (it["type"], tag, it["path"]))
            if it["note"]:
                print("             %s" % it["note"])
        print("")
        print("风险分级: 低=背景叙述/重复规则(可删) / 中=触发模糊(需人工确认) / 高=安全合规(不许删，只压缩措辞)")
        print("盲区: 不读使用日志，无法判断某个 skill 是否真的从未被使用，不输出「未使用」类结论。")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
