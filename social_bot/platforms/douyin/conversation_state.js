(items, selectors) => {
  const visible = el => {
    const r = el.getBoundingClientRect(), s = getComputedStyle(el);
    return r.width > 0 && r.height > 0 && s.display !== 'none' && s.visibility !== 'hidden' && s.opacity !== '0';
  };
  const active = el => [el, el.firstElementChild].filter(Boolean).some(node =>
    node.getAttribute('aria-selected') === 'true' || /active|selected|curConversation/i.test(String(node.className)));
  return items.map(el => {
    const name = (el.querySelector(selectors.title)?.innerText || '').trim();
    const badges = [...el.querySelectorAll(
      '[data-e2e="conversation-unread"], [data-e2e="unread-count"], [data-e2e="unread-badge"], ' +
      '[data-unread-count], [class*="unread" i], [class*="badge" i], [aria-label*="未读"], [aria-label*="unread" i]')];
    const marked = badges.filter(node => {
      if (!visible(node)) return false;
      const value = (node.getAttribute('data-unread-count') ?? node.innerText ?? '').trim();
      return !/^0+$/.test(value) && !['false', '0'].includes(node.getAttribute('data-unread'));
    });
    // Small red dots without semantic attributes are a priority hint only.
    const redDot = !marked.length && [...el.querySelectorAll('div,span,i')].some(node => {
      if (!visible(node)) return false;
      const r = node.getBoundingClientRect(), s = getComputedStyle(node);
      if (r.width > 48 || r.height > 32 || parseFloat(s.borderRadius) < 2) return false;
      const c = s.backgroundColor.match(/[\d.]+/g)?.map(Number) || [];
      return c.length >= 3 && (c.length < 4 || c[3] > 0) && c[0] > 160 && c[0] > c[1]*1.5 && c[0] > c[2]*1.3;
    });
    const preview = (el.querySelector('[data-e2e="conversation-preview"], [class*="ConversationItemHinttextBox"]')?.innerText || '').trim();
    return {name, active:active(el), unread:marked.length > 0 || redDot,
      unread_count:marked.length ? Number((marked[0].getAttribute('data-unread-count') || marked[0].innerText || '').match(/\d+/)?.[0] || 0) : 0,
      hint:preview};
  });
}
