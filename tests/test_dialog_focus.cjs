// Exercise the production modal focus manager with a DOM double, no browser or user state.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const source = fs.readFileSync(path.join(__dirname, '../web/app.js'), 'utf8');
const html = fs.readFileSync(path.join(__dirname, '../web/index.html'), 'utf8');
const first = source.indexOf('  function canFocusDialogElement(');
const last = source.indexOf('  async function request(', first);
assert(first > 0 && last > first);
assert.equal((html.match(/role="dialog" tabindex="-1" aria-modal="true" aria-labelledby=/g) || []).length, 6,
  'All six named dialogs have a programmatically focusable container');

function fixture() {
  const nodes = new Map(), frames = [];
  const document = {activeElement: null, querySelector: () => nodes.get('app-shell')};
  function make(id, parent = null, tabIndex = 0) {
    const element = {id, parent, tabIndex, hidden: false, disabled: false, isConnected: true,
      inert: false, attributes: {}, children: [],
      closest() { let node = this; while (node) { if (node.hidden) return node; node = node.parent; } return null; },
      getClientRects() { return this.closest('[hidden]') ? [] : [{}]; },
      focus() { document.activeElement = this; },
      setAttribute(name, value) { this.attributes[name] = value; },
      removeAttribute(name) { delete this.attributes[name]; },
      contains(target) { while (target) { if (target === this) return true; target = target.parent; } return false; },
      querySelector() { return this.panel; },
      querySelectorAll() { return this.children; },
    };
    nodes.set(id, element);
    return element;
  }
  const shell = make('app-shell');
  const opener = make('opener', shell);
  make('tab-transcript', shell);
  document.activeElement = opener;
  for (const [id, controls] of [
    ['edit-dialog', ['edit-text', 'edit-cancel', 'edit-save']],
    ['region-dialog', ['region-picker-input', 'region-picker-cancel', 'region-picker-save']],
    ['reset-dialog', ['reset-cancel', 'reset-confirm']],
    ['manual-arrival-dialog', ['manual-arrival-cancel', 'manual-arrival-confirm']],
    ['arrival-dialog', ['arrival-skip', 'arrival-save']],
    ['rating-dialog', ['rating-note', 'rating-cancel', 'rating-save']],
  ]) {
    const backdrop = make(id); backdrop.hidden = true;
    backdrop.panel = make(id + '-panel', backdrop, -1);
    backdrop.children = controls.map(control => make(control, backdrop.panel));
  }
  const context = {document, $: id => nodes.get(id), activeDialog: null, dialogOpener: null,
    currentTab: 'transcript', regionSaving: false, tripBusy: false, recording: null, processing: false,
    requestAnimationFrame(callback) { frames.push(callback); },
    closeManualArrival() { context.setDialogOpen('manual-arrival-dialog', false); },
  };
  vm.createContext(context);
  vm.runInContext(source.slice(first, last), context);
  function key(name, shiftKey = false) {
    const event = {key: name, shiftKey, prevented: false,
      preventDefault() { this.prevented = true; }, stopPropagation() {}};
    context.handleDialogKeydown(event);
    return event;
  }
  return {context, nodes, document, shell, opener, key, flush() { while (frames.length) frames.shift()(); }};
}

{
  const f = fixture(), ui = f.context;
  assert(ui.setDialogOpen('edit-dialog', true, 'edit-text'));
  assert.equal(f.document.activeElement.id, 'edit-text');
  assert.equal(f.shell.inert, true);
  assert.equal(f.shell.attributes['aria-hidden'], 'true');
  f.nodes.get('edit-save').focus();
  assert(f.key('Tab').prevented);
  assert.equal(f.document.activeElement.id, 'edit-text', 'Tab wraps at the last dialog control');
  assert(f.key('Tab', true).prevented);
  assert.equal(f.document.activeElement.id, 'edit-save', 'Shift+Tab wraps at the first control');
  f.opener.focus(); ui.keepDialogFocus({target: f.opener});
  assert.equal(f.document.activeElement.id, 'edit-text', 'Programmatic background focus returns to the dialog');
  f.key('Escape'); f.flush();
  assert.equal(f.nodes.get('edit-dialog').hidden, true);
  assert.equal(f.shell.inert, false);
  assert.equal(f.shell.attributes['aria-hidden'], undefined);
  assert.equal(f.document.activeElement, f.opener, 'Cancel restores the actual opening control');
}
{
  const f = fixture(), ui = f.context;
  ui.setDialogOpen('edit-dialog', true, 'edit-text');
  for (const node of f.nodes.get('edit-dialog').children) node.disabled = true;
  f.key('Tab');
  assert.equal(f.document.activeElement.id, 'edit-dialog-panel', 'Busy dialogs retain focus even with no enabled control');
  f.key('Escape');
  assert.equal(f.nodes.get('edit-dialog').hidden, false, 'Escape cannot dismiss an edit being committed');
  f.nodes.get('edit-save').disabled = false;
  f.key('Escape');
  f.opener.isConnected = false;
  f.flush();
  assert.equal(f.document.activeElement.id, 'tab-transcript', 'Removed transcript controls have a visible fallback');
}
{
  const f = fixture(), ui = f.context;
  ui.setDialogOpen('region-dialog', true, 'region-picker-input');
  ui.regionSaving = true;
  f.key('Escape');
  assert.equal(f.nodes.get('region-dialog').hidden, false);
  ui.regionSaving = false;
  f.key('Escape'); f.flush();
  assert.equal(f.document.activeElement, f.opener);
}
{
  const f = fixture(), ui = f.context;
  ui.setDialogOpen('edit-dialog', true, 'edit-text');
  assert.equal(ui.setDialogOpen('arrival-dialog', true), false, 'Automatic arrival cannot cover an unfinished editor');
  assert.equal(f.nodes.get('arrival-dialog').hidden, true);
  ui.setDialogOpen('edit-dialog', false);
  assert.equal(ui.setDialogOpen('arrival-dialog', true), true);
  f.flush();
  assert.equal(f.document.activeElement.id, 'arrival-dialog-panel', 'Deferred restore cannot steal a new dialog focus');
  assert.equal(f.nodes.get('edit-dialog').hidden, true);
  f.key('Escape');
  assert.equal(f.nodes.get('arrival-dialog').hidden, false, 'Escape never invents a save or discard decision');
  f.key('Tab');
  assert.equal(f.document.activeElement.id, 'arrival-skip');
}
console.log('Dialog focus: initial focus, Tab containment, busy guards, return focus and deferred arrival passed.');
