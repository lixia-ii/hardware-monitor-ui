"use strict";
// No browser dependencies: exercise the actual device form handlers with a small DOM stub.
// Run with: node --test tests/unit/test_web_devices.js
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");
const source = fs.readFileSync(path.join(__dirname, "../../src/hwmonitor/system/web/app.js"), "utf8")
  .replace(/\nconnect\(\);\s*$/, "\n"); // Do not start network polling.

function fixture(kind = "tplink", saveError = null) {
  const nodes = new Map();
  function node(selector) {
    if (!nodes.has(selector)) nodes.set(selector, {
      handlers: {}, disabled: false, closed: false,
      addEventListener(name, handler) {this.handlers[name] = handler;},
      close() {this.closed = true;},
      querySelector(q) {return node(selector + " " + q);},
      querySelectorAll() {return this.buttons || [];},
    });
    return nodes.get(selector);
  }
  const device = {id:"tp", type:kind, name:"路由器", url:"http://192.168.0.1", enabled:false,
    nic:null, password_saved:true, notify_clients:true};
  const form = node("#device-form");
  form.elements = {
    name:{value:device.name}, type:{value:kind}, url:{value:device.url}, enabled:{checked:false},
    password:{value:""}, notify_clients:{checked:true},
  };
  form.buttons = [{name:"", disabled:false}, {name:"reconnect", disabled:false}];
  const saved = [], messages = [];
  const context = vm.createContext({
    document:{querySelector:node, addEventListener() {}},
    window:{addEventListener() {}}, device,
    recordSave:async (changes, revision) => {
      assert.ok(form.buttons.every(b => b.disabled), "Disable both submit buttons during the request");
      if (saveError) throw saveError;
      saved.push({changes:JSON.parse(JSON.stringify(changes)), revision});
    },
    recordToast:message => messages.push(message),
  });
  vm.runInContext(source, context);
  vm.runInContext(`snapshot={settings:{routers:[device]}}; editorDevice=device;
    editingRevision="original-revision"; saveChanges=recordSave; toast=recordToast; renderDevices=()=>{};`, context);
  return {form, node, saved, messages, context, submit:async name => {
    await form.handlers.submit({target:form, submitter:{name}, preventDefault() {}});
  }};
}

test("TP-Link reconnect is a direct submit button, not a persistent checkbox", () => {
  const f = fixture();
  vm.runInContext("deviceFields(device)", f.context);
  const html = f.node("#device-fields").innerHTML;
  assert.match(html, /保存并重新连接/);
  assert.doesNotMatch(html, /<input[^>]+name="reconnect"/);
  assert.equal(f.node("#device-reconnect").hidden, false);
  const template = fs.readFileSync(path.join(__dirname, "../../src/hwmonitor/system/web/index.html"), "utf8");
  assert.match(template, /<button type="submit" name="reconnect"/);
  assert.ok(template.indexOf("保存设备</button>") < template.indexOf('name="reconnect"'),
    "Enter should default to ordinary save, not re-enable monitoring");
  for (const kind of ["openwrt", "hwmonitor"]) {
    vm.runInContext(`deviceFields({...device,type:${JSON.stringify(kind)}})`, f.context);
    assert.equal(f.node("#device-reconnect").hidden, true);
  }
});

test("reconnect saves current edits, enables monitoring and requests one retry", async () => {
  const f = fixture();
  f.form.elements.name.value = "改名后的路由器";
  await f.submit("reconnect");
  assert.equal(f.saved.length, 1);
  const device = f.saved[0].changes.routers[0];
  assert.equal(device.reconnect, true);
  assert.equal(device.enabled, true);
  assert.equal(device.password, ""); // Backend retains/re-protects the saved credential.
  assert.equal(device.name, "改名后的路由器");
  assert.equal(f.saved[0].revision, "original-revision");
  assert.ok(f.node("#device-dialog").closed);
  assert.match(f.messages[0], /已安排重新连接/); // Do not claim login has already succeeded.
  assert.ok(f.form.buttons.every(b => !b.disabled));
});

test("ordinary save does not reconnect or enable a disabled device", async () => {
  for (const kind of ["tplink", "openwrt", "hwmonitor"]) {
    const f = fixture(kind);
    await f.submit("");
    const device = f.saved[0].changes.routers[0];
    assert.equal(device.enabled, false);
    assert.equal(device.reconnect, kind === "tplink" ? false : undefined);
    assert.equal(f.messages.length, 0);
  }
});

test("reconnect works when SubmitEvent.submitter is unavailable", async () => {
  const f = fixture();
  // Older iOS/Safari: clicking the button still fires its click handler, but
  // the submit event carries no submitter.
  f.node("#device-reconnect").handlers.click();
  await f.form.handlers.submit({target:f.form, preventDefault() {}});
  assert.equal(f.saved.length, 1);
  const device = f.saved[0].changes.routers[0];
  assert.equal(device.reconnect, true);
  assert.equal(device.enabled, true);
});

test("failed reconnect keeps edits open and re-enables both save buttons", async () => {
  const f = fixture("tplink", new Error("设备密码无法保存，请重新输入"));
  await f.submit("reconnect");
  assert.equal(f.saved.length, 0);
  assert.equal(f.node("#device-dialog").closed, false);
  assert.match(f.node("#device-error").textContent, /请重新输入/);
  assert.ok(f.form.buttons.every(b => !b.disabled));
  assert.equal(f.messages.length, 0);
});
