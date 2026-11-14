import test from 'node:test';
import assert from 'node:assert/strict';
import {mkdtempSync, writeFileSync, readFileSync, rmSync} from 'node:fs';
import {tmpdir} from 'node:os';
import path from 'node:path';
import {fileURLToPath} from 'node:url';
import {spawnSync} from 'node:child_process';
import {loadSilkWasm} from '@tencent-connect/qqbot-nodejs/protocol';

test('real SILK codec roundtrip through local decoder produces 16 kHz WAV', async () => {
  const silk = await loadSilkWasm();
  assert.ok(silk);
  const pcm = Buffer.alloc(16000 * 2 / 5);
  for (let i=0;i<pcm.length/2;i++) pcm.writeInt16LE(Math.round(Math.sin(i*2*Math.PI*440/16000)*3000),i*2);
  const encoded=await silk.encode(pcm,16000);
  const folder=mkdtempSync(path.join(tmpdir(),'qq-codec-test-'));
  try {
    const input=path.join(folder,'test.silk'), output=path.join(folder,'out.wav');
    writeFileSync(input,encoded.data);
    const result=spawnSync(process.execPath,[fileURLToPath(new URL('./decode-voice.mjs',import.meta.url)),input,output]);
    assert.equal(result.status,0,result.stderr.toString());
    const wav=readFileSync(output);
    assert.equal(wav.subarray(0,4).toString(),'RIFF');
    assert.equal(wav.readUInt32LE(24),16000);
    assert.ok(wav.length>44);
  } finally {
    assert.ok(path.resolve(folder).startsWith(path.resolve(tmpdir())+path.sep));
    assert.ok(path.basename(folder).startsWith('qq-codec-test-'));
    rmSync(folder,{recursive:true,force:true});
  }
});
