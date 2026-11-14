import test from 'node:test';
import assert from 'node:assert/strict';
import {mkdtempSync, rmSync} from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import {MemoryPool} from './memory-pool.mjs';

test('disk pool survives restart and deduplicates pending and archived events by chat', () => {
  const dir = mkdtempSync(path.join(os.tmpdir(), 'qq-memory-'));
  try {
    const message = {kind:'group', chat_id:'g', message_id:'m', text:'original'};
    const first = new MemoryPool(dir);
    const file = first.put(message);
    const recovered = new MemoryPool(dir);
    assert.deepEqual(recovered.read(recovered.pending()[0]), message);
    assert.equal(recovered.put(message), null);
    recovered.done(file);
    assert.equal(recovered.pending().length, 0);
    assert.equal(recovered.put(message), null);
    assert.ok(recovered.put({...message, chat_id:'other'}));
  } finally { rmSync(dir, {recursive:true, force:true}); }
});
