boxes => boxes.filter(b => !boxes.some(other => other !== b && other.contains(b))).map(b => {
  const own = b.matches('[class*="isFromMe"], [data-e2e="message-from-me"]') ||
    !!b.querySelector('[class*="isFromMe"], [data-e2e="message-from-me"]');
  const content = b.querySelector('[data-e2e="msg-item-content"]');
  const card = b.querySelector('[class*="ShareAweme"], [data-e2e="share-aweme"]');
  const media = [];
  let text = (content?.innerText || '').trim();
  const root = content || card;
  const videoURL = value => {
    try {
      const u = new URL(value, location.origin);
      if(u.protocol !== 'https:') return '';
      if(['www.douyin.com','douyin.com'].includes(u.hostname)) {
        const id = u.pathname.match(/^\/video\/(\d+)\/?$/)?.[1] || u.searchParams.get('modal_id');
        if(id && /^\d+$/.test(id)) return 'https://www.douyin.com/video/' + id;
      }
      if(u.hostname === 'v.douyin.com' && /^\/[A-Za-z0-9_-]+\/?$/.test(u.pathname)) return u.origin + u.pathname;
    } catch {}
    return '';
  };
  if(card) {
    const url = [...card.querySelectorAll('a[href]')].map(a=>videoURL(a.href)).find(Boolean) ||
      (/^\d+$/.test(card.dataset.videoId || '') ? 'https://www.douyin.com/video/' + card.dataset.videoId : '');
    if(url) media.push({kind:'video',url});
  } else if(root) {
    const videos = [...root.querySelectorAll('video')];
    for(const v of videos.slice(0,1)) {
      const url = v.currentSrc || v.src || v.querySelector('source')?.src || '';
      if(url) media.push({kind:'video_file',url});
    }
    if(!videos.length) {
      for(const img of [...root.querySelectorAll('img')].slice(0,3)) {
        const alt = img.alt || '';
        if(alt && /emoji|emoticon/i.test(img.className)) { text += alt; continue; }
        let url = img.currentSrc || img.src;
        if(!url) continue;
        if(url.startsWith('data:') || url.startsWith('blob:')) {
          if(!img.dataset.socialMediaRef) img.dataset.socialMediaRef = crypto.randomUUID();
          url = 'dom-image:' + img.dataset.socialMediaRef;
        }
        media.push({kind:'image',url});
      }
    }
    for(const a of root.querySelectorAll('a[href]')) {
      const url = videoURL(a.href);
      if(url && !media.some(m=>m.url===url)) media.push({kind:'video',url});
    }
  }
  if(!media.length && text) {
    for(const value of text.match(/https?:\/\/[^\s<>]+/g)||[]) {
      const url = videoURL(value.replace(/[，。！？,.）)]+$/, ''));
      if(url && !media.some(m=>m.url===url)) media.push({kind:'video',url});
    }
  }
  const unsupported = !!root?.querySelector('audio') || (card && !media.length);
  const senderNode = b.querySelector('[data-e2e="message-sender"], [class*="MessageTitleavatarName"], [class*="nickname"], [class*="Nickname"]');
  const sender = (senderNode?.innerText || '').trim();
  return {id:b.getAttribute('data-message-id') || b.getAttribute('data-msg-id') || '', self:own, sender,
    top:b.getBoundingClientRect().top,
    type:unsupported?'unsupported':media.length?'media':text?'text':'unsupported', text, media:media.slice(0,3)};
}).sort((a, b) => a.top - b.top).slice(-50)
