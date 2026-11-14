"""Compatibility alias for the relocated WeChat adapter."""
import sys
from .platforms.wechat import adapter
sys.modules[__name__] = adapter
