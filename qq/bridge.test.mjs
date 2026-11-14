import test from 'node:test';
import assert from 'node:assert/strict';
import {deliver, normalize} from './bridge.mjs';

const msg = {kind: 'c2c', senderId: 'u1', messageId: 'm1', content: '你好',
  replyTarget: {scope: 'c2c', targetId: 'u1', msgId: 'm1'}};

test('unsupported channels and mismatched reply IDs are rejected', () => {
  assert.equal(normalize({...msg, kind: 'guild'}), null);
  assert.equal(normalize({...msg, messageId: 'wrong'}), null);
});
test('ordinary group messages do not trigger, explicit group @ does', () => {
  const group = {...msg, kind: 'group', replyTarget: {...msg.replyTarget, scope: 'group', targetId: 'g1'}};
  assert.equal(normalize({...group, rawEventType: 'GROUP_MESSAGE_CREATE'}), null);
  assert.equal(normalize({...group, rawEventType: 'GROUP_AT_MESSAGE_CREATE'}).chat_id, 'g1');
});
test('reply uses original target and persists sending then sent', async () => {
  const ops = [];
  await deliver({sendText: async (target, text) => {
    assert.deepEqual(target, msg.replyTarget);
    assert.equal(text, '答复');
  }}, async request => {
    ops.push(request.op);
    return {accepted: true, reply: '答复'};
  }, msg);
  assert.deepEqual(ops, ['prepare', 'sending', 'sent']);
});
test('uncertain send is recorded and is never retried automatically', async () => {
  const ops = [];
  let sends = 0;
  const result = await deliver({sendText: async () => {sends++; throw Error('timeout');}},
    async request => {ops.push(request.op); return {accepted: true, reply: '答复'};}, msg);
  assert.equal(sends, 1);
  assert.equal(result.reason, 'send_result_unknown');
  assert.deepEqual(ops, ['prepare', 'sending', 'unknown']);
});
test('denied message never reaches QQ send API', async () => {
  await deliver({sendText: () => assert.fail('unexpected send')}, async () => ({accepted: false}), msg);
});

test('attachment-only images and voice preserve metadata after stripping bot mention', () => {
  const attachments = [{content_type: 'image/png', url: 'https://gchat.qpic.cn/picture'},
    {content_type: 'voice', url: 'https://multimedia.nt.qq.com/voice', voice_wav_url: 'https://multimedia.nt.qq.com/audio.wav', asr_refer_text: '你好'}];
  const result = normalize({...msg, content: '<@123>', attachments});
  assert.equal(result.text, '');
  assert.equal(result.attachments.length, 2);
  assert.equal(result.attachments[1].asr_refer_text, '你好');
  assert.equal(result.attachments[1].voice_wav_url, attachments[1].voice_wav_url);
  assert.equal(normalize({...msg, content: '<@123>'}), null);
  assert.equal(normalize({...msg, senderIsBot: true, attachments}), null);
});

test('group media is global by default and the switch restores mention-only mode', () => {
  const group = {...msg, kind: 'group', content: '', attachments: [{content_type: 'image/png', url: 'https://gchat.qpic.cn/p'}],
    replyTarget: {...msg.replyTarget, scope: 'group', targetId: 'g1'}};
  assert.equal(normalize({...group, rawEventType: 'GROUP_MESSAGE_CREATE'}).is_mentioned, false);
  assert.equal(normalize({...group, rawEventType: 'GROUP_MESSAGE_CREATE'}, {group_multimodal_global: false}), null);
  assert.equal(normalize({...group, rawEventType: 'GROUP_AT_MESSAGE_CREATE'}).attachments.length, 1);
  assert.equal(normalize({...group, rawEventType: 'GROUP_AT_MESSAGE_CREATE'}, {group_multimodal_global: false}).is_mentioned, true);
});

test('global mode accepts voice but excludes disabled media, unsupported files, bots and unknown events', () => {
  const group = {...msg, kind: 'group', rawEventType: 'GROUP_MESSAGE_CREATE', content: '',
    attachments: [{content_type: 'voice', asr_refer_text: '测试'}],
    replyTarget: {...msg.replyTarget, scope: 'group', targetId: 'g1'}};
  assert.ok(normalize(group));
  assert.equal(normalize(group, {speech_recognition_enabled: false}), null);
  assert.equal(normalize({...group, attachments: [{content_type: 'image/png'}]}, {image_reading_enabled: false}), null);
  assert.equal(normalize({...group, attachments: [{content_type: 'application/zip'}]}), null);
  assert.equal(normalize({...group, senderIsBot: true}), null);
  assert.equal(normalize({...group, rawEventType: 'UNKNOWN'}), null);
});

test('documents preserve metadata and obey group mention and document switches', () => {
  const group = {...msg, kind: 'group', rawEventType: 'GROUP_MESSAGE_CREATE', content: '',
    replyTarget: {...msg.replyTarget, scope: 'group', targetId: 'g1'}};
  for (const attachment of [
    {content_type:'application/pdf', url:'https://a.qq.com/opaque.bin'},
    {content_type:'application/octet-stream', filename:'报告.DOCX'},
    {filename:'季度表.xlsx'}, {filename:'演示.pptx'}, {filename:'说明.txt'},
  ]) {
    const event = {...group, attachments:[attachment]};
    assert.deepEqual(normalize(event).attachments[0].filename, attachment.filename || '');
    assert.equal(normalize(event, {document_reading_enabled:false}), null);
    assert.equal(normalize(event, {group_multimodal_global:false}), null);
    assert.ok(normalize({...event, rawEventType:'GROUP_AT_MESSAGE_CREATE'}, {group_multimodal_global:false}));
    assert.ok(normalize({...msg, content:'', attachments:[attachment]}));
  }
  assert.equal(normalize({...group, attachments:[{filename:'bad.exe', content_type:'application/pdf'}]}), null);
});

test('delivery applies the same global-media switch as inbound filtering', async () => {
  const group = {...msg, kind: 'group', rawEventType: 'GROUP_MESSAGE_CREATE', content: '',
    attachments: [{content_type: 'image/png', url: 'https://gchat.qpic.cn/p'}],
    replyTarget: {...msg.replyTarget, scope: 'group', targetId: 'g1'}};
  const send = {sendText: async () => {}};
  assert.equal((await deliver(send, () => assert.fail('unexpected prepare'), group, {group_multimodal_global: false})).accepted, false);
  const requests = [];
  assert.equal((await deliver(send, async request => {
    requests.push(request);
    return {accepted: true, reply: '看到了'};
  }, group)).accepted, true);
  assert.equal(requests[0].message.is_mentioned, false);
  assert.equal(requests[0].message.attachments.length, 1);
});

test('MP4 respects the group global switch and its individual switch', () => {
  const group={...msg,kind:'group',rawEventType:'GROUP_MESSAGE_CREATE',content:'',
    attachments:[{content_type:'application/octet-stream',filename:'clip.mp4',url:'https://a.qq.com/video'}],
    replyTarget:{...msg.replyTarget,scope:'group',targetId:'g1'}};
  assert.ok(normalize(group));
  assert.equal(normalize(group,{video_reading_enabled:false}),null);
  assert.equal(normalize(group,{group_multimodal_global:false}),null);
});
