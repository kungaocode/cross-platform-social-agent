// Bounded network probe. No message handler, model request or send operation.
import {readFileSync, existsSync} from 'node:fs';
import {QQBot} from '@tencent-connect/qqbot-nodejs';

const privatePath = new URL('../private/accounts/qq/credentials.json', import.meta.url);
const credPath = existsSync(privatePath) ? privatePath : new URL('./credentials.json', import.meta.url);
const credentials = existsSync(credPath) ? JSON.parse(readFileSync(credPath, 'utf8').replace(/^\uFEFF/, '')) : {};
let ready = false;
const quiet = {debug(){}, info(){}, warn(){}, error(){}};
const bot = new QQBot({appId:process.env.QQBOT_APPID || credentials.appId,
  appSecret:process.env.QQBOT_SECRET || credentials.appSecret, markdownSupport:false,
  baseUrl:'https://api.bot.qq.com', tokenBaseUrl:'https://api.bot.qq.com', transport:'websocket', logger:quiet});
bot.on('ready', () => { ready = true; bot.stop(); });
bot.on('error', () => {});
const timer = setTimeout(() => bot.stop(), 20000);
try { await bot.start(); } catch { /* deliberately keep credentials and provider details out of logs */ }
finally { clearTimeout(timer); bot.stop(); }
console.log(JSON.stringify({connected:ready, sent_messages:0, timeout_seconds:20}));
process.exit(ready ? 0 : 1);
