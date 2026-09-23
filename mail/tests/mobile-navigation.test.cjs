const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

class Node {
  constructor() {
    this.events = new Map();
    this.style = {};
    this.classes = new Set();
    this.classList = {
      add: (name) => this.classes.add(name),
      remove: (name) => this.classes.delete(name),
      contains: (name) => this.classes.has(name),
      toggle: (name, force) => {
        const enabled = force === undefined ? !this.classes.has(name) : force;
        if (enabled) this.classes.add(name);
        else this.classes.delete(name);
      }
    };
  }
  addEventListener(name, handler) { this.events.set(name, handler); }
  fire(name, event = {}) { return this.events.get(name)?.(event); }
  setAttribute() {}
  replaceChildren() {}
  append() {}
}

const elements = new Map();
const element = (id) => {
  if (!elements.has(id)) elements.set(id, new Node());
  return elements.get(id);
};
const shell = element('shell');
const history = {
  state: null,
  backCalls: 0,
  replaceState(route) { this.state = route; },
  pushState(route) { this.state = route; },
  back() { this.backCalls++; }
};
const window = new Node();
window.parent = window;
const document = new Node();
document.querySelector = () => shell;
document.getElementById = element;
document.createElement = () => new Node();
document.createElementNS = () => new Node();
const context = {
  window, document, history, URL,
  location: {href: 'http://localhost:8099/', origin: 'http://localhost:8099'},
  matchMedia: () => ({matches: true, addEventListener() {}}),
  fetch: () => new Promise(() => {}),
  setInterval() {}, setTimeout() {}, clearTimeout() {}
};
const script = fs.readFileSync(path.join(__dirname, '../app/app.js'), 'utf8');
vm.runInNewContext(script, context);

function openFakeMessage() {
  shell.classList.add('show-reader');
  history.state = {homeMail: true, folder: 'INBOX', uid: 42, depth: 1};
}

openFakeMessage();
element('back-button').fire('click');
assert.equal(shell.classList.contains('show-reader'), false, 'back button closes the reader synchronously');
assert.equal(history.backCalls, 1);

const readingPane = element('reading-pane');
openFakeMessage();
readingPane.fire('touchstart', {touches: [{clientX: 24, clientY: 200}]});
readingPane.fire('touchmove', {touches: [{clientX: 125, clientY: 208}], preventDefault() {}});
readingPane.fire('touchend', {changedTouches: [{clientX: 140, clientY: 210}]});
assert.equal(shell.classList.contains('show-reader'), false, 'right-edge swipe closes the reader');
assert.equal(history.backCalls, 2);

openFakeMessage();
readingPane.fire('touchstart', {touches: [{clientX: 24, clientY: 200}]});
readingPane.fire('touchend', {changedTouches: [{clientX: 28, clientY: 350}]});
assert.equal(shell.classList.contains('show-reader'), true, 'vertical scroll does not navigate back');

readingPane.fire('touchstart', {touches: [{clientX: 180, clientY: 200}]});
readingPane.fire('touchend', {changedTouches: [{clientX: 310, clientY: 207}]});
assert.equal(shell.classList.contains('show-reader'), true, 'horizontal swipe away from edge does not navigate back');

async function startupFolder(defaultRole, folders) {
  const nodes = new Map();
  const get = (id) => {
    if (!nodes.has(id)) nodes.set(id, new Node());
    return nodes.get(id);
  };
  const startupHistory = {
    state: null,
    replaceState(route) { this.state = route; },
    pushState(route) { this.state = route; }
  };
  const startupWindow = new Node();
  startupWindow.parent = startupWindow;
  const startupDocument = new Node();
  startupDocument.querySelector = () => get('shell');
  startupDocument.getElementById = get;
  startupDocument.createElement = () => new Node();
  startupDocument.createElementNS = () => new Node();
  startupDocument.documentElement = new Node();
  const status = {email: '', configured: false, theme: 'system', view_mode: 'split',
    default_folder: defaultRole, show_external_media: true, cache_revision: 0};
  const responses = {'/api/status': status, '/api/folders': {folders}, '/api/messages': {messages: []}};
  vm.runInNewContext(script, {
    window: startupWindow, document: startupDocument, history: startupHistory, URL,
    location: {href: 'http://localhost:8099/', origin: 'http://localhost:8099'},
    matchMedia: () => ({matches: true, addEventListener() {}}),
    fetch: async (request) => ({ok: true, json: async () => responses[new URL(request).pathname]}),
    setInterval() {}, setTimeout() {}, clearTimeout() {}
  });
  await new Promise((resolve) => setImmediate(resolve));
  return startupHistory.state.folder;
}

(async () => {
  const folders = [{name: 'INBOX', role: 'INBOX', label: 'Входящие', unread: 0},
    {name: '[Gmail]/Important', role: 'IMPORTANT', label: 'Важное', unread: 0}];
  assert.equal(await startupFolder('IMPORTANT', folders), '[Gmail]/Important',
    'configured folder role selects the localized Gmail folder');
  assert.equal(await startupFolder('SENT', folders), 'INBOX',
    'missing configured folder falls back to Inbox');
})().catch((error) => { console.error(error); process.exitCode = 1; });
