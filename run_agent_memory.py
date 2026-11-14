"""Administer the four-platform shared memory, without sending social messages."""
from __future__ import annotations

import argparse
import json
import shutil
import sqlite3
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

from social_bot.agent_memory import AgentMemory
from social_bot.store import ConversationStore
from social_bot.modules.memory_recall import MemoryRecallModule

ROOT = Path(__file__).resolve().parent


def account_for(path):
    for prefix in ('wechat-','qq-','douyin-','bilibili-'):
        if path.stem.startswith(prefix):
            return path.stem[len(prefix):]
    return 'default'


def active_sources(config_path):
    root = config_path.parent
    raw = json.loads(config_path.read_text(encoding='utf-8-sig'))
    primary = Path(raw['runtime']['database_path'])
    if not primary.is_absolute():
        primary = root/primary
    paths = {primary.resolve()}
    for app in ('qq','douyin','bilibili'):
        cfg = root/app/'config.json'
        if not cfg.is_file():
            continue
        data = json.loads(cfg.read_text(encoding='utf-8-sig'))
        folder = Path(data['memory_dir'])
        if not folder.is_absolute():
            folder = cfg.parent/folder
        paths.update(p.resolve() for p in folder.glob(app+'-*.sqlite3'))
    paths.add(primary.parent/'video-browser.sqlite3')
    return sorted(p for p in paths if p.is_file())


def sync_sources(sources, shared):
    result=[]
    for path in sources:
        if path.resolve()==shared.resolve():
            continue
        store=ConversationStore(path,agent_memory_path=shared,account_id=account_for(path))
        try:
            store.drain()
            store.flush_agent_memory()
            pending=store.connection.execute("SELECT COUNT(*) FROM agent_outbox WHERE state='pending'").fetchone()[0]
            result.append(dict(source=str(path),pending=pending))
            if pending:
                raise RuntimeError('共享记忆同步未完成，本地 outbox 已保留')
        finally:
            store.close()
    return result


def write_json(path, data):
    temp=path.with_name(path.name+'.agent-memory.tmp')
    temp.write_text(json.dumps(data,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    temp.replace(path)


def enable(config_path, shared=None):
    config_path=Path(config_path).resolve()
    raw=json.loads(config_path.read_text(encoding='utf-8-sig'))
    sources=active_sources(config_path)
    primary=Path(raw['runtime']['database_path'])
    if not primary.is_absolute():
        primary=config_path.parent/primary
    shared=Path(shared).resolve() if shared else primary.parent/'agent-memory.sqlite3'
    stamp=datetime.now(timezone(timedelta(hours=8))).strftime('%Y%m%d-%H%M%S-%f')
    backup=primary.parent.parent/'backups'/('agent-unify-'+stamp)
    backup.mkdir(parents=True)
    shutil.copy2(config_path,backup/'config.json')
    manifest=primary.parent.parent/'current.json'
    if manifest.is_file():
        shutil.copy2(manifest,backup/'current.json')
    snapshots=set(sources)
    if shared.is_file():
        snapshots.add(shared)
    for index,path in enumerate(sorted(snapshots)):
        source=sqlite3.connect(path.resolve().as_uri()+'?mode=ro',uri=True)
        target=sqlite3.connect(backup/f'{index}-{path.name}')
        try:
            source.backup(target)
            if target.execute('PRAGMA integrity_check').fetchone()[0]!='ok':
                raise RuntimeError('记忆备份完整性检查失败')
        finally:
            source.close();target.close()
    hub=AgentMemory(shared)
    try:
        hub.stats()
        synced=sync_sources(sources,shared)
        raw['runtime'].update(agent_memory_enabled=True,agent_memory_path=shared.as_posix(),memory_account_id=raw['runtime'].get('memory_account_id','default'))
        try:
            write_json(config_path,raw)
            if manifest.is_file():
                current=json.loads(manifest.read_text(encoding='utf-8'))
                current['agent_memory_database']=shared.as_posix()
                write_json(manifest,current)
        except Exception:
            shutil.copy2(backup/'config.json',config_path)
            if (backup/'current.json').is_file():
                shutil.copy2(backup/'current.json',manifest)
            raise
        return dict(enabled=True,database=str(shared),backup=str(backup),sources=synced,
                    imported_archived_generations=False,shared=hub.stats())
    finally:
        hub.close()


def main():
    if hasattr(sys.stdout,'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8')
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command',choices=['enable','stats','health','sync','recall','link','unlink','identities','endpoints','pin'])
    parser.add_argument('--config',type=Path,default=ROOT/'config.json')
    parser.add_argument('--shared-database',type=Path)
    parser.add_argument('--database',type=Path,action='append')
    parser.add_argument('--query',default='')
    parser.add_argument('--time-range',choices=['auto','recent','long_term'],default='auto')
    parser.add_argument('--platform',default='wechat')
    parser.add_argument('--account',default='default')
    parser.add_argument('--sender',default='')
    parser.add_argument('--person',default='')
    parser.add_argument('--id',type=int)
    args=parser.parse_args()
    config=args.config.resolve()
    if args.command=='health':
        from social_bot.memory_health import check_memory
        result=check_memory(config)
    elif args.command=='enable':
        result=enable(config,args.shared_database)
    else:
        raw=json.loads(config.read_text(encoding='utf-8-sig'))
        path=args.shared_database or raw['runtime'].get('agent_memory_path')
        if not path:
            parser.error('尚未配置共享记忆库，先运行 enable')
        hub=AgentMemory(path)
        try:
            if args.command=='sync':
                result=dict(sources=sync_sources(args.database or active_sources(config),Path(path)),shared=hub.stats())
            elif args.command=='recall':
                result=MemoryRecallModule(hub,current=dict(platform=args.platform,account=args.account,sender_id=args.sender)).recall(args.query,time_range=args.time_range)
            elif args.command in {'link','unlink'}:
                if not args.sender or (args.command=='link' and not args.person):
                    parser.error('身份操作需要 --sender，关联还需要 --person')
                if args.command=='link':
                    hub.link_identity(args.platform,args.account,args.sender,args.person)
                else:
                    hub.unlink_identity(args.platform,args.account,args.sender)
                result=hub.identities()
            elif args.command=='identities':
                result=hub.identities()
            elif args.command=='endpoints':
                with hub._lock:
                    result=[dict(platform=p,account=a,sender_id=s,sender_name=n) for p,a,s,n in hub._connect().execute(
                        "SELECT platform,account,sender_id,MAX(sender_name) FROM agent_events WHERE role='user' GROUP BY platform,account,sender_id LIMIT 200")]
            elif args.command=='pin':
                if args.id is None:
                    parser.error('pin 需要 --id')
                result=dict(pinned=hub.pin(args.id))
            else:
                result=hub.stats()
        finally:
            hub.close()
    print(json.dumps(result,ensure_ascii=False,indent=2))
    if args.command=='health' and not result['ok']:
        raise SystemExit(1)


if __name__=='__main__':
    main()
