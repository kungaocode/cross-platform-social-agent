"""Persist verified private push events before invoking the existing responder."""
from __future__ import annotations

import json
import math
import time
import uuid

from .backend import DouyinAPIError, positive
from ..common.chat import SocialEvent, SocialResponder


def peer_bindings(credentials, account):
    if not positive(account):
        raise DouyinAPIError('DOUYIN_ACCOUNT_UNCONFIRMED')
    rows = credentials.get('peers', [])
    if not isinstance(rows, list) or len(rows) > 50:
        raise DouyinAPIError('DOUYIN_PEERS_INVALID')
    peers = {}
    for peer in rows:
        if not isinstance(peer, dict):
            raise DouyinAPIError('DOUYIN_PEERS_INVALID')
        uid, conv = positive(peer.get('uid')), peer.get('conversation_id', '')
        parts = conv.split(':') if isinstance(conv, str) else []
        if (not uid or uid == account or uid in peers or len(parts) != 4 or parts[:2] != ['0', '1']
                or set(parts[2:]) != {uid, account} or not positive(peer.get('short_id'))):
            raise DouyinAPIError('DOUYIN_PEERS_INVALID')
        if 'ticket' in peer and (not isinstance(peer['ticket'], str) or not 1 <= len(peer['ticket']) <= 8192):
            raise DouyinAPIError('DOUYIN_PEERS_INVALID')
        peers[uid] = dict(peer, uid=uid, short_id=str(peer['short_id']))
    return peers


def push_event(row, account, peers, started_at):
    if not isinstance(row, dict):
        return None
    uid, mid = positive(row.get('sender')), positive(row.get('server_message_id'))
    peer = peers.get(uid)
    if (not peer or not mid or row.get('conversation_id') != peer['conversation_id']
            or str(row.get('conversation_short_id')) != peer['short_id']
            or row.get('conversation_type') not in (1, '1') or row.get('message_type') not in (7, '7')):
        return None
    try:
        stamp = float(row.get('create_time', 0))
        if stamp > 1e12:
            stamp /= 1000
        if not math.isfinite(stamp) or stamp < started_at or stamp > time.time()+60:
            return None
        payload = json.loads(row.get('content', ''))
    except (TypeError, ValueError):
        return None
    text = payload.get('text') if isinstance(payload, dict) else None
    if not isinstance(text, str) or not text.strip() or len(text) > 8000:
        return None
    return SocialEvent(platform='douyin', account=account, channel='private', id=mid,
        sender=uid, text=text, thread=peer['conversation_id'], sender_name=str(peer.get('name', ''))[:80],
        created_at=stamp)


class APIInboxPump:
    def __init__(self, api, service, config, credentials, *, permitted=lambda: True, reader=None):
        self.api, self.permitted = api, permitted
        self.video_commands = None
        self.started = int(time.time())
        self.peers = peer_bindings(credentials, api.uid)
        if not self.peers:
            raise DouyinAPIError('DOUYIN_API_PEERS_REQUIRED')
        self.responder = SocialResponder(service, 'douyin', api.uid, config=config, reader=reader, permitted=permitted)
        self.responder.set_private_peers(self.peers)
        self.db = service.store.connection
        with self.db:
            self.db.execute('CREATE TABLE IF NOT EXISTS douyin_api_inbox (event_key TEXT PRIMARY KEY, payload TEXT NOT NULL)')
        # Subscribe only after the scope and durable store are ready.
        self.api.call('listen')

    def step(self):
        rows = self.api.call('drain')
        with self.db:
            for row in rows:
                event = push_event(row, self.api.uid, self.peers, self.started)
                if event:
                    payload = dict(event.__dict__)
                    self.db.execute('INSERT OR IGNORE INTO douyin_api_inbox VALUES (?,?)',
                                    (event.key, json.dumps(payload, ensure_ascii=False)))
        queued = self.db.execute('SELECT payload FROM douyin_api_inbox i WHERE NOT EXISTS '
            '(SELECT 1 FROM social_deliveries d WHERE d.event_key=i.event_key) ORDER BY i.rowid LIMIT 5').fetchall()
        for row in queued:
            if not self.permitted():
                break
            event = SocialEvent(**json.loads(row[0]))
            if self.video_commands and self.video_commands.handle(event):
                continue
            self.responder.handle(event, self.send)
        if self.video_commands and self.permitted():
            self.video_commands.poll()

    def send(self, event, text):
        peer = self.peers.get(event.sender)
        if not peer or event.thread != peer['conversation_id'] or not self.permitted():
            raise DouyinAPIError('DOUYIN_PRIVATE_TARGET_CHANGED', before_write=True)
        if not text.strip() or len(text) > 1000:
            raise DouyinAPIError('DOUYIN_PRIVATE_TEXT_INVALID', before_write=True)
        receipt = self.api.call('send', dict(peer=peer, text=text,
            client_message_id=str(uuid.uuid5(uuid.NAMESPACE_URL, 'douyin-reply:'+event.key))))
        if receipt.get('accepted_by_server') is not True:
            raise DouyinAPIError('DOUYIN_SEND_RESULT_UNCONFIRMED')
