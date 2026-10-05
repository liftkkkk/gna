/* GNA SPA —— 流式干活 + 记忆/审计/扩展/设置 */
const $ = (id) => document.getElementById(id);
const esc = (s) => String(s ?? "").replace(/&/g, "&amp;").replace(/</g, "&lt;");
const md = (s) => { try { return marked.parse(String(s ?? "")); } catch { return "<p>" + esc(s) + "</p>"; } };
const now = () => new Date().toTimeString().slice(0, 8);

/* ---------------- 导航 ---------------- */
document.querySelectorAll(".nav-btn").forEach(b => b.onclick = () => {
  document.querySelectorAll(".nav-btn").forEach(x => x.classList.toggle("active", x === b));
  document.querySelectorAll(".page").forEach(p => p.classList.toggle("active", p.id === "page-" + b.dataset.page));
  ({ memory: loadMemory, audit: loadAudit, ext: loadExt, settings: loadSettings }[b.dataset.page] || (() => {}))();
});

async function api(path, opts) {
  const r = await fetch(path, { headers: { "Content-Type": "application/json" }, ...opts });
  if (!r.ok) throw new Error((await r.json().catch(() => ({}))).detail || r.statusText);
  return r.json();
}

/* ---------------- 状态 pill ---------------- */
async function refreshStatus() {
  try {
    const s = await api("/api/status");
    const engine = s.backend === "gx" ? "GX 引擎" : "networkx 兜底";
    $("status-text").textContent = `${s.model} ｜ ${engine}${s.project_root ? " ｜ 项目模式" : ""}`;
  } catch { $("status-text").textContent = "内核离线"; }
}

/* ---------------- 干活（NDJSON 流式）---------------- */
const ICONS = { trace: ["·", ""], plan: ["🧭", "plan"], step: ["", "ok"], gate: ["🚧", "warn"],
                done: ["✅", "ok"], answer: ["🤖", ""], error: ["❌", "bad"] };

function feedAdd(text, cls) {
  const box = $("feed");
  if (box.querySelector(".feed-empty")) box.innerHTML = "";
  const d = document.createElement("div");
  d.className = "ev " + (cls || "");
  d.innerHTML = esc(text) + `<span class="t">${now()}</span>`;
  box.appendChild(d);
  box.scrollTop = box.scrollHeight;
  const n = box.children.length;
  $("feed-count").textContent = n ? `（${n} 个事件）` : "";
}

let history = [];
let streaming = false;

async function send() {
  if (streaming) return;
  const text = $("input").value.trim();
  if (!text) return;
  streaming = true;
  $("send").disabled = true;
  $("live-dot").classList.add("on");
  $("chat").insertAdjacentHTML("beforeend",
    `<div class="msg user">${esc(text).replace(/\n/g, "<br>")}</div>`);
  $("input").value = "";
  $("feed").innerHTML = "";

  const bubble = document.createElement("div");
  bubble.className = "msg gna";
  bubble.innerHTML = `<span class="muted">思考中…</span>`;
  $("chat").appendChild(bubble);
  $("chat").scrollTop = $("chat").scrollHeight;

  let answer = "";
  try {
    const r = await fetch("/api/chat", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ text, history: history.slice(-6), auto_gate: $("auto-gate").checked })
    });
    const reader = r.body.getReader();
    const dec = new TextDecoder();
    let buf = "";
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      buf += dec.decode(value, { stream: true });
      let idx;
      while ((idx = buf.indexOf("\n")) >= 0) {
        const line = buf.slice(0, idx).trim(); buf = buf.slice(idx + 1);
        if (!line) continue;
        let ev; try { ev = JSON.parse(line); } catch { continue; }
        if (ev.t === "trace") feedAdd(ev.line, "");
        else if (ev.t === "plan") feedAdd("计划路径（代价 " + ev.cost + "）：\n" + ev.path.join(" → "), "plan");
        else if (ev.t === "step") feedAdd(`[${ev.index}] ${ev.skill} ${ev.ok ? "✅" : "❌"} ${String(ev.output).slice(0, 90)}`, ev.ok ? "ok" : "bad");
        else if (ev.t === "gate") feedAdd("【门控】" + ev.message + " → " + (ev.allowed ? "放行" : "拒绝"), "warn");
        else if (ev.t === "done") feedAdd("任务收尾：" + (ev.ok ? "成功" : "失败"), ev.ok ? "ok" : "bad");
        else if (ev.t === "answer") answer = ev.text;
        else if (ev.t === "error") { answer = "❌ " + ev.message; feedAdd(ev.message, "bad"); }
      }
    }
  } catch (e) {
    answer = "❌ 连接中断：" + e.message;
  }
  bubble.innerHTML = answer ? md(answer) : "<span class='muted'>（无输出）</span>";
  if (answer) {
    history.push({ role: "user", content: text });
    history.push({ role: "assistant", content: answer });
  }
  streaming = false;
  $("send").disabled = false;
  $("live-dot").classList.remove("on");
  $("chat").scrollTop = $("chat").scrollHeight;
}
$("send").onclick = send;
$("input").addEventListener("keydown", e => {
  if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); send(); }
});

