from __future__ import annotations

from typing import Iterable


# wxauto/微信输入框支持的常用原生表情标记。Unicode emoji 直接作为普通字符串发送。
DEFAULT_STICKERS: tuple[str, ...] = (
    "[微笑]", "[撇嘴]", "[色]", "[发呆]", "[得意]", "[流泪]", "[害羞]", "[闭嘴]", "[睡]", "[大哭]",
    "[尴尬]", "[发怒]", "[调皮]", "[呲牙]", "[惊讶]", "[难过]", "[囧]", "[抓狂]", "[吐]", "[偷笑]",
    "[愉快]", "[白眼]", "[傲慢]", "[困]", "[惊恐]", "[憨笑]", "[悠闲]", "[咒骂]", "[疑问]", "[嘘]",
    "[晕]", "[衰]", "[骷髅]", "[敲打]", "[再见]", "[擦汗]", "[抠鼻]", "[鼓掌]", "[坏笑]", "[右哼哼]",
    "[鄙视]", "[委屈]", "[快哭了]", "[阴险]", "[亲亲]", "[可怜]", "[笑脸]", "[生病]", "[脸红]", "[破涕为笑]",
    "[恐惧]", "[失望]", "[无语]", "[嘿哈]", "[捂脸]", "[奸笑]", "[机智]", "[皱眉]", "[耶]", "[吃瓜]",
    "[加油]", "[汗]", "[天啊]", "[Emm]", "[社会社会]", "[旺柴]", "[好的]", "[打脸]", "[哇]", "[翻白眼]",
    "[666]", "[让我看看]", "[叹气]", "[苦涩]", "[裂开]", "[嘴唇]", "[爱心]", "[心碎]", "[拥抱]", "[强]",
    "[弱]", "[握手]", "[胜利]", "[抱拳]", "[勾引]", "[拳头]", "[OK]", "[合十]", "[啤酒]", "[咖啡]",
    "[蛋糕]", "[玫瑰]", "[凋谢]", "[菜刀]", "[炸弹]", "[便便]", "[月亮]", "[太阳]", "[庆祝]", "[礼物]",
    "[红包]", "[發]", "[福]", "[烟花]", "[爆竹]", "[猪头]", "[跳跳]", "[发抖]", "[转圈]",
)


class EmojiModule:
    """统一聊天和朋友圈的 emoji/微信原生表情提示策略。"""

    def __init__(self, enabled: bool = True, max_count: int = 2, stickers_enabled: bool = True) -> None:
        self.enabled = bool(enabled)
        self.max_count = max(0, int(max_count))
        self.stickers_enabled = bool(stickers_enabled)
        self.stickers = frozenset(DEFAULT_STICKERS)

    def prompt_instruction(self) -> str:
        if not self.enabled:
            return "不要主动添加 emoji 或微信原生表情标记。"
        if self.max_count == 0:
            return "不要添加 Unicode emoji 或微信原生表情标记。"
        sticker_hint = "也可以使用微信原生表情标记，例如 [微笑]、[鼓掌]、[太阳]。" if self.stickers_enabled else "不要使用 [微笑] 这类微信原生表情标记。"
        return (
            f"在合适的位置用 1 到 {self.max_count} 个 Unicode emoji 表达语气、情绪或小心思；{sticker_hint}"
            "让表情和上下文自然匹配，不要每句话都加，也不要为了凑数量强行添加。"
        )

    def contains_sticker(self, text: str, candidates: Iterable[str] | None = None) -> bool:
        stickers = self.stickers if candidates is None else set(candidates)
        return any(sticker in str(text) for sticker in stickers)
