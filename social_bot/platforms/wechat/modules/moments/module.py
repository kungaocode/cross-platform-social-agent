from __future__ import annotations

import logging
import re
import time
import inspect
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

try:  # pywin32 随 wxautox4 一起安装；缺了只是不能清理残留对话框
    import win32con
    import win32gui
    import win32process
except Exception:  # noqa: BLE001
    win32con = None  # type: ignore[assignment]
    win32gui = None  # type: ignore[assignment]
    win32process = None  # type: ignore[assignment]

LOGGER = logging.getLogger('social_bot.modules.moments.module')


class MomentsError(RuntimeError):
    pass


def _shorten(value: Any, limit: int = 60) -> str:
    """日志里只保留正文的前若干字符，避免把整条朋友圈刷进日志。"""
    text = str(value or "").replace("\n", "\\n")
    return text if len(text) <= limit else text[:limit] + "…"


@dataclass(frozen=True)
class MomentComment:
    """从 wxauto 朋友圈对象或 UI 控件解析出的评论。"""

    key: str
    content: str
    sender: str = ""


class MomentsModule:
    """封装 wxautox4 的朋友圈窗口和朋友圈内容对象。"""

    # 朋友圈正文在 UIA 里带作者、图片数、地点和相对时间，所以定位用「包含」而不是相等；
    # 刷新后界面重绘需要时间，因此同一轮定位里多试几次。
    LOCATE_RETRIES = 5
    LOCATE_RETRY_DELAY = 0.4
    # 连续多少轮定位失败后重新激活朋友圈窗口（并做冷却，避免每轮都折腾界面）。
    HARD_REFRESH_AFTER = 3
    HARD_REFRESH_COOLDOWN = 600.0
    # 窗口已经关掉/句柄失效时的重开冷却，比上面的冷却短，方便尽快恢复。
    FORCED_REOPEN_COOLDOWN = 60.0
    # 发出评论后确认它可见的总预算（发评论会让 cell 重绘，留一点时间）。
    VERIFY_TIMEOUT_SECONDS = 3.0

    def __init__(
        self,
        client_getter: Callable[[], Any],
        *,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.client_getter = client_getter
        self.sleep = sleep
        self._window: Any = None
        self._last_published_text = ""
        self._last_published_moment: Any = None
        # wxauto exposes a stable runtimeid/runtime_id/id on some releases.
        # Save it after publishing so refreshes cannot switch to a duplicate post.
        self._last_published_identity: str | None = None
        # 只在第 1、2 次和之后每 15 次打印定位失败详情，避免轮询日志被刷爆。
        self._locate_failures = 0
        self._refresh_errors = 0
        self._window_errors = 0
        self._control_misses = 0
        self._comment_scan_failures = 0
        self._last_comment_signature: tuple[str, ...] | None = None
        self._last_hard_refresh_at: float | None = None
        # 本轮读取过程中是否发现窗口句柄失效（已关窗 / UIA 元素过期）。
        self._window_invalidated = False

    def _should_report_failure(self, count: int) -> bool:
        return count <= 2 or count % 15 == 0

    def has_tracked_target(self) -> bool:
        """是否已经定位到本次发布的那条朋友圈。"""
        return self._last_published_moment is not None

    def _moments_window(self) -> Any:
        if self._window is not None:
            return self._window
        client = self.client_getter()
        if client is None:
            raise MomentsError("微信客户端尚未启动")
        moments = getattr(client, "Moments", None)
        if not callable(moments):
            raise MomentsError("当前 wxauto 版本没有 Moments() 接口")
        self._window = moments()
        if self._window is None:
            raise MomentsError("朋友圈窗口不可用，请先在手机端开启朋友圈")
        return self._window

    @staticmethod
    def _validate_media_files(media_files: Iterable[str] | None) -> list[str] | None:
        if media_files is None:
            return None
        if isinstance(media_files, (str, bytes)):
            media_files = [media_files.decode() if isinstance(media_files, bytes) else media_files]
        paths: list[str] = []
        for item in media_files:
            path = Path(str(item)).expanduser()
            if not path.is_file():
                raise MomentsError(f"媒体文件不存在：{path}")
            # wxautox4 的朋友圈文件选择器要求绝对路径；配置里允许写相对
            # 项目目录的路径（例如 picture/2.jpg），这里统一转成绝对路径，
            # 避免它在 WinOpenUI.input_files 中解析相对路径时报 unpack 错误。
            paths.append(str(path.resolve()))
        return paths

    @staticmethod
    def _moment_content(moment: Any) -> str:
        """Return the most useful textual representation of a wxauto Moment.

        On wxautox4 versions that render a media post, ``Moment.content`` can be
        just ``"1.jpg"`` (or a comma separated list of file names).  The text is
        still available on the UI control and/or on a ``text``/``description``
        attribute.  Keeping this fallback in one place makes locating media
        posts and diagnosing them reliable.
        """
        values = MomentsModule._moment_text_values(moment)
        return values[0] if values else ""

    @classmethod
    def _moment_text_values(cls, moment: Any) -> list[str]:
        """Collect textual fields exposed by different wxauto releases."""
        values: list[str] = []
        for name in (
            "text",
            "description",
            "desc",
            "content_text",
            "content",
            "title",
        ):
            try:
                value = getattr(moment, name, "")
            except Exception:  # noqa: BLE001 - stale UIA objects are common
                continue
            value = str(value or "").strip()
            if value and value not in values:
                values.append(value)
        control = getattr(moment, "control", None)
        if control is not None:
            try:
                value = str(getattr(control, "Name", "") or "").strip()
            except Exception:  # noqa: BLE001
                value = ""
            if value and value not in values:
                values.insert(0, value)
        return values

    @staticmethod
    def _moment_identity(moment: Any) -> str | None:
        """Read a stable identity exposed by wxauto, if available."""
        for name in ("runtimeid", "runtime_id", "moment_id", "momentid", "uuid", "id"):
            try:
                value = getattr(moment, name, None)
            except Exception:  # noqa: BLE001 - stale UIA objects are common
                continue
            if callable(value):
                continue
            value = str(value or "").strip()
            if value:
                return value
        return None

    @staticmethod
    def _normalize_moment_text(value: Any) -> str:
        """去掉所有空白后比较，避免微信渲染和模型输出的换行/空格差异导致定位失败。"""
        return "".join(str(value or "").split())

    @staticmethod
    def _field(value: Any, names: tuple[str, ...]) -> Any:
        if isinstance(value, Mapping):
            for name in names:
                if name in value and value[name] not in (None, ""):
                    return value[name]
        for name in names:
            try:
                result = getattr(value, name)
            except Exception:  # noqa: BLE001 - UI 控件字段可能动态失效
                continue
            if result not in (None, ""):
                return result
        return None

    @classmethod
    def _comment_from_item(cls, item: Any) -> MomentComment | None:
        if isinstance(item, str):
            content = item.strip()
            sender = ""
        else:
            content = str(cls._field(item, ("content", "text", "comment", "body")) or "").strip()
            sender = str(cls._field(item, ("sender", "nickname", "username", "author", "name")) or "").strip()
        if not content:
            return None
        # 用「作者+正文」做 key：微信刷新后控件 RuntimeId 可能变化，而正文是稳定的，
        # 这样同一条评论不会被每轮轮询当成新评论反复回复。
        key = f"{sender}\x1f{content}"
        return MomentComment(key=key, content=content, sender=sender)

    @staticmethod
    def _split_sender(text: str) -> tuple[str, str]:
        """把「昵称：正文」拆成 (昵称, 正文)；拆不出来时昵称留空。"""
        value = str(text or "").strip()
        for separator in ("：", ":"):
            head, found, tail = value.partition(separator)
            head = head.strip()
            tail = tail.strip()
            # 昵称通常很短且不含标点，避免把「注意：明天有雨」这类正文误拆。
            if found and head and tail and len(head) <= 20 and not re.search(r"[，。！？,!.?]", head):
                return head, tail
        return "", value

    @classmethod
    def _comment_fields(cls, control: Any) -> tuple[str, str]:
        """从评论控件解析 (作者, 正文)，兼容新旧两种 UIA 结构。"""
        direct = str(getattr(control, "Name", "") or "").strip()
        child_names = [
            str(getattr(child, "Name", "") or "").strip() for child in cls._children(control)
        ]
        child_names = [name for name in child_names if name]
        if direct and direct.lower() not in {"timelinecommentcell", "comment"}:
            sender, content = cls._split_sender(direct)
            if sender:
                return sender, content
        # 新版把作者和正文拆成两个子控件。
        if len(child_names) >= 2:
            return child_names[0], " ".join(child_names[1:])
        if child_names:
            sender, content = cls._split_sender(child_names[0])
            return (sender, content) if sender else ("", content)
        return "", direct


    @classmethod
    def _comments_from_data(cls, moment: Any) -> list[MomentComment]:
        candidates: list[Any] = []
        for source in (
            cls._field(moment, ("comment_list", "comments", "commentList")),
            cls._field(moment, ("info",)),
        ):
            if isinstance(source, Mapping):
                for name in ("comment_list", "comments", "commentList"):
                    value = source.get(name)
                    if isinstance(value, (list, tuple)):
                        candidates.extend(value)
            elif isinstance(source, (list, tuple)):
                candidates.extend(source)
        result: list[MomentComment] = []
        seen: set[str] = set()
        for item in candidates:
            comment = cls._comment_from_item(item)
            # 某些 wxauto 版本把评论区操作按钮也塞进 comments 字段；没有作者的
            # 「评论区/发送」等操作词不是用户评论，直接丢弃。
            if (
                comment is not None
                and not comment.sender
                and comment.content in cls.COMMENT_STOPWORDS
            ):
                continue
            if comment is not None and comment.key not in seen:
                result.append(comment)
                seen.add(comment.key)
        return result

    @staticmethod
    def _children(control: Any) -> list[Any]:
        get_children = getattr(control, "GetChildren", None)
        if not callable(get_children):
            return []
        try:
            children = get_children()
        except Exception:  # noqa: BLE001 - UIA 控件可能在刷新时失效
            return []
        return list(children or [])

    @classmethod
    def _comments_from_controls(cls, moment: Any) -> list[MomentComment]:
        result: list[MomentComment] = []
        seen: set[str] = set()
        for root in cls._comment_scan_roots(moment):
            for comment in cls._parse_comment_controls(root):
                if comment.key not in seen:
                    result.append(comment)
                    seen.add(comment.key)
        return result

    @staticmethod
    def _control_rect(control: Any) -> tuple[float, float, float, float] | None:
        try:
            rect = getattr(control, "BoundingRectangle", None)
            if callable(rect):
                rect = rect()
            if rect is None:
                return None
            values = tuple(float(getattr(rect, name)) for name in ("left", "top", "right", "bottom"))
            return values if values[2] >= values[0] and values[3] >= values[1] else None
        except Exception:  # noqa: BLE001 - 不同 UIA 版本可能不暴露矩形
            return None

    def _comments_from_window_controls(self, moment: Any) -> list[MomentComment]:
        """目标正文控件断开父子关系时，从朋友圈窗口级控件树兜底读取评论。

        wxautox4 某些刷新状态下正文仍可定位，但 `GetParentControl()` 返回空；评论行
        依然挂在窗口根节点下。多个帖子同时可见时，用评论控件和各正文控件的纵坐标
        选择最近的帖子，避免把其它帖子的评论交给当前目标。
        """
        try:
            window = self._moments_window()
            window_root = getattr(window, "control", None)
        except Exception:
            return []
        if window_root is None:
            return []
        comment_controls = [
            control for control in self._iter_controls(window_root)
            if self._is_comment_cell(control)
        ]
        if not comment_controls:
            return []
        try:
            moments = self._get_moments() or []
        except Exception:
            moments = []
        target_identity = self._moment_identity(moment)
        targets: list[tuple[Any, tuple[float, float, float, float]]] = []
        for candidate in moments:
            rect = self._control_rect(getattr(candidate, "control", None))
            if rect is not None:
                targets.append((candidate, rect))
        target_rect = self._control_rect(getattr(moment, "control", None))
        if target_rect is None:
            target_rect = next((rect for candidate, rect in targets if self._moment_identity(candidate) == target_identity), None)

        result: list[MomentComment] = []
        seen: set[str] = set()
        for control in comment_controls:
            include = False
            comment_rect = self._control_rect(control)
            if not targets or target_rect is None or comment_rect is None:
                # 当前窗口只有一个可定位帖子时，窗口级评论没有歧义。
                include = len(targets) <= 1
            else:
                preceding = [
                    (candidate, rect)
                    for candidate, rect in targets
                    if rect[1] <= comment_rect[1]
                ]
                if preceding:
                    nearest = max(preceding, key=lambda item: item[1][1])[0]
                    include = self._moment_identity(nearest) == target_identity
                else:
                    include = False
            if not include:
                continue
            sender, content = self._comment_fields(control)
            if self._is_comment_action_control(control, sender, content):
                continue
            parsed = self._comment_from_item({"content": content, "sender": sender})
            if parsed is not None and parsed.key not in seen:
                result.append(parsed)
                seen.add(parsed.key)
        if result:
            LOGGER.debug("从朋友圈窗口级控件树读取到评论 %s 条", len(result))
        return result

    # 微信把「评论」「赞」这类操作按钮的类名里也带 Comment，解析时要排掉。
    # 「评论区」是新版客户端常见的按钮文字，不能被当成一条空评论。
    COMMENT_STOPWORDS = frozenset(
        {
            "评论",
            "评论区",
            "写评论",
            "发表评论",
            "查看评论",
            "评论一下",
            "赞",
            "取消",
            "取消赞",
            "发送",
            "回复",
            "删除",
            "全文",
            "收起",
        }
    )
    COMMENT_SCAN_DEPTH = 12
    # moment.control 指向的是正文控件，评论是它的兄弟节点，所以要往上找所属的朋友圈 cell。
    COMMENT_SCAN_UP_LEVELS = 4

    @staticmethod
    def _parent_control(control: Any) -> Any | None:
        get_parent = getattr(control, "GetParentControl", None)
        if not callable(get_parent):
            return None
        try:
            return get_parent()
        except Exception:  # noqa: BLE001 - UIA 控件可能已失效
            return None

    @classmethod
    def _comment_scan_roots(cls, moment: Any) -> list[Any]:
        """返回需要扫描评论的控件根节点。

        wxautox4 的 `Moments.control` 是正文控件（`mmui::TimelineContentCell`），
        评论行是它的兄弟节点，挂在上一层 `mmui::TimelineCell` 下。所以先从正文控件往上
        找到所在的朋友圈 cell，再向下扫描。

        上溯在两种情况下立即停止，避免把别人帖子下面的评论也扫进来：
        已经到了 `TimelineCell`（整条朋友圈），或者遇到了朋友圈列表容器。
        """
        root = getattr(moment, "control", None)
        if root is None:
            return []
        roots: list[Any] = [root]
        current = root
        for _ in range(cls.COMMENT_SCAN_UP_LEVELS):
            class_name = str(getattr(current, "ClassName", "") or "").lower()
            if "timelinecell" in class_name:
                break
            parent = cls._parent_control(current)
            if parent is None:
                break
            parent_class = str(getattr(parent, "ClassName", "") or "").lower()
            if "list" in parent_class:
                break
            roots.append(parent)
            current = parent
        return roots

    @classmethod
    def _is_comment_action_control(
        cls, control: Any, sender: str = "", content: str = ""
    ) -> bool:
        """判断带 ``comment`` 类名的控件是不是操作按钮而不是评论行。

        微信新版 UI 会把「评论区」按钮命名成 ``TimelineCommentButton``，
        它和真正的 ``TimelineCommentCell`` 一样会被粗略的类名匹配命中。
        只有没有作者、且文字是已知操作词时才过滤，避免误删用户真的发了
        “评论”两个字的评论。
        """
        name = str(getattr(control, "Name", "") or "").strip()
        class_name = str(getattr(control, "ClassName", "") or "").lower()
        sender = str(sender or "").strip()
        content = str(content or "").strip()
        if sender:
            return False
        if content in cls.COMMENT_STOPWORDS or name in cls.COMMENT_STOPWORDS:
            return True
        # 其它语言/空 Name 的按钮也可通过 UIA 类型和类名识别。
        control_type = cls._control_type(control)
        return (
            control_type == "buttoncontrol"
            and ("comment" in class_name or "action" in class_name)
        )

    @classmethod
    def _is_comment_cell(cls, control: Any) -> bool:
        """评论行判定：类名里带 comment 的控件（TimelineCommentCell / TimelineCommentText …）。"""
        class_name = str(getattr(control, "ClassName", "") or "").lower()
        return "comment" in class_name

    @classmethod
    def _parse_comment_controls(cls, root: Any) -> list[MomentComment]:
        stack: list[tuple[Any, int]] = [(root, 0)]
        visited: set[int] = set()
        result: list[MomentComment] = []
        seen: set[str] = set()
        while stack:
            control, depth = stack.pop()
            identity = id(control)
            if identity in visited:
                continue
            visited.add(identity)
            if cls._is_comment_cell(control):
                sender, content = cls._comment_fields(control)
                if cls._is_comment_action_control(control, sender, content):
                    continue
                comment = cls._comment_from_item({"content": content, "sender": sender})
                if comment is not None and comment.key not in seen:
                    result.append(comment)
                    seen.add(comment.key)
            if depth < cls.COMMENT_SCAN_DEPTH:
                # reversed() 让栈弹出时保持界面上的显示顺序（先出现的评论先解析）。
                stack.extend((child, depth + 1) for child in reversed(cls._children(control)))
        return result

    def _read_comments(self, moment: Any) -> list[MomentComment]:
        result = self._comments_from_data(moment)
        seen = {comment.key for comment in result}
        for comment in self._comments_from_controls(moment):
            if comment.key not in seen:
                result.append(comment)
                seen.add(comment.key)
        if not result:
            for comment in self._comments_from_window_controls(moment):
                if comment.key not in seen:
                    result.append(comment)
                    seen.add(comment.key)
        if not result:
            self._report_comment_scan_problem(moment)
        return result

    @staticmethod
    def _children_or_error(control: Any) -> tuple[list[Any], str | None]:
        """诊断用：把 GetChildren 的失败原因也带出来，而不是静默当成"没有子控件"。"""
        get_children = getattr(control, "GetChildren", None)
        if not callable(get_children):
            return [], "没有 GetChildren 接口"
        try:
            return list(get_children() or []), None
        except Exception as exc:  # noqa: BLE001 - 失败原因要写进诊断结果
            return [], f"{type(exc).__name__}: {exc}"

    @classmethod
    def _describe_control_tree(cls, root: Any, max_depth: int = 6, limit: int = 24) -> list[str]:
        """把控件树压成「类名 文字」清单，用来排查评论控件到底长什么样。"""
        summary: list[str] = []
        seen: set[str] = set()
        stack: list[tuple[Any, int]] = [(root, 0)]
        visited: set[int] = set()
        while stack and len(summary) < limit:
            control, depth = stack.pop()
            identity = id(control)
            if identity in visited:
                continue
            visited.add(identity)
            class_name = str(getattr(control, "ClassName", "") or "")
            name = str(getattr(control, "Name", "") or "").strip()
            children, error = cls._children_or_error(control)
            entry = f"{class_name}:{_shorten(name, 20)}"
            if entry not in seen:
                seen.add(entry)
                summary.append(entry)
            if error is not None:
                marker = f"!!{class_name}:读取子控件失败({_shorten(error, 40)})"
                if marker not in seen:
                    seen.add(marker)
                    summary.append(marker)
            if depth < max_depth:
                stack.extend((child, depth + 1) for child in reversed(children))
        return summary

    def _report_comment_scan_problem(self, moment: Any) -> None:
        """读不到评论时说清楚原因，并给出控件树摘要。"""
        self._comment_scan_failures += 1
        if not self._should_report_failure(self._comment_scan_failures):
            return
        root = getattr(moment, "control", None)
        if root is None:
            LOGGER.warning(
                "没有读到评论（第 %s 次）：朋友圈对象没有暴露 control 属性，无法扫描评论控件",
                self._comment_scan_failures,
            )
            return
        roots = self._comment_scan_roots(moment)
        outermost = roots[-1] if roots else root
        LOGGER.warning(
            "没有读到评论（第 %s 次）：从正文控件往上找到 %s 层，仍没有类名带 comment 的控件；"
            "扫描范围=%r；控件树摘要=%r",
            self._comment_scan_failures,
            max(0, len(roots) - 1),
            [str(getattr(item, "ClassName", "") or "") for item in roots],
            self._describe_control_tree(outermost),
        )

    @classmethod
    def _control_type(cls, control: Any) -> str:
        """读取 UIA 控件类型；不同 wxauto 版本暴露的字段略有差异。"""
        value = getattr(control, "ControlTypeName", "")
        if callable(value):
            try:
                value = value()
            except Exception:  # noqa: BLE001 - 控件可能已失效
                value = ""
        return str(value or "").strip().lower()

    @classmethod
    def _control_labels(cls, control: Any, *, max_depth: int = 3) -> list[str]:
        """收集控件及其子控件的可见文字，用于匹配评论。"""
        labels: list[str] = []
        stack: list[tuple[Any, int]] = [(control, 0)]
        visited: set[int] = set()
        while stack:
            current, depth = stack.pop()
            identity = id(current)
            if identity in visited:
                continue
            visited.add(identity)
            try:
                name = str(getattr(current, "Name", "") or "").strip()
            except Exception:  # noqa: BLE001 - UIA 属性可能动态失效
                name = ""
            if name and name not in labels:
                labels.append(name)
            if depth < max_depth:
                stack.extend((child, depth + 1) for child in cls._children(current))
        return labels

    @classmethod
    def _comment_control_matches(cls, control: Any, comment: MomentComment, moment_text: str = "") -> bool:
        labels = cls._control_labels(control)
        if not labels:
            return False
        content = str(comment.content or "").strip()
        sender = str(comment.sender or "").strip()
        if not content:
            return False
        normalized_labels = [label.replace("：", ":") for label in labels]
        normalized_content = content.replace("：", ":")
        normalized_sender = sender.replace("：", ":")
        # 朋友圈正文本身也是 TextControl，不能被当成评论目标。
        normalized_moment = cls._normalize_moment_text(moment_text)
        if normalized_moment and all(
            cls._normalize_moment_text(label) == normalized_moment for label in labels
        ):
            return False
        candidates = {normalized_content}
        if normalized_sender:
            candidates.add(f"{normalized_sender}:{normalized_content}")
            candidates.add(f"{normalized_sender}: {normalized_content}")
        for label in normalized_labels:
            if label in candidates:
                return True
            # 控件文字形如「小明:晚上好」，正文是它的后缀。
            if label.endswith(":" + normalized_content) or label.endswith(": " + normalized_content):
                return True
        joined = " ".join(normalized_labels)
        if joined == normalized_content or joined.endswith(normalized_content):
            return True
        # 子串匹配只对足够长的评论启用，避免「好」命中「好看」这类误判。
        if len(normalized_content) >= 4:
            return normalized_content in joined
        return False

    @classmethod
    def _iter_comment_controls(cls, moment: Any) -> list[Any]:
        """遍历可点击的评论控件；评论行本身优先于它内部的 TextControl。

        评论行是正文控件的兄弟节点，所以这里和读评论一样从正文控件上溯再向下扫。
        """
        cells: list[Any] = []
        texts: list[Any] = []
        seen: set[int] = set()
        for root in cls._comment_scan_roots(moment):
            stack: list[tuple[Any, int]] = [(root, 0)]
            visited: set[int] = set()
            while stack:
                control, depth = stack.pop()
                identity = id(control)
                if identity in visited:
                    continue
                visited.add(identity)
                if cls._is_comment_cell(control):
                    if identity not in seen:
                        seen.add(identity)
                        cells.append(control)
                elif cls._control_type(control) == "textcontrol":
                    # 点击评论行内部的 TextControl 不一定会弹出回复框，所以放在评论行之后。
                    if identity not in seen:
                        seen.add(identity)
                        texts.append(control)
                if depth < cls.COMMENT_SCAN_DEPTH:
                    stack.extend((child, depth + 1) for child in reversed(cls._children(control)))
        return cells + texts

    def _find_comment_control(self, moment: Any, comment: MomentComment) -> Any | None:
        moment_text = self._moment_content(moment)
        controls = list(self._iter_comment_controls(moment))
        for control in controls:
            if self._comment_control_matches(control, comment, moment_text):
                return control
        self._control_misses += 1
        if self._should_report_failure(self._control_misses):
            LOGGER.warning(
                "找不到评论对应的可点控件（第 %s 次失败）：评论=%r，作者=%r；候选控件 %s 个，前 5 个=%r",
                self._control_misses,
                _shorten(comment.content, 40),
                comment.sender,
                len(controls),
                [self._control_labels(control)[:3] for control in controls[:5]],
            )
        return None

    # 朋友圈里发新评论要点开的按钮（按 UIA 上的文字定位）。
    COMMENT_BUTTON_NAMES = ("评论", "评论区", "写评论", "发表评论")

    @classmethod
    def _is_comment_button(cls, control: Any) -> bool:
        name = str(getattr(control, "Name", "") or "").strip()
        if name not in cls.COMMENT_BUTTON_NAMES:
            return False
        # 评论行本身的名字是「作者：正文」，不会精确等于「评论」；这里再排一次以防万一。
        return not cls._is_comment_cell(control)

    def _comment_button_candidates(self, moment: Any) -> list[str]:
        """点不到「评论」按钮时，把带「评论」字样的控件列出来，方便对齐真实结构。"""
        candidates: list[str] = []
        roots = list(self._comment_scan_roots(moment))
        try:
            window = self._moments_window()
            window_root = getattr(window, "control", None)
        except Exception:
            window_root = None
        if window_root is not None and all(id(window_root) != id(root) for root in roots):
            roots.append(window_root)
        for root in roots:
            for control in self._iter_controls(root, limit=600):
                name = str(getattr(control, "Name", "") or "").strip()
                if "评论" not in name:
                    continue
                entry = f"{str(getattr(control, 'ClassName', '') or '')}:{_shorten(name, 20)}"
                if entry not in candidates:
                    candidates.append(entry)
        return candidates[:10]

    def _find_comment_button(self, moment: Any) -> Any | None:
        """在当前这条朋友圈里找「评论」按钮。

        刷新后的 wxauto 有时只能拿到正文控件，正文的父节点已经断开；这时
        评论按钮仍在朋友圈窗口根节点下，所以再做一次窗口级兜底查找。
        """
        roots = list(self._comment_scan_roots(moment))
        try:
            window = self._moments_window()
            window_root = getattr(window, "control", None)
        except Exception:
            window_root = None
        if window_root is not None and all(id(window_root) != id(root) for root in roots):
            roots.append(window_root)
        for root in roots:
            for control in self._iter_controls(root, limit=600):
                if self._is_comment_button(control):
                    return control
        return None

    def _find_comment_input(self, moment: Any) -> Any | None:
        """找点开「评论」之后出现的输入框：先看 cell 内联输入框，再看 Qt 弹窗。"""
        roots = list(self._comment_scan_roots(moment))
        try:
            window = self._moments_window()
            window_root = getattr(window, "control", None)
        except Exception:
            window_root = None
        if window_root is not None and all(id(window_root) != id(root) for root in roots):
            roots.append(window_root)
        for root in roots:
            inline = self._find_descendant_control(
                root,
                lambda control: self._control_type(control) == "editcontrol",
            )
            if inline is not None:
                _edit, send = self._dialog_controls(root)
                if send is not None:
                    LOGGER.info("找到内联评论输入框（%s）", str(getattr(root, "ClassName", "") or ""))
                    return root
        dialog = None
        try:
            dialog = self._find_comment_dialog(moment)
        except Exception as exc:  # noqa: BLE001 - 弹窗搜索超时/失败都当作"没找到"
            LOGGER.debug("查找独立评论弹窗失败：%s: %s", type(exc).__name__, exc)
            return None
        if dialog is not None:
            LOGGER.info("找到独立评论弹窗（%s）", str(getattr(dialog, "ClassName", "") or ""))
        return dialog

    def comment_via_ui(self, text: str) -> dict[str, Any]:
        """不依赖 `Moment.Comment()`：点开「评论」按钮，在输入框里打字并发送。

        wxautox4 的 `Comment()` 在部分客户端上会返回成功但并没有真的发出评论，
        所以这里自己走一遍界面流程，每一步都留日志。
        """
        text = str(text or "").strip()
        if not text:
            raise MomentsError("朋友圈评论不能为空")
        if not self._last_published_text:
            return {
                "success": False,
                "reason": "no_recent_published_moment",
                "action": "moments_comment_ui",
            }
        target = self._last_published_moment or self._find_published_moment(self._last_published_text)
        if target is None:
            return {
                "success": False,
                "reason": "published_moment_not_found",
                "action": "moments_comment_ui",
            }
        button = self._find_comment_button(target)
        if button is None:
            candidates = self._comment_button_candidates(target)
            LOGGER.warning("找不到「评论」按钮；带「评论」字样的控件=%r", candidates)
            return {
                "success": False,
                "reason": "comment_button_not_found",
                "action": "moments_comment_ui",
                "candidates": candidates,
            }
        button_name = f"{str(getattr(button, 'ClassName', '') or '')}:{str(getattr(button, 'Name', '') or '')}"
        click = getattr(button, "Click", None) or getattr(button, "click", None)
        if not callable(click):
            return {
                "success": False,
                "reason": "comment_button_not_clickable",
                "action": "moments_comment_ui",
                "button": button_name,
            }
        try:
            click()
            LOGGER.info("已点击「评论」按钮（%s），等待输入框", button_name)
            dialog = None
            for _ in range(20):
                dialog = self._find_comment_input(target)
                if dialog is not None:
                    break
                time.sleep(0.1)
            if dialog is None:
                return {
                    "success": False,
                    "reason": "comment_input_not_found",
                    "action": "moments_comment_ui",
                    "button": button_name,
                }
            sent, reason = self._send_comment_dialog(dialog, text)
            if not sent:
                return {
                    "success": False,
                    "reason": reason or "comment_send_failed",
                    "action": "moments_comment_ui",
                    "button": button_name,
                }
        except Exception as exc:  # noqa: BLE001 - UI 自动化失败交给调用方
            return {
                "success": False,
                "reason": f"comment_ui_error:{type(exc).__name__}:{exc}",
                "action": "moments_comment_ui",
                "button": button_name,
            }
        verified = self._verify_reply_visible(target, text)
        if not verified:
            LOGGER.warning("评论已经点了发送，但在评论区里没能确认到：%r", _shorten(text, 40))
        return {
            "success": True,
            "action": "moments_comment_ui",
            "content": text,
            "button": button_name,
            "verified": verified,
        }

    @staticmethod
    def _first_control(value: Any) -> Any | None:
        if isinstance(value, (list, tuple)):
            return value[0] if value else None
        return value

    @classmethod
    def _find_descendant_control(cls, root: Any, predicate: Callable[[Any], bool]) -> Any | None:
        stack: list[tuple[Any, int]] = [(root, 0)]
        visited: set[int] = set()
        while stack:
            control, depth = stack.pop()
            identity = id(control)
            if identity in visited:
                continue
            visited.add(identity)
            try:
                if predicate(control):
                    return control
            except Exception:  # noqa: BLE001 - 动态 UIA 控件字段可能失效
                pass
            if depth < 8:
                stack.extend((child, depth + 1) for child in cls._children(control))
        return None

    @classmethod
    def _dialog_controls(cls, dialog: Any) -> tuple[Any | None, Any | None]:
        """定位评论弹窗中的输入框和发送按钮。"""
        edit = cls._find_descendant_control(
            dialog,
            lambda control: cls._control_type(control) == "editcontrol",
        )
        send = cls._find_descendant_control(
            dialog,
            lambda control: (
                cls._control_type(control) == "buttoncontrol"
                and str(getattr(control, "Name", "") or "").strip().lower() in {"发送", "send"}
            ),
        )
        # 某些 wxauto 版本只在搜索 API 中暴露子控件，不会把它们挂到 GetChildren()。
        if edit is None:
            getter = getattr(dialog, "EditControl", None)
            if callable(getter):
                try:
                    edit = cls._first_control(getter(foundIndex=1))
                except Exception:  # noqa: BLE001
                    try:
                        edit = cls._first_control(getter())
                    except Exception:  # noqa: BLE001
                        edit = None
        if send is None:
            getter = getattr(dialog, "ButtonControl", None)
            if callable(getter):
                for name in ("发送", "Send"):
                    try:
                        send = cls._first_control(getter(Name=name, foundIndex=1))
                    except Exception:  # noqa: BLE001
                        try:
                            send = cls._first_control(getter(Name=name))
                        except Exception:  # noqa: BLE001
                            send = None
                    if send is not None:
                        break
        return edit, send

    def _find_comment_dialog(self, target: Any) -> Any | None:
        """查找点击评论后出现的 Qt 评论窗口。"""
        parent = getattr(target, "parent", None)
        root = getattr(parent, "control", None)
        if root is None:
            window = self._moments_window()
            root = getattr(window, "control", None)

        # wxauto4 官方实现使用这个窗口类；通过根窗口查找可避免把普通微信窗口当成评论框。
        try:
            from wxautox4.uia import WindowControl
            from wxautox4.utils.tools import find_all_windows_from_root

            pid = getattr(root, "pid", None) if root is not None else None
            if callable(pid):
                try:
                    pid = pid()
                except Exception:  # noqa: BLE001
                    pid = None
            kwargs = {"classname": "Qt51514QWindowToolSaveBits"}
            if pid not in (None, ""):
                kwargs["pid"] = pid
            for element in find_all_windows_from_root(**kwargs) or []:
                # wxauto4 返回的通常已经是 UIA Control；旧版工具函数可能返回底层 element。
                dialog = element
                if not callable(getattr(dialog, "GetChildren", None)):
                    dialog = WindowControl(element=element)
                edit, send = self._dialog_controls(dialog)
                if edit is not None and send is not None:
                    return dialog
        except Exception as exc:  # noqa: BLE001 - 无窗口或 wxauto 版本没有该搜索函数
            LOGGER.warning(
                "查找评论弹窗失败（find_all_windows_from_root）：%s: %s", type(exc).__name__, exc
            )

        # 测试替身和少数 wxauto 版本允许从朋友圈根控件直接搜索。
        if root is not None:
            getter = getattr(root, "WindowControl", None)
            if callable(getter):
                try:
                    dialog = self._first_control(
                        getter(ClassName="Qt51514QWindowToolSaveBits", foundIndex=1)
                    )
                except Exception:  # noqa: BLE001
                    dialog = None
                if dialog is not None:
                    edit, send = self._dialog_controls(dialog)
                    if edit is not None and send is not None:
                        return dialog
            dialog = self._find_descendant_control(
                root,
                lambda control: "qt51514qwindowtoolsavebits" in str(
                    getattr(control, "ClassName", "") or ""
                ).lower(),
            )
            if dialog is not None:
                edit, send = self._dialog_controls(dialog)
                if edit is not None and send is not None:
                    return dialog
        return None

    @classmethod
    def _send_comment_dialog(cls, dialog: Any, text: str) -> tuple[bool, str | None]:
        edit, send = cls._dialog_controls(dialog)
        if edit is None:
            return False, "comment_input_not_found"
        try:
            click = getattr(edit, "Click", None) or getattr(edit, "click", None)
            if callable(click):
                click()
            send_keys = getattr(edit, "SendKeys", None) or getattr(edit, "send_keys", None)
            if not callable(send_keys):
                return False, "comment_input_not_supported"
            send_keys("{Ctrl}a")
            # 粘贴依赖系统剪贴板；用完把用户原来的内容放回去。
            set_clipboard = None
            previous_clipboard: str | None = None
            try:
                from wxautox4.utils import win32 as _win32
            except Exception:  # noqa: BLE001
                _win32 = None
            if _win32 is not None:
                set_clipboard = getattr(_win32, "SetClipboardText", None)
                getter = getattr(_win32, "GetClipboardText", None)
                if callable(getter):
                    try:
                        previous_clipboard = str(getter())
                    except Exception:  # noqa: BLE001 - 读剪贴板失败就不还原
                        previous_clipboard = None
            if callable(set_clipboard):
                set_clipboard(text)
                send_keys("{Ctrl}v")
            else:
                for character in text:
                    send_keys(character)
            if send is not None:
                click_send = getattr(send, "Click", None) or getattr(send, "click", None)
                if callable(click_send):
                    click_send()
                else:
                    send_keys("{Enter}")
            else:
                send_keys("{Enter}")
            if previous_clipboard and previous_clipboard != text and callable(set_clipboard):
                try:
                    set_clipboard(previous_clipboard)
                except Exception:  # noqa: BLE001 - 还原剪贴板失败不影响已发送的回复
                    pass
        except Exception as exc:  # noqa: BLE001 - UI 自动化异常交给调用方处理
            return False, f"comment_reply_error:{type(exc).__name__}:{exc}"
        return True, None

    @staticmethod
    def _response_failed(response: Any) -> bool:
        """兼容 wxauto 的 WxResponse，以及旧版返回 None 的实现。"""
        if response is False:
            return True
        if isinstance(response, dict) and response.get("status") not in (None, "成功"):
            return True
        status = getattr(response, "status", None)
        return isinstance(status, str) and status != "成功"

    def _get_moments(self) -> list[Any] | None:
        """读取当前朋友圈列表；读不到时返回 None（和「列表为空」区分开）。"""
        try:
            window = self._moments_window()
            get_moments = getattr(window, "GetMoments", None)
            if not callable(get_moments):
                LOGGER.warning("当前 wxauto 版本没有 GetMoments() 接口，无法定位自己的朋友圈")
                return None
            return list(get_moments() or [])
        except Exception as exc:  # noqa: BLE001 - 读取失败说明窗口可能已经关掉
            self._window_errors += 1
            if self._should_report_failure(self._window_errors):
                LOGGER.warning(
                    "读取朋友圈列表失败（第 %s 次）：%s: %s；已作废当前窗口句柄，稍后重新打开",
                    self._window_errors,
                    type(exc).__name__,
                    exc,
                )
            self._invalidate_window()
            return None

    def _invalidate_window(self) -> None:
        """当前朋友圈窗口句柄已失效：丢掉缓存，让下一次读取重新打开窗口。

        微信关闭朋友圈窗口或客户端重绘后，缓存下来的 UIA 元素会变成死引用
        （典型报错是 COMError「事件无法调用任何订户」或 Find Control Timeout）。
        """
        self._window = None
        self._last_published_moment = None
        self._window_invalidated = True

    def _match_moment(self, candidates: list[Any], text: str) -> tuple[Any | None, str]:
        """在列表里找本次发布的朋友圈，返回 (对象, 命中方式)。"""
        wanted = self._normalize_moment_text(text)
        if not wanted:
            return None, ""
        # 长正文可能在列表里被截断显示，用正文开头做一次宽松匹配。
        head = wanted[:12]
        best: tuple[Any | None, str] = (None, "")
        for moment in candidates:
            values = self._moment_text_values(moment)
            for content in values:
                if content == text:
                    return moment, "exact"
                normalized = self._normalize_moment_text(content)
                if not normalized:
                    continue
                if normalized == wanted:
                    return moment, "normalized"
                # 朋友圈控件文字是「作者 + 正文 + 图片数 + 地点 + 时间」，正文是它的子串。
                if wanted in normalized:
                    return moment, "contains"
                if best[0] is None and len(head) >= 12 and head in normalized:
                    best = (moment, "head")
        return best

    def _find_published_moment(self, text: str, retries: int = 1) -> Any | None:
        """定位本次发布的朋友圈；界面重绘有延迟，所以支持同一轮内多试几次。"""
        attempts = max(1, int(retries))
        last_candidates: list[Any] | None = None
        for attempt in range(attempts):
            candidates = self._get_moments()
            if candidates is None:
                # 窗口读不到（多半已经关掉），继续重试只是白等超时，交给上层重开窗口。
                break
            last_candidates = candidates
            # Once the initial publish has been associated with a Moment
            # object, identity is authoritative after every UI refresh.
            if self._last_published_identity:
                for candidate in candidates:
                    if self._moment_identity(candidate) == self._last_published_identity:
                        self._locate_failures = 0
                        return candidate
            target, how = self._match_moment(candidates, text)
            if target is not None:
                self._locate_failures = 0
                if how != "exact":
                    LOGGER.info(
                        "按 %s 匹配定位到自己的朋友圈：%r",
                        how,
                        _shorten(self._moment_content(target), 60),
                    )
                return target
            if attempt + 1 < attempts:
                self.sleep(self.LOCATE_RETRY_DELAY)
        self._locate_failures += 1
        if self._should_report_failure(self._locate_failures):
            if last_candidates is None:
                # 列表压根没读到（窗口失效），上一条 warning 已经说明了原因。
                LOGGER.warning(
                    "定位不到本次发布的朋友圈（第 %s 次失败）：朋友圈列表读取失败，见上一条日志",
                    self._locate_failures,
                )
            else:
                LOGGER.warning(
                    "找不到本次发布的朋友圈（第 %s 次失败）：发布正文=%r；当前列表 %s 条，前 3 条=%r",
                    self._locate_failures,
                    _shorten(text, 80),
                    len(last_candidates),
                    [_shorten(" | ".join(self._moment_text_values(moment)), 40) for moment in last_candidates[:3]],
                )
        return None

    def _window_process_id(self) -> int | None:
        """当前朋友圈窗口所属的微信进程 pid。"""
        window = self._window
        control = getattr(window, "control", None)
        if control is None:
            return None
        pid = getattr(control, "ProcessId", None)
        if callable(pid):
            try:
                pid = pid()
            except Exception:  # noqa: BLE001 - 控件可能已失效
                return None
        try:
            return int(pid) if pid else None
        except (TypeError, ValueError):
            return None

    def _close_stray_dialogs(self) -> int:
        """关掉微信进程里残留的模态对话框（典型是输入媒体失败后没关掉的"选择文件"窗口）。

        这类对话框会挡住整个朋友圈窗口：隐私菜单、发表按钮都点不到，表现为
        `'Menu' object has no attribute 'control'`；而且它属于微信进程，
        重开朋友圈窗口甚至重启脚本都不会消失。
        """
        if win32gui is None or win32con is None or win32process is None:
            return 0
        pid = self._window_process_id()
        if not pid:
            return 0
        closed: list[str] = []

        def visit(hwnd: int, _extra: Any) -> None:
            try:
                if not win32gui.IsWindowVisible(hwnd):
                    return
                _thread_id, window_pid = win32process.GetWindowThreadProcessId(hwnd)
                if window_pid != pid:
                    return
                class_name = win32gui.GetClassName(hwnd)
                if class_name != "#32770":  # 标准 Windows 对话框
                    return
                title = win32gui.GetWindowText(hwnd)
                win32gui.PostMessage(hwnd, win32con.WM_CLOSE, 0, 0)
                closed.append(f"{class_name}:{_shorten(title, 30)}")
            except Exception:  # noqa: BLE001 - 单个窗口处理失败不影响其它
                return

        try:
            win32gui.EnumWindows(visit, None)
        except Exception as exc:  # noqa: BLE001 - 枚举失败就算了
            LOGGER.debug("枚举微信窗口失败：%s: %s", type(exc).__name__, exc)
            return 0
        if closed:
            LOGGER.warning("关掉了 %s 个残留的微信对话框：%r", len(closed), closed)
        return len(closed)

    @staticmethod
    def _compat_media_input_files(dialog: Any, files: Any) -> None:
        """兼容 wxautox4 41.x 在新版文件对话框中的 ``input_files`` 崩溃。

        41.x 的实现把文件对话框顶层子控件硬解包成两个值；微信更新后这里
        可能多出/少一个辅助控件，于是抛出 ``not enough values to unpack``。
        这里按控件类型寻找编辑框和“打开”按钮，不依赖固定子控件数量。
        """
        if isinstance(files, (str, bytes)):
            files = [files.decode() if isinstance(files, bytes) else files]
        if not isinstance(files, (list, tuple)):
            raise TypeError("files must be str or list")
        root = getattr(dialog, "control", None)
        if root is None:
            raise RuntimeError("文件选择窗口没有 control")
        normalized = [str(Path(str(item)).expanduser().resolve()) for item in files]
        filename_str = " ".join(f'"{item}"' for item in normalized)

        edit = None
        getter = getattr(root, "EditControl", None)
        if callable(getter):
            try:
                edit = getter(ClassName="Edit")
            except Exception:  # noqa: BLE001 - 不同 Windows 版本类名可能不同
                try:
                    edit = getter()
                except Exception:  # noqa: BLE001
                    edit = None
        if edit is None:
            edit = MomentsModule._find_descendant_control(
                root, lambda control: MomentsModule._control_type(control) == "editcontrol"
            )
        if edit is None:
            raise RuntimeError("文件选择窗口没有文件名输入框")

        hwnd_edit = getattr(edit, "NativeWindowHandle", None)
        if callable(hwnd_edit):
            hwnd_edit = hwnd_edit()
        if win32gui is not None and win32con is not None and hwnd_edit:
            win32gui.SendMessage(hwnd_edit, win32con.WM_SETTEXT, 0, filename_str)
        else:
            click = getattr(edit, "Click", None) or getattr(edit, "click", None)
            if callable(click):
                click()
            send_keys = getattr(edit, "SendKeys", None) or getattr(edit, "send_keys", None)
            if not callable(send_keys):
                raise RuntimeError("文件名输入框不支持写入")
            send_keys("{Ctrl}a")
            send_keys(filename_str)

        controls = MomentsModule._iter_controls(root, limit=80)
        buttons = [
            control
            for control in controls
            if MomentsModule._control_type(control) == "buttoncontrol"
        ]
        open_button = next(
            (
                control
                for control in buttons
                if str(getattr(control, "Name", "") or "").strip().lower()
                in {"打开", "打开(o)", "open", "选择"}
            ),
            buttons[0] if buttons else None,
        )
        if open_button is None:
            raise RuntimeError("文件选择窗口没有打开按钮")
        hwnd_open = getattr(open_button, "NativeWindowHandle", None)
        if callable(hwnd_open):
            hwnd_open = hwnd_open()
        if win32gui is not None and win32con is not None and hwnd_open:
            win32gui.SendMessage(hwnd_open, win32con.BM_CLICK, 0, 0)
        else:
            click = getattr(open_button, "Click", None) or getattr(open_button, "click", None)
            if not callable(click):
                raise RuntimeError("打开按钮不可点击")
            click()

    @staticmethod
    def _invoke_publish(publish: Any, text: str, media: Any, privacy: Any) -> tuple[Any, str | None]:
        """调用一次 Publish，把异常转成原因字符串（wxauto 内部崩起来五花八门）。"""
        patched_input_files: tuple[Any, Any] | None = None
        try:
            # wxautox4 exposes ``wait_upload`` on newer releases.  Passing
            # named arguments avoids a positional mismatch with older builds,
            # while the signature check keeps test doubles and old wxauto
            # versions compatible.
            kwargs = {"text": text, "media_files": media, "privacy_config": privacy}
            try:
                parameters = inspect.signature(publish).parameters
            except (TypeError, ValueError):
                parameters = {}
            if media and "wait_upload" in parameters:
                kwargs["wait_upload"] = 8
            # 只对 wxautox4 的真实 Publish 打补丁；测试替身和其它实现不受影响。
            if media and str(getattr(publish, "__module__", "")).startswith("wxautox4"):
                try:
                    from wxautox4.ui.moment import WinOpenUI

                    original = WinOpenUI.input_files
                    WinOpenUI.input_files = MomentsModule._compat_media_input_files
                    patched_input_files = (WinOpenUI, original)
                except Exception:  # noqa: BLE001 - 没有该类时继续走官方实现
                    patched_input_files = None
            return publish(**kwargs), None
        except Exception as exc:  # noqa: BLE001 - 交给调用方决定降级/重试
            return None, f"{type(exc).__name__}: {exc}"
        finally:
            if patched_input_files is not None:
                owner, original = patched_input_files
                try:
                    owner.input_files = original
                except Exception:  # noqa: BLE001 - 恢复失败不影响发布结果
                    pass

    def _publish_confirmed(self, text: str) -> bool:
        """报错之后确认一下：这条内容是不是其实已经发出去了（防重复发布）。"""
        try:
            return self._find_published_moment(text, retries=1) is not None
        except Exception:  # noqa: BLE001 - 确认不了就当作没发出去
            return False

    def publish(
        self,
        text: str,
        media_files: Iterable[str] | None = None,
        privacy_config: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        text = str(text or "").strip()
        if not text:
            raise MomentsError("朋友圈内容不能为空")
        media = self._validate_media_files(media_files)
        if privacy_config is not None and not isinstance(privacy_config, dict):
            raise MomentsError("privacy_config 必须是对象")
        # 空字典等于"不改隐私设置"。wxautox4 在部分客户端上会去点隐私菜单然后崩
        # （Menu object has no attribute 'control'），能不碰就不碰。
        privacy: dict[str, Any] | None = dict(privacy_config) if privacy_config else None
        window = self._moments_window()
        publish = getattr(window, "Publish", None)
        if not callable(publish):
            raise MomentsError("当前 wxauto 版本没有 Publish() 接口")
        # 上一次失败可能在微信进程里留下模态对话框，先把界面恢复干净再发。
        self._close_stray_dialogs()

        media_error: str | None = None
        response: Any = None
        already_published = False
        use_media = bool(media)
        if use_media:
            response, media_error = self._invoke_publish(publish, text, media, privacy)
            if media_error is not None:
                LOGGER.warning(
                    "带媒体发布失败（%s），改为纯文字重试；媒体=%r",
                    media_error,
                    [_shorten(item, 60) for item in media or []],
                )
                # 媒体失败通常留下一个模态"选择文件"对话框，不清掉后面的纯文字也别想发出去。
                self._close_stray_dialogs()
        if not use_media or media_error is not None:
            # 没配媒体、或媒体阶段已经失败：走纯文字发布。
            # 媒体失败发生在"输入媒体"阶段（正文还没写进去），所以重试不会造成重复发布。
            response, publish_error = self._invoke_publish(publish, text, None, privacy)
            if publish_error is not None:
                LOGGER.warning("纯文字发布报错：%s", publish_error)
                if self._publish_confirmed(text):
                    LOGGER.warning("报错了但朋友圈里已经能看到这条内容，按发布成功处理，不再重试")
                    already_published = True
                    publish_error = None
                else:
                    # 换一个干净的朋友圈窗口再试一次：发布视图/隐私菜单的状态可能是坏的。
                    if self._reopen_moments_window("发布报错后重试", forced=True):
                        retry_window = self._moments_window()
                        retry_publish = getattr(retry_window, "Publish", None)
                        if callable(retry_publish):
                            response, publish_error = self._invoke_publish(
                                retry_publish, text, None, privacy
                            )
                            if publish_error is not None and self._publish_confirmed(text):
                                LOGGER.warning("重试报错但内容已经发出，按发布成功处理")
                                already_published = True
                                publish_error = None
                if publish_error is not None:
                    return {
                        "success": False,
                        "reason": f"publish_error:{publish_error}",
                        "action": "moments_publish",
                        "media_dropped": media_error,
                    }
        if not already_published and self._response_failed(response):
            return {
                "success": False,
                "reason": "publish_failed",
                "action": "moments_publish",
                "response": response,
                "media_dropped": media_error,
            }
        self._last_published_text = text
        self._last_published_identity = None
        # 刚发完界面还没绘制出新帖子，给它几次重试的机会，避免一上来就 tracked=False。
        self._last_published_moment = self._find_published_moment(text, retries=3)
        if self._last_published_moment is not None:
            self._last_published_identity = self._moment_identity(self._last_published_moment)
        tracked = self._last_published_moment is not None
        LOGGER.info("朋友圈发布完成：tracked=%s，正文=%r", tracked, _shorten(text, 80))
        return {
            "success": True,
            "action": "moments_publish",
            "content": text,
            "tracked": tracked,
            "target_identity": self._last_published_identity,
            "media_dropped": media_error,
        }

    def _verify_reply_visible(self, moment: Any, text: str, timeout_seconds: float | None = None) -> bool:
        """发出评论后确认它真的出现在评论区；确认不了只记日志，不算发送失败。

        发评论会让朋友圈那条 cell 重新渲染，缓存的控件往往已经失效——读到空是假阴性。
        所以读不到时会中途重新刷新并重新定位一次，用新的控件再读一遍。
        """
        wanted = self._normalize_moment_text(text)
        if not wanted:
            return False
        if getattr(moment, "control", None) is None:
            # 连控件都拿不到，说明这个版本没法读评论区，别白等。
            LOGGER.debug("朋友圈对象没有暴露 control，无法确认评论是否发出")
            return False
        budget = self.VERIFY_TIMEOUT_SECONDS if timeout_seconds is None else timeout_seconds
        deadline = time.monotonic() + max(0.0, budget)
        target = moment
        relocated = False
        while True:
            try:
                comments = self._read_comments(target)
            except Exception:  # noqa: BLE001 - 校验失败不应该影响已发出的评论
                comments = []
            for comment in comments:
                if wanted in self._normalize_moment_text(comment.content):
                    return True
            if not relocated and self._last_published_text:
                relocated = True
                self._refresh_window()
                fresh = self._find_published_moment(self._last_published_text)
                if fresh is not None:
                    target = fresh
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.2)

    def reply_to_comment(self, comment: MomentComment, text: str) -> dict[str, Any]:
        """点击指定评论后，在点对点评论框中发送回复。"""
        text = str(text or "").strip()
        if not text:
            raise MomentsError("朋友圈回复不能为空")
        if not self._last_published_text:
            return {
                "success": False,
                "reason": "no_recent_published_moment",
                "action": "moments_comment_reply",
            }
        target = self._last_published_moment or self._find_published_moment(self._last_published_text)
        if target is None:
            return {
                "success": False,
                "reason": "published_moment_not_found",
                "action": "moments_comment_reply",
            }
        control = self._find_comment_control(target, comment)
        if control is None:
            return {
                "success": False,
                "reason": "comment_control_not_found",
                "action": "moments_comment_reply",
                "comment": comment.content,
                "sender": comment.sender,
            }
        click = getattr(control, "Click", None) or getattr(control, "click", None)
        if not callable(click):
            return {
                "success": False,
                "reason": "comment_control_not_clickable",
                "action": "moments_comment_reply",
            }
        try:
            click()
            # 评论窗口由微信异步创建，给 UIA 一个短暂的刷新机会。
            dialog = None
            for _ in range(10):
                dialog = self._find_comment_dialog(target)
                if dialog is not None:
                    break
                time.sleep(0.05)
            if dialog is None:
                return {
                    "success": False,
                    "reason": "comment_dialog_not_found",
                    "action": "moments_comment_reply",
                }
            sent, reason = self._send_comment_dialog(dialog, text)
            if not sent:
                return {
                    "success": False,
                    "reason": reason or "comment_reply_failed",
                    "action": "moments_comment_reply",
                }
            verified = self._verify_reply_visible(target, text)
            if not verified:
                LOGGER.warning(
                    "回复已经点了发送，但在朋友圈里没能确认到这条回复：%r", _shorten(text, 40)
                )
        except Exception as exc:  # noqa: BLE001 - UI 自动化失败交回轮询器
            return {
                "success": False,
                "reason": f"comment_reply_error:{type(exc).__name__}:{exc}",
                "action": "moments_comment_reply",
            }
        return {
            "success": True,
            "action": "moments_comment_reply",
            "content": text,
            "comment": comment.content,
            "sender": comment.sender,
            "verified": verified,
        }

    def comment_last(self, text: str, comment: MomentComment | None = None) -> dict[str, Any]:
        """发表评论；传入 comment 时改为点击该评论后的点对点回复。"""
        if comment is not None:
            return self.reply_to_comment(comment, text)
        text = str(text or "").strip()
        if not text:
            raise MomentsError("朋友圈评论不能为空")
        if not self._last_published_text:
            return {"success": False, "reason": "no_recent_published_moment", "action": "moments_comment"}
        target = self._last_published_moment or self._find_published_moment(self._last_published_text)
        if target is None:
            return {
                "success": False,
                "reason": "published_moment_not_found",
                "action": "moments_comment",
            }
        comment = getattr(target, "Comment", None)
        if not callable(comment):
            return {"success": False, "reason": "comment_not_supported", "action": "moments_comment"}
        try:
            response = comment(text)
            if self._response_failed(response):
                return {
                    "success": False,
                    "reason": "comment_failed",
                    "action": "moments_comment",
                    "response": response,
                }
        except Exception as exc:  # noqa: BLE001 - UI 自动化失败交回模型
            return {
                "success": False,
                "reason": f"comment_error:{type(exc).__name__}:{exc}",
                "action": "moments_comment",
            }
        verified = self._verify_reply_visible(target, text)
        if not verified:
            LOGGER.warning("评论已经通过 Comment() 发出，但在评论区里没能确认到：%r", _shorten(text, 40))
        return {
            "success": True,
            "action": "moments_comment",
            "content": text,
            "verified": verified,
        }

    def refresh_last_published(self) -> bool:
        """刷新朋友圈窗口并重新定位本程序最近发布的朋友圈。"""
        if not self._last_published_text:
            return False
        self._window_invalidated = False
        # 刷新本身失败也继续尝试定位：能读到新帖子就没必要纠结刷新接口的返回值。
        self._refresh_window()
        target = self._find_published_moment(self._last_published_text, retries=self.LOCATE_RETRIES)
        if target is None and self._window_invalidated:
            # 读取过程中发现窗口已经关掉，立刻重新打开再试一次。
            if self._reopen_moments_window("朋友圈窗口已失效", forced=True):
                target = self._find_published_moment(
                    self._last_published_text, retries=self.LOCATE_RETRIES
                )
        if target is None and self._locate_failures >= self.HARD_REFRESH_AFTER:
            # 窗口还活着但列表一直看不到自己刚发的帖子，让微信重新加载朋友圈。
            if self._reopen_moments_window("连续定位不到本次发布"):
                target = self._find_published_moment(
                    self._last_published_text, retries=self.LOCATE_RETRIES
                )
        if target is None:
            return False
        self._last_published_moment = target
        self._refresh_errors = 0
        self._window_errors = 0
        return True

    def _refresh_window(self) -> bool:
        """刷新朋友圈界面；返回是否确实刷新成功。"""
        try:
            window = self._moments_window()
        except Exception as exc:  # noqa: BLE001 - 窗口打不开时下一轮再试
            self._window_errors += 1
            if self._should_report_failure(self._window_errors):
                LOGGER.warning(
                    "打开朋友圈窗口失败（第 %s 次）：%s: %s",
                    self._window_errors,
                    type(exc).__name__,
                    exc,
                )
            self._invalidate_window()
            return False
        refresh = getattr(window, "Refresh", None)
        if not callable(refresh):
            self._refresh_errors += 1
            if self._should_report_failure(self._refresh_errors):
                LOGGER.warning(
                    "当前 wxauto 版本没有 MomentsWnd.Refresh()，改为直接点工具栏刷新按钮"
                )
            return self._click_refresh_button(window)
        try:
            response = refresh()
        except Exception as exc:  # noqa: BLE001 - 窗口可能已经关掉
            self._refresh_errors += 1
            if self._should_report_failure(self._refresh_errors):
                LOGGER.warning(
                    "刷新朋友圈窗口失败（第 %s 次）：%s: %s",
                    self._refresh_errors,
                    type(exc).__name__,
                    exc,
                )
            self._invalidate_window()
            return False
        if self._response_failed(response):
            self._refresh_errors += 1
            if self._should_report_failure(self._refresh_errors):
                LOGGER.warning(
                    "朋友圈刷新接口返回失败（第 %s 次）：%r；改用工具栏刷新按钮",
                    self._refresh_errors,
                    response,
                )
            return self._click_refresh_button(window)
        return True

    def _click_refresh_button(self, window: Any) -> bool:
        """Refresh() 不可用时的兜底：直接点朋友圈工具栏上的「刷新」。"""
        buttons = getattr(window, "toolbar_buttons", None)
        if not isinstance(buttons, Mapping):
            LOGGER.warning("朋友圈窗口没有暴露 toolbar_buttons，无法点刷新按钮")
            return False
        for name, control in buttons.items():
            if "刷新" not in str(name):
                continue
            click = getattr(control, "Click", None) or getattr(control, "click", None)
            if callable(click):
                try:
                    click()
                    return True
                except Exception as exc:  # noqa: BLE001 - 下一轮继续尝试
                    LOGGER.warning("点击朋友圈刷新按钮失败：%s: %s", type(exc).__name__, exc)
                    return False
        LOGGER.warning("朋友圈工具栏里没有「刷新」按钮，可用按钮=%r", list(buttons.keys()))
        return False

    def _reopen_moments_window(self, reason: str, *, forced: bool = False) -> bool:
        """让微信重新加载朋友圈窗口。

        这里**故意不调用 Close()**：关窗是异步的，紧接着重开很可能拿到正在关闭的那个窗口，
        表现为「先匹配成功、20 秒后 COMError 事件无法调用任何订户 / ListControl 找不到」。
        直接作废句柄再走一次 WeChat.Moments()，让微信自己把窗口激活并加载到最新列表。
        """
        now = time.monotonic()
        cooldown = self.FORCED_REOPEN_COOLDOWN if forced else self.HARD_REFRESH_COOLDOWN
        if self._last_hard_refresh_at is not None and now - self._last_hard_refresh_at < cooldown:
            return False
        self._last_hard_refresh_at = now
        LOGGER.warning("重新打开朋友圈窗口（%s）", reason)
        self._window = None
        self._last_published_moment = None
        self.sleep(0.6)
        try:
            self._moments_window()
        except Exception as exc:  # noqa: BLE001 - 打不开时下一轮再试
            LOGGER.warning("重新打开朋友圈窗口失败：%s: %s", type(exc).__name__, exc)
            return False
        self.sleep(0.6)
        self._window_invalidated = False
        return True

    def read_last_comments(self) -> list[MomentComment]:
        """读取最近发布目标当前可见的评论；当前版本未暴露评论时返回空列表。"""
        target = self._last_published_moment or self._find_published_moment(self._last_published_text)
        if target is None:
            LOGGER.debug("还没有定位到发布目标，暂时不读评论")
            return []
        comments = self._read_comments(target)
        # 只在读取结果变化时打 INFO，既能看见「读到了什么」，又不会每轮刷屏。
        signature = tuple(comment.key for comment in comments)
        if signature != self._last_comment_signature:
            self._last_comment_signature = signature
            LOGGER.info(
                "朋友圈评论读取结果：%s 条 %r",
                len(comments),
                [(comment.sender, comment.content) for comment in comments[:5]],
            )
        return comments

    def diagnose(self, last_text: str = "", max_moments: int = 5) -> dict[str, Any]:
        """诊断朋友圈评论读取链路：列出可见朋友圈、控件树摘要和解析出的评论。

        只读，不发布、不点击、不评论；供 `python run_moments.py --diagnose` 使用。
        """
        report: dict[str, Any] = {"success": False}
        try:
            window = self._moments_window()
        except Exception as exc:  # noqa: BLE001 - 打不开朋友圈窗口时直接给出原因
            report["reason"] = f"open_moments_failed:{type(exc).__name__}:{exc}"
            return report
        report["window"] = type(window).__name__
        report["has_refresh"] = callable(getattr(window, "Refresh", None))
        buttons = getattr(window, "toolbar_buttons", None)
        if isinstance(buttons, Mapping):
            report["toolbar_buttons"] = [str(name) for name in buttons]
        # 整个朋友圈窗口的控件树：用来确认评论行到底挂在哪一层、类名叫什么。
        window_control = getattr(window, "control", None)
        report["has_window_control"] = window_control is not None
        if window_control is not None:
            all_controls = self._iter_controls(window_control)
            report["window_control_count"] = len(all_controls)
            report["window_comment_controls"] = [
                f"{str(getattr(control, 'ClassName', '') or '')}:"
                f"{_shorten(str(getattr(control, 'Name', '') or ''), 40)}"
                for control in all_controls
                if self._is_comment_cell(control)
            ][:20]
            report["window_tree"] = self._describe_control_tree(
                window_control, max_depth=6, limit=60
            )
        candidates = self._get_moments()
        if candidates is None:
            report["reason"] = "get_moments_failed"
            return report
        report["moment_count"] = len(candidates)
        report["moments"] = [
            _shorten(self._moment_content(moment), 100) for moment in candidates[:max_moments]
        ]
        target = None
        if last_text:
            target, how = self._match_moment(candidates or [], last_text)
            report["matched_by"] = how
        if target is None and candidates:
            target = candidates[0]
            report["matched_by"] = report.get("matched_by") or "first_visible"
        if target is None:
            report["reason"] = "no_moment_visible"
            report["success"] = True
            return report
        report["target_content"] = _shorten(self._moment_content(target), 100)
        report["target_identity"] = self._moment_identity(target)
        root = getattr(target, "control", None)
        report["has_control"] = root is not None
        roots = self._comment_scan_roots(target)
        report["scan_roots"] = [str(getattr(item, "ClassName", "") or "") for item in roots]
        if roots:
            report["comment_cells"] = [
                f"{str(getattr(control, 'ClassName', '') or '')}:"
                f"{_shorten(str(getattr(control, 'Name', '') or ''), 40)}"
                for control in self._iter_controls(roots[-1])
                if self._is_comment_cell(control)
            ]
            report["control_tree"] = self._describe_control_tree(roots[-1], max_depth=8, limit=40)
        parsed = self._read_comments(target)
        report["parsed_comments"] = [
            {"sender": comment.sender, "content": comment.content} for comment in parsed
        ]
        report["success"] = True
        return report

    @classmethod
    def _iter_controls(cls, root: Any, limit: int = 1500) -> list[Any]:
        stack: list[tuple[Any, int]] = [(root, 0)]
        visited: set[int] = set()
        controls: list[Any] = []
        while stack and len(controls) < limit:
            control, depth = stack.pop()
            identity = id(control)
            if identity in visited:
                continue
            visited.add(identity)
            controls.append(control)
            if depth < cls.COMMENT_SCAN_DEPTH:
                stack.extend((child, depth + 1) for child in reversed(cls._children(control)))
        return controls

    def reset_target(self) -> None:
        self._last_published_text = ""
        self._last_published_moment = None
        self._last_published_identity = None

    def close(self) -> None:
        """关闭当前朋友圈窗口；独立运行结束时调用。

        窗口可能已经被微信关掉（此时 UIA 句柄是死引用，Close() 会抛 COMError），
        所以这里只记日志不外抛，避免退出时盖掉真正有用的错误。
        """
        window = self._window
        self._window = None
        self._last_published_moment = None
        self._last_published_identity = None
        if window is None:
            return
        close = getattr(window, "Close", None) or getattr(window, "close", None)
        if not callable(close):
            return
        try:
            close()
        except Exception as exc:  # noqa: BLE001 - 窗口已关闭或句柄失效都属于正常收尾
            LOGGER.info("关闭朋友圈窗口时窗口已不可用（%s: %s），忽略", type(exc).__name__, exc)
