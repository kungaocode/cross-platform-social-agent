"""DeepSeek social message bot core."""

from .network_policy import enforce_direct_networking

enforce_direct_networking()

from .service import BotService, IncomingMessage

__all__ = ["BotService", "IncomingMessage"]
