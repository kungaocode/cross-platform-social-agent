"""Diagnose one configured vision request without exporting credentials or image data."""
import argparse
import hashlib
import json
import socket
import sys
import time
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlsplit

from social_bot import net
from social_bot.cache import configured_cache_root
from social_bot.modules.vision.module import QwenVisionAnalyzer
from social_bot.settings import load_settings
from social_bot.video_evidence import safe_code


def main():
    if hasattr(sys.stdout,'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8')
        sys.stderr.reconfigure(encoding='utf-8')
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--route',choices=['configured','direct'],default='direct')
    parser.add_argument('--image',type=Path)
    parser.add_argument('--timeout',type=int,default=45)
    parser.add_argument('--max-tokens',type=int,default=1400)
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args()
    path=args.image
    if path is None:
        path=next(iter(sorted(configured_cache_root().glob('deep-video-evidence-*/contact-sheet*.jpg'),
                              key=lambda p:p.stat().st_mtime,reverse=True)),None)
    if path is None:
        raise SystemExit('No captured video contact sheet was found.')
    settings=load_settings(Path(__file__).resolve().parent/'config.json')
    vision=QwenVisionAnalyzer.from_settings(settings.multimodal)
    vision.timeout_seconds=args.timeout;vision.max_tokens=args.max_tokens
    from PIL import Image
    with Image.open(path) as picture:
        dimensions=picture.size
    report=dict(route='direct',model=vision.model,endpoint_host=urlsplit(vision.base_url).hostname,
        timeout_seconds=args.timeout,max_tokens=args.max_tokens,image_bytes=path.stat().st_size,
        image_sha256=hashlib.sha256(path.read_bytes()).hexdigest(),image_dimensions=list(dimensions),
        credential_values_exported=False)
    report['project_proxy_enabled']=False
    original=net.urlopen
    old_direct=net._direct
    began=time.monotonic()

    @contextmanager
    def traced(request,timeout):
        report['phase']='opening_request'
        start=time.monotonic()
        response=original(request,timeout=timeout,prefer_direct=args.route=='direct')
        report['headers_after_seconds']=round(time.monotonic()-start,3)
        report['http_status']=response.status
        class Reader:
            def read(self,*arguments):
                report['phase']='reading_response_body'
                started=time.monotonic()
                raw=response.read(*arguments)
                report['body_read_seconds']=round(time.monotonic()-started,3)
                data=json.loads(raw)
                choices=data.get('choices') or []
                report['finish_reason']=choices[0].get('finish_reason') if choices else None
                report['response_bytes']=len(raw)
                report['usage']=data.get('usage')
                report['phase']='response_read'
                return raw
        with response:
            yield Reader()
    def network(request,timeout):
        report['scheduler_wait_seconds']=round(time.monotonic()-began,3)
        started=time.monotonic()
        try:
            return old_direct(request,timeout)
        finally:
            report['network_to_headers_seconds']=round(time.monotonic()-started,3)
    try:
        with patch.object(net,'urlopen',traced),patch.object(net,'_direct',network):
            text=vision(str(path),'图中六格是同一视频不同时间的真实采样。请按时间概述可见内容，不超过450字。没有音频，不推测声音。')
        report.update(success=True,text_characters=len(text),excerpt=text[:180])
    except Exception as exc:
        report.update(success=False,reason=safe_code(exc),exception_type=type(exc).__name__,
            cause_type=type(exc.__cause__).__name__ if exc.__cause__ else None)
    report['elapsed_seconds']=round(time.monotonic()-began,3)
    args.output.parent.mkdir(parents=True,exist_ok=True)
    args.output.write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    print(json.dumps(report,ensure_ascii=False),flush=True)


if __name__=='__main__':
    main()
