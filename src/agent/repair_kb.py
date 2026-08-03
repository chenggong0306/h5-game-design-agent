"""修复知识库：自修成功经验持久化，同类问题直接复用（不再每次重新推导）。

学自 OpenGame Debug Skill 的 "living protocol of verified fixes"：
自检发现问题 → 模型修复 → 复检确认问题消失时，把（错误签名 → 已验证做法摘要）记进
data/repair_kb.json；之后 _build_repair_message 遇到同签名问题会附上历史做法。
运行越多命中越多，收益复利。任何环节出错都安静退化——知识库绝不能反过来阻断修复。
"""

import json
import os
import re
import threading
import time
from pathlib import Path

from src.config import settings

_KB_MAX_ENTRIES = 200   # 容量上限：超出按 (hits, last_seen) 淘汰最弱条目
_SUMMARY_MAX = 600      # 修复摘要截断（存模型自述的"改了什么"，不存整份 diff）
_lock = threading.Lock()
_cache: dict | None = None


def _kb_path() -> Path:
    # 与 langgraph checkpoint 同级（data/），随项目数据一起备份/清理
    return Path(settings.chroma_persist_dir).parent / "repair_kb.json"


def signature(issue: dict) -> str:
    """稳定签名：issue id + 归一化 msg。

    归一化把行号/URL/文件路径/十六进制/具体数字抹成占位，保留"错误的形状"——
    `TypeError: Cannot read properties of undefined (reading 'x') at game.js:123`
    和同错的 456 行版本必须命中同一条目。
    """
    msg = str(issue.get("msg") or "")
    msg = re.sub(r"https?://\S+", "<url>", msg)
    msg = re.sub(r"[A-Za-z]:\\\S+", "<path>", msg)
    msg = re.sub(r"(?<![\w<])/[\w.\-/]{6,}", "<path>", msg)
    msg = re.sub(r"0x[0-9a-fA-F]+", "<hex>", msg)
    msg = re.sub(r"\d+", "<n>", msg)
    msg = re.sub(r"\s+", " ", msg).strip().lower()
    return f"{issue.get('id') or 'runtime'}::{msg[:160]}"


def _load() -> dict:
    global _cache
    if _cache is not None:
        return _cache
    try:
        data = json.loads(_kb_path().read_text(encoding="utf-8"))
        entries = data.get("entries")
        _cache = {"entries": entries if isinstance(entries, dict) else {}}
    except (FileNotFoundError, json.JSONDecodeError, OSError, ValueError):
        _cache = {"entries": {}}  # 文件缺失/损坏 → 空库重建，绝不抛出
    return _cache


def _save(data: dict) -> None:
    path = _kb_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, path)  # 原子替换：读侧永远看到完整文件


def record_success(fixed_issues: list[dict], fix_summary: str) -> int:
    """自修后复检确认问题消失时调用。同签名条目累计 hits、保留最新摘要。返回记录条数。"""
    summary = re.sub(r"\s+", " ", str(fix_summary or "")).strip()[:_SUMMARY_MAX]
    if not summary or not fixed_issues:
        return 0
    with _lock:
        data = _load()
        entries = data["entries"]
        n = 0
        for issue in fixed_issues:
            sig = signature(issue)
            entry = entries.get(sig) or {"hits": 0, "msg": str(issue.get("msg") or "")[:200]}
            entry["hits"] = int(entry.get("hits", 0)) + 1
            entry["fix"] = summary
            entry["last_seen"] = time.strftime("%Y-%m-%d")
            entries[sig] = entry
            n += 1
        if len(entries) > _KB_MAX_ENTRIES:
            ranked = sorted(
                entries.items(),
                key=lambda kv: (kv[1].get("hits", 0), kv[1].get("last_seen", "")),
            )
            for sig, _ in ranked[: len(entries) - _KB_MAX_ENTRIES]:
                entries.pop(sig, None)
        try:
            _save(data)
        except OSError:
            pass  # 磁盘故障不阻断对话；内存态仍有效，重启后从文件态重建
        return n


def _sig_tokens(sig: str) -> set[str]:
    """签名 msg 部分的比较词元：英文词 + 占位符 + 单个汉字。"""
    msg = sig.split("::", 1)[-1]
    return set(re.findall(r"[a-z]{2,}|<\w+>|[一-鿿]", msg))


def _find_entry(entries: dict, issue: dict) -> tuple[dict | None, bool]:
    """先精确签名，miss 时在同 id 前缀里按词元 Jaccard ≥0.6 找最相似的（近似命中）。"""
    sig = signature(issue)
    exact = entries.get(sig)
    if exact:
        return exact, True
    prefix = sig.split("::", 1)[0] + "::"
    my_tokens = _sig_tokens(sig)
    if not my_tokens:
        return None, False
    best, best_score = None, 0.0
    for other_sig, entry in entries.items():
        if not other_sig.startswith(prefix):
            continue
        other = _sig_tokens(other_sig)
        if not other:
            continue
        score = len(my_tokens & other) / len(my_tokens | other)
        if score > best_score:
            best, best_score = entry, score
    if best is not None and best_score >= 0.6:
        return best, False
    return None, False


def hints_for(issues: list[dict]) -> str:
    """为修复 prompt 生成历史经验附言（精确命中优先，同类相似问题兜底）；无命中返回空串。"""
    try:
        entries = _load()["entries"]
        lines = []
        for issue in issues:
            entry, exact = _find_entry(entries, issue)
            if entry and entry.get("fix"):
                label = "历史修复成功" if exact else "相似问题曾修复成功"
                lines.append(
                    f"- 「{str(issue.get('msg') or '')[:80]}」{label} {entry.get('hits', 1)} 次，"
                    f"已验证做法：{entry['fix']}"
                )
        if not lines:
            return ""
        return (
            "\n\n【修复知识库·同类问题的历史已验证做法（优先采用，避免重新试错；"
            "但仍须先在当前代码里核实根因一致）】\n" + "\n".join(lines)
        )
    except Exception:
        return ""  # 知识库任何异常都不能影响修复流程


def reset_cache_for_tests() -> None:
    global _cache
    with _lock:
        _cache = None
