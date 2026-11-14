// Converts only supported official SDK events. Reply routing stays with the SDK.
const documentExtensions = new Set(['.txt', '.md', '.csv', '.json', '.xml', '.html', '.htm', '.pdf', '.docx', '.xlsx', '.xls', '.pptx']);
const documentMimes = new Set(['application/pdf', 'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
  'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet', 'application/vnd.ms-excel',
  'application/vnd.openxmlformats-officedocument.presentationml.presentation', 'text/plain', 'text/markdown',
  'text/csv', 'application/json', 'application/xml', 'text/xml', 'text/html']);

function isDocument(att) {
  let name = (att.filename || att.url || '').split(/[?#]/)[0];
  try { name = decodeURIComponent(name); } catch { return false; }
  const suffix = name.match(/\.[^./\\]+$/)?.[0].toLowerCase();
  if (documentExtensions.has(suffix)) return true;
  if (suffix && att.filename) return false;
  return documentMimes.has(att.content_type.toLowerCase().split(';')[0].trim());
}

export function normalize(msg, config = {}) {
  if (!msg || !['c2c', 'group'].includes(msg.kind)) return null;
  const target = msg.replyTarget;
  if (!target || target.scope !== msg.kind || !target.targetId || !msg.senderId || !msg.messageId) return null;
  if (target.msgId !== msg.messageId) return null;
  if (msg.senderIsBot) return null;
  const text = typeof msg.content === 'string' ? msg.content.replace(/<@!?\d+>/g, '').trim() : '';
  const attachments = Array.isArray(msg.attachments) ? msg.attachments.filter(att => att && typeof att === 'object').map(att => ({
    content_type: typeof att.content_type === 'string' ? att.content_type : '',
    url: typeof att.url === 'string' ? att.url : '',
    filename: typeof att.filename === 'string' ? att.filename : '',
    voice_wav_url: typeof att.voice_wav_url === 'string' ? att.voice_wav_url : '',
    asr_refer_text: typeof att.asr_refer_text === 'string' ? att.asr_refer_text : '',
  })) : [];
  const mentioned = msg.kind === 'group' && msg.rawEventType === 'GROUP_AT_MESSAGE_CREATE';
  if (msg.kind === 'group' && !mentioned) {
    const readableMedia = attachments.some(att => {
      const mime = att.content_type.toLowerCase().split(';')[0];
      const image = mime.startsWith('image/');
      const video = mime === 'video/mp4' || /\.mp4(?:[?#]|$)/i.test(att.filename || att.url);
      const voice = mime === 'voice' || mime.startsWith('audio/') || att.voice_wav_url || att.asr_refer_text
        || /\.(silk|slk|slac|amr|wav|mp3|m4a|ogg|opus|aac|flac)(?:[?#]|$)/i.test(att.filename || att.url);
      return (image && config.image_reading_enabled !== false) || (voice && config.speech_recognition_enabled !== false)
        || (video && config.video_reading_enabled !== false) || (isDocument(att) && config.document_reading_enabled !== false);
    });
    if (msg.rawEventType !== 'GROUP_MESSAGE_CREATE' || config.group_multimodal_global === false || !readableMedia) return null;
  }
  if (!text && !attachments.length) return null;
  return {kind: msg.kind, chat_id: target.targetId, sender_id: msg.senderId,
    message_id: msg.messageId, text, is_mentioned: mentioned, ...(attachments.length ? {attachments} : {})};
}

export async function deliver(bot, request, msg, config = {}) {
  const message = normalize(msg, config);
  if (!message) return {accepted: false, reason: 'unsupported_message'};
  const result = await request({op: 'prepare', message});
  if (!result.accepted || !result.reply) return result;
  const started = await request({op: 'sending', message});
  if (!started.accepted) return started;
  try {
    await bot.sendText(msg.replyTarget, result.reply);
  } catch {
    await request({op: 'unknown', message});
    return {accepted: false, reason: 'send_result_unknown'};
  }
  await request({op: 'sent', message});
  return {accepted: true};
}
