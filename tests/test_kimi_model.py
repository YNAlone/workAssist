import unittest

from feishu_claude_automation.config import (
    extract_model_from_text,
    normalize_kimi_model,
    try_normalize_kimi_model,
)
from feishu_claude_automation.parser import parse_command


class NormalizeKimiModelTests(unittest.TestCase):
    def test_default_when_empty(self) -> None:
        self.assertEqual(normalize_kimi_model(None), "kimi-for-coding")
        self.assertEqual(normalize_kimi_model(""), "kimi-for-coding")
        self.assertEqual(normalize_kimi_model("  "), "kimi-for-coding")

    def test_kimi_for_coding(self) -> None:
        self.assertEqual(normalize_kimi_model("kimi-for-coding"), "kimi-for-coding")

    def test_k3_aliases(self) -> None:
        self.assertEqual(normalize_kimi_model("k3"), "k3")
        self.assertEqual(normalize_kimi_model("K3"), "k3")
        self.assertEqual(normalize_kimi_model("kimi-k3"), "k3")

    def test_highspeed(self) -> None:
        self.assertEqual(
            normalize_kimi_model("kimi-for-coding-highspeed"),
            "kimi-for-coding-highspeed",
        )

    def test_unsupported(self) -> None:
        with self.assertRaises(ValueError):
            normalize_kimi_model("gpt-4o")
        self.assertIsNone(try_normalize_kimi_model("gpt-4o"))


class ExtractModelFromTextTests(unittest.TestCase):
    def test_phrases(self) -> None:
        self.assertEqual(extract_model_from_text("帮我用 k3 改一下登录页"), "k3")
        self.assertEqual(extract_model_from_text("切换到 kimi-for-coding"), "kimi-for-coding")
        self.assertEqual(extract_model_from_text("模型=kimi-for-coding-highspeed"), "kimi-for-coding-highspeed")
        self.assertEqual(extract_model_from_text("请使用 K3 模型"), "k3")

    def test_no_match(self) -> None:
        self.assertIsNone(extract_model_from_text("帮我在 workAssist 修个 bug"))


class ParseCommandModelTests(unittest.TestCase):
    def test_model_field(self) -> None:
        request = parse_command(
            '/ai-fix repo=acme/demo branch=main model=k3 desc="Fix refund bug"'
        )
        self.assertIsNotNone(request)
        assert request is not None
        self.assertEqual(request.model, "k3")


if __name__ == "__main__":
    unittest.main()
