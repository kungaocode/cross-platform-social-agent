"""Create ignored local configuration from public templates without overwriting files."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def initialize(root: Path, memory: Path):
    memory.mkdir(parents=True, exist_ok=True)
    created = []

    def save(destination, data):
        path = root / destination
        if path.exists():
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open('x', encoding='utf-8') as stream:
            stream.write(json.dumps(data, ensure_ascii=False, indent=2) + '\n')
        created.append(destination)

    config = json.loads((root/'config.example.json').read_text(encoding='utf-8-sig'))
    config['runtime'].update(database_path=str(memory/'wechat-default.sqlite3'),
                             agent_memory_path=str(memory/'agent-memory.sqlite3'),
                             research_duckduckgo_profile=str(memory/'duckduckgo-browser-profile'))
    save('config.json', config)
    for platform in ('qq', 'bilibili', 'douyin'):
        data = json.loads((root/platform/'config.example.json').read_text(encoding='utf-8-sig'))
        data['memory_dir'] = str(memory)
        save(platform+'/config.json', data)
    api = json.loads((root/'douyin/config.api.example.json').read_text(encoding='utf-8-sig'))
    api['memory_dir'] = str(memory)
    save('douyin/config.api.local.json', api)
    scheduler = json.loads((root/'scheduler.example.json').read_text(encoding='utf-8'))
    scheduler['database_path'] = str(memory.parent/'scheduler.sqlite3')
    save('scheduler.json', scheduler)
    for platform in ('qq', 'bilibili'):
        data = json.loads((root/platform/'credentials.example.json').read_text(encoding='utf-8'))
        save('private/accounts/'+platform+'/credentials.json', data)
    search = json.loads((root/'search_credentials.example.json').read_text(encoding='utf-8'))
    save('private/secrets/search_credentials.json', search)
    for name in ('api_deepseek.txt', 'api_qwen.txt', 'api_audio.txt', 'weather_api.txt'):
        path = root/'private/secrets'/name
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.exists():
            with path.open('x', encoding='utf-8'):
                pass
            created.append('private/secrets/'+name)
    return created


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--memory-dir', type=Path, default=ROOT/'data/memory/current')
    args = parser.parse_args()
    created = initialize(ROOT, args.memory_dir.expanduser().resolve())
    print(f'Created {len(created)} local files; existing files were preserved.')
    print('Fill private/secrets and private/accounts, then configure allowlists before starting listeners.')


if __name__ == '__main__':
    main()
