"""Operate one explicitly named group's announcement; writes are never retried."""
import argparse
import json
import sys
from pathlib import Path
from types import SimpleNamespace

from social_bot.platforms.wechat.announcement import GroupAnnouncementModule


def main():
    if hasattr(sys.stdout,'reconfigure'):sys.stdout.reconfigure(encoding='utf-8')
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command',choices=['read','replace','append','revise'])
    parser.add_argument('--group',required=True)
    parser.add_argument('--text')
    parser.add_argument('--config',default=str(Path(__file__).parent/'config.json'))
    args=parser.parse_args()
    from social_bot.settings import load_settings
    settings=load_settings(args.config)
    if args.group not in settings.scope.groups:
        parser.error('目标群必须在配置白名单中')
    if args.command!='read' and not args.text:
        parser.error('写操作需显式提供 --text，revise 时填写修改要求')
    from social_bot.deepseek import DeepSeekClient
    module=GroupAnnouncementModule(task_client=DeepSeekClient(settings.api_key,settings.deepseek),
        backup_dir=Path(settings.runtime.database_path).parent/'announcement-backups')
    chat=SimpleNamespace(who=args.group)
    try:
        result=module.read(chat) if args.command=='read' else module.update(chat,operation=args.command,text=args.text)
    except Exception as exc:
        import traceback
        traceback.print_exc(file=sys.stderr)
        result=dict(success=False,reason=str(exc))
    print(json.dumps(result,ensure_ascii=False,indent=2))
    return 0 if result.get('success') else 1


if __name__=='__main__':
    raise SystemExit(main())
