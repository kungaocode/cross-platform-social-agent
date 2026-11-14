"""Check Git index/HEAD for private paths, common secret formats and known local values.

Only locations and finding categories are printed. This is a publication guard,
not a replacement for review or GitHub secret scanning.
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BLOCKED = re.compile(
    r'(^|/)(private|data|reports|picture|node_modules|\.venv|__pycache__)(/|$)'
    r'|(^|/)(config\.json|config\.api\.local\.json|credentials\.json|credentials-api\.json|scheduler\.json)$'
    r'|(^|/)(api[^/]*\.txt|weather_api[^/]*|search_api[^/]*|search_credentials\.json|\.env(?:\..*)?)$'
    r'|\.(bak|backup|sqlite\d*(?:-\w+)?|db(?:-\w+)?|log|har|pem|key|p12|pfx|mp4|zip)$', re.I)
PATTERNS = {
    'github_token': re.compile(r'\b(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{40,})'),
    'api_key': re.compile(r'\bsk-[A-Za-z0-9_-]{20,}'),
    'private_key': re.compile(r'-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----\s+[A-Za-z0-9+/]{24,}'),
    'signed_media_url': re.compile(r'https?://[^\s\x22\x27<>]+[?&](?:X-Amz-Signature|x-signature|msToken)=[A-Za-z0-9_%+-]{20,}', re.I),
    'personal_home_path': re.compile(r'[A-Z]:[/\\]Users[/\\](?!test|example|user[/\\])[^/\\\s\x22\x27]+[/\\]', re.I),
}


def git(*args):
    return subprocess.check_output(['git', '-C', str(ROOT), *args])


def local_values():
    values = set()
    sensitive = re.compile(r'key|secret|cookie|token|ticket|(?:^|_)uid$|(?:^|_)id$|private_users|groups|peers|owner_name|targets', re.I)

    def collect(data, key='', force=False):
        if isinstance(data, dict):
            for name, value in data.items():
                collect(value, name, force)
        elif isinstance(data, list):
            for value in data:
                collect(value, key, force)
        elif isinstance(data, str) and (force or sensitive.search(key)) and len(data) >= 6:
            if key.endswith(('_env', '_file', '_path', '_directory')):
                return
            if data not in {'default', 'anonymous'} and not data.startswith(('http:', 'https:', 'data/')):
                values.add(data)
            if key.lower() == 'cookie':
                for pair in data.split(';'):
                    if '=' in pair:
                        value = pair.split('=', 1)[1].strip()
                        if len(value) >= 12:
                            values.add(value)

    files = [ROOT/'config.json', *(ROOT/p/'config.json' for p in ('qq','bilibili','douyin')),
             ROOT/'douyin/config.api.local.json']
    config_files = set(files)
    for directory in ('private/secrets', 'private/accounts'):
        path = ROOT/directory
        if path.exists():
            files.extend(p for p in path.rglob('*') if p.is_file())
    # Inspect exactly the configured external account credential file, without copying it.
    api_config = ROOT/'douyin/config.api.local.json'
    if api_config.exists():
        data = json.loads(api_config.read_text(encoding='utf-8-sig'))
        candidate = Path(data.get('api_backend', {}).get('credentials_file') or
                         Path(data['memory_dir'])/('douyin-api-'+data['account_id']+'.json'))
        files.append(candidate if candidate.is_absolute() else api_config.parent/candidate)
    for path in files:
        if not path.is_file() or path.stat().st_size > 1_000_000:
            continue
        text = path.read_text(encoding='utf-8-sig', errors='replace')
        if path.suffix == '.json':
            try:
                collect(json.loads(text), force=path not in config_files)
            except ValueError:
                pass
        else:
            for value in text.splitlines():
                value = value.strip()
                if re.fullmatch(r'[A-Za-z0-9_.=+-]{20,}', value):
                    values.add(value)
                values.update(re.findall(r'\bsk-[A-Za-z0-9_-]{16,}', value))
    return values


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--head', action='store_true', help='Inspect committed HEAD instead of index')
    args = parser.parse_args()
    paths = git('ls-tree', '-r', '--name-only', '-z', 'HEAD') if args.head else git('ls-files', '-z')
    known = local_values()
    findings = []
    count = 0
    for raw in paths.split(b'\0'):
        if not raw:
            continue
        path = raw.decode('utf-8')
        count += 1
        if BLOCKED.search(path) or (path.startswith('third_party/') and path != 'third_party/README.md'):
            findings.append((path, 0, 'private_or_runtime_path'))
        spec = ('HEAD:' if args.head else ':') + path
        content = git('show', spec)
        if b'\0' in content:
            findings.append((path, 0, 'unreviewed_binary'))
            continue
        text = content.decode('utf-8', errors='replace')
        for category, pattern in PATTERNS.items():
            for match in pattern.finditer(text):
                # Explicit fake keys used by automated fixtures are not credentials.
                if category == 'api_key' and any(word in match.group().lower() for word in ('test', 'placeholder', 'example', 'offline')):
                    continue
                findings.append((path, text.count('\n', 0, match.start())+1, category))
        for value in known:
            offset = text.find(value)
            if offset >= 0:
                findings.append((path, text.count('\n', 0, offset)+1, 'known_local_value'))
    for path, line, category in sorted(set(findings)):
        print(f'{path}:{line}: {category}')
    print(json.dumps({'files_checked': count, 'findings': len(set(findings))}))
    return 1 if findings else 0


if __name__ == '__main__':
    raise SystemExit(main())
