import test from 'node:test';
import assert from 'node:assert/strict';
import {spawnSync} from 'node:child_process';

test('QQ bootstrap strips inherited proxy configuration before loading the SDK', () => {
  const result = spawnSync(process.execPath, ['--input-type=module', '-e',
    `await import('./qq/direct-network.mjs');console.log(JSON.stringify({http:process.env.HTTP_PROXY,https:process.env.https_proxy,all:process.env.ALL_PROXY,ddgs:process.env.DDGS_PROXY,no:process.env.NO_PROXY,node:process.env.NODE_USE_ENV_PROXY}))`],
    {cwd:new URL('../', import.meta.url),env:{...process.env,HTTP_PROXY:'http://127.0.0.1:9',https_proxy:'http://127.0.0.1:9',ALL_PROXY:'socks5://127.0.0.1:9',DDGS_PROXY:'http://127.0.0.1:9'},encoding:'utf8'});
  assert.equal(result.status,0,result.stderr);
  assert.deepEqual(JSON.parse(result.stdout),{no:'*',node:'0'});
});
