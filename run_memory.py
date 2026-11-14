"""Local memory administration. Rebuild creates a fresh generation, never imports legacy data."""
from __future__ import annotations

import argparse
import json
import shutil
import sqlite3
from datetime import datetime
from pathlib import Path

from social_bot.store import ConversationStore

ROOT = Path(__file__).resolve().parent


def rebuild(root):
    root = root.resolve()
    stamp = datetime.now().strftime('%Y%m%d-%H%M%S-%f')
    generation = root / ('generation-' + stamp)
    archive = root / 'backups' / stamp
    archive.mkdir(parents=True)
    generation.mkdir()
    configs = [ROOT / 'config.json', ROOT / 'qq/config.json', ROOT / 'douyin/config.json', ROOT / 'scheduler.json', ROOT / 'bilibili/config.json']
    documents = [json.loads(p.read_text(encoding='utf-8-sig')) for p in configs]
    sources = {ROOT / 'data/bot.sqlite3', Path(documents[0]['runtime']['database_path'])}
    for doc in [documents[1], documents[2], documents[4]]:
        old = Path(doc['memory_dir'])
        if old.is_dir():
            sources.update(old.glob('*.sqlite3'))
    backups = []
    for index, source in enumerate(sorted(sources)):
        if not source.is_file():
            continue
        target = archive / f'{index}-{source.name}'
        src = sqlite3.connect(source.resolve().as_uri() + '?mode=ro', uri=True)
        dst = sqlite3.connect(target)
        try:
            src.backup(dst)
            if dst.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
                raise RuntimeError('Backup integrity check failed')
        finally:
            dst.close()
            src.close()
        backups.append(str(target))
    for index, path in enumerate(configs):
        shutil.copy2(path, archive / f'config-{index}.json')
    # Initialize before switching paths. All memory tiers are in this transactional store.
    db = generation / 'wechat-default.sqlite3'
    store = ConversationStore(db)
    store.close()
    documents[0]['runtime']['database_path'] = db.as_posix()
    if documents[0]['runtime'].get('agent_memory_enabled'):
        from social_bot.agent_memory import AgentMemory
        shared=generation/'agent-memory.sqlite3'
        hub=AgentMemory(shared)
        hub.stats();hub.close()
        documents[0]['runtime']['agent_memory_path']=shared.as_posix()
    for doc in [documents[1], documents[2], documents[4]]:
        doc['memory_dir'] = generation.as_posix()
    documents[3]['database_path'] = (root / 'scheduler.sqlite3').as_posix()
    written = []
    try:
        for path, doc in zip(configs, documents):
            temp = path.with_suffix('.memory-tmp')
            temp.write_text(json.dumps(doc, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
            temp.replace(path)
            written.append(path)
    except Exception:
        for index, path in enumerate(configs):
            if path in written:
                shutil.copy2(archive / f'config-{index}.json', path)
        raise
    manifest = dict(generation=str(generation), archive=str(archive), backups=backups,
                    old_data_imported=False, database=str(db))
    if documents[0]['runtime'].get('agent_memory_enabled'):
        manifest['agent_memory_database']=documents[0]['runtime']['agent_memory_path']
    (root / 'current.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding='utf-8')
    return manifest


def main():
    parser = argparse.ArgumentParser(description='三层记忆库与持久入库池管理')
    parser.add_argument('command', choices=['rebuild', 'stats', 'drain', 'retry', 'recall', 'pin', 'forget'])
    parser.add_argument('--root', type=Path, default=Path('E:/memory_for_deepseek'))
    parser.add_argument('--database', type=Path)
    parser.add_argument('--session')
    parser.add_argument('--query', default='')
    parser.add_argument('--id', type=int)
    args = parser.parse_args()
    if args.command == 'rebuild':
        print(json.dumps(rebuild(args.root), ensure_ascii=False, indent=2))
        return
    path = args.database or Path(json.loads((ROOT / 'config.json').read_text(encoding='utf-8'))['runtime']['database_path'])
    if not path.is_file():
        parser.error('数据库不存在')
    from social_bot.settings import load_settings
    from dataclasses import replace
    runtime=load_settings(ROOT/'config.json').runtime
    if path.resolve().parent != Path(runtime.database_path).resolve().parent:
        runtime=replace(runtime,agent_memory_enabled=False)
    store = ConversationStore.from_runtime(runtime,database_path=path)
    try:
        if args.command == 'drain':
            while store.drain():
                pass
            result = store.memory_stats()
        elif args.command == 'retry':
            result = dict(retried=store.retry_failed(), **store.memory_stats())
        elif args.command == 'recall':
            if not args.session:
                parser.error('recall 需要 --session')
            result = store.recall(args.session, args.query)
        elif args.command == 'forget':
            if not args.session:
                parser.error('forget 需要 --session；清除此会话全部记忆和入库记录')
            store.clear(args.session)
            result = dict(forgotten=args.session)
        elif args.command == 'pin':
            if args.id is None:
                parser.error('pin 需要 --id')
            with store._lock,store.connection:
                count = store.connection.execute("UPDATE memories SET tier='core',importance=1 WHERE id=?", (args.id,)).rowcount
                store._queue_agent_pin(args.id)
            store.flush_agent_memory()
            result = dict(pinned=count)
        else:
            result = store.memory_stats()
        print(json.dumps(result, ensure_ascii=False, indent=2))
    finally:
        store.close()


if __name__ == '__main__':
    main()
