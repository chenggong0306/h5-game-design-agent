"""Source-project reuse modes and readiness helpers.

The mode is deliberately stored on the source skill instead of inferred by the
model on every turn.  A complete, locally runnable web project is safe to port
mechanically; an incomplete reference must never pretend to be a faithful port.
"""

from __future__ import annotations

from typing import Any


SOURCE_MODE_FAITHFUL = "faithful_port"
SOURCE_MODE_EXTEND = "extend"
SOURCE_MODE_INSPIRED = "inspired"
SOURCE_MODES = {
    SOURCE_MODE_FAITHFUL,
    SOURCE_MODE_EXTEND,
    SOURCE_MODE_INSPIRED,
}


def web_bundle_readiness(manifest: dict[str, Any] | None) -> tuple[bool, list[str]]:
    """Return whether an imported HTML entry can be executed without missing code.

    Images/audio are handled by the source-asset bundle.  Here we only require an
    HTML entry and every directly loaded local CSS/JS dependency to be readable.
    External code is intentionally not considered runnable because preview
    verification blocks arbitrary network access.
    """

    data = manifest or {}
    reasons: list[str] = []
    if not str(data.get("entrypoint") or "").strip():
        reasons.append("缺少 HTML 入口")
    if int(data.get("dependency_overflow_count") or 0) > 0:
        reasons.append("HTML 依赖数量超过安全上限")
    for dependency in data.get("dependencies") or []:
        if not isinstance(dependency, dict):
            continue
        status = str(dependency.get("status") or "")
        # readable = 可读源码；library = 已保存并可回源的运行时库（three.js/jQuery 等），
        # 生成游戏用 <script src> 引入即满足运行依赖，导出时也会内联，故同样视为可运行。
        if status in ("readable", "library"):
            continue
        reference = str(
            dependency.get("reference")
            or dependency.get("resolved_path")
            or "未知依赖"
        )
        if status == "external":
            reasons.append(f"外部运行依赖不可离线验证：{reference}")
        else:
            reasons.append(f"本地运行依赖不可用：{reference}（{status or 'missing'}）")
    # Stable order and bounded diagnostics keep API/prompt payloads predictable.
    return not reasons, list(dict.fromkeys(reasons))[:20]


def default_source_mode(manifest: dict[str, Any] | None) -> str:
    runnable, _ = web_bundle_readiness(manifest)
    return SOURCE_MODE_FAITHFUL if runnable else SOURCE_MODE_INSPIRED


def normalise_source_mode(value: Any, manifest: dict[str, Any] | None = None) -> str:
    mode = str(value or "").strip().lower()
    if mode in SOURCE_MODES:
        return mode
    return default_source_mode(manifest)


def mode_label(mode: str) -> str:
    return {
        SOURCE_MODE_FAITHFUL: "忠实移植",
        SOURCE_MODE_EXTEND: "保留玩法后增强",
        SOURCE_MODE_INSPIRED: "仅参考创作",
    }.get(mode, mode)


__all__ = [
    "SOURCE_MODE_FAITHFUL",
    "SOURCE_MODE_EXTEND",
    "SOURCE_MODE_INSPIRED",
    "SOURCE_MODES",
    "default_source_mode",
    "mode_label",
    "normalise_source_mode",
    "web_bundle_readiness",
]