/* ---------------- 记忆 ---------------- */
async function loadMemory() {
  const d = await api("/api/memory");
  $("memory-hero").innerHTML = md(d.hero);
  $("memory-knowledge").innerHTML = md(d.knowledge);
  $("memory-skills").innerHTML = md(d.skills);
}
$("recall-btn").onclick = async () => {
  $("recall-out").innerHTML = "<span class='muted'>检索中…</span>";
  const d = await api("/api/memory/search?q=" + encodeURIComponent($("recall-q").value));
  $("recall-out").innerHTML = md(d.md);
};

/* ---------------- 审计 ---------------- */
let auditRaw = false;
async function loadAudit() {
  const d = await api("/api/timeline?raw=" + (auditRaw ? 1 : 0));
  $("timeline").innerHTML = md(d.md);
}
$("audit-toggle").onclick = async () => {
  auditRaw = !auditRaw;
  $("audit-toggle").textContent = auditRaw ? "返回人话时间线" : "查看原始 ΔW 事件";
  await loadAudit();
};

/* ---------------- 扩展 ---------------- */
async function loadExt() {
  const d = await api("/api/ext");
  $("mcp-list").innerHTML = d.mcp.length
    ? md(d.mcp.map(m => `- **${m.name}**（${m.enabled ? "启用" : "禁用"}）：\`${m.command} ${m.args.join(" ")}\``).join("\n"))
    : "<p class='muted'>（无——粘贴你的 mcpServers JSON 导入，例如 gx-memory）</p>";
  $("skill-list").innerHTML = d.skills.length
    ? md(d.skills.map(s => `- 【${s.name}】${s.description}`).join("\n"))
    : "<p class='muted'>（尚未安装自定义技能——在下方导入或创建）</p>";
  $("mem-dir").textContent = `${d.memory.dir}（${d.memory.count} 条，待入库 ${d.memory.pending}）`;
  const mf = await api("/api/memory/files");
  $("mem-files").innerHTML = mf.files.length
    ? md(mf.files.map(f => `- ${f.name}（${(f.size / 1024).toFixed(1)} KB）${f.ingested ? "✅已入图" : "⏳待入库"}`).join("\n"))
    : "<p class='muted'>（暂无记忆文件）</p>";
}
function extStatus(msg) { $("ext-status").innerHTML = md("✅ " + msg); loadExt(); }
$("mcp-add").onclick = async () => {
  try {
    const d = await api("/api/mcp", { method: "POST", body: JSON.stringify({
      name: $("mcp-name").value, command: $("mcp-cmd").value,
      args: $("mcp-args").value.split(",").map(a => a.trim()).filter(Boolean),
      env: $("mcp-env").value.trim() ? JSON.parse($("mcp-env").value) : {} }) });
    extStatus(d.ok ? `MCP 已注册（${d.tools.length} 个工具：${d.tools.join(", ")}）` : `已保存但连接失败：${d.error}`);
  } catch (e) { $("ext-status").innerHTML = md("❌ " + e.message); }
};
$("mcp-import").onclick = async () => {
  try {
    const d = await api("/api/mcp/import", { method: "POST", body: JSON.stringify({ json: $("mcp-json").value }) });
    extStatus(`已从 JSON 导入 ${d.count} 个 MCP 服务器：${d.names.join(", ")}（重启前端后工具自动注册）`);
    refreshStatus();
  } catch (e) { $("ext-status").innerHTML = md("❌ 导入失败：" + e.message); }
};
$("mcp-test-all").onclick = async () => {
  $("ext-status").innerHTML = "<span class='muted'>测试中…（每个服务器需拉起进程，稍候）</span>";
  const d = await api("/api/ext");
  for (const m of d.mcp) {
    if (!m.enabled) continue;
    try {
      const r = await api("/api/mcp/test", { method: "POST", body: JSON.stringify({ name: m.name }) });
      $("ext-status").innerHTML = md((r.ok ? `✅ ${m.name}：${r.tools.join(", ")}` : `❌ ${m.name}：${r.error}`));
    } catch (e) { $("ext-status").innerHTML = md(`❌ ${m.name}：${e.message}`); }
  }
  loadExt();
};
$("mcp-del-btn").onclick = async () => { await api("/api/mcp/" + encodeURIComponent($("mcp-del").value), { method: "DELETE" }); extStatus("已删除"); };
$("sk-import-btn").onclick = async () => {
  try {
    const d = await api("/api/skills/import", { method: "POST", body: JSON.stringify({ path: $("sk-import").value }) });
    extStatus(`技能包已导入：${d.name}（相关任务自动注入对话）`);
  } catch (e) { $("ext-status").innerHTML = md("❌ 导入失败：" + e.message); }
};
$("sk-add").onclick = async () => {
  await api("/api/skills", { method: "POST", body: JSON.stringify({
    name: $("sk-name").value, description: $("sk-desc").value, body: $("sk-body").value }) });
  extStatus("技能已保存（相关任务自动注入对话）");
};
$("sk-del-btn").onclick = async () => { await api("/api/skills/" + encodeURIComponent($("sk-del").value), { method: "DELETE" }); extStatus("已删除"); };
$("mem-import-btn").onclick = async () => {
  try {
    const d = await api("/api/memory/import", { method: "POST", body: JSON.stringify({ path: $("mem-import").value }) });
    extStatus(`已导入 ${d.imported} 个记忆文件：${d.files.join(", ")}（重启前端后自动入图）`);
  } catch (e) { $("ext-status").innerHTML = md("❌ 导入失败：" + e.message); }
};
$("mem-add").onclick = async () => {
  const d = await api("/api/memory/remember", { method: "POST", body: JSON.stringify({
    title: $("mem-title").value, content: $("mem-content").value }) });
  extStatus(`已存入记忆并入图（${d.facts} 条事实）← ${d.file}`);
};

