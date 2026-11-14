"""Inspect and test shared search APIs without a model or social-message listener."""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import replace
from pathlib import Path

from social_bot.modules.research import ResearchModule
from social_bot.modules.research.module import BACKENDS
from social_bot.modules.research.duckduckgo import DuckDuckGoError
from social_bot.settings import load_settings

ROOT = Path(__file__).resolve().parent


def main():
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8')
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, default=ROOT/'config.json')
    commands = parser.add_subparsers(dest='command', required=True)
    commands.add_parser('check')
    verify = commands.add_parser('browser-verify', help='打开专用会话，验证码由本人手动完成')
    verify.add_argument('--query', default='Python documentation')
    commands.add_parser('browser-status', help='检查人工验证状态并保存会话')
    commands.add_parser('browser-stop', help='关闭专用搜索浏览器')
    search = commands.add_parser('search')
    search.add_argument('query')
    search.add_argument('--backend', choices=['configured', *sorted(BACKENDS)], default='configured')
    search.add_argument('--count', type=int, default=3)
    search.add_argument('--output', type=Path)
    args = parser.parse_args()
    runtime = load_settings(args.config.resolve()).runtime
    if args.command == 'search' and args.backend != 'configured':
        runtime = replace(runtime, research_backends=(args.backend,))
    module = ResearchModule.from_settings(runtime)
    if args.command.startswith('browser-'):
        browser = module.browser_client()
        if args.command == 'browser-verify':
            result = browser.prepare(args.query[:500])
            result['note'] = ('请在打开的专用窗口手动完成验证码，然后运行 browser-status。'
                              if result.get('state') == 'verification_required' else '专用会话已就绪；窗口保留以供后续搜索。')
        elif args.command == 'browser-status':
            result = browser.status()
        else:
            result = browser.stop()
    elif args.command == 'check':
        result = module.status()
        result['domestic_ready_count'] = sum(item['configured'] for item in result['backends']
                                            if item['provider'] in {'bocha_api', 'baidu_api'})
        result['note'] = '仅检查配置；未联网、未调用模型、未发送社交消息。'
    else:
        result = module.search(args.query, args.count)
        if args.output:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2)+'\n', encoding='utf-8')
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if args.command == 'check' or args.command.startswith('browser-') or result['success'] else 1


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except (ValueError, OSError):
        print(json.dumps(dict(success=False, reason='search_config_or_file_invalid')))
        raise SystemExit(1)
    except DuckDuckGoError as exc:
        print(json.dumps(dict(success=False, reason=exc.reason), ensure_ascii=False))
        raise SystemExit(1)
