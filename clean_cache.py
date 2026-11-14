"""Manually clear the configured dedicated cache. Never called by the bot."""
import argparse
import json
from social_bot.cache import MediaCache


def main():
    parser=argparse.ArgumentParser(description='清空专用媒体缓存，保留原文件、记忆库、备份和登录态')
    parser.add_argument('--dry-run',action='store_true',help='只预览，不删除')
    args=parser.parse_args()
    try:
        result=MediaCache().clear(dry_run=args.dry_run)
    except (OSError,ValueError) as exc:
        print(json.dumps(dict(success=False,error=str(exc)),ensure_ascii=False,indent=2))
        return 2
    print(json.dumps(result,ensure_ascii=False,indent=2))
    return 1 if result['errors'] or result['skipped_active'] else 0


if __name__=='__main__':
    raise SystemExit(main())
