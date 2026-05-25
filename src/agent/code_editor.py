"""代码编辑工具 - 支持对代码进行精确的增删改查操作"""


def _normalize_line_endings(text: str) -> str:
    return text.replace('\r\n', '\n')


class CodeEditor:
    """代码编辑器工具，提供 str_replace / insert / delete / view 操作"""

    @staticmethod
    def view_lines(code: str, start: int = 1, end: int = -1) -> dict:
        """查看指定行范围的代码

        Args:
            code: 完整代码
            start: 起始行号（1-based）
            end: 结束行号（-1 表示到末尾）
        Returns:
            {"success": True, "content": "带行号的代码", "total_lines": int}
        """
        lines = code.split('\n')
        total = len(lines)
        if end == -1:
            end = total
        start = max(1, min(start, total))
        end = max(start, min(end, total))

        numbered = []
        for i in range(start - 1, end):
            numbered.append(f"{i + 1:4d} | {lines[i]}")

        return {
            "success": True,
            "content": '\n'.join(numbered),
            "total_lines": total,
            "range": [start, end],
        }

    @staticmethod
    def str_replace(code: str, old_str: str, new_str: str) -> dict:
        """替换代码中的指定片段。

        移植自 MCP filesystem server 的 applyFileEdits 逻辑：
        1. 先精确字符串匹配
        2. 失败后逐行 trim 对比（忽略缩进差异）
        3. 匹配成功时保留原始缩进

        Args:
            code: 完整代码
            old_str: 要替换的原始代码片段
            new_str: 替换后的新代码片段（空字符串=删除）
        Returns:
            {"success": True/False, "code": "修改后代码", "message": "..."}
        """
        if not old_str:
            return {"success": False, "code": code, "message": "old_str 不能为空"}

        content = _normalize_line_endings(code)
        normalized_old = _normalize_line_endings(old_str)
        normalized_new = _normalize_line_endings(new_str)

        # 1. 先尝试精确匹配
        count = content.count(normalized_old)
        if count > 1:
            return {
                "success": False,
                "code": code,
                "message": f"找到 {count} 处匹配，请提供更精确的代码片段以避免歧义",
            }
        if count == 1:
            new_code = content.replace(normalized_old, normalized_new, 1)
            before_lines = content[:content.index(normalized_old)].count('\n') + 1
            old_line_count = normalized_old.count('\n') + 1
            new_line_count = normalized_new.count('\n') + 1 if normalized_new else 0
            return {
                "success": True,
                "code": new_code,
                "message": f"已替换第 {before_lines}-{before_lines + old_line_count - 1} 行"
                           f"（{old_line_count} 行 → {new_line_count} 行）",
                "line_start": before_lines,
                "lines_removed": old_line_count,
                "lines_added": new_line_count,
            }

        # 2. 精确匹配失败，逐行 trim 对比（忽略空白差异）
        old_lines = normalized_old.split('\n')
        content_lines = content.split('\n')

        for i in range(len(content_lines) - len(old_lines) + 1):
            potential = content_lines[i:i + len(old_lines)]
            if all(o.strip() == c.strip() for o, c in zip(old_lines, potential)):
                # 保留第一行的原始缩进
                original_indent = len(content_lines[i]) - len(content_lines[i].lstrip())
                indent_str = content_lines[i][:original_indent]

                new_lines_raw = normalized_new.split('\n') if normalized_new else []
                adjusted = []
                for j, line in enumerate(new_lines_raw):
                    if j == 0:
                        adjusted.append(indent_str + line.lstrip())
                    else:
                        old_indent = len(old_lines[j].expandtabs()) - len(old_lines[j].lstrip()) if j < len(old_lines) else 0
                        new_indent = len(line.expandtabs()) - len(line.lstrip())
                        rel = new_indent - old_indent
                        adjusted.append(indent_str + ' ' * max(0, rel) + line.lstrip())

                content_lines[i:i + len(old_lines)] = adjusted
                new_code = '\n'.join(content_lines)
                return {
                    "success": True,
                    "code": new_code,
                    "message": f"已替换第 {i + 1}-{i + len(old_lines)} 行（空白归一化匹配）"
                               f"（{len(old_lines)} 行 → {len(adjusted)} 行）",
                    "line_start": i + 1,
                    "lines_removed": len(old_lines),
                    "lines_added": len(adjusted),
                }

        # 3. 都失败，给出提示行号
        hint_line = None
        for i, line in enumerate(content_lines):
            if old_lines[0].strip() and old_lines[0].strip() in line.strip():
                hint_line = i + 1
                break
        hint = f"（但找到了类似内容在第 {hint_line} 行）" if hint_line else ""
        return {
            "success": False,
            "code": code,
            "message": f"未找到要替换的代码片段{hint}",
        }

    @staticmethod
    def insert_after(code: str, after_line: int, new_str: str) -> dict:
        """在指定行之后插入代码

        Args:
            code: 完整代码
            after_line: 在此行之后插入（0=插入到最前面）
            new_str: 要插入的代码
        Returns:
            {"success": True/False, "code": "修改后代码", "message": "..."}
        """
        lines = code.split('\n')
        total = len(lines)

        if after_line < 0 or after_line > total:
            return {
                "success": False,
                "code": code,
                "message": f"行号 {after_line} 超出范围（总共 {total} 行）",
            }

        new_lines = new_str.split('\n')
        lines[after_line:after_line] = new_lines

        return {
            "success": True,
            "code": '\n'.join(lines),
            "message": f"已在第 {after_line} 行后插入 {len(new_lines)} 行",
            "line_start": after_line + 1,
            "lines_added": len(new_lines),
        }


    @staticmethod
    def replace_lines(code: str, start: int, end: int, new_str: str) -> dict:
        """替换指定行范围

        Args:
            code: 完整代码
            start: 起始行号（1-based）
            end: 结束行号（1-based, inclusive）
            new_str: 替换后的代码；空字符串表示删除该范围
        Returns:
            {"success": True/False, "code": "修改后代码", "message": "..."}
        """
        lines = code.split('\n')
        total = len(lines)

        if start < 1 or end > total or start > end:
            return {
                "success": False,
                "code": code,
                "message": f"行号范围 {start}-{end} 无效（总共 {total} 行）",
            }

        new_lines = new_str.split('\n') if new_str else []
        lines[start - 1:end] = new_lines

        return {
            "success": True,
            "code": '\n'.join(lines),
            "message": f"已替换第 {start}-{end} 行"
                       f"（{end - start + 1} 行 → {len(new_lines)} 行）",
            "line_start": start,
            "lines_removed": end - start + 1,
            "lines_added": len(new_lines),
        }

    @staticmethod
    def delete_lines(code: str, start: int, end: int) -> dict:
        """删除指定行范围

        Args:
            code: 完整代码
            start: 起始行号（1-based）
            end: 结束行号（1-based, inclusive）
        Returns:
            {"success": True/False, "code": "修改后代码", "message": "..."}
        """
        lines = code.split('\n')
        total = len(lines)

        if start < 1 or end > total or start > end:
            return {
                "success": False,
                "code": code,
                "message": f"行号范围 {start}-{end} 无效（总共 {total} 行）",
            }

        deleted = lines[start - 1:end]
        del lines[start - 1:end]

        return {
            "success": True,
            "code": '\n'.join(lines),
            "message": f"已删除第 {start}-{end} 行（共 {end - start + 1} 行）",
            "line_start": start,
            "lines_removed": end - start + 1,
            "deleted_content": '\n'.join(deleted),
        }

    @staticmethod
    def search(code: str, query: str) -> dict:
        """搜索代码中包含关键字的行

        Args:
            code: 完整代码
            query: 搜索关键字
        Returns:
            {"success": True, "matches": [{"line": int, "content": str}]}
        """
        lines = code.split('\n')
        matches = []
        query_lower = query.lower()
        for i, line in enumerate(lines):
            if query_lower in line.lower():
                matches.append({"line": i + 1, "content": line.rstrip()})

        return {
            "success": True,
            "matches": matches,
            "message": f"找到 {len(matches)} 处匹配",
        }
