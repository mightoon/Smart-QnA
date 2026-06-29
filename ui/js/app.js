/* Smart QnA 控制台前端逻辑。 */
(function () {
  "use strict";

  const $ = (sel) => document.querySelector(sel);
  const $$ = (sel) => document.querySelectorAll(sel);

  // -------------------- 标签切换 --------------------
  $$(".tab").forEach((tab) => {
    tab.addEventListener("click", () => {
      $$(".tab").forEach((t) => t.classList.remove("active"));
      tab.classList.add("active");
      const target = tab.dataset.tab;
      $$(".panel").forEach((p) => p.classList.remove("active"));
      $("#tab-" + target).classList.add("active");
      if (target === "config") loadConfig();
    });
  });

  // -------------------- 健康检查 --------------------
  async function checkHealth() {
    const dot = $("#healthDot");
    const text = $("#healthText");
    try {
      const resp = await fetch("/api/health");
      if (!resp.ok) throw new Error("bad status");
      dot.className = "dot ok";
      text.textContent = "服务正常";
    } catch (e) {
      dot.className = "dot err";
      text.textContent = "服务不可用";
    }
  }
  checkHealth();
  setInterval(checkHealth, 30000);

  // -------------------- 工具函数 --------------------
  function esc(s) {
    return String(s).replace(/[&<>"']/g, (c) =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c])
    );
  }

  // ================================================================
  // 问答测试
  // ================================================================
  const messages = $("#messages");
  const queryInput = $("#queryInput");
  const sendBtn = $("#sendBtn");
  const clearBtn = $("#clearBtn");
  const sourceList = $("#sourceList");
  const apiCall = $("#apiCall");

  function addMsg(role, text) {
    const div = document.createElement("div");
    div.className = "msg " + role;
    div.textContent = text;
    messages.appendChild(div);
    messages.scrollTop = messages.scrollHeight;
    return div;
  }

  function renderSources(sources) {
    if (!sources || !sources.length) {
      sourceList.textContent = "—";
      return;
    }
    sourceList.innerHTML = sources
      .map(
        (s, i) =>
          `<div class="item">[${i + 1}] <b>${esc(s.source)}</b>` +
          (s.title ? " · " + esc(s.title) : "") +
          (s.score ? " (" + Number(s.score).toFixed(2) + ")" : "") +
          `</div>`
      )
      .join("");
  }

  // 「调用接口」显示本服务被调用的接口与请求参数
  function renderApiCall(reqBody) {
    const endpoint = "POST /api/chat";
    const json = JSON.stringify(reqBody, null, 2);
    apiCall.innerHTML =
      '<div class="endpoint">' + esc(endpoint) + "</div>" +
      "<pre>" + esc(json) + "</pre>";
  }

  sendBtn.addEventListener("click", async () => {
    const query = queryInput.value.trim();
    if (!query) return;
    const mode = $("#modeSelect").value;
    const top_k = parseInt($("#topkInput").value, 10) || 5;
    const stream = $("#streamCheck").checked;

    const reqBody = { query, mode: mode || null, top_k, stream };
    addMsg("user", query);
    queryInput.value = "";
    sendBtn.disabled = true;
    sourceList.textContent = "检索中…";
    apiCall.textContent = "—";
    renderApiCall(reqBody);

    if (stream) {
      await streamChat(reqBody);
    } else {
      await blockingChat(reqBody);
    }
    sendBtn.disabled = false;
  });

  clearBtn.addEventListener("click", () => {
    messages.innerHTML = "";
    sourceList.textContent = "—";
    apiCall.textContent = "—";
  });

  queryInput.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      sendBtn.click();
    }
  });

  // 流式（SSE）
  async function streamChat(reqBody) {
    const assistant = addMsg("assistant", "");
    let buf = "";
    let pending = "";

    const resp = await fetch("/api/chat", {
      method: "POST",
      headers: { "Content-Type": "application/json", Accept: "text/event-stream" },
      body: JSON.stringify(reqBody),
    });

    if (!resp.ok || !resp.body) {
      addMsg("error", "请求失败：" + resp.status);
      return;
    }

    const reader = resp.body.getReader();
    const decoder = new TextDecoder();

    while (true) {
      const { value, done } = await reader.read();
      if (done) break;
      pending += decoder.decode(value, { stream: true });
      const blocks = pending.split("\n\n");
      pending = blocks.pop();
      for (const block of blocks) {
        handleEvent(block, {
          onMeta: (data) => renderSources(data.sources),
          onToken: (data) => {
            buf += data.content;
            assistant.textContent = buf;
            messages.scrollTop = messages.scrollHeight;
          },
          onError: (data) => addMsg("error", "错误：" + (data.error?.message || "未知")),
          onDone: () => {
            if (!buf) assistant.textContent = "(空回复)";
          },
        });
      }
    }
  }

  function handleEvent(block, cbs) {
    let event = "message";
    let dataStr = "";
    for (const line of block.split("\n")) {
      if (line.startsWith("event:")) event = line.slice(6).trim();
      else if (line.startsWith("data:")) dataStr += line.slice(5).trim();
    }
    if (!dataStr) return;
    let data = {};
    try { data = JSON.parse(dataStr); } catch (e) { return; }
    if (event === "meta") cbs.onMeta(data);
    else if (event === "token") cbs.onToken(data);
    else if (event === "error") cbs.onError(data);
    else if (event === "done") cbs.onDone(data);
  }

  // 非流式
  async function blockingChat(reqBody) {
    const assistant = addMsg("assistant", "思考中…");
    try {
      const resp = await fetch("/api/chat", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(reqBody),
      });
      const data = await resp.json();
      if (!resp.ok) {
        assistant.className = "msg error";
        assistant.textContent = "错误：" + (data.error?.message || resp.status);
        return;
      }
      assistant.textContent = data.answer || "(空回复)";
      renderSources(data.sources);
    } catch (e) {
      assistant.className = "msg error";
      assistant.textContent = "请求异常：" + e.message;
    }
  }

  // ================================================================
  // 配置管理：每个工具多份配置，CRUD + 切换 + 验证
  // ================================================================
  const SECTION_META = {
    llm: { title: "大模型 (LLM)", fields: [
      { key: "name", label: "配置名称", type: "text" },
      { key: "api_base", label: "API Base", type: "text" },
      { key: "api_key", label: "API Key", type: "password", sensitive: true },
      { key: "model", label: "Model", type: "text" },
      { key: "entity_model", label: "Entity Model", type: "text" },
      { key: "temperature", label: "Temperature", type: "number" },
      { key: "max_tokens", label: "Max Tokens", type: "number" },
    ]},
    elasticsearch: { title: "Elasticsearch", fields: [
      { key: "name", label: "配置名称", type: "text" },
      { key: "version", label: "ES 版本", type: "select", options: [
        { value: "v8", label: "v8" },
        { value: "v7", label: "v7" },
      ]},
      { key: "url", label: "URL", type: "text" },
      { key: "username", label: "Username（无认证可留空）", type: "text" },
      { key: "password", label: "Password（无认证可留空）", type: "password", sensitive: true },
      { key: "article_index", label: "Article Index", type: "text" },
      { key: "article_fields", label: "Article 字段映射", type: "fieldsmapping" },
      { key: "qna_index", label: "QnA Index", type: "text" },
      { key: "qna_fields", label: "QnA 字段映射", type: "fieldsmapping" },
      { key: "verify_certs", label: "Verify Certs", type: "checkbox" },
    ]},
    neo4j: { title: "Neo4j 图数据库", fields: [
      { key: "name", label: "配置名称", type: "text" },
      { key: "uri", label: "URI", type: "text" },
      { key: "username", label: "Username", type: "text" },
      { key: "password", label: "Password", type: "password", sensitive: true },
      { key: "database", label: "Database", type: "text" },
      { key: "node_key", label: "节点主键属性名", type: "text" },
      { key: "max_neighbors", label: "最大邻居数", type: "number" },
      { key: "excluded_relations", label: "排除的关系类型 (逗号分隔)", type: "text" },
    ]},
    rerank: { title: "Rerank 重排模型", fields: [
      { key: "name", label: "配置名称", type: "text" },
      { key: "api_base", label: "API Base", type: "text" },
      { key: "api_key", label: "API Key", type: "password", sensitive: true },
      { key: "model", label: "Model", type: "text" },
      { key: "top_n", label: "Top N（融合后保留条数）", type: "number" },
    ]},
    embedding: { title: "Embedding 向量化模型", fields: [
      { key: "name", label: "配置名称", type: "text" },
      { key: "api_base", label: "API Base", type: "text" },
      { key: "api_key", label: "API Key", type: "password", sensitive: true },
      { key: "model", label: "Model", type: "text" },
    ]},
    milvus: { title: "Milvus 向量数据库", fields: [
      { key: "name", label: "配置名称", type: "text" },
      { key: "uri", label: "URI", type: "text" },
      { key: "collection_name", label: "Collection", type: "text" },
      { key: "vector_field", label: "Vector Field", type: "text" },
      { key: "text_field", label: "Text Field", type: "text" },
      { key: "metric_type", label: "Metric Type", type: "text" },
    ]},
  };

  let configCache = null; // 全部脱敏配置

  $("#loadConfigBtn").addEventListener("click", loadConfig);

  async function loadConfig() {
    try {
      const resp = await fetch("/api/config");
      const data = await resp.json();
      if (!resp.ok) {
        alert("加载失败：" + (data.error?.message || resp.status));
        return;
      }
      configCache = data;
      renderConfigSections(data);
    } catch (e) {
      alert("加载异常：" + e.message);
    }
  }

  function renderConfigSections(data) {
    const root = $("#configSections");
    root.innerHTML = "";
    const sections = Object.keys(SECTION_META);
    sections.forEach((section, idx) => {
      const card = document.createElement("div");
      card.className = "cfg-card";
      card.dataset.section = section;
      card.innerHTML =
        '<div class="cfg-card-head"><h2>' + esc(SECTION_META[section].title) +
        '</h2><button class="primary small" data-act="add">+ 新增配置</button></div>' +
        '<div class="cfg-list" data-role="list"></div>' +
        '<div class="cfg-editor" data-role="editor" hidden></div>';
      root.appendChild(card);
      renderItemList(section, data[section]);

      if (idx < sections.length - 1) {
        const hr = document.createElement("hr");
        hr.className = "cfg-divider";
        root.appendChild(hr);
      }
    });

    // server 段
    const serverCard = document.createElement("div");
    serverCard.className = "cfg-card";
    serverCard.innerHTML =
      '<div class="cfg-card-head"><h2>服务 (Server)</h2></div>' +
      '<div class="cfg-server">' +
        '<label>Host<input id="srvHost" type="text" /></label>' +
        '<label>Port<input id="srvPort" type="number" /></label>' +
        '<button class="primary small" data-act="save-server">保存</button>' +
      '</div>';
    root.appendChild(serverCard);
    $("#srvHost", serverCard).value = data.server?.host || "";
    $("#srvPort", serverCard).value = data.server?.port || "";
    serverCard.querySelector('[data-act="save-server"]').addEventListener("click", saveServer);
  }

  function renderItemList(section, secData) {
    const card = $(".cfg-card[data-section='" + section + "']");
    const listEl = card.querySelector('[data-role="list"]');
    const items = (secData && secData.items) || {};
    const active = (secData && secData.active) || null;
    listEl.innerHTML = "";
    Object.keys(items).forEach((id) => {
      const item = items[id];
      const row = document.createElement("div");
      row.className = "cfg-item" + (id === active ? " active" : "");
      row.innerHTML =
        '<span class="name">' + esc(item.name || id) +
          (id === active ? ' <span class="badge">生效中</span>' : "") +
        '</span>' +
        '<span class="validate-result" data-role="vres"></span>' +
        '<span class="actions">' +
          (id !== active ? '<button class="small success" data-act="activate">设为生效</button>' : "") +
          '<button class="small ghost" data-act="validate">验证</button>' +
          '<button class="small ghost" data-act="edit">编辑</button>' +
          '<button class="small danger" data-act="delete">删除</button>' +
        '</span>';
      row.dataset.id = id;
      listEl.appendChild(row);
    });

    listEl.querySelectorAll(".cfg-item").forEach((row) => {
      const id = row.dataset.id;
      row.querySelector('[data-act="activate"]')?.addEventListener("click", () => activateItem(section, id, row));
      row.querySelector('[data-act="validate"]')?.addEventListener("click", () => validateExisting(section, id, row));
      row.querySelector('[data-act="edit"]')?.addEventListener("click", () => openEditor(section, id));
      row.querySelector('[data-act="delete"]')?.addEventListener("click", () => deleteItem(section, id));
    });

    card.querySelector('[data-act="add"]')?.addEventListener("click", () => openEditor(section, null));
  }

  // ---------- 编辑表单 ----------
  function openEditor(section, itemId) {
    const card = $(".cfg-card[data-section='" + section + "']");
    const editor = card.querySelector('[data-role="editor"]');
    const fields = SECTION_META[section].fields;
    const existing = itemId ? configCache[section].items[itemId] : null;

    let html = '<div class="form-grid">';
    fields.forEach((f) => {
      const val = existing ? existing[f.key] : "";
      if (f.type === "select") {
        const opts = (f.options || []).map((o) =>
          '<option value="' + esc(o.value) + '"' + (val === o.value ? " selected" : "") + ">" + esc(o.label) + "</option>"
        ).join("");
        html += '<label>' + esc(f.label) + '<select name="' + f.key + '">' + opts + '</select></label>';
      } else if (f.type === "fieldsmapping") {
        const bodyVal = (val && val.body) || "";
        const titleVal = (val && val.title) || "";
        html += '<fieldset class="field-mapping"><legend>' + esc(f.label) + '</legend>' +
          '<label>正文内容字段<input name="' + f.key + '.body" type="text" value="' + esc(bodyVal) + '" /></label>' +
          '<label>标题字段<input name="' + f.key + '.title" type="text" value="' + esc(titleVal) + '" /></label>' +
          '</fieldset>';
      } else if (f.type === "checkbox") {
        html += '<label class="checkbox"><input name="' + f.key + '" type="checkbox" ' +
          (val ? "checked" : "") + " /> " + esc(f.label) + "</label>";
      } else {
        html += '<label>' + esc(f.label) + '<input name="' + f.key + '" type="' + f.type + '" value="' +
          esc(val || "") + '" ' + (f.sensitive ? 'placeholder="留空则不修改"' : "") + " /></label>";
      }
    });
    html += "</div>";
    html += '<div class="editor-actions">' +
      '<button class="primary small" data-act="save">保存</button>' +
      '<button class="ghost small" data-act="validate">验证</button>' +
      '<button class="ghost small" data-act="cancel">取消</button>' +
      '<span class="editor-msg" data-role="msg"></span>' +
      "</div>";

    editor.innerHTML = html;
    editor.hidden = false;
    editor.dataset.id = itemId || "";

    editor.querySelector('[data-act="save"]').addEventListener("click", () => saveItem(section, editor));
    editor.querySelector('[data-act="validate"]').addEventListener("click", () => validateFromEditor(section, editor));
    editor.querySelector('[data-act="cancel"]').addEventListener("click", () => { editor.hidden = true; editor.innerHTML = ""; });
  }

  function collectForm(editor) {
    const data = {};
    editor.querySelectorAll("input, select").forEach((el) => {
      const name = el.getAttribute("name");
      if (!name) return;
      let value;
      if (el.type === "checkbox") value = el.checked;
      else if (el.type === "number") value = el.value === "" ? "" : Number(el.value);
      else value = el.value;

      if (name.includes(".")) {
        const [parent, child] = name.split(".", 2);
        if (!data[parent]) data[parent] = {};
        data[parent][child] = value;
      } else {
        data[name] = value;
      }
    });
    return data;
  }

  function setEditorMsg(editor, ok, msg) {
    const el = editor.querySelector('[data-role="msg"]');
    el.className = "editor-msg " + (ok ? "ok" : "fail");
    el.textContent = msg;
  }

  // ---------- 验证 ----------
  async function doValidate(section, body) {
    try {
      const resp = await fetch("/api/config/" + section + "/validate", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      const data = await resp.json();
      if (!resp.ok) return { ok: false, message: data.detail?.error?.message || data.error?.message || "验证失败" };
      return data;
    } catch (e) {
      return { ok: false, message: e.message };
    }
  }

  async function validateExisting(section, id, row) {
    const vres = row.querySelector('[data-role="vres"]');
    vres.className = "validate-result";
    vres.textContent = "验证中…";
    const r = await doValidate(section, { id });
    vres.className = "validate-result " + (r.ok ? "ok" : "fail");
    vres.textContent = r.ok ? "✓ 可用" : "✗ " + r.message;
  }

  async function validateFromEditor(section, editor) {
    const itemId = editor.dataset.id;
    const fields = collectForm(editor);
    // 编辑已存在项：敏感字段若未改动（含*），用 id 验证以使用真实凭据；否则内联验证
    let body;
    if (itemId && sensitiveUnchanged(section, fields)) {
      body = { id: itemId, ...stripSensitive(fields) };
    } else {
      body = fields;
    }
    setEditorMsg(editor, false, "验证中…");
    const r = await doValidate(section, body);
    setEditorMsg(editor, r.ok, r.ok ? "✓ 验证通过" : "✗ " + r.message);
  }

  function sensitiveUnchanged(section, fields) {
    return SECTION_META[section].fields
      .filter((f) => f.sensitive)
      .every((f) => !fields[f.key] || String(fields[f.key]).includes("*"));
  }
  function stripSensitive(fields) {
    // 内联验证时敏感字段未改动则不发送
    const out = {};
    for (const k in fields) {
      if (typeof fields[k] === "string" && fields[k].includes("*")) continue;
      out[k] = fields[k];
    }
    return out;
  }

  // ---------- 保存（新增/修改）----------
  async function saveItem(section, editor) {
    const itemId = editor.dataset.id;
    const fields = collectForm(editor);

    // 修改时先验证（用 id 验证真实凭据）
    if (itemId) {
      setEditorMsg(editor, false, "验证中…");
      const r = await doValidate(section, { id: itemId, ...stripSensitive(fields) });
      if (!r.ok) {
        setEditorMsg(editor, false, "✗ 验证未通过：" + r.message);
        return;
      }
    }

    const url = itemId
      ? "/api/config/" + section + "/items/" + encodeURIComponent(itemId)
      : "/api/config/" + section + "/items";
    const method = itemId ? "PUT" : "POST";
    try {
      const resp = await fetch(url, {
        method,
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(fields),
      });
      const data = await resp.json();
      if (!resp.ok) {
        setEditorMsg(editor, false, "✗ 保存失败：" + (data.detail?.error?.message || data.error?.message || resp.status));
        return;
      }
      setEditorMsg(editor, true, "✓ 已保存");
      await loadConfig();
    } catch (e) {
      setEditorMsg(editor, false, "✗ 保存异常：" + e.message);
    }
  }

  // ---------- 切换生效 ----------
  async function activateItem(section, id, row) {
    const vres = row.querySelector('[data-role="vres"]');
    vres.className = "validate-result";
    vres.textContent = "验证中…";
    const r = await doValidate(section, { id });
    if (!r.ok) {
      vres.className = "validate-result fail";
      vres.textContent = "✗ 验证未通过，未切换：" + r.message;
      return;
    }
    try {
      const resp = await fetch("/api/config/" + section + "/active", {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ id }),
      });
      if (!resp.ok) {
        const d = await resp.json();
        vres.className = "validate-result fail";
        vres.textContent = "✗ 切换失败：" + (d.detail?.error?.message || resp.status);
        return;
      }
      await loadConfig();
    } catch (e) {
      vres.className = "validate-result fail";
      vres.textContent = "✗ " + e.message;
    }
  }

  // ---------- 删除 ----------
  async function deleteItem(section, id) {
    if (!confirm("确认删除配置「" + (configCache[section].items[id]?.name || id) + "」？")) return;
    try {
      const resp = await fetch("/api/config/" + section + "/items/" + encodeURIComponent(id), { method: "DELETE" });
      if (!resp.ok) {
        const d = await resp.json();
        alert("删除失败：" + (d.detail?.error?.message || resp.status));
        return;
      }
      await loadConfig();
    } catch (e) {
      alert("删除异常：" + e.message);
    }
  }

  // ---------- server ----------
  async function saveServer() {
    const body = { host: $("#srvHost").value, port: parseInt($("#srvPort").value, 10) || 8000 };
    try {
      const resp = await fetch("/api/config/server", {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      if (!resp.ok) {
        const d = await resp.json();
        alert("保存失败：" + (d.detail?.error?.message || resp.status));
        return;
      }
      alert("已保存");
    } catch (e) {
      alert("保存异常：" + e.message);
    }
  }
})();
