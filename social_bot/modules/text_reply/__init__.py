"""Legacy imports mapped to the canonical WeChat modules."""
import importlib
import sys
package = importlib.import_module('social_bot.platforms.wechat.modules.text_reply')
sys.modules[__name__ + '.module'] = importlib.import_module('social_bot.platforms.wechat.modules.text_reply.module')
sys.modules[__name__] = package
