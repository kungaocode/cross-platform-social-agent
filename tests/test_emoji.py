import unittest

from social_bot.modules.emoji import DEFAULT_STICKERS, EmojiModule


class EmojiModuleTests(unittest.TestCase):
    def test_prompt_explains_emotion_and_sticker_usage(self) -> None:
        prompt = EmojiModule(max_count=2).prompt_instruction()

        self.assertIn("情绪或小心思", prompt)
        self.assertIn("[微笑]", prompt)
        self.assertIn("不要为了凑数量", prompt)

    def test_default_stickers_are_detectable(self) -> None:
        module = EmojiModule()

        self.assertIn("[太阳]", DEFAULT_STICKERS)
        self.assertTrue(module.contains_sticker("早安[太阳]"))
        self.assertFalse(module.contains_sticker("早安☀️"))

    def test_disabled_prompt_forbids_both_forms(self) -> None:
        prompt = EmojiModule(enabled=False).prompt_instruction()

        self.assertIn("不要主动添加 emoji", prompt)
        self.assertIn("微信原生表情", prompt)


if __name__ == "__main__":
    unittest.main()
