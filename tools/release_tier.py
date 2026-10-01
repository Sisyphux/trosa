#!/usr/bin/env python3
"""发布风险分级：由发布流水线根据候选 diff 计算，不由开发方声明。

改动的“级别”决定流水线对它的处理方式，规则本身属于受保护位置（流水线一侧），
开发方只能提交 commit，无法把某个改动标成低风险来绕过门禁或放行。

    T2  只要命中以下任一，就必须人工 approve 后才发布：
          * migrations/ 下任何文件（数据库迁移，不可逆高风险）
          * deploy/ 下任何文件（发布/备份/恢复/门禁/流水线自身）
          * serve.py、config.py（正式运行时入口与配置）
          * AGENTS.md（治理文件）
          * tools/ 下文件名含 release/migration/backup/restore 的工具
    T0  所有改动都只落在 docs/、design/、tests/ 前缀，或以 .md 结尾
        （纯文档/测试，不影响运行时代码）→ 合入 main，不部署
    T1  其余常规代码改动 → 门禁绿后自动发布

T2 优先于 T0/T1。空改动集合按 T0 处理（流水线另行要求候选 diff 非空）。

开发期 ``--dev-gate`` 只给开发方 ``agent-worktree.sh test`` 用，决定本地跑哪一档，
规则比发布分级更保守（fail closed），且不改变任何发布前门禁：

    fast  改动全部落在 docs/、design/、tests/、tools/ 或以 .md 结尾（文档/CLI/测试）
    full  其余情况（含 app.py、app/static、migrations/ 等运行时代码，空改动，以及
          任何 T2 受保护路径）

发布前的 pre-publish 门禁（``release-commit.sh`` 与常驻流水线）从不读这个档位，
始终执行完整门禁。

用法：
    tools/release_tier.py [--paths PATH ...] [--explain] [--dev-gate]
    printf '%s\n' a.py b.md | tools/release_tier.py

stdout 只打印级别（T0/T1/T2；带 --dev-gate 时为 fast/full），便于 shell 直接取用；
--explain 把理由写到 stderr。
"""

from __future__ import annotations

import argparse
import sys

T2_EXACT = frozenset({"serve.py", "config.py", "AGENTS.md"})
T2_PREFIXES = ("migrations/", "deploy/")
T2_TOOL_KEYWORDS = ("release", "migration", "backup", "restore")
T0_PREFIXES = ("docs/", "design/", "tests/")


def _t2_reason(path: str) -> str | None:
    if path.startswith(T2_PREFIXES):
        return "命中受保护路径（迁移/发布/运行时基础设施）"
    if path in T2_EXACT:
        return "命中正式运行时入口或治理文件"
    if path.startswith("tools/"):
        base = path.rsplit("/", 1)[-1]
        if any(keyword in base for keyword in T2_TOOL_KEYWORDS):
            return "命中发布/迁移/备份工具"
    return None


def _is_t0(path: str) -> bool:
    return path.startswith(T0_PREFIXES) or path.endswith(".md")


def classify(paths) -> tuple[str, list[str]]:
    """返回 (级别, 理由列表)。"""
    paths = [p for p in (paths or []) if p]
    if not paths:
        return "T0", ["候选 diff 为空"]
    for path in paths:
        reason = _t2_reason(path)
        if reason:
            return "T2", [f"{path}: {reason}"]
    not_t0 = [path for path in paths if not _is_t0(path)]
    if not_t0:
        return "T1", [f"{path}: 常规运行时代码改动" for path in not_t0]
    return "T0", [f"{path}: 纯文档/测试改动" for path in paths]


# 开发期 test 分档：只决定开发方本地跑“快档（语法 + 单元测试子集）”还是“完整档”。
# 它不参与发布判定：release-commit.sh 与常驻流水线从不读这个档位，始终完整档。
# 规则比发布分级更保守（fail closed）：只要有一条路径无法判定为安全，就走完整档。
DEV_FAST_PREFIXES = ("docs/", "design/", "tests/", "tools/")


def dev_gate_tier(paths) -> str:
    """返回开发期门禁档位：``fast`` 或 ``full``。

    * 空改动无法判定范围 → ``full``；
    * 命中 T2 受保护路径（migrations/、deploy/、serve.py、config.py、AGENTS.md、
      release/migration/backup/restore 工具）→ ``full``；
    * 全部落在 docs/、design/、tests/、tools/ 或以 .md 结尾 → ``fast``；
    * 其余运行时代码（app.py、app/static、db.py 等）→ ``full``。
    """
    paths = [p for p in (paths or []) if p]
    if not paths:
        return "full"
    for path in paths:
        if _t2_reason(path):
            return "full"
        if not (path.startswith(DEV_FAST_PREFIXES) or path.endswith(".md")):
            return "full"
    return "fast"


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="计算候选改动的发布风险级别（T0/T1/T2）",
        add_help=True,
    )
    parser.add_argument("--paths", nargs="*", default=None, help="显式给出改动路径；省略则从 stdin 按行读取")
    parser.add_argument("--explain", action="store_true", help="把判定理由写到 stderr")
    parser.add_argument(
        "--dev-gate",
        action="store_true",
        help="输出开发期 test 分档 fast/full（只给 agent-worktree.sh test 用；发布门禁不读它）",
    )
    args = parser.parse_args(argv)

    if args.paths is None:
        raw = sys.stdin.read()
        paths = [line.strip() for line in raw.replace("\x00", "\n").splitlines()]
    else:
        paths = args.paths

    if args.dev_gate:
        gate = dev_gate_tier(paths)
        if args.explain:
            print(f"开发期分档：{gate}", file=sys.stderr)
        print(gate)
        return 0

    tier, reasons = classify(paths)
    if args.explain:
        if len(paths) == 1 and reasons and reasons[0].endswith("候选 diff 为空"):
            print(f"分级理由：{reasons[0]}", file=sys.stderr)
        else:
            for reason in reasons:
                print(f"分级理由：{reason}", file=sys.stderr)
    print(tier)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
