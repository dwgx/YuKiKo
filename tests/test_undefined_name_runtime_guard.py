"""全仓守卫：可执行位置的未定义符号（F821）一个都不许有。

背景：`core/agent_tools_social.py` 的六个 QZone 符号、`core/tools_video.py` 漏
`SearchEngine`，都是同一种形态 —— 某个名字既没 import 也没在本模块定义，
`ruff --select F821` 早就报了，但没人看基线。

为什么单测和冒烟都抓不到：
1. 只在该代码路径**被真正走到**时才抛 NameError；
2. handler 的 `except Exception` 会把 NameError 包成
   `error="extract_error"` / `display="资源检索失败: ..."` 这类**像业务失败的话**。

实测代价：`bilibili_audio_extract` 线上 2 次调用全废，而它正是音乐放不出来时
模型给用户的替代方案 —— 用户连着撞两个死路，日志里看不出是代码 bug。

## 为什么不能对 F821 一刀切

本仓大量文件有 `from __future__ import annotations`，注解不在定义时求值，
所以 `-> Callable[..., Awaitable[Any]]` 这种**注解位**的 F821 运行时无害
（CLAUDE.md 也说明某些 F821 是刻意的）。判据：

* 出现在 `->` 返回注解、参数 `: 类型`、`AnnAssign` 的注解位 → 无害
* 出现在赋值右侧 / 函数调用 / 属性访问 → **运行时炸弹**

本文件用 ruff 定位 F821，再用 AST 判定每处是注解位还是可执行位，
只对可执行位断言。注解位那批留给 `test_annotation_only_undefined_names_stay_deferred`
兜住 —— 它们无害的前提是那个 `__future__` import 还在。
"""

from __future__ import annotations

import ast
import json
import subprocess
import unittest
from functools import lru_cache
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent
_RUFF = _REPO / ".venv" / "bin" / "ruff"


@lru_cache(maxsize=1)
def _f821_findings() -> tuple[tuple[str, int, int], ...]:
    """全仓 F821，返回 (相对路径, row, col)。ruff 自己会套用 per-file-ignores。"""

    ruff = str(_RUFF) if _RUFF.exists() else "ruff"
    proc = subprocess.run(
        [ruff, "check", "--output-format=json", "--select", "F821", "."],
        cwd=_REPO,
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode not in (0, 1):
        raise unittest.SkipTest(f"ruff 跑不起来，无法执行本守卫: {proc.stderr.strip()[:200]}")

    findings = []
    for item in json.loads(proc.stdout or "[]"):
        path = Path(item["filename"])
        rel = path.relative_to(_REPO) if path.is_absolute() else path
        findings.append((rel.as_posix(), item["location"]["row"], item["location"]["column"]))
    return tuple(sorted(findings))


@lru_cache(maxsize=None)
def _annotation_spans(rel_path: str) -> tuple[tuple[int, int, int, int], ...]:
    """该文件所有注解子树的 (起行, 起列, 止行, 止列)，列号 1-based 对齐 ruff。"""

    tree = ast.parse((_REPO / rel_path).read_text(encoding="utf-8"))
    spans = []
    for node in ast.walk(tree):
        annotations = []
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            annotations.append(node.returns)
        elif isinstance(node, ast.arg):
            annotations.append(node.annotation)
        elif isinstance(node, ast.AnnAssign):
            annotations.append(node.annotation)
        for ann in annotations:
            if ann is None or ann.end_lineno is None or ann.end_col_offset is None:
                continue
            spans.append((ann.lineno, ann.col_offset + 1, ann.end_lineno, ann.end_col_offset + 1))
    return tuple(spans)


def _is_inside_annotation(rel_path: str, row: int, col: int) -> bool:
    for start_row, start_col, end_row, end_col in _annotation_spans(rel_path):
        after_start = (row, col) >= (start_row, start_col)
        before_end = (row, col) <= (end_row, end_col)
        if after_start and before_end:
            return True
    return False


def _executable_position_findings() -> list[tuple[str, int, int]]:
    return [f for f in _f821_findings() if not _is_inside_annotation(f[0], f[1], f[2])]


class UndefinedNameRuntimeGuardTests(unittest.TestCase):
    def test_ruff_f821_baseline_is_readable(self) -> None:  # noqa: D401
        """守卫自身的自检：ruff 必须真跑出结果，否则本文件是空转的绿灯。"""

        findings = _f821_findings()
        self.assertTrue(
            findings,
            "全仓 F821 一条都没有 —— 要么 ruff 配置变了没扫到文件，"
            "要么本守卫的调用方式失效。空结果不等于干净。",
        )

    def test_annotation_classifier_recognises_a_known_annotation(self) -> None:
        """自检：分类器必须真能认出注解位，否则它会把所有 F821 判成炸弹。"""

        annotated = [f for f in _f821_findings() if _is_inside_annotation(f[0], f[1], f[2])]
        self.assertTrue(
            annotated,
            "没有任何 F821 被判成注解位 —— 分类器坏了（本仓存在 "
            "`-> Callable[..., Awaitable[Any]]` 这类刻意的注解位 F821）",
        )

    def test_no_undefined_name_in_executable_position(self) -> None:
        bombs = _executable_position_findings()
        rendered = "\n".join(f"  {path}:{row}:{col}" for path, row, col in bombs)
        self.assertEqual(
            bombs,
            [],
            "这些位置引用了既没 import 也没定义的名字，且不在注解位 —— "
            "该代码路径一走到就 NameError，而 handler 的 except 会把它伪装成业务失败:\n"
            f"{rendered}\n"
            "改法：补一行 import（或修正符号名）。不要靠 try/except 兜。",
        )

    def test_files_relying_on_deferred_annotations_keep_the_future_import(self) -> None:
        """注解位 F821 无害的唯一前提：那个 __future__ import 还在。

        谁删掉它，这些文件会在 import 时就 NameError —— 整个 bot 起不来。
        """

        annotated_files = sorted(
            {path for path, row, col in _f821_findings() if _is_inside_annotation(path, row, col)}
        )
        missing = [
            path
            for path in annotated_files
            if "from __future__ import annotations" not in (_REPO / path).read_text(encoding="utf-8")
        ]
        self.assertEqual(
            missing,
            [],
            "这些文件的注解引用了未定义名字，却没有 `from __future__ import annotations` —— "
            f"注解会在定义时求值并直接 NameError: {missing}",
        )


if __name__ == "__main__":
    unittest.main()
