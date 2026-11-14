from __future__ import annotations

import logging
import inspect
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from ...commands import match_command


LOGGER = logging.getLogger('social_bot.modules.text_reply.module')


@dataclass(frozen=True)
class TextReplyOutcome:
    """一次文字回复处理的结果。"""

    handled: bool
    sent: bool = False
    reply: str = ""
    reason: str = ""
    response: Any = None


def _is_failed_response(result: Any) -> bool:
    """兼容 wxauto4 返回 Chat 对象、None 和 WxResponse 三种结果。"""
    return isinstance(result, dict) and result.get("status") not in (None, "成功")


class TextReplyModule:
    """把统一消息交给 BotService，并将文字回复发回原聊天窗口。

    ``normalize`` 和 ``send_reply`` 都通过依赖注入传入，模块本身不依赖微信适配器，
    因此文字回复可以单独测试，也可以切换成引用发送。
    """

    def __init__(
        self,
        service: Any,
        bot_name: str,
        normalize: Callable[..., dict | None],
        accept_self_if: Callable[[str], bool] | None,
        sent_replies: Any,
        send_reply: Callable[[Any, Any, str], Any] | None = None,
        tool_executor_factory: Callable[[Any, Any], Any] | None = None,
    ) -> None:
        self.service = service
        self.bot_name = bot_name
        self.normalize = normalize
        self.accept_self_if = accept_self_if
        self.sent_replies = sent_replies
        self.send_reply = send_reply or self._send_as_text
        self.tool_executor_factory = tool_executor_factory

    @staticmethod
    def _send_as_text(message: Any, chat: Any, reply: str) -> Any:
        del message
        return chat.SendMsg(reply)

    def handle(self, raw_message: Any, chat: Any) -> TextReplyOutcome:
        """处理一条 wxauto 消息，不让业务异常逃逸到监听线程。"""
        message = self.normalize(
            raw_message,
            chat,
            self.bot_name,
            accept_self_if=self.accept_self_if,
        )
        if message is None:
            return TextReplyOutcome(handled=False, reason="ignored")

        LOGGER.info(
            "收到微信消息 chat=%s type=%s sender=%s text=%r",
            message["chat_id"],
            message["chat_type"],
            message["sender_id"],
            message["text"][:40],
        )
        tool_executor = None
        try:
            if self.tool_executor_factory is None:
                result = self.service.handle(message)
            else:
                tool_executor = self.tool_executor_factory(raw_message, chat)
                try:
                    parameters = inspect.signature(self.service.handle).parameters.values()
                    accepts_tools = any(
                        parameter.name == "tool_executor"
                        or parameter.kind is inspect.Parameter.VAR_KEYWORD
                        for parameter in parameters
                    )
                except (TypeError, ValueError):
                    # 某些代理对象没有可 introspect 的签名，按新接口尝试。
                    accepts_tools = True
                result = (
                    self.service.handle(message, tool_executor=tool_executor)
                    if accepts_tools
                    else self.service.handle(message)
                )
        except Exception:  # noqa: BLE001 - 监听线程必须保持存活
            LOGGER.exception("处理微信消息失败 chat=%s", message["chat_id"])
            return TextReplyOutcome(handled=True, reason="service_error")

        if not result.get("accepted") or not result.get("reply"):
            reason = str(result.get("reason") or "not_accepted")
            command = match_command(message["text"])
            if command is not None and not result.get("accepted"):
                hint = {
                    "group_mention_required": f"请在当前群中 @ {self.bot_name or '机器人'} 后重发指令",
                    "group_not_active": "请先使用配置的接通控制词激活当前群",
                    "group_not_allowed": "当前群不在白名单中",
                    "cooldown": "当前会话仍在冷却，请稍后发送新消息",
                    "duplicate_message": "该消息已处理，跳过重复调用",
                }.get(reason, "请根据 reason 排查配置或执行状态")
                log = LOGGER.info if reason == 'duplicate_message' else LOGGER.warning
                log(
                    "微信固定指令未执行 chat=%s tool=%s reason=%s hint=%s",
                    message["chat_id"], command.rule.tool, reason, hint,
                )
            return TextReplyOutcome(
                handled=True,
                reason=reason,
            )

        reply = str(result["reply"])
        jid=result.get('reply_job_id')
        marker=getattr(self.service,'mark_reply_delivery',None)
        def delivered(state,error=''):
            return marker(jid,state,error) if jid and callable(marker) else True
        # 工具已经通过 wxauto 发出引用或完成撤回时，不再把模型的确认文字重复发一遍。
        if result.get("tool_already_sent"):
            delivered('sent')
            return TextReplyOutcome(
                handled=True,
                sent=True,
                reply=reply,
                reason="tool_action_sent",
            )
        # 先记录再发送，防止 wxauto 把机器人自己的回复回调回来时形成自回环。
        if not delivered('sending'):
            return TextReplyOutcome(handled=True,reply=reply,reason='delivery_already_claimed')
        self.sent_replies.append(reply.strip())
        try:
            delivery=result.get("reply_delivery")
            if delivery:
                commit=getattr(tool_executor,"commit_reply",None)
                if not callable(commit):raise RuntimeError("deferred reply has no delivery adapter")
                sent=commit(reply,delivery)
            else:
                sent = self.send_reply(raw_message, chat, reply)
        except Exception:  # noqa: BLE001 - UI 自动化失败不能杀掉监听线程
            delivered('unknown','send raised; delivery not confirmed')
            LOGGER.exception("微信回复发送失败 chat=%s", message["chat_id"])
            return TextReplyOutcome(handled=True, reply=reply, reason="send_error")

        if _is_failed_response(sent):
            delivered('failed',sent.get('message','send failed'))
            LOGGER.error(
                "微信回复未送达 chat=%s：%s",
                message["chat_id"],
                sent.get("message"),
            )
            return TextReplyOutcome(
                handled=True,
                reply=reply,
                reason="send_failed",
                response=sent,
            )

        delivered('sent')
        return TextReplyOutcome(
            handled=True,
            sent=True,
            reply=reply,
            response=sent,
        )
