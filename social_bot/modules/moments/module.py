"""Compatibility alias; implementation lives under platforms/wechat."""
import importlib
import sys
sys.modules[__name__] = importlib.import_module('social_bot.platforms.wechat.modules.moments.module')
