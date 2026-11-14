"""独立的微信消息能力模块。

每个目录只负责一种可替换的消息能力，微信适配器负责把它们组合起来。
"""

from .quote import QuoteModule, QuoteOutcome
from .recall import RecallModule, RecallOutcome
from .text_reply import TextReplyModule, TextReplyOutcome
from .ocr import OCRModule
from .research import ResearchModule
from .speech import QwenAudioTranscriber, SpeechModule
from .time import TimeModule
from .weather import WeatherError, WeatherModule
from .emoji import DEFAULT_STICKERS, EmojiModule
from .moments import MomentComment, MomentsError, MomentsModule, MomentsRunError, MomentsRunner
from .media import IMAGE_EXTENSIONS, MediaModule, MediaOutcome
from .group import GroupModule, GroupOutcome, is_group_chat
from .vision import QwenTextAnalyzer, QwenVisionAnalyzer, VisionModule, VisionOutcome
from .poke import PokeModule, PokeOutcome, is_poke_message
from .document import DocumentModule, DocumentOutcome
from .link import LinkModule, LinkOutcome
from .audio import AUDIO_EXTENSIONS, AudioModule, AudioOutcome

__all__ = [
    "QuoteModule",
    "QuoteOutcome",
    "RecallModule",
    "RecallOutcome",
    "TextReplyModule",
    "TextReplyOutcome",
    "WeatherError",
    "WeatherModule",
    "DEFAULT_STICKERS",
    "EmojiModule",
    "ResearchModule",
    "OCRModule",
    "SpeechModule",
    "QwenAudioTranscriber",
    "TimeModule",
    "MomentsError",
    "MomentsModule",
    "MomentComment",
    "MomentsRunError",
    "MomentsRunner",
    "IMAGE_EXTENSIONS",
    "MediaModule",
    "MediaOutcome",
    "GroupModule",
    "GroupOutcome",
    "is_group_chat",
    "VisionModule",
    "VisionOutcome",
    "QwenVisionAnalyzer",
    "QwenTextAnalyzer",
    "PokeModule",
    "PokeOutcome",
    "is_poke_message",
    "DocumentModule",
    "DocumentOutcome",
    "LinkModule",
    "LinkOutcome",
    "AUDIO_EXTENSIONS",
    "AudioModule",
    "AudioOutcome",
]
