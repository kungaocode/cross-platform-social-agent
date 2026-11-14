"""QQ 工具入口：共享只读能力和独立的空间发布。"""
from ..common.tools import ReadOnlyChatTools, tool
from .qzone import match_publish_command


class QQToolExecutor(ReadOnlyChatTools):
    def __init__(self, runtime, text, *, publisher=None, event_key="", sender="", kind="c2c",
                 weather=None, research=None, time_module=None, links=None, attachments=None):
        super().__init__(runtime, text, weather=weather, research=research, time_module=time_module, links=links)
        self.publisher, self.event_key, self.sender, self.kind = publisher, event_key, sender, kind
        self.command = match_publish_command(text)
        self.attachments = [] if attachments is None else attachments

    @property
    def preferred_tool(self):
        return "qq_publish_shuoshuo" if self.command and self.publisher and self.publisher.authorized(self.sender, self.kind) else None

    @property
    def definitions(self):
        definitions = super().definitions
        if self.preferred_tool:
            definitions.append(tool("qq_publish_shuoshuo", "根据当前用户明确要求发布 QQ 空间说说。程序自动携带本条消息的全部图片附件，模型不要提供路径或图片地址。复制模式保持原文；生成模式只传最终正文。confirm=true 表示用户已要求发布，无须再问确认。",
                {"text": {"type": "string", "maxLength": 2000}, "confirm": {"type": "boolean"}}, ["text", "confirm"]))
        return definitions

    def execute(self, name, arguments):
        if name != "qq_publish_shuoshuo":
            result = super().execute(name, arguments)
            if result.get("reason") == "chat_tool_unavailable":
                result["reason"] = "qq_tool_unavailable"
            elif result.get("reason") == "chat_tool_error":
                result["reason"] = "qq_tool_error"
            return result
        if not self.preferred_tool:
            return {"success": False, "reason": "qq_tool_unavailable"}
        try:
            return self.publisher.publish(self.event_key, self.sender, self.kind, self.command, arguments,
                                          attachments=self.attachments)
        except Exception as exc:
            return {"success": False, "reason": "qq_tool_error", "error_type": type(exc).__name__}
