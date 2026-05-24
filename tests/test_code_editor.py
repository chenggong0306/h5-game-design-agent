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

    def test_replace_lines_replaces_requested_range(self):
        code = "const speed = 100;\nconst color = 'red';\ndraw();"

        result = CodeEditor.replace_lines(code, 1, 2, "const speed = 200;")

        self.assertIs(result["success"], True)
        self.assertEqual(result["code"], "const speed = 200;\ndraw();")
        self.assertEqual(result["lines_removed"], 2)
        self.assertEqual(result["lines_added"], 1)

    def test_replace_lines_can_delete_range(self):
        code = "a\nb\nc"

        result = CodeEditor.replace_lines(code, 2, 2, "")

        self.assertIs(result["success"], True)
        self.assertEqual(result["code"], "a\nc")

    def test_replace_lines_rejects_invalid_range(self):
        code = "a\nb"

        result = CodeEditor.replace_lines(code, 2, 3, "x")

        self.assertIs(result["success"], False)
        self.assertEqual(result["code"], code)


if __name__ == "__main__":
    unittest.main()
