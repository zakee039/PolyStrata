import assert from "node:assert/strict";
import test from "node:test";
import { readFileSync } from "node:fs";

const css = readFileSync(new URL("../src/style.css", import.meta.url), "utf8");
const ui = readFileSync(new URL("../src/main.ts", import.meta.url), "utf8");
const native = readFileSync(new URL("../src-tauri/src/lib.rs", import.meta.url), "utf8");

test("LAN access toggle renders the persisted setting, including its on-state", () => {
  assert.match(ui, /data-setting="lan_access"/);
  assert.match(ui, /prefs\?\.lan_access\?"checked":""/);
  assert.match(css, /\.api-compact \.toggle-compact input:checked \+ \.switch\s*\{[^}]*background:/);
  assert.match(css, /\.api-compact \.toggle-compact input:checked \+ \.switch:before\s*\{[^}]*translateX/);
});

test("direct LAN exposure is described as HTTP and the default is loopback", () => {
  assert.match(ui, /局域网 HTTP 直连/);
  assert.match(native, /lan_access:\s*false/);
  assert.match(native, /if p\.lan_access \{ "0\.0\.0\.0" \} else \{ "127\.0\.0\.1" \}/);
});

test("enabled LAN displays an up-to-date clickable Chat address", () => {
  assert.match(ui, /function lanChatLink\(/);
  assert.match(ui, /lan_chat_url/);
  assert.match(ui, /class="lan-chat-link"/);
  assert.match(ui, /!prefs\?\.lan_access/);
  assert.match(native, /fn active_lan_ipv4\(/);
  assert.match(native, /"lan_chat_url":lan_chat_url/);
  assert.match(native, /if prefs\.lan_access && owned && metrics\.is_some\(\)/);
});
