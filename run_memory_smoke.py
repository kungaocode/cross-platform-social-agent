"""Real DeepSeek recall check with synthetic data in a temporary, isolated database."""
import json
import tempfile
from dataclasses import replace
from pathlib import Path

from social_bot.service import BotService
from social_bot.settings import load_settings


def main():
    settings = load_settings('config.json')
    results = {}
    with tempfile.TemporaryDirectory() as folder:
        settings = replace(settings,
            scope=replace(settings.scope, private_users=frozenset({'memory-smoke'})),
            runtime=replace(settings.runtime, database_path=str(Path(folder)/'smoke.sqlite3'),
                            agent_memory_enabled=False,
                            max_context_messages=2, cooldown_seconds=0, tool_calling_enabled=False),
            deepseek=replace(settings.deepseek, max_tokens=120, system_prompt='根据本轮提供的历史资料回答。'))
        bot = BotService(settings)
        try:
            for platform in ('wechat', 'qq'):
                session = platform + ':private:memory-smoke'
                bot.store.add(session, 'user', '请记住：我的测试代号是 MEMORY-CHECK-7429', 'fact', sender_id='memory-smoke')
                for i in range(8):
                    bot.store.add(session, 'user', f'这是一条无关测试消息{i}', f'filler-{i}')
                result = bot.handle(dict(platform=platform, chat_type='private', chat_id='memory-smoke',
                                         sender_id='memory-smoke', message_id='question',
                                         text='我的测试代号是什么？只回答代号。'))
                results[platform] = dict(accepted=result['accepted'],
                    recalled='MEMORY-CHECK-7429' in result.get('reply', ''), reason=result.get('reason', ''))
        finally:
            bot.close()
    report = dict(isolated_database=True, live_model=True, sent_social_messages=0, results=results)
    Path('reports/memory-live-smoke.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if all(item['recalled'] for item in results.values()) else 1


if __name__ == '__main__':
    raise SystemExit(main())
