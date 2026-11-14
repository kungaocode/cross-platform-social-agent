"""Small bounded wire reader for fields missing from the pinned upstream schemas.

Field numbers follow upstream dy_live/protobuf/proto/default/im_proto.proto.
No network requests or credential logging live here.
"""
from __future__ import annotations

import re


def fields(data):
    if not isinstance(data, bytes) or len(data) > 2_000_000:
        raise ValueError('invalid protobuf size')
    offset = 0

    def number():
        nonlocal offset
        value = 0
        for shift in range(0, 70, 7):
            if offset >= len(data):
                raise ValueError('truncated protobuf')
            byte = data[offset]
            offset += 1
            value |= (byte & 127) << shift
            if byte < 128:
                return value
        raise ValueError('oversized varint')

    result = {}
    count = 0
    while offset < len(data):
        tag = number()
        key, wire = tag >> 3, tag & 7
        if not key:
            raise ValueError('invalid field')
        if wire == 0:
            value = number()
        elif wire in (1, 2, 5):
            size = number() if wire == 2 else (8 if wire == 1 else 4)
            end = offset + size
            if end > len(data):
                raise ValueError('truncated field')
            value = data[offset:end]
            offset = end
        else:
            raise ValueError('unsupported wire type')
        result.setdefault(key, []).append(value)
        count += 1
        if count > 20000:
            raise ValueError('too many fields')
    return result


def single(row, key, default=None):
    values = row.get(key, [])
    if len(values) > 1:
        raise ValueError('ambiguous field')
    return values[0] if values else default


def conversation_rows(data, account):
    """Extract existing private conversation records, never create conversations."""
    found = {}
    budget = [0]

    def walk(raw, depth=0):
        budget[0] += 1
        if depth > 10 or budget[0] > 3000:
            return
        try:
            row = fields(raw)
        except ValueError:
            return
        conv = single(row, 1) if len(row.get(1, [])) == 1 else None
        if isinstance(conv, bytes) and re.fullmatch(rb'0:1:[1-9][0-9]{0,24}:[1-9][0-9]{0,24}', conv):
            conv = conv.decode('ascii')
            parts = conv.split(':')[2:]
            short, kind, ticket = single(row, 2), single(row, 3), single(row, 4)
            if (account in parts and len(set(parts)) == 2 and type(short) is int and short > 0
                    and kind == 1 and isinstance(ticket, bytes) and 0 < len(ticket) <= 8192):
                try:
                    ticket = ticket.decode('utf-8')
                except UnicodeError:
                    return
                uid = next(p for p in parts if p != account)
                peer = dict(uid=uid, conversation_id=conv, short_id=str(short), ticket=ticket)
                if uid in found and found[uid] != peer:
                    raise ValueError('ambiguous conversation')
                found[uid] = peer
                return
        for values in row.values():
            for value in values:
                if isinstance(value, bytes) and value:
                    walk(value, depth+1)

    walk(data)
    return list(found.values())


def send_receipt(data):
    row = fields(data)
    if single(row, 1) != 100 or single(row, 3, 0) != 0:
        raise ValueError('send rejected')
    body = fields(single(row, 6, b''))
    sent = fields(single(body, 100, b''))
    mid = single(sent, 1)
    if (type(mid) is not int or mid <= 0 or single(sent, 3, 0) != 0
            or single(sent, 5, 0) != 0 or single(sent, 8, 0) != 0):
        raise ValueError('send unconfirmed')
    return dict(accepted_by_server=True, server_message_id=str(mid))
