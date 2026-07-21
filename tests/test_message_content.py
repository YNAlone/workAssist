import json
import unittest
from unittest.mock import MagicMock

from feishu_claude_automation.message_content import (
    enrich_message_for_llm,
    extract_doc_token,
    parse_feishu_event_message,
)


class MessageContentTests(unittest.TestCase):
    def test_text_message(self) -> None:
        payload = {
            "event": {
                "message": {
                    "message_id": "m1",
                    "message_type": "text",
                    "content": json.dumps({"text": "hello https://example.com/x"}, ensure_ascii=False),
                }
            }
        }
        parsed = parse_feishu_event_message(payload)
        self.assertEqual(parsed.plain_text(), "hello https://example.com/x")
        kinds = [p.kind for p in parsed.parts]
        self.assertIn("text", kinds)
        self.assertIn("link", kinds)
        envelope = parsed.to_llm_text()
        self.assertIn("kind=text", envelope)
        self.assertIn("kind=link", envelope)

    def test_post_with_link_and_image(self) -> None:
        content = {
            "zh_cn": {
                "title": "标题",
                "content": [
                    [
                        {"tag": "text", "text": "见文档 "},
                        {
                            "tag": "a",
                            "href": "https://foo.feishu.cn/docx/Abc123Token",
                            "text": "方案",
                        },
                    ],
                    [{"tag": "img", "image_key": "img_abc"}],
                ],
            }
        }
        payload = {
            "event": {
                "message": {
                    "message_id": "m2",
                    "message_type": "post",
                    "content": json.dumps(content, ensure_ascii=False),
                }
            }
        }
        parsed = parse_feishu_event_message(payload)
        kinds = [p.kind for p in parsed.parts]
        self.assertIn("text", kinds)
        self.assertIn("doc", kinds)
        self.assertIn("image", kinds)
        doc = next(p for p in parsed.parts if p.kind == "doc")
        self.assertEqual(extract_doc_token(doc.url), ("docx", "Abc123Token"))

    def test_enrich_fetches_doc_and_image(self) -> None:
        content = {
            "zh_cn": {
                "content": [
                    [
                        {
                            "tag": "a",
                            "href": "https://foo.feishu.cn/docx/DocToken1",
                            "text": "文档",
                        }
                    ],
                    [{"tag": "img", "image_key": "img_1"}],
                ]
            }
        }
        payload = {
            "event": {
                "message": {
                    "message_id": "om_1",
                    "message_type": "post",
                    "content": json.dumps(content, ensure_ascii=False),
                }
            }
        }
        parsed = parse_feishu_event_message(payload)
        client = MagicMock()
        client.settings.dry_run = False
        client.download_message_resource.return_value = (b"\x89PNG", "image/png")
        client.fetch_docx_raw_content.return_value = "文档正文ABC"
        enrich_message_for_llm(parsed, client)
        doc = next(p for p in parsed.parts if p.kind == "doc")
        image = next(p for p in parsed.parts if p.kind == "image")
        self.assertEqual(doc.meta.get("body"), "文档正文ABC")
        self.assertTrue(image.data_base64)
        content_blocks = parsed.to_llm_content()
        self.assertIsInstance(content_blocks, list)
        self.assertEqual(content_blocks[1]["type"], "image_url")
        self.assertIn("document body", parsed.to_llm_text())


if __name__ == "__main__":
    unittest.main()
