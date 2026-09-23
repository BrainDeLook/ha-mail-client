(() => {
  'use strict';
  const $ = (id) => document.getElementById(id);
  const state = {folder: 'INBOX', folders: [], messages: [], current: null, search: '', theme: 'system', externalMedia: true,
    cacheRevision: null, listRequest: 0, statusBusy: false};
  const shell = document.querySelector('.shell');
  const base = new URL('./', location.href);
  let toastTimer;

  async function api(path, body) {
    const response = await fetch(new URL(path, base), {
      method: body === undefined ? 'GET' : 'POST',
      headers: body === undefined ? {} : {'Content-Type': 'application/json'},
      body: body === undefined ? undefined : JSON.stringify(body),
      cache: 'no-store'
    });
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || `Ошибка ${response.status}`);
    return data;
  }

  function toast(message) {
    $('toast').textContent = message;
    $('toast').hidden = false;
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => { $('toast').hidden = true; }, 4500);
  }

  function folderLabel(folder) {
    return ({INBOX: 'Входящие', IMPORTANT: 'Важное', FLAGGED: 'Помеченные', ALL: 'Вся почта',
      SENT: 'Отправленные', DRAFTS: 'Черновики', JUNK: 'Спам', TRASH: 'Корзина'})[folder.role] || folder.label || folder.name;
  }

  function formatDate(date) {
    if (!date) return '';
    const value = new Date(date);
    return Number.isNaN(value.getTime()) ? '' : new Intl.DateTimeFormat('ru', {day: 'numeric', month: 'short', hour: '2-digit', minute: '2-digit'}).format(value);
  }

  function shortSender(sender) {
    return (sender || '').replace(/\s*<[^>]+>/, '').replace(/^"|"$/g, '') || sender || 'Неизвестный отправитель';
  }

  function makeButton(className, label, click) {
    const button = document.createElement('button');
    button.type = 'button';
    button.className = className;
    button.addEventListener('click', click);
    return button;
  }

  function renderFolders() {
    const parent = $('folders');
    parent.replaceChildren();
    for (const folder of state.folders) {
      const button = makeButton('folder' + (state.folder === folder.name ? ' active' : ''), '', () => openFolder(folder.name));
      const glyph = document.createElement('span');
      glyph.className = 'glyph';
      glyph.textContent = ({INBOX: '▣', IMPORTANT: '◆', FLAGGED: '☆', ALL: '▦',
        SENT: '↗', DRAFTS: '▤', JUNK: '⚠', TRASH: '⌫'})[folder.role] || '▧';
      const text = document.createElement('span');
      text.textContent = folderLabel(folder);
      button.append(glyph, text);
      if (folder.unread) {
        const count = document.createElement('span');
        count.className = 'folder-count';
        count.textContent = String(folder.unread);
        button.append(count);
      }
      parent.append(button);
    }
  }

  function renderMessages() {
    const parent = $('message-list');
    parent.replaceChildren();
    $('list-count').textContent = `${state.messages.length} писем в кэше`;
    if (!state.messages.length) {
      const empty = document.createElement('div');
      empty.className = 'reader-empty';
      empty.style.height = '300px';
      empty.textContent = 'Писем пока нет. Проверьте настройки Gmail или дождитесь синхронизации.';
      parent.append(empty);
      return;
    }
    for (const message of state.messages) {
      const unread = !message.flags.includes('\\Seen');
      const card = makeButton('message-card' + (unread ? ' unread' : '') + (state.current?.uid === message.uid ? ' active' : ''), '', () => openMessage(message.uid));
      card.setAttribute('role', 'listitem');
      const top = document.createElement('div'); top.className = 'card-top';
      const sender = document.createElement('span'); sender.className = 'row-sender'; sender.textContent = shortSender(message.sender);
      const date = document.createElement('span'); date.className = 'row-date'; date.textContent = formatDate(message.sent_at);
      top.append(sender, date);
      const subject = document.createElement('div'); subject.className = 'row-subject'; subject.textContent = (message.flags.includes('\\Flagged') ? '★ ' : '') + (message.subject || '(без темы)');
      const snippet = document.createElement('div'); snippet.className = 'row-snippet'; snippet.textContent = message.snippet;
      card.append(top, subject, snippet);
      parent.append(card);
    }
  }

  async function refreshFolders() {
    const data = await api('api/folders');
    state.folders = data.folders;
    const selected = state.folders.find((folder) => folder.name === state.folder);
    if (selected) $('folder-title').textContent = folderLabel(selected);
    renderFolders();
  }

  async function openFolder(name) {
    state.folder = name;
    state.current = null;
    state.search = '';
    $('search').value = '';
    $('reader-empty').hidden = false;
    $('message-detail').hidden = true;
    for (const id of ['remote-button', 'unread-button', 'star-button', 'reply-button']) $(id).hidden = true;
    shell.classList.remove('show-sidebar', 'show-reader');
    const folder = state.folders.find((item) => item.name === name);
    $('folder-title').textContent = folder ? folderLabel(folder) : name;
    renderFolders();
    await loadMessages();
    if (folder && !['INBOX', 'SENT', 'DRAFTS', 'JUNK', 'TRASH'].includes(folder.role)) {
      api('api/sync', {folder: name}).then((result) => {
        if (result.queued) toast('Загружаю эту папку…');
      }).catch((error) => toast(error.message));
    }
  }

  async function loadMessages() {
    const folder = state.folder;
    const search = state.search;
    const request = ++state.listRequest;
    const result = await api(`api/messages?folder=${encodeURIComponent(folder)}&search=${encodeURIComponent(search)}`);
    if (state.folder !== folder || state.search !== search || request !== state.listRequest) return;
    state.messages = result.messages;
    if (state.current) {
      const cached = state.messages.find((message) => message.uid === state.current.uid);
      if (cached) {
        state.current.flags = cached.flags;
        $('star-button').textContent = cached.flags.includes('\\Flagged') ? '★' : '☆';
      }
    }
    renderMessages();
  }

  async function openMessage(uid) {
    try {
      const folder = state.folder;
      const result = await api(`api/message?folder=${encodeURIComponent(folder)}&uid=${uid}&remote=${state.externalMedia ? 1 : 0}`);
      if (state.folder !== folder) return;
      const message = result.message;
      message.remoteLoaded = state.externalMedia;
      state.current = message;
      $('reader-empty').hidden = true;
      $('message-detail').hidden = false;
      $('detail-folder').textContent = $('folder-title').textContent.toUpperCase();
      $('detail-subject').textContent = message.subject || '(без темы)';
      $('detail-sender').textContent = message.sender;
      $('detail-recipient').textContent = `Кому: ${message.recipients}`;
      $('detail-date').textContent = formatDate(message.sent_at);
      renderBody(message);
      $('sender-avatar').textContent = shortSender(message.sender).slice(0, 1).toUpperCase();
      renderAttachments(message);
      for (const id of ['unread-button', 'star-button', 'reply-button']) $(id).hidden = false;
      $('star-button').textContent = message.flags.includes('\\Flagged') ? '★' : '☆';
      shell.classList.add('show-reader');
      renderMessages();
      if (!message.flags.includes('\\Seen')) await changeFlag('\\Seen', true);
    } catch (error) { toast(error.message); }
  }

  function renderBody(message) {
    const text = $('detail-body');
    const frame = $('detail-html');
    frame.mailObserver?.disconnect();
    text.textContent = message.body;
    text.hidden = Boolean(message.html);
    frame.hidden = !message.html;
    $('remote-button').hidden = !message.html || Boolean(message.remoteLoaded);
    if (message.html) {
      const dark = document.documentElement.classList.contains('dark');
      frame.srcdoc = `<meta charset="utf-8"><meta http-equiv="Content-Security-Policy" content="default-src 'none'; script-src 'none'; img-src http: https: data:; media-src http: https: data:; style-src 'unsafe-inline'; frame-src 'none'; form-action 'none'"><style>html,body{overflow:hidden}body{font:14px/1.6 -apple-system,BlinkMacSystemFont,Segoe UI,sans-serif;color:${dark ? '#e8edf5' : '#293442'};background:${dark ? '#151d28' : '#fff'};margin:0;word-break:normal;overflow-wrap:normal}img,video{max-width:100%;height:auto}table{max-width:100%;border-collapse:collapse}td,th{padding:4px}a{color:${dark ? '#8bc2ff' : '#176bd7'}}</style>${message.html}`;
      frame.addEventListener('load', () => {
        try {
          const doc = frame.contentDocument;
          const body = doc.body;
          const resize = () => { frame.style.height = `${Math.max(200, body.scrollHeight + 30)}px`; };
          resize();
          frame.mailObserver = new ResizeObserver(resize);
          frame.mailObserver.observe(body);
          doc.addEventListener('wheel', (event) => {
            if (event.ctrlKey || event.metaKey || !event.deltaY) return;
            event.preventDefault();
            $('reading-pane').scrollTop += event.deltaY;
          }, {passive: false});
          let lastTouchY = null;
          doc.addEventListener('touchstart', (event) => {
            lastTouchY = event.touches.length === 1 ? event.touches[0].clientY : null;
          }, {passive: true});
          doc.addEventListener('touchmove', (event) => {
            if (lastTouchY === null || event.touches.length !== 1) return;
            const nextY = event.touches[0].clientY;
            $('reading-pane').scrollTop += lastTouchY - nextY;
            lastTouchY = nextY;
            event.preventDefault();
          }, {passive: false});
        } catch { frame.style.height = '700px'; }
      }, {once: true});
    } else {
      frame.removeAttribute('srcdoc');
    }
  }

  function renderAttachments(message) {
    const box = $('attachments');
    box.replaceChildren();
    for (const part of message.parts || []) {
      const link = document.createElement('a');
      link.href = new URL(`api/part?folder=${encodeURIComponent(message.folder)}&uid=${message.uid}&part=${part.part_id}`, base).href;
      link.target = '_blank';
      link.rel = 'noopener noreferrer';
      link.textContent = `↧ ${part.filename || 'Вложение'} · ${part.content_type}`;
      box.append(link);
    }
    box.hidden = !box.childElementCount;
    $('attachment-note').hidden = !message.has_attachments || Boolean(box.childElementCount);
  }

  async function changeFlag(flag, enabled) {
    if (!state.current) return;
    try {
      await api('api/flag', {folder: state.folder, uid: state.current.uid, flag, enabled});
      const flags = new Set(state.current.flags.split(' ').filter(Boolean));
      if (enabled) flags.add(flag); else flags.delete(flag);
      state.current.flags = [...flags].join(' ');
      const item = state.messages.find((message) => message.uid === state.current.uid);
      if (item) item.flags = state.current.flags;
      renderMessages();
      refreshFolders().catch(() => {});
    } catch (error) { toast(error.message); }
  }

  function showCompose(reply = false) {
    $('compose-form').reset();
    $('send-status').textContent = '';
    if (reply && state.current) {
      const match = state.current.sender.match(/<([^>]+)>/);
      $('compose-to').value = match ? match[1] : state.current.sender;
      $('compose-subject').value = /^Re:/i.test(state.current.subject) ? state.current.subject : `Re: ${state.current.subject}`;
      $('compose-body').value = `\n\n—\n${state.current.sender} написал(а):\n> ${state.current.body.split('\n').join('\n> ')}`;
    }
    $('compose-dialog').showModal();
    (reply ? $('compose-body') : $('compose-to')).focus();
  }

  async function refreshStatus() {
    if (state.statusBusy) return;
    state.statusBusy = true;
    try {
      const status = await api('api/status');
      $('account').textContent = status.email || 'Настройте Gmail в аддоне';
      state.theme = status.theme || 'system';
      state.externalMedia = status.show_external_media !== false;
      applyTheme();
      $('sync-button').classList.toggle('is-syncing', Boolean(status.syncing));
      if (!status.configured) $('sync-state').textContent = 'Нужны настройки';
      else if (status.syncing) $('sync-state').textContent = 'Синхронизация…';
      else if (status.sync_queued) $('sync-state').textContent = 'В очереди…';
      else if (status.last_error) $('sync-state').textContent = 'Ошибка синхронизации';
      else $('sync-state').textContent = status.last_sync ? `Обновлено ${formatDate(status.last_sync)}` : 'Ожидание';
      $('sync-state').title = status.last_error || '';
      if (status.cache_revision !== state.cacheRevision) {
        await refreshFolders();
        await loadMessages();
        state.cacheRevision = status.cache_revision;
      }
    } catch (error) {
      $('sync-state').textContent = 'Нет связи';
      $('sync-button').classList.remove('is-syncing');
    }
    finally { state.statusBusy = false; }
  }

  const darkMedia = matchMedia('(prefers-color-scheme: dark)');
  function applyTheme() {
    const wasDark = document.documentElement.classList.contains('dark');
    document.documentElement.classList.toggle('dark', state.theme === 'dark' ||
      (state.theme === 'system' && darkMedia.matches));
    if (wasDark !== document.documentElement.classList.contains('dark') && state.current) renderBody(state.current);
  }
  darkMedia.addEventListener('change', applyTheme);

  $('compose-button').addEventListener('click', () => showCompose());
  $('reply-button').addEventListener('click', () => showCompose(true));
  $('remote-button').addEventListener('click', async () => {
    if (!state.current) return;
    try {
      const uid = state.current.uid;
      const folder = state.folder;
      const result = await api(`api/message?folder=${encodeURIComponent(folder)}&uid=${uid}&remote=1`);
      if (state.current?.uid !== uid || state.folder !== folder) return;
      state.current.html = result.message.html;
      state.current.remoteLoaded = true;
      renderBody(state.current);
    } catch (error) { toast(error.message); }
  });
  $('close-compose').addEventListener('click', () => $('compose-dialog').close());
  $('star-button').addEventListener('click', () => changeFlag('\\Flagged', !state.current?.flags.includes('\\Flagged')));
  $('unread-button').addEventListener('click', () => changeFlag('\\Seen', false));
  $('menu-button').addEventListener('click', () => shell.classList.toggle('show-sidebar'));
  $('back-button').addEventListener('click', () => shell.classList.remove('show-reader'));
  $('sync-button').addEventListener('click', async () => {
    try {
      const result = await api('api/sync', {});
      toast(result.queued ? 'Проверка почты поставлена в очередь' : 'Проверка уже выполняется');
      refreshStatus();
    } catch (error) { toast(error.message); }
  });
  $('search').addEventListener('input', () => { clearTimeout($('search').timer); $('search').timer = setTimeout(() => { state.search = $('search').value; loadMessages().catch((error) => toast(error.message)); }, 250); });
  $('compose-form').addEventListener('submit', async (event) => {
    event.preventDefault();
    $('send-button').disabled = true;
    $('send-status').textContent = 'Отправка…';
    try {
      await api('api/send', {to: $('compose-to').value, subject: $('compose-subject').value, body: $('compose-body').value});
      $('compose-dialog').close();
      toast('Письмо отправлено');
    } catch (error) { $('send-status').textContent = error.message; }
    finally { $('send-button').disabled = false; }
  });

  $('ha-button').addEventListener('click', () => {
    if (window.parent !== window) window.parent.postMessage({type: 'home-assistant/toggle-menu'}, location.origin);
    else location.assign('/');
  });
  if (window.parent !== window) {
    const origin = location.origin;
    window.parent.postMessage({type: 'home-assistant/subscribe-properties', kioskMode: true}, origin);
    window.addEventListener('pagehide', () => window.parent.postMessage({type: 'home-assistant/unsubscribe-properties'}, origin));
  }

  refreshFolders().then(() => openFolder('INBOX')).catch((error) => toast(error.message));
  refreshStatus();
  setInterval(refreshStatus, 8000);
})();
