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
      add: (...names) => names.forEach((name) => this.classes.add(name)),
      remove: (...names) => names.forEach((name) => this.classes.delete(name)),
      contains: (name) => this.classes.has(name),
      toggle: (name, force) => {
        const enabled = force === undefined ? !this.classes.has(name) : force;
        if (enabled) this.classes.add(name);
        else this.classes.delete(name);
      }
    };
  }
  addEventListener(name, handler) {
    if (!this.events.has(name)) this.events.set(name, []);
    this.events.get(name).push(handler);
  }
  removeEventListener(name, handler) {
    this.events.set(name, (this.events.get(name) || []).filter((item) => item !== handler));
  }
  fire(name, event = {}) { for (const handler of this.events.get(name) || []) handler(event); }
  setAttribute() {}
  replaceChildren() {}
  append() {}
  attachShadow() {
    const root = new Node();
    root.querySelector = () => root.content || null;
    Object.defineProperty(root, 'innerHTML', {set(value) {
      root.markup = value;
      root.content = new Node();
      root.content.scrollWidth = 620;
    }});
    this.shadowRoot = root;
    return root;
  }
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
window.innerHeight = 800;
const document = new Node();
document.querySelector = () => shell;
document.getElementById = element;
document.createElement = () => new Node();
document.createElementNS = () => new Node();
const timers = [];
const context = {
  window, document, history, URL,
  location: {href: 'http://localhost:8099/', origin: 'http://localhost:8099'},
  matchMedia: () => ({matches: true, addEventListener() {}}),
  fetch: () => new Promise(() => {}),
  setInterval() {},
  setTimeout(callback) { timers.push(callback); return timers.length; },
  clearTimeout(id) { if (id) timers[id - 1] = null; },
  requestAnimationFrame() { return 1; }, cancelAnimationFrame() {},
  ResizeObserver: class { observe() {} disconnect() {} }
};
const script = fs.readFileSync(path.join(__dirname, '../app/app.js'), 'utf8');
vm.runInNewContext(script.replace('  function renderBody(message) {',
  '  window.renderBodyForTest = renderBody;\n  function renderBody(message) {'), context);

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
assert.equal(readingPane.style.transform, 'translate3d(101px,0,0)', 'reader tracks the finger');
readingPane.fire('touchend', {changedTouches: [{clientX: 140, clientY: 210}]});
assert.equal(shell.classList.contains('show-reader'), true, 'reader animates before closing');
timers.pop()();
assert.equal(shell.classList.contains('show-reader'), false, 'right-edge swipe closes the reader');
assert.equal(history.backCalls, 2);

openFakeMessage();
readingPane.fire('touchstart', {touches: [{clientX: 24, clientY: 200}]});
readingPane.fire('touchend', {changedTouches: [{clientX: 28, clientY: 350}]});
assert.equal(shell.classList.contains('show-reader'), true, 'vertical scroll does not navigate back');

readingPane.fire('touchstart', {touches: [{clientX: 24, clientY: 200}]});
readingPane.fire('touchmove', {touches: [{clientX: 65, clientY: 202}], preventDefault() {}});
readingPane.fire('touchend', {changedTouches: [{clientX: 68, clientY: 204}]});
assert.equal(readingPane.style.transform, 'translate3d(0px,0,0)', 'short swipe springs back');
timers.pop()();
assert.equal(shell.classList.contains('show-reader'), true, 'short swipe keeps the message open');

let prevented = false;
readingPane.fire('touchstart', {touches: [{clientX: 180, clientY: 200}]});
readingPane.fire('touchmove', {touches: [{clientX: 300, clientY: 207}], preventDefault() { prevented = true; }});
assert.equal(prevented, true, 'swipe from the middle owns horizontal movement');
assert.equal(readingPane.style.transform, 'translate3d(120px,0,0)', 'middle swipe moves the reader');
readingPane.fire('touchend', {changedTouches: [{clientX: 310, clientY: 207}]});
timers.pop()();
assert.equal(shell.classList.contains('show-reader'), false, 'swipe from the middle navigates back');

openFakeMessage();
readingPane.fire('touchstart', {touches: [{clientX: 180, clientY: 200}]});
readingPane.fire('touchmove', {touches: [{clientX: 188, clientY: 280}], preventDefault() { throw Error('vertical scroll prevented'); }});
readingPane.fire('touchend', {changedTouches: [{clientX: 310, clientY: 280}]});
assert.equal(shell.classList.contains('show-reader'), true, 'vertical movement from the middle still scrolls');

