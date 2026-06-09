import unittest

import src.agent.game_agent as game_agent
import src.utils.persistence as persistence


# 所有测试用到的会话 id，setUp/tearDown 统一清理（内存 + 磁盘），保证用例间隔离
_TEST_SESSIONS = [
    "session-a", "session-b", "view-session", "write-once", "write-chunked",
    "write-incomplete", "append-empty", "replace-empty", "doctype-frag",
    "autocommit", "autocommit-bad", "reconcile", "reconcile2", "resurrect",
    "resolve", "resolve-empty", "append-recover", "append-rewrite", "autoclose-script",
]


class AgentCodeSessionTests(unittest.TestCase):
    """覆盖根因2（工具面）与根因4（唯一真相源 / 协调 / 原子清理）。"""

    def _clean(self):
        for sid in _TEST_SESSIONS:
            game_agent._code_by_session.pop(sid, None)
            game_agent._staging_by_session.pop(sid, None)
            game_agent._code_session_last_access.pop(sid, None)
            try:
                persistence.delete_session_code(sid)
            except Exception:
                pass

    setUp = _clean
    tearDown = _clean

    # ---------- 根因2：工具面 ----------

    def test_edit_tools_are_isolated_by_context_session(self):
        token_a = game_agent._begin_code_session("session-a", "let speed = 100;\ndrawA();")
        try:
            message_a = game_agent.replace_code.invoke({
                "old_str": "let speed = 100;",
                "new_str": "let speed = 200;",
            })
            self.assertIn("已替换", message_a)
            self.assertEqual(game_agent._get_current_code(), "let speed = 200;\ndrawA();")

            token_b = game_agent._begin_code_session("session-b", "let speed = 50;\ndrawB();")
            try:
                message_b = game_agent.delete_code.invoke({"start_line": 2, "end_line": 2})
                self.assertIn("已删除", message_b)
                self.assertEqual(game_agent._get_current_code(), "let speed = 50;")
            finally:
                game_agent._end_code_session(token_b)

            self.assertEqual(game_agent._get_current_code(), "let speed = 200;\ndrawA();")
        finally:
            game_agent._end_code_session(token_a)

    def test_view_code_reads_current_session_code(self):
        token = game_agent._begin_code_session("view-session", "a\nb\nc")
        try:
            content = game_agent.view_code.invoke({"start_line": 2, "end_line": 2})
            self.assertIn("共 3 行", content)
            self.assertIn("   2 | b", content)
            self.assertNotIn("   1 | a", content)
        finally:
            game_agent._end_code_session(token)

    def test_write_game_single_call_commits(self):
        token = game_agent._begin_code_session("write-once", "")
        try:
            html = "<!DOCTYPE html>\n<html><body><canvas></canvas></body></html>"
            message = game_agent.write_game.invoke({"html": html})
            self.assertIn("已写入并生效", message)
            self.assertEqual(game_agent._get_current_code(), html)
        finally:
            game_agent._end_code_session(token)

    def test_chunked_write_stages_until_final_segment(self):
        token = game_agent._begin_code_session("write-chunked", "OLD GAME")
        try:
            seg1 = "<!DOCTYPE html>\n<html><head><style>x</style></head>"
            msg1 = game_agent.write_game.invoke({"html": seg1, "more": True})
            self.assertIn("暂存", msg1)
            self.assertEqual(game_agent._get_current_code(), "OLD GAME")  # 暂存期间不动生效代码

            seg2 = "<body><script>game()</script></body></html>"
            msg2 = game_agent.append_game.invoke({"html": seg2, "more": False})
            self.assertIn("已写入并生效", msg2)
            self.assertEqual(game_agent._get_current_code(), seg1 + seg2)
        finally:
            game_agent._end_code_session(token)

    def test_incomplete_segment_stages_without_error_and_keeps_old_code(self):
        # 内容驱动：未出现 </html> → 暂存并提示继续（非错误），旧代码不动
        token = game_agent._begin_code_session("write-incomplete", "OLD GAME")
        try:
            msg = game_agent.write_game.invoke({"html": "<!DOCTYPE html>\n<html><body>"})
            self.assertNotIn("[ERROR", msg)            # 不再标记为失败
            self.assertIn("尚未闭合", msg)
            self.assertEqual(game_agent._get_current_code(), "OLD GAME")
            # 续写补上 </html> → 自动生效（无需 more 标志）
            msg2 = game_agent.append_game.invoke({"html": "<canvas></canvas><script>1</script></body></html>"})
            self.assertIn("已写入并生效", msg2)
            self.assertIn("</html>", game_agent._get_current_code())
        finally:
            game_agent._end_code_session(token)

    def test_staging_survives_across_turns_for_continuation(self):
        # 回归：分段写入跨回合时，暂存不应在新回合开始被清空（否则 append 报 APPEND_TO_EMPTY）
        t1 = game_agent._begin_code_session("write-chunked", "OLD GAME")
        game_agent.write_game.invoke({"html": "<!DOCTYPE html>\n<html><head><style>x</style></head>"})
        game_agent._autocommit_staging("write-chunked")          # 回合1结束：未闭合 → 保留暂存
        self.assertEqual(game_agent._get_current_code(), "OLD GAME")
        game_agent._end_code_session(t1)
        # 回合2：新回合开始不清空暂存，append 续写补全 → 自动生效
        t2 = game_agent._begin_code_session("write-chunked", "OLD GAME")
        try:
            msg = game_agent.append_game.invoke({"html": "<body><script>1</script></body></html>"})
            self.assertIn("已写入并生效", msg)
            self.assertIn("</html>", game_agent._get_current_code())
        finally:
            game_agent._end_code_session(t2)

    def test_autocommit_salvages_truncated_game_with_script(self):
        # 末段被截断（有文档头+脚本但缺 </html>）→ 回合结束兜底补全闭合并提交，不丢失整局
        token = game_agent._begin_code_session("autocommit", "OLD")
        try:
            game_agent.write_game.invoke({"html": "<!DOCTYPE html><html><body><canvas></canvas><script>game()</script>"})
            self.assertEqual(game_agent._get_current_code(), "OLD")  # 未闭合，暂存未提交
            game_agent._autocommit_staging("autocommit")
            code = game_agent._get_current_code()
            self.assertIn("</html>", code)
            self.assertIn("game()", code)
        finally:
            game_agent._end_code_session(token)

    def test_append_without_start_returns_actionable_error(self):
        token = game_agent._begin_code_session("append-empty", "OLD GAME")
        try:
            msg = game_agent.append_game.invoke({"html": "<x>", "more": False})
            self.assertIn("先调用 write_game", msg)
            self.assertEqual(game_agent._get_current_code(), "OLD GAME")
        finally:
            game_agent._end_code_session(token)

    def test_autocommit_closes_unclosed_script_before_body_html(self):
        # 末段截断在未闭合 <script> 里 → 兜底补全必须先补 </script> 再补 </body></html>，
        # 否则补出来的闭合标签落在 script 内被当 JS、整页解析坏掉。
        token = game_agent._begin_code_session("autoclose-script", "OLD")
        try:
            game_agent.write_game.invoke({"html":
                "<!DOCTYPE html><html><body><canvas></canvas><script>let x=1; // 被截断"})
            self.assertEqual(game_agent._get_current_code(), "OLD")  # 未闭合，未提交
            game_agent._autocommit_staging("autoclose-script")
            code = game_agent._get_current_code()
            low = code.lower()
            self.assertEqual(low.count("<script"), low.count("</script>"))  # script 配平
            self.assertIn("</html>", low)
            self.assertLess(low.index("</script>"), low.index("</body>"))   # 顺序正确
        finally:
            game_agent._end_code_session(token)

    def test_append_recovers_from_committed_complete_doc(self):
        # 自愈：模型把第1段写成完整文档（已提交、暂存清空），随后 append 续写不应报错，
        # 而是从已生效代码去掉结尾闭合标签后接着写，写到 </html> 整份生效。
        token = game_agent._begin_code_session("append-recover", "")
        try:
            doc = "<!DOCTYPE html>\n<html><body><canvas></canvas></body></html>"
            game_agent.write_game.invoke({"html": doc})           # 完整文档 → 立即提交
            self.assertEqual(game_agent._get_current_code(), doc)
            msg = game_agent.append_game.invoke({"html": "<script>game()</script></body></html>"})
            self.assertNotIn("[ERROR", msg)
            self.assertIn("已写入并生效", msg)
            code = game_agent._get_current_code()
            self.assertIn("<script>game()</script>", code)
            self.assertIn("<canvas></canvas>", code)              # 旧正文保留
            self.assertEqual(code.lower().count("</html>"), 1)    # 只有一个闭合标签
        finally:
            game_agent._end_code_session(token)

    def test_append_full_doc_after_commit_is_rewrite(self):
        # 自愈分支2：append 的内容本身就是一份完整新文档 → 当作整体重写
        token = game_agent._begin_code_session("append-rewrite", "")
        try:
            game_agent.write_game.invoke({"html": "<!DOCTYPE html><html><body>OLD</body></html>"})
            new_doc = "<!DOCTYPE html><html><body>NEW GAME</body></html>"
            msg = game_agent.append_game.invoke({"html": new_doc})
            self.assertIn("已写入并生效", msg)
            self.assertEqual(game_agent._get_current_code(), new_doc)
        finally:
            game_agent._end_code_session(token)

    def test_replace_code_requires_old_str(self):
        token = game_agent._begin_code_session("replace-empty", "old code")
        try:
            msg = game_agent.replace_code.invoke({"old_str": "", "new_str": "x"})
            self.assertIn("old_str 不能为空", msg)
            self.assertEqual(game_agent._get_current_code(), "old code")
        finally:
            game_agent._end_code_session(token)

    def test_replace_code_with_doctype_fragment_does_not_wipe_file(self):
        token = game_agent._begin_code_session("doctype-frag", "<!DOCTYPE html><html><body>OLD FULL GAME</body></html>")
        try:
            game_agent.replace_code.invoke({"old_str": "OLD FULL GAME", "new_str": "<!DOCTYPE new fragment"})
            code = game_agent._get_current_code()
            self.assertIn("<html>", code)
            self.assertIn("<body>", code)
            self.assertIn("<!DOCTYPE new fragment", code)
        finally:
            game_agent._end_code_session(token)

    def test_complete_doc_commits_immediately(self):
        # 内容驱动：出现 </html> 立即生效，不依赖 more 标志（即使误传 more=True）
        token = game_agent._begin_code_session("autocommit", "OLD")
        try:
            msg = game_agent.write_game.invoke({"html": "<!DOCTYPE html><html></html>", "more": True})
            self.assertIn("已写入并生效", msg)
            self.assertEqual(game_agent._get_current_code(), "<!DOCTYPE html><html></html>")
        finally:
            game_agent._end_code_session(token)

    def test_autocommit_discards_incomplete_doc(self):
        token = game_agent._begin_code_session("autocommit-bad", "OLD GAME")
        try:
            game_agent.write_game.invoke({"html": "<!DOCTYPE html><html><body>", "more": True})
            game_agent._autocommit_staging("autocommit-bad")
            self.assertEqual(game_agent._get_current_code(), "OLD GAME")  # 不完整 → 丢弃，保留旧代码
        finally:
            game_agent._end_code_session(token)

    # ---------- 根因4：唯一真相源 / 协调 / 原子清理 ----------

    def test_stale_client_code_does_not_clobber_server(self):
        # 服务端已有权威代码；下一回合前端发来不一致的旧代码但未声明手改 → 保留服务端
        t1 = game_agent._begin_code_session("reconcile", "SERVER AUTH CODE", client_dirty=True)
        game_agent._end_code_session(t1)
        t2 = game_agent._begin_code_session("reconcile", "STALE CLIENT CODE", client_dirty=False)
        try:
            self.assertEqual(game_agent._get_current_code(), "SERVER AUTH CODE")
        finally:
            game_agent._end_code_session(t2)

    def test_dirty_client_code_is_adopted(self):
        t1 = game_agent._begin_code_session("reconcile2", "SERVER AUTH CODE", client_dirty=True)
        game_agent._end_code_session(t1)
        t2 = game_agent._begin_code_session("reconcile2", "USER EDITED CODE", client_dirty=True)
        try:
            self.assertEqual(game_agent._get_current_code(), "USER EDITED CODE")
        finally:
            game_agent._end_code_session(t2)

    def test_no_resurrection_after_disk_delete(self):
        # 模拟 clear_session：删内存 + 删磁盘后，空 current_code 的新会话不应复活旧代码
        t = game_agent._begin_code_session("resurrect", "OLD GAME", client_dirty=True)
        game_agent._end_code_session(t)
        self.assertEqual(persistence.load_session_code("resurrect"), "OLD GAME")
        game_agent._code_by_session.pop("resurrect", None)
        persistence.delete_session_code("resurrect")
        t2 = game_agent._begin_code_session("resurrect", "", client_dirty=False)
        try:
            self.assertEqual(game_agent._get_current_code(), "")
        finally:
            game_agent._end_code_session(t2)

    def test_resolve_final_code_does_not_scrape_when_buffer_present(self):
        token = game_agent._begin_code_session("resolve", "<html>EXISTING</html>", client_dirty=True)
        try:
            reply = "顺便给你看段示例\n```html\n<!DOCTYPE html><html>SNIPPET</html>\n```"
            code = game_agent.GameDesignAgent._resolve_final_code("<html>EXISTING</html>", reply)
            self.assertEqual(code, "<html>EXISTING</html>")  # 返回缓冲区真值
            self.assertNotIn("SNIPPET", code)               # 绝不被聊天片段覆盖
        finally:
            game_agent._end_code_session(token)

    def test_resolve_final_code_rescues_full_doc_on_empty_session(self):
        token = game_agent._begin_code_session("resolve-empty", "", client_dirty=False)
        try:
            reply = "```html\n<!DOCTYPE html><html>FULL</html>\n```"
            code = game_agent.GameDesignAgent._resolve_final_code("", reply)
            self.assertIn("FULL", code)
        finally:
            game_agent._end_code_session(token)

    def test_extract_code_requires_complete_document(self):
        partial = "这是说明\n```html\n<canvas id='c'></canvas>\n```\n"
        self.assertIsNone(game_agent.GameDesignAgent._extract_code(partial))
        complete = "好的\n```html\n<!DOCTYPE html><html><body></body></html>\n```\n"
        self.assertIn("</html>", game_agent.GameDesignAgent._extract_code(complete) or "")


if __name__ == "__main__":
    unittest.main()
