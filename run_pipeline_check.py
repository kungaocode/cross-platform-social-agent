"""Run the complete offline pipeline check. Never loads production keys or config."""
from __future__ import annotations

import importlib
import json
import os
import sys
import tempfile
from unittest.mock import patch
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT=Path(__file__).resolve().parent


def main():
    os.chdir(ROOT)
    suite=unittest.defaultTestLoader.discover(str(ROOT/'tests'),pattern='test_pipeline_logic.py')
    with tempfile.TemporaryDirectory(prefix='pipeline-cache-') as cache_folder:
        with patch.dict(os.environ,{'SOCIAL_BOT_CACHE_DIR':str(Path(cache_folder)/'cache')}):
            result=unittest.TextTestRunner(verbosity=2).run(suite)
    traces=importlib.import_module('test_pipeline_logic').PIPELINE_TRACES
    report=dict(checked_at=datetime.now(timezone(timedelta(hours=8))).isoformat(),
        offline=True,production_config_loaded=False,production_memory_written=False,
        external_model_requests=0,social_messages_sent=0,
        model_responses='scripted local HTTP server',
        real_components=['BotService','DeepSeekClient HTTP serialization','SQLite inbox/history/recall',
                         'shared request scheduler','WeChat tools and TextReplyModule',
                         'QQ Node bridge and Python worker','FFmpeg MP4 probe','VideoAgentTools'],
        simulated_components=['Flash and Chat response content','Qwen perception response',
                              'external web/video contents','browser video observations','social send endpoints'],
        tests_run=result.testsRun,failures=len(result.failures),errors=len(result.errors),
        passed=result.wasSuccessful(),cases=traces,
        known_limit='Prepared handoffs can resume final Chat without replaying tools; unknown external sends or unfinished tool operations require review.')
    output=ROOT/'reports/pipeline-offline-check.json'
    output.parent.mkdir(exist_ok=True)
    output.write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    print('离线链路报告：'+str(output))
    return 0 if result.wasSuccessful() else 1


if __name__=='__main__':
    raise SystemExit(main())