const mailFrame = element('detail-html');
mailFrame.clientWidth = 320;
window.renderBodyForTest({body: '', html: '<table width="620"><tr><td>Long mail</td></tr></table>', remoteLoaded: false});
const mailRoot = mailFrame.shadowRoot;
assert.match(mailRoot.markup, /\.mail-content/);
assert.equal(mailRoot.content.innerHTML, '<table width="620"><tr><td>Long mail</td></tr></table>');
assert.equal(mailRoot.content.style.zoom, String(320 / 620), 'wide email fits phone width');
let verticalPrevented = false;
mailRoot.fire('touchstart', {touches: [{clientX: 150, clientY: 200}]});
mailRoot.fire('touchmove', {touches: [{clientX: 152, clientY: 120}], preventDefault() { verticalPrevented = true; }});
assert.equal(verticalPrevented, false, 'HTML content leaves vertical scrolling native');
mailRoot.fire('touchend', {changedTouches: [{clientX: 152, clientY: 120}]});
mailRoot.fire('touchstart', {touches: [{clientX: 150, clientY: 200}]});
mailRoot.fire('touchmove', {touches: [{clientX: 250, clientY: 202}], preventDefault() {}});
assert.equal(readingPane.style.transform, 'translate3d(100px,0,0)', 'swipe still moves HTML email');
mailRoot.fire('touchcancel');
timers.pop()();
context.matchMedia = () => ({matches: false, addEventListener() {}});
mailFrame.mailResizeHandler();
assert.equal(mailRoot.content.style.zoom, '', 'desktop email is not scaled');

context.matchMedia = () => ({matches: true, addEventListener() {}});
shell.classList.remove('show-reader');
const listPane = element('list-pane');
const sidebar = element('sidebar');
sidebar.clientWidth = 250;
listPane.fire('touchstart', {touches: [{clientX: 100, clientY: 200}]});
listPane.fire('touchmove', {touches: [{clientX: 220, clientY: 203}], preventDefault() {}});
assert.equal(sidebar.style.transform, 'translate3d(-130px,0,0)', 'sidebar follows the finger');
listPane.fire('touchend', {changedTouches: [{clientX: 220, clientY: 203}]});
assert.equal(shell.classList.contains('show-sidebar'), true, 'right swipe opens folders');
sidebar.fire('touchstart', {touches: [{clientX: 200, clientY: 200}]});
sidebar.fire('touchmove', {touches: [{clientX: 170, clientY: 202}], preventDefault() {}});
sidebar.fire('touchend', {changedTouches: [{clientX: 170, clientY: 202}]});
assert.equal(shell.classList.contains('show-sidebar'), true, 'short left swipe restores folders');
sidebar.fire('touchstart', {touches: [{clientX: 200, clientY: 200}]});
sidebar.fire('touchmove', {touches: [{clientX: 200, clientY: 280}], preventDefault() { throw Error('vertical folder scroll prevented'); }});
sidebar.fire('touchend', {changedTouches: [{clientX: 200, clientY: 280}]});
assert.equal(shell.classList.contains('show-sidebar'), true, 'vertical folder scroll keeps sidebar open');
sidebar.fire('touchstart', {touches: [{clientX: 200, clientY: 200}]});
sidebar.fire('touchmove', {touches: [{clientX: 90, clientY: 202}], preventDefault() {}});
assert.equal(sidebar.style.transform, 'translate3d(-110px,0,0)', 'sidebar follows closing finger');
sidebar.fire('touchend', {changedTouches: [{clientX: 90, clientY: 202}]});
assert.equal(shell.classList.contains('show-sidebar'), false, 'left swipe closes folders');
listPane.fire('touchstart', {touches: [{clientX: 100, clientY: 200}]});
listPane.fire('touchmove', {touches: [{clientX: 220, clientY: 203}], preventDefault() {}});
listPane.fire('touchend', {changedTouches: [{clientX: 220, clientY: 203}]});
element('sidebar-scrim').fire('click');
assert.equal(shell.classList.contains('show-sidebar'), false);
listPane.fire('touchstart', {touches: [{clientX: 100, clientY: 200}]});
listPane.fire('touchmove', {touches: [{clientX: 105, clientY: 280}], preventDefault() { throw Error('vertical scroll prevented'); }});
listPane.fire('touchend', {changedTouches: [{clientX: 110, clientY: 280}]});
assert.equal(shell.classList.contains('show-sidebar'), false, 'vertical list scroll does not open folders');

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
