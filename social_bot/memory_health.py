"""Read-only checks of configured shared memory and durable replica queues."""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path


def _read(path):
    return json.loads(path.read_text(encoding='utf-8-sig'))


def _resolve(value, base):
    path = Path(value).expanduser()
    return (base/path).resolve() if not path.is_absolute() else path.resolve()


def _readonly(path):
    return sqlite3.connect(path.as_uri()+'?mode=ro', uri=True, timeout=5)


def check_memory(config_path):
    """Never initialize a missing database, import old data, or print message bodies."""
    config_path = Path(config_path).resolve()
    result = dict(ok=False, errors=[], warnings=[], platforms=[], sources=[])
    errors, warnings = result['errors'], result['warnings']
    try:
        raw = _read(config_path)
        runtime = raw['runtime']
        if runtime.get('agent_memory_enabled') is not True:
            errors.append('shared_memory_disabled')
        if not runtime.get('agent_memory_path'):
            errors.append('shared_memory_path_missing')
            return result
        shared = _resolve(runtime['agent_memory_path'], config_path.parent)
        primary = _resolve(runtime['database_path'], config_path.parent)
        result['shared_database'] = str(shared)
        result['retry_seconds'] = runtime.get('agent_memory_retry_seconds', 30)
        sources = {primary, primary.parent/'video-browser.sqlite3'}
        result['platforms'].append(dict(platform='wechat', shared_database=str(shared),
                                        enabled=raw.get('wechat', {}).get('enabled', False)))
        for platform in ('qq', 'douyin', 'bilibili'):
            cfg = config_path.parent/platform/'config.json'
            if not cfg.is_file():
                warnings.append(platform+':config_missing')
                continue
            try:
                data = _read(cfg)
                base = _resolve(data['base_config'], cfg.parent)
                inherited = _read(base)['runtime']
                inherited_path = _resolve(inherited['agent_memory_path'], base.parent)
                aligned = inherited.get('agent_memory_enabled') is True and inherited_path == shared
                result['platforms'].append(dict(platform=platform, shared_database=str(inherited_path),
                                                enabled=data.get('enabled', True), aligned=aligned))
                if not aligned:
                    errors.append(platform+':shared_config_mismatch')
                folder = _resolve(data['memory_dir'], cfg.parent)
                if not folder.is_dir():
                    errors.append(platform+':memory_dir_missing')
                sources.update(folder.glob(platform+'-*.sqlite3'))
            except (OSError, ValueError, KeyError, TypeError):
                errors.append(platform+':invalid_config')
        manifest = primary.parent.parent/'current.json'
        if manifest.is_file():
            current = _read(manifest)
            if current.get('agent_memory_database') and _resolve(current['agent_memory_database'],manifest.parent) != shared:
                errors.append('manifest_shared_path_mismatch')
            if current.get('generation') and _resolve(current['generation'],manifest.parent) != primary.parent:
                errors.append('manifest_generation_mismatch')
        if not shared.is_file():
            errors.append('shared_database_missing')
            return result
        hub = _readonly(shared)
        try:
            integrity = hub.execute('PRAGMA quick_check').fetchone()[0]
            result['shared'] = dict(integrity=integrity,
                events=hub.execute('SELECT COUNT(*) FROM agent_events').fetchone()[0],
                platforms=dict(hub.execute('SELECT platform,COUNT(*) FROM agent_events GROUP BY platform')),
                identities=hub.execute('SELECT COUNT(*) FROM agent_identities').fetchone()[0])
            if integrity != 'ok':
                errors.append('shared_integrity_failed')
            if not result['shared']['identities']:
                warnings.append('no_cross_platform_identity_links')
        finally:
            hub.close()
        for path in sorted(sources):
            if path == shared:
                errors.append('local_and_shared_database_are_same')
                continue
            if not path.is_file():
                if path == primary:
                    warnings.append('wechat:local_database_not_created')
                continue
            source = dict(database=str(path))
            result['sources'].append(source)
            db = _readonly(path)
            try:
                source['integrity'] = db.execute('PRAGMA quick_check').fetchone()[0]
                if source['integrity'] != 'ok':
                    errors.append(path.name+':integrity_failed')
                source['messages'] = db.execute('SELECT COUNT(*) FROM messages').fetchone()[0]
                tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                for table in ('memory_inbox', 'agent_outbox'):
                    if table in tables:
                        source[table] = dict(db.execute('SELECT state,COUNT(*) FROM '+table+' GROUP BY state'))
                        if any(state != 'done' and count for state,count in source[table].items()):
                            warnings.append(path.name+':'+table+'_unfinished')
                if 'agent_replica_meta' not in tables:
                    warnings.append(path.name+':shared_replica_not_initialized')
                    continue
                meta = dict(db.execute('SELECT key,value FROM agent_replica_meta'))
                db.execute('ATTACH DATABASE ? AS shared', (shared.as_uri()+'?mode=ro',))
                source['missing_shared_events'] = db.execute('''SELECT COUNT(*) FROM messages m
                    WHERE NOT EXISTS (SELECT 1 FROM shared.agent_events e WHERE e.event_key=? || m.id)''',
                    (meta['source_id']+':message:',)).fetchone()[0]
                if source['missing_shared_events']:
                    warnings.append(path.name+':messages_not_in_shared_memory')
            finally:
                db.close()
        result['ok'] = not errors
    except (OSError, ValueError, KeyError, TypeError, sqlite3.Error) as exc:
        # Only an error class: configuration or SQL errors can contain private strings.
        errors.append('health_check_failed:'+type(exc).__name__)
    return result
