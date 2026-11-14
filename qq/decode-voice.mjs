// Local SILK -> mono 16 kHz WAV. Paths stay local; no network or shell execution.
import {readFileSync, writeFileSync} from 'node:fs';
import {loadSilkWasm, stripAmrHeader, pcmToWav} from '@tencent-connect/qqbot-nodejs/protocol';

try {
  const silk = await loadSilkWasm();
  const bytes = stripAmrHeader(readFileSync(process.argv[2]));
  if (!silk || !silk.isSilk(bytes)) throw Error('unsupported_voice');
  const decoded = await silk.decode(bytes, 16000);
  if (!decoded.data.length || decoded.data.length > 16000 * 2 * 180) throw Error('voice_too_long');
  writeFileSync(process.argv[3], pcmToWav(decoded.data, 16000));
} catch {
  console.error('voice_decode_failed');
  process.exitCode = 1;
}
