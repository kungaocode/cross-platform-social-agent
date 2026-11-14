from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from social_bot.modules.group import GroupModule, is_group_chat
from social_bot.modules.media import MediaModule
from social_bot.tools import WeChatToolExecutor


class MediaModuleTests(unittest.TestCase):
    def test_sends_existing_image_through_current_chat(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "sticker.png"
            path.write_bytes(b"fake image")
            chat = mock.Mock()
            chat.SendFiles.return_value = {"status": "成功"}

            result = MediaModule().send_image(chat, str(path))

            self.assertTrue(result.success)
            chat.SendFiles.assert_called_once_with(str(path.resolve()))

    def test_rejects_non_image_before_ui_call(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "notes.txt"
            path.write_text("not an image", encoding="utf-8")
            chat = mock.Mock()

            with self.assertRaises(ValueError):
                MediaModule().send_image(chat, str(path))
            chat.SendFiles.assert_not_called()


class GroupModuleTests(unittest.TestCase):
    def test_unknown_chat_object_is_not_assumed_to_be_group(self) -> None:
        self.assertFalse(is_group_chat(mock.Mock()))

    def test_updates_supported_group_fields(self) -> None:
        chat = mock.Mock(chat_type="group")
        module = GroupModule()

        self.assertTrue(module.set_announcement(chat, "今晚八点开会").success)
        self.assertTrue(module.set_my_nickname(chat, "大肥鱼").success)
        self.assertTrue(module.set_remark(chat, "项目群").success)
        chat.SetGroupAnnouncement.assert_called_once_with("今晚八点开会")
        chat.SetGroupMyNickname.assert_called_once_with("大肥鱼")
        chat.SetGroupRemark.assert_called_once_with("项目群")


class ToolCapabilityTests(unittest.TestCase):
    def test_media_and_group_tools_require_explicit_command(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "hello.jpg"
            path.write_bytes(b"fake image")
            chat = mock.Mock(chat_type="group")
            chat.SendFiles.return_value = {"status": "成功"}
            no_command = SimpleNamespace(content="这张图是什么")
            executor = WeChatToolExecutor(
                mock.Mock(), no_command, chat, allow_media=True, allow_group_tools=True
            )
            names = {item["function"]["name"] for item in executor.definitions}
            self.assertIn("wechat_send_image", names)
            self.assertIn("wechat_send_sticker", names)
            self.assertIn("wechat_set_group_announcement", names)
            self.assertFalse(
                executor.execute(
                    "wechat_send_image", {"path": str(path), "confirm": True}
                )["success"]
            )
            chat.SendFiles.assert_not_called()

    def test_explicit_commands_execute_media_and_group_actions(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "hello.jpg"
            path.write_bytes(b"fake image")
            chat = mock.Mock(chat_type="group")
            chat.SendFiles.return_value = {"status": "成功"}
            message = SimpleNamespace(content="请发送图片给我")
            executor = WeChatToolExecutor(
                mock.Mock(), message, chat, allow_media=True, allow_group_tools=True
            )

            sent = executor.execute("wechat_send_image", {"path": str(path), "confirm": True})

            self.assertTrue(sent["success"])
            self.assertTrue(sent["side_effect_performed"])
            announcement = SimpleNamespace(content="请把群公告改成：今晚八点开会")
            executor.current_message = announcement
            updated = executor.execute(
                "wechat_set_group_announcement", {"text": "今晚八点开会", "confirm": True}
            )
            self.assertTrue(updated["success"])
            chat.SetGroupAnnouncement.assert_called_once_with("今晚八点开会")

            executor.current_message = SimpleNamespace(content="修改我在群聊里的名称为大肥鱼")
            nickname = executor.execute(
                "wechat_set_group_nickname", {"nickname": "大肥鱼", "confirm": True}
            )
            self.assertTrue(nickname["success"])
            chat.SetGroupMyNickname.assert_called_once_with("大肥鱼")


if __name__ == "__main__":
    unittest.main()