/* ---------------- 设置 ---------------- */
async function loadSettings() {
  const d = await api("/api/models");
  const cur = d.profiles.find(p => p.id === d.active) || {};
  $("cur-model").innerHTML = `<div><div class="name">${esc(cur.name || d.model)}</div>` +
    `<div class="meta">${esc(d.model)} ｜ ${esc(cur.base_url || "离线 Mock")}</div></div>` +
    `<div class="badge">${d.project_root ? "项目模式" : "默认沙箱"}</div>`;
  const sel = $("pf-select");
  sel.innerHTML = d.profiles.map(p => `<option value="${p.id}">${p.name}</option>`).join("");
  sel.value = d.active;
  $("proj-input").value = d.project_root || "";
}
$("pf-activate").onclick = async () => {
  await api("/api/models/activate", { method: "POST", body: JSON.stringify({ id: $("pf-select").value }) });
  $("pf-status").innerHTML = md("✅ 已启用 " + $("pf-select").value); refreshStatus();
};
$("pf-save").onclick = async () => {
  try {
    const d = await api("/api/models/save", { method: "POST", body: JSON.stringify({
      name: $("pf-name").value, model: $("pf-model").value, base_url: $("pf-base").value,
      api_key: $("pf-key").value, temperature: 0.3 }) });
    $("pf-status").innerHTML = md("✅ 已保存并启用 " + d.active); loadSettings(); refreshStatus();
  } catch (e) { $("pf-status").innerHTML = md("❌ " + e.message); }
};
$("pf-test").onclick = async () => {
  $("pf-status").innerHTML = "<span class='muted'>测试中…</span>";
  const d = await api("/api/models/test", { method: "POST", body: JSON.stringify({
    model: $("pf-model").value, base_url: $("pf-base").value, api_key: $("pf-key").value, temperature: 0.3 }) });
  $("pf-status").innerHTML = md(d.ok ? `✅ 连通：${d.reply}` : `❌ ${d.error}`);
};
$("proj-save").onclick = async () => {
  try {
    const d = await api("/api/project", { method: "POST", body: JSON.stringify({ path: $("proj-input").value }) });
    $("proj-status").innerHTML = md("✅ 沙箱根 → " + d.workspace); refreshStatus();
  } catch (e) { $("proj-status").innerHTML = md("❌ " + e.message); }
};

/* ---------------- 启动 ---------------- */
refreshStatus();
