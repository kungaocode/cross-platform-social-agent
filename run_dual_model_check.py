"""Live dual-model protocol check with synthetic tools; no social sends or memory writes."""
import json
import os
import re
import time
from dataclasses import replace
from pathlib import Path

from social_bot.deepseek import DeepSeekClient
from social_bot.settings import load_settings


def main():
    root=Path(__file__).resolve().parent
    os.chdir(root)
    app=load_settings(root/'config.json')
    settings=replace(app.deepseek,max_tokens=160,task_max_tokens=600,
                     timeout_seconds=40,task_timeout_seconds=60)
    client=DeepSeekClient(app.api_key,settings)
    requests=[]
    original=client._request
    def traced(payload):
        item=dict(requested_model=payload.get('model',settings.model),has_tools=bool(payload.get('tools')))
        requests.append(item)
        start=time.monotonic()
        try:
            data=original(payload)
            item.update(success=True,response_model=data.get('model'))
            return data
        except Exception as exc:
            code=re.search(r'HTTP (\d+)',str(exc))
            item.update(success=False,error='HTTP_'+code[1] if code else type(exc).__name__)
            raise
        finally:
            item['seconds']=round(time.monotonic()-start,2)
    client._request=traced
    executed=[]
    def execute(name,arguments):
        executed.append(name)
        return dict(success=True,test_only=True,value='7429',source='synthetic_local_fixture')
    report=dict(collaboration_enabled=settings.collaboration_enabled,
                chat_model=settings.model,task_model=settings.task_model,
                social_messages_sent=0,memory_written=False)
    try:
        result=client.complete_with_tools([
            dict(role='system',content='这是协议测试。只回答工具返回的测试值，不执行任何外部操作。'),
            dict(role='user',content='查找测试值，最终只回答该值。')],
            [dict(type='function',function=dict(name='test_lookup',description='读取本地合成测试值',
                parameters=dict(type='object',properties={},additionalProperties=False)))],execute,
            tool_choice=dict(type='function',function=dict(name='test_lookup')))
        report.update(passed='7429' in result.content and executed==['test_lookup']
                      and requests[-1]['requested_model']=='deepseek-chat' and not requests[-1]['has_tools'],
                      tool_executions=len(executed),final_contains_test_value='7429' in result.content)
    except Exception:
        report.update(passed=False,tool_executions=len(executed))
    report['requests']=requests
    output=root/'reports/dual-model-live-check.json'
    output.parent.mkdir(exist_ok=True)
    output.write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    print(json.dumps(report,ensure_ascii=False,indent=2))
    return 0 if report['passed'] else 1


if __name__=='__main__':
    raise SystemExit(main())
