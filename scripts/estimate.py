#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""context-cfo 估算内核：把字符数换算成 token 区间。

设计原则（详见 ../DESIGN-NOTES.md）：
- 只读，不修改被扫描目录。
- 分字符类型估算：CJK 与其余字符用不同系数，避免中文场景被系统性低估。
- 给区间不给单点：每个字符类贡献一个 [lo, hi]，求和得文件区间。
- 可手算复核：任何数字都能按本文件声明的公式用字符数反推。
- 盲区：不读使用日志，无法判断某个 skill 是否真被用过，只输出 unknown。

用法：
    python estimate.py <目录> [--price-per-1m 价格] [--load-mode auto|full|meta] [--json]

输出（默认人类可读账单；--json 输出机器可读）四个信息块：
总量 / 构成 / 单次会话成本 / 可砍量。
"""

import argparse
import json
import os
import sys
import unicodedata

# 常用上下文窗口大小（token），用于「窗口占用率」。可配置。
DEFAULT_CONTEXT_WINDOW = 200000

# ---------------------------------------------------------------------------
# 换算系数（单位：token / 字符）
# 依据见 DESIGN-NOTES.md「系数依据」。此处只声明，不在这里论证。
# ---------------------------------------------------------------------------
# CJK 字符（含中文、日文假名、韩文、全角标点）每个字符约 1 个 token，
# 但随上下文波动。取 0.9 ~ 1.3。
CJK_LO = 0.9
CJK_HI = 1.3

# 其余字符（ASCII 字母数字、空白、符号、半角标点、换行）。
# 英文按约 4 字符/token；代码/JSON 密度略高，按 3.5 字符/token 起。
# 保守区间：每字符 0.25 ~ 0.35 token（即 2.86 ~ 4.0 字符/token）。
OTHER_LO = 0.25
OTHER_HI = 0.35

# 文件固定开销（token/文件）：每个文件在打包进 prompt 时会有少量分隔符
# 与元数据开销。这是 order-of-magnitude 估计，默认 2，可在设计笔记中复核。
PER_FILE_OVERHEAD = 2


def char_class(ch):
    """返回 'cjk' 或 'other'。"""
    o = ord(ch)
    if o < 128:
        return "other"
    # 汉字、假名、谚文、CJK 扩展、全角标点、中文标点
    if (0x4E00 <= o <= 0x9FFF or 0x3400 <= o <= 0x4DBF or
            0xF900 <= o <= 0xFAFF or 0x20000 <= o <= 0x2FFFF or
            0x3040 <= o <= 0x30FF or 0xAC00 <= o <= 0xD7AF or
            0x3000 <= o <= 0x303F or 0xFF00 <= o <= 0xFFEF or
            0x2018 <= o <= 0x201D or 0x201C <= o <= 0x201F or
            0x3001 <= o <= 0x3002 or 0x2026 == o):
        return "cjk"
    return "other"


def count_chars(text):
    """统计一段文本的 CJK 字符数与其余字符数。

    换行符统一按 \\n 处理：CRLF 的 \\r 是平台 artifact，主流 tokenizer
    会把 \\r\\n 合并，故归一化后不重复计入，保证 Windows/Unix 结果一致。
    """
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    cjk = 0
    other = 0
    for ch in text:
        if ch in ("\n", "\t", " "):
            other += 1
        elif char_class(ch) == "cjk":
            cjk += 1
        else:
            other += 1
    return cjk, other


def token_range(cjk, other, per_file_overhead=PER_FILE_OVERHEAD):
    """按字符数算 token 区间 [lo, hi]。"""
    lo = cjk * CJK_LO + other * OTHER_LO + per_file_overhead
    hi = cjk * CJK_HI + other * OTHER_HI + per_file_overhead
    return lo, hi


def iter_files(root):
    """只读遍历 root 下所有常规文件，返回相对路径列表（按路径排序，保证确定性）。"""
    out = []
    for dirpath, dirnames, filenames in os.walk(root):
        # 排序保证确定性
        dirnames.sort()
        for fn in sorted(filenames):
            full = os.path.join(dirpath, fn)
            if os.path.isfile(full):
                rel = os.path.relpath(full, root)
                out.append(rel)
    out.sort()
    return out


def read_text(path):
    """读取文件文本，UTF-8 失败则回退 latin-1（不计为乱码，只按字节展开）。"""
    with open(path, "rb") as f:
        data = f.read()
    for enc in ("utf-8", "latin-1"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


def frontmatter_only(text):
    """返回文件 frontmatter 部分（若存在），否则返回空串。

    加载行为「meta」：harness 只注入 SKILL.md 的 frontmatter（name+description）。
    本函数提取 '---' ... '---' 之间的内容，用于按需加载假设。
    """
    if not text.startswith("---"):
        return ""
    end = text.find("---", 3)
    if end < 0:
        return ""
    return text[:end + 3]


def resolve_auto_mode(files, root):
    """auto 语义：目录里若有带正文的 SKILL.md（frontmatter 之后仍有实质内容），
    则该 harness 大概率全量注入，按 full；否则按 meta。返回 'full' 或 'meta'。
    """
    for rel in files:
        if not (rel.endswith("SKILL.md") or rel.endswith(".md")):
            continue
        try:
            text = read_text(os.path.join(root, rel))
        except OSError:
            continue
        if text.startswith("---"):
            end = text.find("---", 3)
            if end >= 0:
                body = text[end + 3:].strip()
                if body:
                    return "full"
        else:
            # 无 frontmatter 的 md，本身就有正文
            if text.strip():
                return "full"
    return "meta"


def scan(root, load_mode="auto"):
    """扫描 root，按 load_mode 决定每个文件计入多少字符。

    - full：全量计入（正文 + frontmatter）。
    - meta：只计入 frontmatter（无 frontmatter 的文件计 0，但保留文件记录）。
    - auto：resolve_auto_mode 判定——有带正文的 SKILL.md 即 full，否则 meta。
    """
    files = iter_files(root)
    if load_mode == "auto":
        effective = resolve_auto_mode(files, root)
    else:
        effective = load_mode

    records = []
    for rel in files:
        full = os.path.join(root, rel)
        try:
            text = read_text(full)
        except OSError as e:
            records.append({
                "path": rel, "cjk": 0, "other": 0,
                "lo": 0, "hi": 0, "error": str(e),
            })
            continue

        if effective == "meta":
            text = frontmatter_only(text)
        # full / effective full：text 保持全量

        cjk, other = count_chars(text)
        lo, hi = token_range(cjk, other)
        records.append({
            "path": rel, "cjk": cjk, "other": other,
            "lo": lo, "hi": hi, "error": None,
        })
    return records, effective


def aggregate(records):
    total_cjk = sum(r["cjk"] for r in records)
    total_other = sum(r["other"] for r in records)
    total_lo = sum(r["lo"] for r in records)
    total_hi = sum(r["hi"] for r in records)
    return {
        "n_files": len(records),
        "cjk": total_cjk,
        "other": total_other,
        "lo": total_lo,
        "hi": total_hi,
    }


def build_bill(records, agg, price_per_1m, load_mode,
               context_window=DEFAULT_CONTEXT_WINDOW, sessions_per_month=0):
    # 单次会话成本 = 区间中点 token × 单价
    mid = (agg["lo"] + agg["hi"]) / 2
    cost_lo = agg["lo"] / 1e6 * price_per_1m
    cost_hi = agg["hi"] / 1e6 * price_per_1m
    cost_mid = mid / 1e6 * price_per_1m

    # 上下文窗口占用率（用区间中点 token）
    occupancy = (mid / context_window * 100) if context_window > 0 else 0.0

    # 月度投影
    monthly = None
    if sessions_per_month > 0:
        monthly = {
            "sessions_per_month": sessions_per_month,
            "lo": cost_lo * sessions_per_month,
            "hi": cost_hi * sessions_per_month,
            "mid": cost_mid * sessions_per_month,
        }

    return {
        "aggregate": agg,
        "cost": {"lo": cost_lo, "hi": cost_hi, "mid": cost_mid},
        "price_per_1m": price_per_1m,
        "load_mode": load_mode,
        "context_window": context_window,
        "occupancy_pct": occupancy,
        "monthly": monthly,
        "files": records,
    }


def _find_top_file(files):
    """返回 token 中点最大的文件，用于首屏钩子。"""
    best = None
    for r in files:
        if r.get("error"):
            continue
        mid = (r["lo"] + r["hi"]) / 2
        if best is None or mid > best[1]:
            best = (r["path"], mid, r["lo"], r["hi"])
    return best


def render(bill):
    agg = bill["aggregate"]
    mid = (agg["lo"] + agg["hi"]) / 2
    lines = []
    lines.append("=" * 72)
    lines.append("AI 上下文成本账单")
    lines.append("=" * 72)
    lines.append("")

    # —— 首屏钩子：一句话结论 ——
    top = _find_top_file(bill["files"])
    if agg["n_files"] == 0:
        lines.append("这个目录是空的，没有 token 开销。")
    else:
        lines.append("一句话结论：")
        if top:
            top_share = (top[1] / mid * 100) if mid > 0 else 0
            lines.append("  最烧钱的是 %s，占总量约 %.0f%%。" % (top[0], top_share))
        lines.append("  每次加载约 %.0f~%.0f token，成本 %.4f~%.4f 元。"
                     % (agg["lo"], agg["hi"], bill["cost"]["lo"], bill["cost"]["hi"]))
    lines.append("")
    mode_display = bill["load_mode"]
    if bill.get("effective_load_mode") and bill["load_mode"] == "auto":
        mode_display = "auto(判定为 %s)" % bill["effective_load_mode"]
    lines.append("假设：加载行为 %s ｜ 单价 %.2f 元/百万 token ｜ 窗口 %d"
                 % (mode_display, bill["price_per_1m"], bill["context_window"]))
    lines.append("")
    lines.append("【1 总量】")
    lines.append("  文件数     : %d" % agg["n_files"])
    lines.append("  CJK 字符   : %d" % agg["cjk"])
    lines.append("  其余字符   : %d" % agg["other"])
    lines.append("  token 区间 : %.0f ~ %.0f  (中点 %.0f)" % (agg["lo"], agg["hi"], mid))
    lines.append("  窗口占用   : %.1f%%（基于 %d token 窗口）"
                 % (bill["occupancy_pct"], bill["context_window"]))
    lines.append("")
    lines.append("【2 构成】（按文件，从大到小）")
    ordered = sorted([r for r in bill["files"] if not r.get("error")],
                     key=lambda r: -(r["lo"] + r["hi"]) / 2)
    for r in ordered:
        lines.append("  %-44s %6.0f ~ %6.0f token" % (r["path"], r["lo"], r["hi"]))
    for r in bill["files"]:
        if r.get("error"):
            lines.append("  %-44s ERR %s" % (r["path"], r["error"]))
    lines.append("")
    lines.append("【3 成本】")
    lines.append("  单次会话   : %.4f ~ %.4f 元（中点 %.4f）"
                 % (bill["cost"]["lo"], bill["cost"]["hi"], bill["cost"]["mid"]))
    if bill["monthly"]:
        m = bill["monthly"]
        lines.append("  月度投影   : %.2f ~ %.2f 元（按每月 %d 次会话）"
                     % (m["lo"], m["hi"], m["sessions_per_month"]))
    else:
        lines.append("  月度投影   : 未启用（用 --sessions-per-month 传入月会话数）")
    lines.append("")
    lines.append("【4 可砍量】")
    lines.append("  死重清单见 scripts/audit.py（不读使用日志，无法判定「从未使用」）。")
    lines.append("")
    return "\n".join(lines)


def main(argv):
    ap = argparse.ArgumentParser(description="context-cfo token 估算")
    ap.add_argument("root", help="要扫描的配置目录")
    ap.add_argument("--price-per-1m", type=float, default=10.0,
                    help="单价（元/百万 token），默认 10")
    ap.add_argument("--load-mode", choices=["auto", "full", "meta"],
                    default="auto", help="常驻/按需加载行为假设")
    ap.add_argument("--context-window", type=int, default=DEFAULT_CONTEXT_WINDOW,
                    help="上下文窗口大小（token），默认 200000")
    ap.add_argument("--sessions-per-month", type=int, default=0,
                    help="每月会话次数，用于月度成本投影，默认 0（不投影）")
    ap.add_argument("--json", action="store_true", help="输出 JSON")
    args = ap.parse_args(argv)

    if not os.path.isdir(args.root):
        print("错误：目录不存在：%s" % args.root, file=sys.stderr)
        return 2

    records, effective_mode = scan(args.root, load_mode=args.load_mode)
    agg = aggregate(records)
    bill = build_bill(records, agg, args.price_per_1m, args.load_mode,
                      context_window=args.context_window,
                      sessions_per_month=args.sessions_per_month)
    # auto 模式下把实际解析出的模式也带上，供展示
    bill["effective_load_mode"] = effective_mode

    if args.json:
        print(json.dumps(bill, ensure_ascii=False, indent=2))
    else:
        print(render(bill))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
