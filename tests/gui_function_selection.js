"use strict";
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const elements = new Map();
const get = id => {
  if (!elements.has(id)) elements.set(id, { hidden: false, innerHTML: "", textContent: "", classList: { toggle() {} },
    querySelectorAll: () => [], setAttribute() {} });
  return elements.get(id);
};
const calls = [], pending = [];
const ctx = vm.createContext({ $: get, S: { fn: "one", ov: {kind: "fork"}, claims: [], editDirty: true },
  document: { querySelectorAll: () => [] },
  api: url => { calls.push(url); return new Promise((resolve, reject) => pending.push({resolve, reject})); },
  highlightAsm: x => x, esc: x => x, fnBadge() {}, renderFnChart() {}, setEditor() {},
  showFnTab() {}, toast() {}, fnLink() {}, clock() {}, hex8() {} });
const source = fs.readFileSync(path.join(__dirname, "../src/decompnet/app/static/js/function.js"), "utf8");
vm.runInContext(source.slice(source.indexOf("let fnBusy"), source.indexOf("/* Editing a function")), ctx);
const data = name => ({ name, asm: "assembly for " + name, scores: [], calls: [], called_by: [], notes: [], status: "open" });
(async () => {
  const first = vm.runInContext("refreshFn(true)", ctx);
  ctx.S.fn = "two"; await vm.runInContext("refreshFn(true)", ctx);
  ctx.S.fn = "three"; await vm.runInContext("refreshFn(true)", ctx);
  assert.equal(calls.length, 1);
  pending[0].resolve(data("one"));
  await new Promise(setImmediate);
  assert.equal(calls.length, 2);
  assert.match(calls[1], /name=three$/);
  assert.equal(get("fnAsm").innerHTML, "", "stale response must not replace the current selection");
  pending[1].resolve(data("three"));
  await first;
  assert.equal(ctx.S.fnDetail.name, "three");
  assert.equal(get("fnAsm").innerHTML, "assembly for three");
  assert.equal(get("fnLoad").hidden, true);
  ctx.S.fn = "four";
  const failure = vm.runInContext("refreshFn(true)", ctx);
  pending[2].reject(new Error("read failed")); await failure;
  assert.equal(get("fnLoadError").hidden, false);
  assert.equal(get("fnLoadError").textContent, "read failed");
  console.log("Latest function selection, stale responses and visible error checks passed");
})().catch(error => { console.error(error); process.exitCode = 1; });
