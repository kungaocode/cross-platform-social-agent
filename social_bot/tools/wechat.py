"""Compatibility alias for WeChat tools."""
import sys
from ..platforms.wechat import tools
sys.modules[__name__] = tools
