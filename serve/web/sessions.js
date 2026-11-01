// PolyStrata service-owned conversation history. Runs after app.js in same page.
"use strict";
let historyRecords = [];
function historySidebarSet(open) {
  if (!sessionV1) return;
  $("session-sidebar").classList.toggle("collapsed", !open);
  $("sidebar-expand").hidden = open;
  store.set("history-sidebar-open", open);
}
$("sidebar-toggle").onclick = () => historySidebarSet(false);
$("sidebar-expand").onclick = () => historySidebarSet(true);

function renderHistorySidebar(list) {
  historyRecords = list.slice();
  const wrapper = $("session-history");
  wrapper.replaceChildren();
  for (const group of [["Pinned", historyRecords.filter((s) => s.pinned)],
                       ["Recent", historyRecords.filter((s) => !s.pinned)]]) {
    if (group[0] === "Pinned" && !group[1].length) continue;
    const heading = document.createElement("div");
    heading.className = "session-heading";
    heading.textContent = group[0];
    wrapper.appendChild(heading);
    if (!group[1].length) {
      const none = document.createElement("div");
      none.className = "session-none";
      none.textContent = "No conversations yet";
      wrapper.appendChild(none);
    }
    for (const item of group[1]) {
      const row = document.createElement("div");
      row.className = "session-row" + (item.id === sessionId ? " selected" : "");
      const title = document.createElement("button");
      title.type = "button";
      title.className = "session-title";
      title.title = title.textContent = item.title || "New chat";
      title.onclick = async () => {
        if (busy) { toast("warn", "Wait for the response to finish"); return; }
        if (item.id === sessionId) return;
        try { await sessionSaveChain; await loadServerChat(item.id); }
        catch (e) { toast("error", "Cannot open conversation", String(e)); }
      };
      row.appendChild(title);
      if (item.kv_locked || item.kv_cached) {
        const flag = document.createElement("span");
        flag.className = "session-state";
        flag.textContent = item.kv_locked ? "♧" : "◈";
        flag.title = item.kv_locked ? "KV locked for at least one model" : "KV cached";
        row.appendChild(flag);
      }
      const more = document.createElement("button");
      more.type = "button";
      more.className = "session-dots";
      more.title = more.ariaLabel = "Conversation actions";
      more.textContent = "···";
      const menu = document.createElement("div");
      menu.className = "session-actions";
      menu.hidden = true;
      more.onclick = (e) => {
        e.stopPropagation();
        wrapper.querySelectorAll(".session-actions").forEach((node) => {
          if (node !== menu) node.hidden = true;
        });
        menu.hidden = !menu.hidden;
      };
      row.append(more, menu);
      const sizeText = item.kv_bytes ? " (" + (item.kv_bytes / 1048576).toFixed(0) + " MiB)" : "";
      const actions = [
        ["rename", "Rename"],
        ["pin", item.pinned ? "Unpin" : "Pin"],
        ["export", "Export Markdown"],
        ["kv", item.kv_locked_current ? "Release current model KV" + sizeText : "Lock current model KV"],
        ["delete", "Delete"]
      ];
      for (const pair of actions) {
        const action = document.createElement("button");
        action.type = "button";
        action.textContent = pair[1];
        action.onclick = async (e) => {
          e.stopPropagation();
          menu.hidden = true;
          await historyAction(item, pair[0]);
        };
        menu.appendChild(action);
      }
      wrapper.appendChild(row);
    }
  }
  $("session-sidebar").hidden = false;
  historySidebarSet(store.get("history-sidebar-open", true) !== false);
}
async function historyRequest(id, method = "GET", body = null) {
  const response = await fetch("v1/strata/sessions/" + encodeURIComponent(id), {
    method, headers: headers(method !== "GET"),
    ...(body !== null ? {body: JSON.stringify(body)} : {})
  });
  const content = await response.json().catch(() => ({}));
  if (!response.ok) throw Error(content.error?.message || "HTTP " + response.status);
  return content;
}
function historyToMarkdown(record) {
  const turns = record.web_messages?.length ? record.web_messages : record.api_messages || [];
  const content = turns.map((m) => {
    const body = typeof (m.text || m.content) === "string" ? (m.text || m.content) :
      JSON.stringify(m.content || "", null, 2);
    return "## " + (m.role === "user" ? "You" : "Assistant") + "\n\n" + body + "\n";
  });
  return "# " + (record.title || "Conversation") + "\n\n" + content.join("\n");
}
async function historyAction(record, action) {
  if (busy) { toast("warn", "Finish the response first"); return; }
  try {
    await sessionSaveChain;
    if (action === "rename") {
      const title = prompt("Conversation name", record.title || "");
      if (title === null) return;
      await historyRequest(record.id, "POST", {action: "rename", title: title.trim()});
    } else if (action === "pin") {
      await historyRequest(record.id, "POST", {action: "pin", pinned: !record.pinned});
    } else if (action === "export") {
      const saved = await historyRequest(record.id);
      const blob = new Blob([historyToMarkdown(saved)], {type: "text/markdown;charset=utf-8"});
      const anchor = document.createElement("a");
      anchor.href = URL.createObjectURL(blob);
      anchor.download = (saved.title || "conversation").replace(/[\\/:*?"<>|]/g, "_").slice(0, 70) + ".md";
      anchor.click();
      setTimeout(() => URL.revokeObjectURL(anchor.href), 5000);
      return;
    } else if (action === "kv") {
      const saved = await historyRequest(record.id, "POST",
        {action: "lock_kv", locked: !record.kv_locked_current});
      toast("info", saved.kv_pending ? "KV lock queued" : (saved.kv_locked_current ? "KV locked" : "KV released"),
        saved.kv_pending ? "The KV will be permanently saved after this model next processes the session." : "");
    } else if (action === "delete") {
      if (!confirm("Delete conversation and its KV snapshots permanently?\n" + (record.title || "New chat"))) return;
      await historyRequest(record.id, "DELETE");
      if (record.id === sessionId) {
        sessionId = makeSessionId();
        store.set("session-id", sessionId);
        messages = [];
        store.set("chat", []);
        renderChat();
      }
    }
    await refreshSessionPicker();
  } catch (e) {
    toast("error", "Conversation operation failed", String(e), 7500);
  }
}
