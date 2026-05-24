"""代码编辑工具 - 支持对代码进行精确的增删改查操作"""


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
        """替换代码中的指定片段

        Args:
            code: 完整代码
            old_str: 要替换的原始代码片段
            new_str: 替换后的新代码片段（空字符串=删除）
        Returns:
            {"success": True/False, "code": "修改后代码", "message": "..."}
        """
        if not old_str:
            return {"success": False, "code": code, "message": "old_str 不能为空"}

        count = code.count(old_str)
        if count == 0:
            # 尝试忽略首尾空白匹配
            stripped = old_str.strip()
            lines = code.split('\n')
            found = False
            for i, line in enumerate(lines):
                if stripped in line.strip():
                    found = True
                    break
            hint = f"（但找到了类似内容在第 {i+1} 行）" if found else ""
            return {
                "success": False,
                "code": code,
                "message": f"未找到要替换的代码片段{hint}",
            }
        if count > 1:
            return {
                "success": False,
                "code": code,
                "message": f"找到 {count} 处匹配，请提供更精确的代码片段以避免歧义",
            }

        new_code = code.replace(old_str, new_str, 1)

        # 计算变更的行号
        before_lines = code[:code.index(old_str)].count('\n') + 1
        old_line_count = old_str.count('\n') + 1
        new_line_count = new_str.count('\n') + 1 if new_str else 0

        return {
            "success": True,
            "code": new_code,
            "message": f"已替换第 {before_lines}-{before_lines + old_line_count - 1} 行"
                       f"（{old_line_count} 行 → {new_line_count} 行）",
            "line_start": before_lines,
            "lines_removed": old_line_count,
            "lines_added": new_line_count,
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
