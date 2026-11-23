"use strict";
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const elements = new Map();
const get = id => {
  if (!elements.has(id)) elements.set(id, { hidden: false, textContent: "", classList: { toggle() {} }, firstElementChild: { style: {} } });
  return elements.get(id);
};
const ctx = vm.createContext({ $: get, cFunctions: () => 100, plural: n => String(n) });
const source = fs.readFileSync(path.join(__dirname, "../src/decompnet/app/static/js/steering.js"), "utf8");
vm.runInContext(source.slice(source.indexOf("function renderAutopilot("), source.indexOf("const cwName")), ctx);
ctx.ov = { kind: "fork", ready: true, matched: 0, autopilot: { state: "running", phase: "finishing near matches",
  finish_scanned: 2, finish_total: 4, finish_matched: 1 } };
vm.runInContext("renderAutopilot(ov)", ctx);
assert.equal(get("apBar").hidden, false);
assert.equal(get("apBar").firstElementChild.style.width, "50%");
assert.match(get("apSub").textContent, /2 of 4 examined · 1 matched/);
ctx.ov.autopilot.state = "done";
ctx.ov.autopilot.matched = 1;
vm.runInContext("renderAutopilot(ov)", ctx);
assert.equal(get("apBar").hidden, true);
assert.match(get("apSub").textContent, /1 near matches finished from 2 examined/);
console.log("Finishing progress and completed summary passed");
