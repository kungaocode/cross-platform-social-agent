"""Test the user's DeepSeek Flash SDK example and a plain deepseek-chat request.

Only synthetic Hello messages; API keys and reasoning text are never saved.
"""
from __future__ import annotations

import json
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor
from importlib.metadata import version
from pathlib import Path

from openai import OpenAI
import httpx

from social_bot.scheduler import configured_pool
from social_bot.settings import load_settings

ROOT = Path(__file__).resolve().parent


def safe(value, key):
    value = str(value).replace(key, '[REDACTED]') if key else str(value)
    return re.sub(r'sk-[A-Za-z0-9_-]+', '[REDACTED]', value)[:600]


def check(model, key):
    record = dict(requested_model=model, sdk='openai', base_url='https://api.deepseek.com',
                  endpoint='/chat/completions', automatic_retries=0)
    options = dict(model=model, messages=[
        {'role':'system','content':'You are a helpful assistant'},
        {'role':'user','content':'Hello'}], stream=False)
    if model == 'deepseek-flash':
        options.update(reasoning_effort='high',extra_body={'thinking':{'type':'enabled'}})
        record.update(thinking='enabled',reasoning_effort='high')
    else:
        record['thinking']='not_specified'
    pool = configured_pool()
    lease = pool.acquire() if pool else None
    started = time.monotonic()
    try:
        with OpenAI(api_key=key,base_url='https://api.deepseek.com',timeout=60,max_retries=0,
                    http_client=httpx.Client(trust_env=False, timeout=60)) as client:
            response = client.chat.completions.create(**options)
        message = response.choices[0].message
        content = message.content or ''
        record.update(success=bool(content.strip()), response_model=response.model,
            finish_reason=response.choices[0].finish_reason, content_preview=content[:200],
            has_reasoning=bool(getattr(message,'reasoning_content',None)),
            usage=response.usage.model_dump() if response.usage else None)
    except Exception as exc:
        body = getattr(exc,'body',None)
        error = body.get('error',body) if isinstance(body,dict) else {}
        response = getattr(exc,'response',None)
        record.update(success=False,exception=type(exc).__name__,status_code=getattr(exc,'status_code',None),
            provider_code=safe(error.get('code',''),key) if isinstance(error,dict) else '',
            provider_message=safe(error.get('message',str(exc)),key) if isinstance(error,dict) else safe(str(exc),key),
            cause=type(exc.__cause__).__name__ if exc.__cause__ else None,
            response_content_type=response.headers.get('content-type') if response is not None else None)
    finally:
        record['seconds']=round(time.monotonic()-started,2)
        if lease:
            lease.close()
    print(json.dumps(record,ensure_ascii=False),flush=True)
    return record


def main():
    os.chdir(ROOT)
    settings = load_settings(ROOT/'config.json')
    key = os.environ.get('DEEPSEEK_API_KEY') or settings.api_key
    report = dict(openai_sdk_version=version('openai'), key_source='environment' if os.environ.get('DEEPSEEK_API_KEY') else 'local_config',
                  social_messages_sent=0,memory_written=False)
    with ThreadPoolExecutor(max_workers=2) as pool:
        report['tests'] = list(pool.map(lambda model: check(model,key),('deepseek-flash','deepseek-chat')))
    report['passed']=all(item['success'] for item in report['tests'])
    output = ROOT/'reports/deepseek-sdk-check.json'
    output.parent.mkdir(exist_ok=True)
    output.write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    print('Report: '+str(output),flush=True)
    return 0 if report['passed'] else 1


if __name__=='__main__':
    raise SystemExit(main())
