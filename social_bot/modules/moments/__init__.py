"""Legacy imports mapped to the canonical WeChat modules."""
import importlib
import sys
package = importlib.import_module('social_bot.platforms.wechat.modules.moments')
sys.modules[__name__ + '.module'] = importlib.import_module('social_bot.platforms.wechat.modules.moments.module')
sys.modules[__name__ + '.runner'] = importlib.import_module('social_bot.platforms.wechat.modules.moments.runner')
sys.modules[__name__] = package
