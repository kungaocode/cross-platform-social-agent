"""Inspect handoffs and retry final Chat only; never sends to social platforms."""
import argparse
import json
from pathlib import Path
from social_bot.service import BotService
from social_bot.settings import load_settings


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command',choices=['list','retry'])
    parser.add_argument('--id')
    parser.add_argument('--config',default=str(Path(__file__).resolve().parent/'config.json'))
    parser.add_argument('--database',type=Path)
    args=parser.parse_args()
    settings=load_settings(args.config)
    if args.database:
        from dataclasses import replace
        settings=replace(settings,runtime=replace(settings.runtime,database_path=str(args.database.resolve()),
                         agent_memory_enabled=settings.runtime.agent_memory_enabled and args.database.resolve().parent==Path(settings.runtime.database_path).resolve().parent))
    if not Path(settings.runtime.database_path).is_file():parser.error('数据库不存在')
    bot=BotService(settings)
    try:
        if args.command=='list':
            fields=['id','session_id','state','delivery_state','updated']
            result=[dict(zip(fields,row)) for row in bot.store.connection.execute('SELECT j.id,j.session_id,j.state,d.state,j.updated FROM reply_jobs j LEFT JOIN reply_deliveries d ON d.job_id=j.id ORDER BY j.updated DESC LIMIT 100')]
        else:
            if not args.id:parser.error('retry 需要 --id')
            result=bot.retry_reply(args.id)
        print(json.dumps(result,ensure_ascii=False,indent=2))
    finally:bot.close()


if __name__=='__main__':main()
