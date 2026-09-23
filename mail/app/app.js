(() => {
  'use strict';
  const $ = (id) => document.getElementById(id);
  const state = {folder: 'INBOX', folders: [], messages: [], current: null, search: ''};
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
    return ({INBOX: 'Входящие', SENT: 'Отправленные', DRAFTS: 'Черновики', JUNK: 'Спам', TRASH: 'Корзина'})[folder.role] || folder.name;
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
      glyph.textContent = ({INBOX: '▣', SENT: '↗', DRAFTS: '▤', JUNK: '⚠', TRASH: '⌫'})[folder.role] || '▧';
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
    renderFolders();
  }

  async function openFolder(name) {
    state.folder = name;
    state.current = null;
    state.search = '';
    $('search').value = '';
    $('reader-empty').hidden = false;
    $('message-detail').hidden = true;
    for (const id of ['unread-button', 'star-button', 'reply-button']) $(id).hidden = true;
    shell.classList.remove('show-sidebar', 'show-reader');
    const folder = state.folders.find((item) => item.name === name);
    $('folder-title').textContent = folder ? folderLabel(folder) : name;
    renderFolders();
    await loadMessages();
    if (folder?.role === 'OTHER') {
      api('api/sync', {folder: name}).then(() => toast('Загружаю эту папку…')).catch((error) => toast(error.message));
    }
  }

  async function loadMessages() {
    const folder = state.folder;
    const result = await api(`api/messages?folder=${encodeURIComponent(folder)}&search=${encodeURIComponent(state.search)}`);
    if (state.folder !== folder) return;
    state.messages = result.messages;
    renderMessages();
  }

  async function openMessage(uid) {
    try {
      const folder = state.folder;
      const result = await api(`api/message?folder=${encodeURIComponent(folder)}&uid=${uid}`);
      if (state.folder !== folder) return;
      const message = result.message;
      state.current = message;
      $('reader-empty').hidden = true;
      $('message-detail').hidden = false;
      $('detail-folder').textContent = $('folder-title').textContent.toUpperCase();
      $('detail-subject').textContent = message.subject || '(без темы)';
      $('detail-sender').textContent = message.sender;
      $('detail-recipient').textContent = `Кому: ${message.recipients}`;
      $('detail-date').textContent = formatDate(message.sent_at);
      $('detail-body').textContent = message.body;
      $('sender-avatar').textContent = shortSender(message.sender).slice(0, 1).toUpperCase();
      $('attachment-note').hidden = !message.has_attachments;
      for (const id of ['unread-button', 'star-button', 'reply-button']) $(id).hidden = false;
      $('star-button').textContent = message.flags.includes('\\Flagged') ? '★' : '☆';
      shell.classList.add('show-reader');
      renderMessages();
      if (!message.flags.includes('\\Seen')) await changeFlag('\\Seen', true);
    } catch (error) { toast(error.message); }
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
    try {
      const status = await api('api/status');
      $('account').textContent = status.email || 'Настройте Gmail в аддоне';
      if (!status.configured) $('sync-state').textContent = 'Нужны настройки';
      else if (status.syncing) $('sync-state').textContent = 'Синхронизация…';
      else if (status.last_error) $('sync-state').textContent = 'Ошибка синхронизации';
      else $('sync-state').textContent = status.last_sync ? `Обновлено ${formatDate(status.last_sync)}` : 'Ожидание';
      if (status.last_error) $('sync-state').title = status.last_error;
      if (status.last_sync && status.last_sync !== refreshStatus.lastSync) {
        refreshStatus.lastSync = status.last_sync;
        await refreshFolders();
        await loadMessages();
      }
    } catch (error) { $('sync-state').textContent = 'Нет связи'; }
  }

  $('compose-button').addEventListener('click', () => showCompose());
  $('reply-button').addEventListener('click', () => showCompose(true));
  $('close-compose').addEventListener('click', () => $('compose-dialog').close());
  $('star-button').addEventListener('click', () => changeFlag('\\Flagged', !state.current?.flags.includes('\\Flagged')));
  $('unread-button').addEventListener('click', () => changeFlag('\\Seen', false));
  $('menu-button').addEventListener('click', () => shell.classList.toggle('show-sidebar'));
  $('back-button').addEventListener('click', () => shell.classList.remove('show-reader'));
  $('sync-button').addEventListener('click', async () => { try { await api('api/sync', {}); toast('Проверка почты запущена'); } catch (error) { toast(error.message); } });
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

  if (window.parent !== window && location.pathname.includes('/api/hassio_ingress/')) {
    const origin = location.origin;
    $('ha-button').hidden = false;
    $('ha-button').addEventListener('click', () => window.parent.postMessage({type: 'home-assistant/toggle-menu'}, origin));
    window.parent.postMessage({type: 'home-assistant/subscribe-properties', kioskMode: true}, origin);
    window.addEventListener('pagehide', () => window.parent.postMessage({type: 'home-assistant/unsubscribe-properties'}, origin));
  }

  refreshFolders().then(() => openFolder('INBOX')).catch((error) => toast(error.message));
  refreshStatus();
  setInterval(refreshStatus, 15000);
})();
