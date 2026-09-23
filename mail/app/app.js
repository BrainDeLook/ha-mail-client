(() => {
  'use strict';
  const $ = (id) => document.getElementById(id);
  const state = {folder: 'INBOX', folders: [], messages: [], current: null, search: '', theme: 'system', viewMode: 'split', defaultFolderRole: 'INBOX', externalMedia: true,
    cacheRevision: null, listRequest: 0, messageRequest: 0, statusBusy: false};
  const shell = document.querySelector('.shell');
  const base = new URL('./', location.href);
  let toastTimer;
  let routeRequest = 0;
  let listReturnPending = false;
  const historyTraversal = window.performance?.getEntriesByType?.('navigation')?.[0]?.type === 'back_forward';
  const restoredRoute = historyTraversal && history.state?.homeMail ? history.state : null;
  const initialRoute = restoredRoute || {homeMail: true, folder: 'INBOX', uid: null, depth: 0};
  let startupFolderPending = !restoredRoute;
  history.replaceState(initialRoute, '');

  function recordRoute(folder, uid) {
    listReturnPending = false;
    const previous = history.state;
    if (previous?.homeMail && previous.folder === folder && previous.uid === uid) return;
    history.pushState({homeMail: true, folder, uid, depth: (previous?.depth || 0) + 1}, '');
  }

  function setSidebarOpen(open) {
    shell.classList.toggle('show-sidebar', open);
    $('menu-button').setAttribute('aria-expanded', String(open));
  }

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

  function defaultFolderName() {
    return state.folders.find((folder) => folder.role === state.defaultFolderRole)?.name ||
      state.folders.find((folder) => folder.role === 'INBOX')?.name || state.folders[0]?.name || 'INBOX';
  }

  function formatDate(date) {
    if (!date) return '';
    const value = new Date(date);
    return Number.isNaN(value.getTime()) ? '' : new Intl.DateTimeFormat('ru', {day: 'numeric', month: 'short', hour: '2-digit', minute: '2-digit'}).format(value);
  }

  function shortSender(sender) {
    return (sender || '').replace(/\s*<[^>]+>/, '').replace(/^"|"$/g, '') || sender || 'Неизвестный отправитель';
  }

  function setStarred(starred) {
    $('star-button').classList.toggle('is-marked', starred);
    $('star-button').setAttribute('aria-pressed', String(starred));
  }

  function makeButton(className, label, click) {
    const button = document.createElement('button');
    button.type = 'button';
    button.className = className;
    button.addEventListener('click', click);
    return button;
  }

  const folderPaths = {
    INBOX: '<path d="M4 5h16v12l-3 3H7l-3-3V5Z"/><path d="M4 14h5l1.5 2h3l1.5-2h5"/>',
    IMPORTANT: '<path d="m12 2 9 10-9 10-9-10L12 2Z"/><path d="M12 7v6m0 4h.01"/>',
    FLAGGED: '<path d="m12 2 3.1 6.4 7.1 1-5.1 5 .9 7-6-3.3-6 3.3.9-7-5.1-5 7.1-1L12 2Z"/>',
    ALL: '<rect x="3" y="3" width="7" height="7" rx="1"/><rect x="14" y="3" width="7" height="7" rx="1"/><rect x="3" y="14" width="7" height="7" rx="1"/><rect x="14" y="14" width="7" height="7" rx="1"/>',
    SENT: '<path d="m3 11 18-8-8 18-2.5-7.5L3 11Z"/><path d="M10.5 13.5 21 3"/>',
    DRAFTS: '<path d="M6 3h9l4 4v14H6V3Z"/><path d="M15 3v5h4M9 12h7M9 16h7"/>',
    JUNK: '<path d="m12 3 10 18H2L12 3Z"/><path d="M12 9v5m0 3h.01"/>',
    TRASH: '<path d="M4 7h16M9 7V4h6v3m3 0-1 14H7L6 7m4 4v6m4-6v6"/>',
    FOLDER: '<path d="M3 6h7l2 2h9v12H3V6Z"/>'
  };

  function folderIcon(role) {
    const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
    svg.setAttribute('viewBox', '0 0 24 24');
    svg.setAttribute('fill', 'none');
    svg.setAttribute('aria-hidden', 'true');
    svg.innerHTML = folderPaths[role] || folderPaths.FOLDER;
    return svg;
  }

  function renderFolders() {
    const parent = $('folders');
    parent.replaceChildren();
    for (const folder of state.folders) {
      const button = makeButton('folder' + (state.folder === folder.name ? ' active' : ''), '', () => openFolder(folder.name));
      const glyph = document.createElement('span');
      glyph.className = 'glyph';
      glyph.append(folderIcon(folder.role));
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
      const preview = document.createElement('div'); preview.className = 'card-preview'; preview.append(subject, snippet);
      card.append(top, preview);
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

  async function openFolder(name, recordHistory = true) {
    ++state.messageRequest;
    if (recordHistory) {
      ++routeRequest;
      recordRoute(name, null);
    }
    state.folder = name;
    state.current = null;
    state.search = '';
    $('search').value = '';
    $('reader-empty').hidden = false;
    $('message-detail').hidden = true;
    for (const id of ['remote-button', 'unread-button', 'star-button', 'reply-button']) $(id).hidden = true;
    setSidebarOpen(false);
    shell.classList.remove('show-reader');
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
        setStarred(cached.flags.includes('\\Flagged'));
      }
    }
    renderMessages();
  }

  async function openMessage(uid, recordHistory = true) {
    try {
      const folder = state.folder;
      if (recordHistory) {
        ++routeRequest;
        recordRoute(folder, uid);
      }
      const request = ++state.messageRequest;
      const result = await api(`api/message?folder=${encodeURIComponent(folder)}&uid=${uid}&remote=${state.externalMedia ? 1 : 0}`);
      if (state.folder !== folder || request !== state.messageRequest) return;
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
      setStarred(message.flags.includes('\\Flagged'));
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
      const remote = message.remoteLoaded ? 'http: https: ' : '';
      frame.srcdoc = `<meta charset="utf-8"><meta http-equiv="Content-Security-Policy" content="default-src 'none'; script-src 'none'; img-src 'self' ${remote}data:; media-src 'self' ${remote}data:; style-src 'unsafe-inline'; frame-src 'none'; form-action 'none'"><style>html,body{overflow:hidden}body{font:14px/1.6 Arial,sans-serif;color:#202124;background:#fff;margin:0;word-break:normal;overflow-wrap:normal}img,video{max-width:100%}table{max-width:100%}a{color:#1a73e8}</style>${message.html}`;
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
          let touchStartX = null;
          let touchStartY = null;
          installSwipeBack(doc);
          doc.addEventListener('touchstart', (event) => {
            lastTouchY = event.touches.length === 1 ? event.touches[0].clientY : null;
            touchStartX = event.touches.length === 1 ? event.touches[0].clientX : null;
            touchStartY = lastTouchY;
          }, {passive: true});
          doc.addEventListener('touchmove', (event) => {
            if (lastTouchY === null || event.touches.length !== 1) return;
            if (Math.abs(event.touches[0].clientX - touchStartX) >
                Math.abs(event.touches[0].clientY - touchStartY) * 1.2) return;
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
      if (flag === '\\Flagged') setStarred(enabled);
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
      state.viewMode = status.view_mode === 'list' ? 'list' : 'split';
      state.defaultFolderRole = status.default_folder || 'INBOX';
      shell.classList.toggle('list-mode', state.viewMode === 'list');
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
        if (startupFolderPending && state.folders.length) {
          state.folder = defaultFolderName();
          history.replaceState({...history.state, folder: state.folder, uid: null}, '');
          startupFolderPending = false;
          renderFolders();
        }
        if (state.folders.length) await loadMessages();
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
    const root = document.documentElement;
    const wasDark = root.classList.contains('dark');
    const wasHaDark = root.classList.contains('ha-dark');
    const haDark = state.theme === 'ha_dark';
    const dark = haDark || state.theme === 'dark' || (state.theme === 'system' && darkMedia.matches);
    root.classList.toggle('dark', dark);
    root.classList.toggle('ha-dark', haDark);
    if ((wasDark !== dark || wasHaDark !== haDark) && state.current) renderBody(state.current);
  }
  darkMedia.addEventListener('change', applyTheme);

  function returnToList() {
    if (!shell.classList.contains('show-reader')) return;
    ++routeRequest;
    ++state.messageRequest;
    state.current = null;
    $('reader-empty').hidden = false;
    $('message-detail').hidden = true;
    for (const id of ['remote-button', 'unread-button', 'star-button', 'reply-button']) $(id).hidden = true;
    shell.classList.remove('show-reader');
    renderMessages();
    if (history.state?.homeMail && history.state.depth > 0) {
      listReturnPending = true;
      history.back();
    } else if (history.state?.homeMail) {
      history.replaceState({...history.state, uid: null}, '');
    }
  }

  function installSwipeBack(target) {
    let start = null;
    target.addEventListener('touchstart', (event) => {
      if (!matchMedia('(max-width: 700px)').matches ||
          !shell.classList.contains('show-reader') || event.touches.length !== 1) {
        start = null;
        return;
      }
      const touch = event.touches[0];
      start = {x: touch.clientX, y: touch.clientY, edge: touch.clientX <= 96};
    }, {passive: true});
    target.addEventListener('touchmove', (event) => {
      if (!start?.edge || event.touches.length !== 1) return;
      const dx = event.touches[0].clientX - start.x;
      const dy = event.touches[0].clientY - start.y;
      if (dx > 20 && dx > Math.abs(dy) * 1.4) event.preventDefault();
    }, {passive: false});
    target.addEventListener('touchend', (event) => {
      if (!start || event.changedTouches.length !== 1) return;
      const dx = event.changedTouches[0].clientX - start.x;
      const dy = event.changedTouches[0].clientY - start.y;
      if (start.edge && dx >= 90 && Math.abs(dy) <= Math.min(80, dx * 0.5)) returnToList();
      start = null;
    }, {passive: true});
    target.addEventListener('touchcancel', () => { start = null; }, {passive: true});
  }

  installSwipeBack($('reading-pane'));

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
  $('menu-button').addEventListener('click', () => setSidebarOpen(!shell.classList.contains('show-sidebar')));
  $('sidebar-scrim').addEventListener('click', () => setSidebarOpen(false));
  document.addEventListener('keydown', (event) => {
    if (event.key === 'Escape' && shell.classList.contains('show-sidebar')) setSidebarOpen(false);
  });
  $('back-button').addEventListener('click', returnToList);
  window.addEventListener('popstate', async (event) => {
    const route = event.state;
    if (listReturnPending) {
      if (route?.homeMail && route.uid !== null) {
        history.back();
        return;
      }
      listReturnPending = false;
    }
    if (!route?.homeMail) return;
    const request = ++routeRequest;
    try {
      await openFolder(route.folder, false);
      if (request === routeRequest && route.uid !== null) await openMessage(route.uid, false);
    } catch (error) { toast(error.message); }
  });
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

  async function initialize() {
    try {
      const status = await api('api/status');
      state.defaultFolderRole = status.default_folder || 'INBOX';
    } catch { /* The regular status poll will show a connection error. */ }
    await refreshFolders();
    if (state.folders.length) {
      if (startupFolderPending || !state.folders.some((folder) => folder.name === initialRoute.folder)) {
        initialRoute.folder = defaultFolderName();
        initialRoute.uid = null;
        history.replaceState(initialRoute, '');
        startupFolderPending = false;
      }
      await openFolder(initialRoute.folder, false);
      if (initialRoute.uid !== null) await openMessage(initialRoute.uid, false);
    }
    await refreshStatus();
  }
  initialize().catch((error) => toast(error.message));
  setInterval(refreshStatus, 8000);
})();
