#!/usr/bin/env python3
"""Daylight 视觉层对比度检查。

读取 app/static/visual-v3.css 第一个 :root 块里的 --dl-* 色值，逐对计算
WCAG 对比度并对照阈值：

  text  ≥ 4.5:1  承载信息的文字（日期、标签、说明、placeholder）
  ui    ≥ 3.0:1  界面元素、键盘焦点环
  deco  不检查     纯装饰（分隔线、圆点、背景光），只输出数值供参考

另做一次静态扫描（--dl-gold / --dl-faint 不得用作信息文字色，焦点规则不得取 --dl-gold），
防止以后把亮金或淡灰重新用回承载信息的位置。

用法：
  python3 tools/check_daylight_contrast.py            # 输出全表，有失败则退出码 1
  python3 tools/check_daylight_contrast.py --failures # 只输出失败项
禁用态豁免（WCAG 1.4.3），这里不检查。
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

CSS = Path(__file__).resolve().parent.parent / "app" / "static" / "visual-v3.css"

SURFACES = ["dl-paper", "dl-paper-2", "dl-paper-3", "dl-canvas"]
SOFT_SURFACES = ["dl-gold-soft", "dl-danger-soft", "dl-ok-soft"]

# 承载信息的文字色：必须 ≥4.5:1
TEXT_TOKENS = ["dl-ink", "dl-ink-2", "dl-ink-3", "dl-mut", "dl-gold-ink"]
# 状态文字色（可能落在 paper 或对应 soft 底上）
STATUS_TEXT = {
    "dl-danger": ["dl-danger-soft"],
    "dl-ok": ["dl-ok-soft"],
    "dl-info": [],
    "dl-warn": [],
    "dl-gold-ink": ["dl-gold-soft"],
}
# 焦点环色：必须 ≥3:1
FOCUS_TOKENS = ["dl-focus"]
# 仅装饰：不判定，只报告
DECORATIVE_TOKENS = ["dl-gold", "dl-faint"]
# 房间渐变最底端：文字不应落在这里，只报告
FLOOR = "dl-canvas-low"

TEXT_MIN = 4.5
UI_MIN = 3.0


def parse_root(css: str) -> dict[str, str]:
    m = re.search(r":root\s*\{(.*?)\n\}", css, re.S)
    if not m:
        raise SystemExit("找不到 :root 块")
    return dict(re.findall(r"--([\w-]+)\s*:\s*([^;]+);", m.group(1)))


def parse_color(value: str, tokens: dict[str, str], under=None):
    value = value.strip()
    v = re.match(r"var\(--([\w-]+)\)", value)
    if v:
        return parse_color(tokens[v.group(1)], tokens, under)
    h = re.match(r"#([0-9a-fA-F]{6})$", value)
    if h:
        n = int(h.group(1), 16)
        return ((n >> 16) & 255, (n >> 8) & 255, n & 255)
    r = re.match(r"rgba?\(([^)]+)\)", value)
    if r:
        parts = [float(p) for p in r.group(1).split(",")]
        if len(parts) == 4 and under is not None:
            a = parts[3]
            return tuple(round(parts[i] * a + under[i] * (1 - a)) for i in range(3))
        return tuple(int(p) for p in parts[:3])
    raise ValueError(f"无法解析颜色：{value}")


def lum(rgb) -> float:
    def ch(c):
        c /= 255
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4
    r, g, b = (ch(c) for c in rgb)
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def ratio(fg, bg) -> float:
    a, b = lum(fg), lum(bg)
    hi, lo = max(a, b), min(a, b)
    return (hi + 0.05) / (lo + 0.05)


# 品牌 Logo 不受 WCAG 1.4.11 约束，允许保留亮金
LOGO_ALLOW = (".login-logo",)
DISABLED_RE = re.compile(r":disabled|\[disabled\]|\.disabled|\[aria-disabled")


def scan_usage(css: str) -> list[str]:
    """静态扫描：亮金/淡灰不得承载信息，焦点规则不得取亮金。"""
    problems = []
    for block in re.finditer(r"([^{}]+)\{([^{}]*)\}", css):
        selector = " ".join(block.group(1).split())
        body = block.group(2)
        exempt = bool(DISABLED_RE.search(selector)) or selector.startswith(LOGO_ALLOW)
        if not exempt and re.search(r"(?<![\w-])color\s*:\s*var\(--dl-(faint|gold)\)", body):
            problems.append(f"信息文字/图形不得使用 --dl-faint / --dl-gold：{selector[:90]}")
        is_focus = ":focus" in selector
        if is_focus and re.search(r"(outline|border-color)[^;]*var\(--dl-gold\)", body):
            problems.append(f"焦点样式必须使用 --dl-focus，而不是 --dl-gold：{selector[:90]}")
    return problems


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--failures", action="store_true", help="只输出失败项")
    args = ap.parse_args()

    tokens = parse_root(CSS.read_text(encoding="utf-8"))

    def color(name, under=None):
        return parse_color(tokens[name], tokens, under)

    rows = []  # (kind, fg, bg, ratio, minimum or None)

    for fg in TEXT_TOKENS:
        for bg in SURFACES:
            rows.append(("text", fg, bg, ratio(color(fg), color(bg)), TEXT_MIN))
    for fg, soft in STATUS_TEXT.items():
        for bg in ["dl-paper"] + soft:
            rows.append(("text", fg, bg, ratio(color(fg), color(bg)), TEXT_MIN))
    for fg in FOCUS_TOKENS:
        if fg not in tokens:
            continue
        for bg in SURFACES + ["dl-gold-soft"]:
            rows.append(("focus", fg, bg, ratio(color(fg), color(bg)), UI_MIN))
        # 实心墨色按钮上的焦点环用暖白（--dl-focus-on-ink），间距留白后落在页面底色上
        if "dl-focus-on-ink" in tokens:
            rows.append(("focus", "dl-focus-on-ink", "dl-ink",
                         ratio(color("dl-focus-on-ink"), color("dl-ink")), UI_MIN))
            rows.append(("focus", "dl-focus", "dl-ink",
                         ratio(color("dl-focus"), color("dl-ink")), 1.0))
    # 现状里 focus 环直接取 dl-gold；没有 dl-focus 时把它当焦点色检查，得到修复前基线
    if "dl-focus" not in tokens:
        for bg in SURFACES + ["dl-gold-soft"]:
            rows.append(("focus", "dl-gold", bg, ratio(color("dl-gold"), color(bg)), UI_MIN))
    for fg in DECORATIVE_TOKENS:
        for bg in SURFACES:
            rows.append(("deco", fg, bg, ratio(color(fg), color(bg)), None))

    for fg in ["dl-ink-3", "dl-mut"]:
        rows.append(("deco", fg, FLOOR, ratio(color(fg), color(FLOOR)), None))

    # 半透明 hairline 分隔线：只报告
    for fg in ["dl-hair", "dl-hair-strong"]:
        for bg in SURFACES:
            rows.append(("deco", fg, bg, ratio(color(fg, color(bg)), color(bg)), None))

    failed = 0
    print(f"{'类别':<6}{'前景':<18}{'背景':<16}{'比值':>7}  阈值  结果")
    for kind, fg, bg, r, need in rows:
        ok = need is None or r >= need
        if need is not None and not ok:
            failed += 1
        if args.failures and ok:
            continue
        need_s = "-" if need is None else f"{need:g}"
        mark = "装饰" if need is None else ("PASS" if ok else "FAIL")
        print(f"{kind:<6}{fg:<18}{bg:<16}{r:>7.2f}  {need_s:<5} {mark}")

    css_text = CSS.read_text(encoding="utf-8")
    violations = scan_usage(css_text)
    for v in violations:
        print(f"usage FAIL  {v}")
    failed += len(violations)

    print(f"\n失败 {failed} 项（阈值：文字 {TEXT_MIN}:1，界面/焦点 {UI_MIN}:1）")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
