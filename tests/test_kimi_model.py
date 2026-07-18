import unittest

from feishu_claude_automation.config import normalize_kimi_model


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


if __name__ == "__main__":
    unittest.main()
