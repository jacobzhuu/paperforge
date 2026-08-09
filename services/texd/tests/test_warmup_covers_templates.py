"""预热文档必须覆盖模板导言区里的每一个宏包。

texd 运行时无外网、Tectonic 包缓存只在镜像构建期预热。模板里加一个
`\\usepackage{...}` 而忘了同步预热文档，等于给**所有**用到该语法的论文埋一颗雷：
运行时 `File 'xxx.sty' not found` → 降级逻辑注释掉宏包 → 依赖它的环境随即
`undefined` → PDF 彻底编不出来。longtable 就是这么漏掉的（三个模板都加载它，
四个预热文档一个都没有），带表格的论文因此 100% 失败。

Tectonic 的缓存按 **文件** 存、且构建期所有预热文档共用同一个缓存目录，
所以判据是「并集覆盖并集」：任一预热文档拉过 `ctex.sty`，全部模板就都能用。

这条测试是那次事故的回归护栏：预热与模板的漂移必须在 CI 就炸，而不是等用户
导出时才炸。
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
TEMPLATE_DIR = REPO_ROOT / "packages" / "latex_render" / "latex_render" / "templates"
WARMUP_DIR = REPO_ROOT / "services" / "texd" / "warmup"

_USEPACKAGE_RE = re.compile(r"\\usepackage(?:\[[^\]]*\])?\{([^}]*)\}")

# 模板用 `\IfFileExists{gbt7714.sty}{...}{...}` 显式声明为可选：取不到就走
# 分支里的降级路径，不是故障。它们不必进预热硬性清单。
OPTIONAL_PACKAGES = frozenset({"gbt7714"})


def _packages(paths: list[Path]) -> set[str]:
    """`\\usepackage{amsmath,amssymb}` 算两个包。"""
    return {
        name.strip()
        for path in paths
        for match in _USEPACKAGE_RE.findall(path.read_text(encoding="utf-8"))
        for name in match.split(",")
        if name.strip()
    }


def test_warmup_covers_every_template_package() -> None:
    templates = sorted(TEMPLATE_DIR.glob("*.tex.j2"))
    warmups = sorted(WARMUP_DIR.glob("warmup-*.tex"))
    assert templates and warmups, "模板或预热文档不见了，这条护栏就形同虚设"

    required = _packages(templates) - OPTIONAL_PACKAGES
    missing = required - _packages(warmups)
    assert not missing, (
        f"模板加载了 {sorted(missing)}，但没有任何预热文档拉过它们。"
        " Tectonic 运行时无外网，未预热的宏包会让所有用到该语法的论文编译失败。"
        f" 请把它们补进 {WARMUP_DIR.name}/ 下对应的预热文档导言区。"
    )


def test_longtable_is_warmed() -> None:
    """回归钉子：longtable 缺席预热导致「带表格的论文一律编不出 PDF」。

    渲染器对所有单栏模板的表格都用 longtable（可分页、续页重复表头），
    所以它不是可选项。
    """
    assert "longtable" in _packages(sorted(WARMUP_DIR.glob("warmup-*.tex")))
