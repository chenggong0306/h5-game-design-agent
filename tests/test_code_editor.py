import unittest

from src.agent.code_editor import CodeEditor


class CodeEditorTests(unittest.TestCase):
    def test_view_lines_returns_numbered_range(self):
        code = "a\nb\nc"

        result = CodeEditor.view_lines(code, 2, 3)

        self.assertIs(result["success"], True)
        self.assertEqual(result["total_lines"], 3)
        self.assertEqual(result["range"], [2, 3])
        self.assertIn("   2 | b", result["content"])
        self.assertIn("   3 | c", result["content"])

    def test_delete_lines_removes_range(self):
        code = "a\nb\nc"

        result = CodeEditor.delete_lines(code, 2, 2)

        self.assertIs(result["success"], True)
        self.assertEqual(result["code"], "a\nc")
        self.assertEqual(result["deleted_content"], "b")

    def test_delete_lines_rejects_invalid_range(self):
        code = "a\nb"

        result = CodeEditor.delete_lines(code, 2, 3)

        self.assertIs(result["success"], False)
        self.assertEqual(result["code"], code)


if __name__ == "__main__":
    unittest.main()
