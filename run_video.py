"""本地 MP4 理解测试，使用 api_audio 凭据，不发送聊天消息。"""
import argparse
import json
import logging
from pathlib import Path
from social_bot.settings import load_settings
from social_bot.modules.video import OmniVideoAnalyzer, VideoError

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('path', type=Path)
    parser.add_argument('--question', default='请概述视频内容、主要事件、可见文字和可辨识声音。')
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(message)s')
    root = Path(__file__).resolve().parent
    import os
    os.chdir(root)
    config = json.loads((root/'config.json').read_text(encoding='utf-8'))
    analyzer = OmniVideoAnalyzer.from_settings(load_settings(root/'config.json'), config.get('video'))
    if analyzer is None: raise SystemExit('视频模型凭据未配置')
    result = analyzer.analyze_path(args.path, args.question)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2)+'\n', encoding='utf-8')
    print(json.dumps(result, ensure_ascii=False, indent=2))

if __name__ == '__main__':
    try: main()
    except VideoError as exc: raise SystemExit('视频处理失败：'+str(exc))
