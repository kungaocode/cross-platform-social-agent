import './direct-network.mjs';
import {readFileSync, existsSync} from 'node:fs';
import {spawn} from 'node:child_process';
import {createInterface} from 'node:readline';
import {fileURLToPath} from 'node:url';
import path from 'node:path';
import {QQBot} from '@tencent-connect/qqbot-nodejs';
import {deliver, normalize} from './bridge.mjs';
import {MemoryPool} from './memory-pool.mjs';

const here = path.dirname(fileURLToPath(import.meta.url));
const root = path.dirname(here);
if (process.argv.includes('--sdk-check')) {
  console.log('QQ SDK 1.0.5 加载通过。');
  process.exit(0);
}
const configPath = path.resolve(process.env.QQ_CONFIG || path.join(here, 'config.json'));
const config = JSON.parse(readFileSync(configPath, 'utf8').replace(/^\uFEFF/, ''));
const privateCredPath = path.join(root, 'private', 'accounts', 'qq', 'credentials.json');
const credPath = existsSync(privateCredPath) ? privateCredPath : path.join(here, 'credentials.json');
const credentials = existsSync(credPath) ? JSON.parse(readFileSync(credPath, 'utf8').replace(/^\uFEFF/, '')) : {};
const appId = process.env.QQBOT_APPID || credentials.appId;
const appSecret = process.env.QQBOT_SECRET || credentials.appSecret;
if (!appId || !appSecret) {
  console.error('缺少 QQ 凭据：设置 QQBOT_APPID / QQBOT_SECRET，或填写 private/accounts/qq/credentials.json（见 README）。');
  process.exit(2);
}
const worker = spawn(process.env.QQ_PYTHON || 'python', ['-u', '-m', 'social_bot.platforms.qq.worker', configPath, appId],
  {cwd: root, windowsHide: true, env: {...process.env, PYTHONIOENCODING: 'utf-8'}, stdio: ['pipe', 'pipe', 'inherit']});
const lines = createInterface({input: worker.stdout});
let pending;
let stopped = false;
let bot;
const receive = (timeoutMs = 600000) => new Promise((resolve, reject) => {
  if (pending) return reject(new Error('Worker requests must be serialized'));
  const timer = setTimeout(() => {
    pending = undefined;
    reject(new Error('Worker timeout'));
    worker.kill();
  }, timeoutMs);
  pending = {resolve: value => {clearTimeout(timer); pending = undefined; resolve(value);},
    reject: error => {clearTimeout(timer); pending = undefined; reject(error);}};
});
lines.on('line', line => {
  try { pending?.resolve(JSON.parse(line)); }
  catch { pending?.reject(new Error('Invalid worker response')); }
});
worker.on('error', () => pending?.reject(new Error('Python worker could not start')));
worker.on('exit', () => {
  pending?.reject(new Error('Python worker exited'));
  if (!stopped) { process.exitCode = 1; stopped = true; bot?.stop(); }
});
worker.stdin.on('error', () => pending?.reject(new Error('Worker input closed')));
async function request(data) {
  const video = data.message?.attachments?.some(att => att.content_type === 'video/mp4' || /\.mp4(?:[?#]|$)/i.test(att.filename || att.url));
  const response = receive(video ? 1800000 : 600000);
  worker.stdin.write(JSON.stringify(data) + '\n');
  return response;
}
const ready = await receive();
if (!ready.ready) throw new Error('Worker initialization failed');
if (process.argv.includes('--check')) {
  console.log('QQ SDK、Python 服务、记忆目录和配置加载通过；未连接 QQ 或调用模型。');
  stopped = true;
  worker.stdin.end();
} else {
  const memoryPool = new MemoryPool(path.join(config.memory_dir, 'qq-pool-' + appId));
  async function archive(file) {
    const result = await request({op:'archive', message:memoryPool.read(file)});
    if (!result.accepted) throw new Error('Memory archive pending: ' + result.reason);
    memoryPool.done(file);
  }
  // Recover memory only: uncertain/stale messages must never be resent on restart.
  for (const file of memoryPool.pending()) {
    try { await archive(file); } catch { console.error('QQ 待入库事件仍保留在磁盘池中。'); }
  }
  bot = new QQBot({appId, appSecret, markdownSupport: false,
    baseUrl: 'https://api.bot.qq.com', tokenBaseUrl: 'https://api.bot.qq.com', transport: 'websocket'});
  bot.on('ready', () => console.log('QQ 已连接，群图片/语音/MP4/文档全局读取：' + (config.group_multimodal_global !== false ? '开启' : '关闭（需 @）') + '；普通群文字需 @。'));
  bot.on('error', () => console.error('QQ 连接错误，请检查凭据、权限和网络。'));
  let queue = Promise.resolve();
  let queued = 0;
  const limit = Math.max(1, Math.min(100, Number(config.max_queue) || 20));
  bot.on('message', (ctx, inbound) => {
    const msg = inbound || ctx.message;
    const normalized = normalize(msg, config);
    if (stopped || !normalized) return;
    const allow = normalized.kind === 'group' ? config.groups : config.private_users;
    const id = normalized.kind === 'group' ? normalized.chat_id : normalized.sender_id;
    if (!Array.isArray(allow) || !allow.includes(id)) {
      console.log(`未在白名单，未调用模型：${normalized.kind} ${id}`);
      return;
    }
    let poolFile;
    try { poolFile = memoryPool.put(normalized); }
    catch { console.error('QQ 入库池写盘失败，本条未确认接收。'); return; }
    if (!poolFile) return;
    if (queued >= limit) {
      console.warn('QQ 回复队列已满，本条已落盘，将仅归档记忆。');
      queue = queue.then(() => archive(poolFile)).catch(() => console.error('QQ 归档失败，事件保留在磁盘池。'));
      return;
    }
    queued++;
    queue = queue.then(async () => {
      if (stopped) return;
      const guardedBot = {sendText: (...args) => {
        if (stopped) throw new Error('Stopped');
        return bot.sendText(...args);
      }};
      const result = await deliver(guardedBot, request, msg, config);
      if (!result.accepted) console.log(`QQ 消息未发送：${result.reason || 'state_conflict'}`);
      await archive(poolFile);
    }).catch(() => {
      console.error('QQ 消息处理失败；没有自动重发。');
    }).finally(() => queued--);
  });
  function stop() { stopped = true; bot.stop(); }
  process.on('SIGINT', stop);
  process.on('SIGTERM', stop);
  try { await bot.start(); }
  catch { console.error('QQ 启动或连接失败，请核对机器人凭据及接入权限。'); process.exitCode = 1; }
  finally { stopped = true; await queue; worker.stdin.end(); }
}
