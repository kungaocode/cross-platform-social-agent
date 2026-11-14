import {mkdirSync, existsSync, openSync, writeFileSync, fsyncSync, closeSync, renameSync, readdirSync, readFileSync} from 'node:fs';
import {createHash} from 'node:crypto';
import path from 'node:path';

// Disk-backed admission before the model queue. Recovery archives, never resends.
export class MemoryPool {
  constructor(directory) { this.directory = directory; mkdirSync(directory, {recursive: true}); }
  put(message) {
    const key = createHash('sha256').update(JSON.stringify([message.kind, message.chat_id, message.message_id])).digest('hex');
    const file = path.join(this.directory, key + '.pending.json');
    if (existsSync(file) || existsSync(file.replace('.pending.json', '.done.json'))) return null;
    const temp = file + '.tmp';
    const fd = openSync(temp, 'w');
    try { writeFileSync(fd, JSON.stringify(message)); fsyncSync(fd); } finally { closeSync(fd); }
    renameSync(temp, file);
    return file;
  }
  pending() { return readdirSync(this.directory).filter(f => f.endsWith('.pending.json')).sort().map(f => path.join(this.directory, f)); }
  read(file) { return JSON.parse(readFileSync(file, 'utf8')); }
  done(file) { renameSync(file, file.replace('.pending.json', '.done.json')); }
}
