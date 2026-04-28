/**
 * workspace_chat.js  —  Enterprise AI Workspace
 *
 * Responsibilities:
 *   • SSE streaming chat via /api/workspace/chat/stream
 *   • Conversation list management (load, switch, new, delete, star)
 *   • Message rendering (user + assistant bubbles)
 *   • Inline code detection → "View Artifact" button
 *   • Citation display
 *   • Tool call indicators
 *   • Message actions: copy, feedback, edit
 *   • Auto-resize textarea
 *   • Keyboard shortcuts (Enter / Shift+Enter)
 *   • Starter suggestions & URL-param pre-fill
 */

const WS = (() => {
  /* ──────────────────────────────────────────
     State
  ────────────────────────────────────────── */
  let _convId      = null;   // current conversation ID
  let _workspaceId = null;   // team workspace context (null = personal)
  let _streaming   = false;
  let _sessionTokens = 0;
  let _settings    = {};
  let _model       = "gpt-4o";

  // current artifact being previewed in modal
  let _artifactContent = "";
  let _artifactLang    = "";
  let _artifactMsgId   = null;
  let _savedArtifactId = null;  // prevents duplicate saves per modal open

  // pending file attachments: [{name, mimeType, textContent, dataUrl, size}]
  let _attachedFiles = [];
  let _projectId     = null;

  /* ──────────────────────────────────────────
     Init
  ────────────────────────────────────────── */
  function init({ conversations = [], defaultModel = "gpt-4o", settings = {}, workspaceId = 0, projectId = 0 }) {
    _settings    = settings;
    _model       = defaultModel;
    _workspaceId = workspaceId || null;
    _projectId   = projectId  || null;

    // Apply saved model
    const sel = document.getElementById("modelSelect");
    if (sel) sel.value = _model;

    // Model change — auto-enable search for models that support it, disable for those that don't
    const modelSel = document.getElementById("modelSelect");
    if (modelSel) {
      const _syncSearchToggle = (autoEnable = false) => {
        const opt = modelSel.options[modelSel.selectedIndex];
        const supportsSearch = opt?.dataset?.search === "true";
        const searchToggle = document.getElementById("toolWebSearch");
        const searchLabel  = searchToggle?.closest("label");
        if (searchToggle) {
          searchToggle.disabled = !supportsSearch;
          if (!supportsSearch) {
            searchToggle.checked = false;
          } else if (autoEnable) {
            searchToggle.checked = true;  // auto-enable when switching to a search-capable model
          }
        }
        if (searchLabel) {
          searchLabel.style.opacity = supportsSearch ? "" : "0.4";
          searchLabel.title = supportsSearch ? "Web Search" : "Web Search (not supported by this model)";
        }
      };
      modelSel.addEventListener("change", () => _syncSearchToggle(true));
      _syncSearchToggle(true); // auto-enable on page load for capable models
    }

    // Project filter — hide conversations not matching selected project
    const projFilter = document.getElementById("projectFilter");
    if (projFilter) {
      projFilter.addEventListener("change", () => {
        const pid = projFilter.value;
        document.querySelectorAll(".ws-conv-item").forEach(el => {
          const elPid = el.dataset.projectId || "";
          el.style.display = (!pid || elPid === pid) ? "" : "none";
        });
      });
    }

    // Check URL params
    const params = new URLSearchParams(window.location.search);
    const convParam   = params.get("conv");
    const promptParam = params.get("prompt");
    const projectParam = params.get("project");

    // Pre-fill prompt from library
    if (promptParam) {
      const inp = document.getElementById("wsInput");
      if (inp) inp.value = promptParam;
      autoResizeTextarea(inp);
    }

    // Load conversation only when explicitly requested via URL param
    if (convParam) {
      loadConversation(parseInt(convParam));
    }

    // Sidebar toggle saved state
    const collapsed = localStorage.getItem("ws_sidebar_collapsed") === "1";
    if (collapsed) document.getElementById("wsConvSidebar")?.classList.add("collapsed");
  }

  /* ──────────────────────────────────────────
     Conversation loading
  ────────────────────────────────────────── */
  async function loadConversation(convId, listItem) {
    if (_streaming) return; // don't switch mid-stream

    _convId = convId;

    // Highlight active item
    document.querySelectorAll(".ws-conv-item").forEach(el => el.classList.remove("active"));
    if (listItem) {
      listItem.classList.add("active");
    } else {
      const el = document.querySelector(`.ws-conv-item[data-conv-id="${convId}"]`);
      if (el) el.classList.add("active");
    }

    // Fetch conversation + messages
    try {
      const resp = await fetch(`/api/workspace/conversations/${convId}`);
      const data = await resp.json();
      if (data.status !== "ok") return;

      const conv = data.conversation;
      const msgs = data.messages || [];
      const cits = data.citations || {};

      // Update topbar
      document.getElementById("currentConvTitle").textContent = conv.title || "New conversation";

      // Sync star button state
      const starBtn  = document.getElementById("btnStarConv");
      const starIcon = starBtn?.querySelector("i");
      if (starIcon) {
        const starred = !!conv.is_starred;
        starIcon.className = starred ? "fas fa-star" : "far fa-star";
        starBtn.classList.toggle("text-warning", starred);
        starBtn.classList.toggle("text-muted",   !starred);
      }

      // Apply model
      const modelSel = document.getElementById("modelSelect");
      if (modelSel && conv.model) modelSel.value = conv.model;
      _model = conv.model || _model;

      // Hide welcome screen
      const welcome = document.getElementById("wsWelcome");
      if (welcome) welcome.style.display = "none";

      // Render messages
      const container = document.getElementById("wsMessages");
      container.innerHTML = "";

      msgs.forEach(msg => {
        if (msg.is_deleted) return;
        if (msg.role === "system") return;
        const msgCits = cits[msg.id] || [];

        let displayContent = msg.content;
        let attachments;
        if (msg.role === "user") {
          const parsed = _parseStoredUserMessage(msg.content);
          displayContent = parsed.displayText || "(file attachment)";
          if (parsed.attachments.length > 0) attachments = parsed.attachments;
        }

        renderMessage(msg.role, displayContent, {
          msgId:      msg.id,
          model:      msg.model,
          tokens:     msg.total_tokens,
          latency:    msg.latency_ms,
          citations:  msgCits,
          isEdit:     msg.is_edited,
          attachments,
        });
      });

      scrollToBottom();

    } catch (e) {
      console.error("loadConversation failed", e);
    }
  }

  /* ──────────────────────────────────────────
     Send message
  ────────────────────────────────────────── */
  async function sendMessage() {
    const input = document.getElementById("wsInput");
    const text  = (input?.value || "").trim();
    if (!text && _attachedFiles.length === 0) return;
    if (_streaming) return;

    const modelSelectEl = document.getElementById("modelSelect");
    const model         = modelSelectEl?.value || _model;
    // The model <option> tags carry data-provider (see workspace/chat.html) so
    // provider travels with the model choice — one dropdown, no separate picker.
    const provider      = modelSelectEl?.selectedOptions?.[0]?.dataset.provider || "openai";
    const webSearch     = document.getElementById("toolWebSearch")?.checked || false;
    const systemPrompt = document.getElementById("systemPromptInput")?.value || "";

    // Build full message: user text + appended text file contents
    let fullMessage = text;
    const imageAttachments = [];
    for (const f of _attachedFiles) {
      if (f.textContent !== null) {
        const content = f.textContent.length > 15000
          ? f.textContent.slice(0, 15000) + "\n... [file truncated at 15 000 chars]"
          : f.textContent;
        fullMessage += `\n\n--- Attached File: ${f.name} ---\n${content}\n--- End of File ---`;
      } else if (f.dataUrl) {
        // Image — send separately for Vision API
        imageAttachments.push({ name: f.name, mimeType: f.mimeType, dataUrl: f.dataUrl });
      }
    }
    if (!fullMessage.trim() && imageAttachments.length === 0) return;

    // Snapshot files for bubble rendering, then clear
    const filesForBubble = [..._attachedFiles];
    _attachedFiles = [];
    _renderFileChips();

    // Render user bubble immediately (show display text + chips, not raw file content)
    const userMsgId = "user-" + Date.now();
    renderMessage("user", text || "(file attachment)", { tempId: userMsgId, attachments: filesForBubble });
    scrollToBottom();

    // Clear input
    input.value = "";
    autoResizeTextarea(input);
    setStreamingState(true);

    // Show typing indicator
    showTyping("AI is thinking…");

    try {
      const resp = await fetch("/api/workspace/chat/stream", {
        method:  "POST",
        headers: { "Content-Type": "application/json" },
        body:    JSON.stringify({
          conversation_id: _convId,
          message:         fullMessage,
          model,
          provider,
          tools:           [],
          system_prompt:   systemPrompt,
          images:          imageAttachments,
          workspace_id:    _workspaceId,
          project_id:      _projectId,
        }),
      });

      if (!resp.ok) {
        const err = await resp.json().catch(() => ({}));
        hideTyping();
        setStreamingState(false);
        showError(err.message || "Request failed");
        return;
      }

      hideTyping();

      // SSE streaming
      const reader  = resp.body.getReader();
      const decoder = new TextDecoder();
      let buffer    = "";
      let asstBubble = null;  // current assistant bubble element
      let startEvent = null;

      while (true) {
        const { done, value } = await reader.read();
        if (done) break;

        buffer += decoder.decode(value, { stream: true });
        const lines = buffer.split("\n");
        buffer = lines.pop();  // last incomplete line stays in buffer

        for (const line of lines) {
          if (!line.startsWith("data:")) continue;
          const raw = line.slice(5).trim();
          if (!raw) continue;

          let event;
          try { event = JSON.parse(raw); } catch { continue; }

          switch (event.type) {
            case "start": {
              startEvent = event;
              _convId = event.conversation_id;
              // Create assistant bubble
              asstBubble = createAssistantBubble(event.message_id);
              scrollToBottom();
              break;
            }

            case "chunk": {
              if (asstBubble) {
                appendChunkToBubble(asstBubble, event.text);
                scrollToBottom();
              }
              break;
            }

            case "tool_start": {
              showToolIndicator(event.tool, event.query);
              if (event.tool === "web_search") {
                showTyping(`Searching the web${event.query ? ': ' + event.query : ''}…`);
              } else if (event.tool === "get_teams_chats_with_person") {
                showTyping(`Reading Teams chats${event.query ? ' with ' + event.query : ''}…`);
              } else if (event.tool === "get_outlook_emails") {
                showTyping(`Reading Outlook emails${event.query ? ' from ' + event.query : ''}…`);
              }
              break;
            }

            case "tool_done": {
              hideTyping();
              hideToolIndicator();
              break;
            }

            case "done": {
              hideTyping();
              // Finalise the bubble
              if (asstBubble) {
                finaliseBubble(asstBubble, event.message_id, {
                  model:   event.model,
                  tokens:  event.total_tokens,
                  latency: event.latency_ms,
                  citations: event.citations || [],
                });
              }
              _sessionTokens += (event.total_tokens || 0);
              updateTokenCounter();

              // Update conversation list title
              updateConvListTitle(event.conversation_id || _convId);

              // Show citations bar
              if (event.citations?.length) {
                showCitationsBar(event.citations);
              }
              break;
            }

            case "error": {
              hideTyping();
              showError(event.message || "An error occurred");
              if (asstBubble) {
                asstBubble.querySelector(".ws-bubble").innerHTML +=
                  `<div class="text-danger small mt-1"><i class="fas fa-exclamation-circle me-1"></i>${escHtml(event.message)}</div>`;
              }
              break;
            }
          }
        }
      }

    } catch (e) {
      console.error("sendMessage failed", e);
      hideTyping();
      showError("Connection error. Please try again.");
    } finally {
      setStreamingState(false);
    }
  }

  /* ──────────────────────────────────────────
     Rendering helpers
  ────────────────────────────────────────── */

  function renderMessage(role, content, opts = {}) {
    const container = document.getElementById("wsMessages");
    if (!container) return;

    const wrap = document.createElement("div");
    wrap.className = `ws-msg-wrap ${role}`;
    if (opts.msgId) wrap.dataset.msgId = opts.msgId;

    // Role label
    const roleLabel = document.createElement("div");
    roleLabel.className = "ws-msg-role";
    roleLabel.textContent = role === "user" ? "You" : "AI";
    wrap.appendChild(roleLabel);

    // Bubble
    const bubble = document.createElement("div");
    bubble.className = `ws-bubble ${role}`;
    // Prepend file attachment chips for user messages
    let chipsHtml = "";
    if (opts.attachments && opts.attachments.length > 0) {
      chipsHtml = `<div class="ws-file-chips-bubble">` +
        opts.attachments.map(f => {
          if (f.dataUrl) {
            return `<span class="ws-chip-bubble-item"><img src="${escHtml(f.dataUrl)}" class="ws-chip-bubble-thumb" alt="">${escHtml(f.name)}</span>`;
          }
          const iconClass = _fileIcon(f.name);
          return `<span class="ws-chip-bubble-item"><i class="fas ${iconClass}"></i>${escHtml(f.name)}</span>`;
        }).join("") + `</div>`;
    }
    bubble.innerHTML = chipsHtml + formatContent(content, role);
    wrap.appendChild(bubble);

    // Actions (only for existing messages, not temp user messages)
    if (opts.msgId) {
      wrap.appendChild(buildMsgActions(role, opts));
    }

    // Citations inline
    if (opts.citations?.length) {
      const citBar = document.createElement("div");
      citBar.className = "d-flex flex-wrap gap-1 mt-1";
      opts.citations.forEach((c, i) => {
        const chip = document.createElement("a");
        chip.href = c.url;
        chip.target = "_blank";
        chip.className = "ws-citation-chip";
        chip.innerHTML = `<i class="fas fa-link"></i>${escHtml(c.title || `Source ${i+1}`)}`;
        citBar.appendChild(chip);
      });
      wrap.appendChild(citBar);
    }

    // Meta (tokens, latency) for assistant
    if (role === "assistant" && opts.msgId && opts.tokens) {
      const meta = document.createElement("div");
      meta.className = "small text-muted mt-1";
      meta.style.fontSize = ".7rem";
      meta.innerHTML = `<i class="fas fa-coins me-1"></i>${opts.tokens} tokens`;
      if (opts.latency) meta.innerHTML += ` &nbsp;·&nbsp; <i class="fas fa-clock me-1"></i>${opts.latency}ms`;
      if (opts.model)   meta.innerHTML += ` &nbsp;·&nbsp; ${escHtml(opts.model)}`;
      wrap.appendChild(meta);
    }

    container.appendChild(wrap);
    return wrap;
  }

  function createAssistantBubble(msgId) {
    const container = document.getElementById("wsMessages");
    const wrap = document.createElement("div");
    wrap.className = "ws-msg-wrap assistant";
    wrap.dataset.msgId = msgId;

    const roleLabel = document.createElement("div");
    roleLabel.className = "ws-msg-role";
    roleLabel.textContent = "AI";
    wrap.appendChild(roleLabel);

    const bubble = document.createElement("div");
    bubble.className = "ws-bubble assistant";
    bubble.innerHTML = '<span class="ws-streaming-cursor"></span>';
    wrap.appendChild(bubble);

    container.appendChild(wrap);
    return wrap;
  }

  function appendChunkToBubble(wrapEl, text) {
    const bubble = wrapEl.querySelector(".ws-bubble");
    if (!bubble) return;

    // Remove streaming cursor
    const cursor = bubble.querySelector(".ws-streaming-cursor");
    if (cursor) cursor.remove();

    // Append text node (simple for now; full markdown render happens on finalise)
    const existing = bubble.querySelector(".ws-stream-text");
    if (existing) {
      existing.textContent += text;
    } else {
      const span = document.createElement("span");
      span.className = "ws-stream-text";
      span.textContent = text;
      bubble.appendChild(span);
    }

    // Re-add cursor at end
    const cur = document.createElement("span");
    cur.className = "ws-streaming-cursor";
    bubble.appendChild(cur);
  }

  function finaliseBubble(wrapEl, msgId, opts = {}) {
    const bubble = wrapEl.querySelector(".ws-bubble");
    if (!bubble) return;

    // Get full streamed text
    const streamSpan = bubble.querySelector(".ws-stream-text");
    const fullText   = streamSpan ? streamSpan.textContent : bubble.textContent;

    // Re-render with markdown + code blocks
    bubble.innerHTML = formatContent(fullText, "assistant");

    // Add action bar
    wrapEl.dataset.msgId = msgId;
    const actions = buildMsgActions("assistant", {
      msgId,
      model:   opts.model,
      tokens:  opts.tokens,
      latency: opts.latency,
    });
    wrapEl.appendChild(actions);

    // Meta
    if (opts.tokens || opts.latency) {
      const meta = document.createElement("div");
      meta.className = "small text-muted mt-1";
      meta.style.fontSize = ".7rem";
      meta.innerHTML = `<i class="fas fa-coins me-1"></i>${opts.tokens||0} tokens`;
      if (opts.latency) meta.innerHTML += ` &nbsp;·&nbsp; <i class="fas fa-clock me-1"></i>${opts.latency}ms`;
      if (opts.model)   meta.innerHTML += ` &nbsp;·&nbsp; ${escHtml(opts.model)}`;
      wrapEl.appendChild(meta);
    }

    // Citations
    if (opts.citations?.length) {
      const citBar = document.createElement("div");
      citBar.className = "d-flex flex-wrap gap-1 mt-1";
      opts.citations.forEach((c, i) => {
        const chip = document.createElement("a");
        chip.href = c.url;
        chip.target = "_blank";
        chip.className = "ws-citation-chip";
        chip.innerHTML = `<i class="fas fa-link me-1"></i>${escHtml(c.title || `Source ${i+1}`)}`;
        citBar.appendChild(chip);
      });
      wrapEl.appendChild(citBar);
    }
  }

  function buildMsgActions(role, opts) {
    const bar = document.createElement("div");
    bar.className = "ws-msg-actions";

    // Copy
    const copyBtn = document.createElement("button");
    copyBtn.className = "ws-msg-action-btn";
    copyBtn.innerHTML = '<i class="fas fa-copy me-1"></i>Copy';
    copyBtn.onclick = () => {
      const bubble = bar.parentElement?.querySelector(".ws-bubble");
      navigator.clipboard.writeText(bubble?.textContent || "");
      if (opts.msgId) {
        fetch(`/api/workspace/messages/${opts.msgId}/copy`, {
          method: "POST", headers: {"Content-Type":"application/json"},
          body: JSON.stringify({ copy_type: "text" })
        });
      }
      copyBtn.innerHTML = '<i class="fas fa-check me-1"></i>Copied!';
      setTimeout(() => { copyBtn.innerHTML = '<i class="fas fa-copy me-1"></i>Copy'; }, 1500);
    };
    bar.appendChild(copyBtn);

    // Feedback (assistant only)
    if (role === "assistant" && opts.msgId) {
      const upBtn   = makeFeedbackBtn("thumbs_up",   "up",   opts.msgId);
      const downBtn = makeFeedbackBtn("thumbs_down", "down", opts.msgId);
      bar.appendChild(upBtn);
      bar.appendChild(downBtn);
    }

    return bar;
  }

  function makeFeedbackBtn(rating, dir, msgId) {
    const btn = document.createElement("button");
    btn.className = `ws-feedback-btn thumbs-${dir}`;
    btn.innerHTML = `<i class="fa${dir==='up'?'s':'r'} fa-thumbs-${dir}"></i>`;
    btn.onclick = async () => {
      await fetch(`/api/workspace/messages/${msgId}/feedback`, {
        method: "POST", headers: {"Content-Type":"application/json"},
        body: JSON.stringify({ rating })
      });
      btn.classList.add(`active-${dir}`);
    };
    return btn;
  }

  /* ──────────────────────────────────────────
     Content formatting
  ────────────────────────────────────────── */

  function formatContent(text, role) {
    if (!text) return "";

    // Step 1 — stash code blocks BEFORE escaping.
    // If we escape first and then inject multi-line HTML, the later
    // \n→<br> pass corrupts tag attributes (style="…" and onclick="…"
    // become visible text). Stashing lets us restore clean HTML after
    // all text transforms are done.
    const _blocks = [];
    let html = text.replace(/```(\w*)\n?([\s\S]*?)```/g, (_, lang, code) => {
      _blocks.push({ lang: lang || "text", code: code.trim() });
      return `\x00CB${_blocks.length - 1}\x00`;
    });

    // Step 2 — escape HTML in the prose (placeholders are \x00…\x00, safe)
    html = escHtml(html);

    // Step 3 — inline markdown on escaped prose
    html = html.replace(/`([^`\n]+)`/g, '<code>$1</code>');
    html = html.replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>');
    html = html.replace(/__([^_]+)__/g,     '<strong>$1</strong>');
    html = html.replace(/\*([^*\n]+)\*/g,   '<em>$1</em>');
    html = html.replace(/_([^_\n]+)_/g,     '<em>$1</em>');
    html = html.replace(/^[•\-\*] (.+)$/gm, '<li>$1</li>');
    html = html.replace(/(<li>[\s\S]*?<\/li>)/g, '<ul>$1</ul>');
    html = html.replace(/^\d+\. (.+)$/gm,   '<li>$1</li>');
    html = html.replace(/^### (.+)$/gm, '<h6 class="mt-2 mb-1">$1</h6>');
    html = html.replace(/^## (.+)$/gm,  '<h5 class="mt-2 mb-1">$1</h5>');
    html = html.replace(/^# (.+)$/gm,   '<h4 class="mt-2 mb-1">$1</h4>');
    html = html.replace(/\n\n/g, '</p><p>');
    html = html.replace(/\n/g,   '<br>');

    // Step 4 — restore code blocks with proper single-line HTML.
    // Code content lives in <code> element so onclick never embeds raw text.
    html = html.replace(/\x00CB(\d+)\x00/g, (_, i) => {
      const { lang, code } = _blocks[parseInt(i, 10)];
      const safeLang = escHtml(lang);
      const safeCode = escHtml(code);
      const artifactBtn = code.length > 80
        ? `<button class="btn btn-sm btn-outline-light py-0 px-2" style="font-size:.7rem;" onclick="WS.openArtifactFromBlock(this)"><i class="fas fa-expand-alt me-1"></i>View</button>`
        : "";
      return `<div class="ws-code-block"><div class="ws-code-header"><span>${safeLang}</span><div class="d-flex gap-2 align-items-center">${artifactBtn}<button class="btn btn-sm btn-link text-secondary p-0" style="font-size:.7rem;" onclick="WS.copyCode(this)"><i class="fas fa-copy"></i></button></div></div><pre><code class="language-${safeLang}">${safeCode}</code></pre></div>`;
    });

    return html;
  }

  /* ──────────────────────────────────────────
     Artifact viewer
  ────────────────────────────────────────── */

  function openArtifact(content, lang) {
    _artifactContent = content;
    _artifactLang    = lang;
    _savedArtifactId = null;
    document.getElementById("artifactModalTitle").textContent = `Artifact — ${lang || "code"}`;
    document.getElementById("artifactContent").textContent = content;
    const saveBtn = document.getElementById("artifactSaveBtn");
    if (saveBtn) {
      saveBtn.disabled = false;
      saveBtn.innerHTML = '<i class="fas fa-save me-1"></i>Save';
    }
    new bootstrap.Modal("#artifactModal").show();
  }

  function copyArtifact() {
    navigator.clipboard.writeText(_artifactContent);
  }

  function saveArtifact() {
    if (_savedArtifactId) { showToast("Already saved to Artifacts", "info"); return; }
    if (!_artifactContent) return;

    const saveBtn = document.getElementById("artifactSaveBtn");
    if (saveBtn) { saveBtn.disabled = true; saveBtn.innerHTML = '<i class="fas fa-spinner fa-spin me-1"></i>Saving…'; }

    fetch("/api/workspace/artifacts", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        artifact_type:   _langToArtifactType(_artifactLang),
        title:           _artifactTitle(_artifactLang, _artifactContent),
        content:         _artifactContent,
        language:        _artifactLang,
        conversation_id: _convId,
      }),
    })
      .then(r => r.json())
      .then(d => {
        if (d.status === "ok") {
          _savedArtifactId = d.artifact_id;
          showToast("Artifact saved!", "success");
          if (saveBtn) { saveBtn.innerHTML = '<i class="fas fa-check me-1"></i>Saved'; }
        } else {
          if (saveBtn) { saveBtn.disabled = false; saveBtn.innerHTML = '<i class="fas fa-save me-1"></i>Save'; }
          showToast(d.message || "Failed to save artifact", "error");
        }
      })
      .catch(() => {
        if (saveBtn) { saveBtn.disabled = false; saveBtn.innerHTML = '<i class="fas fa-save me-1"></i>Save'; }
        showToast("Connection error saving artifact", "error");
      });
  }

  function _langToArtifactType(lang) {
    const l = (lang || "").toLowerCase();
    if (l === "sql") return "sql";
    if (l === "html") return "html";
    if (l === "markdown" || l === "md") return "markdown";
    return "code";
  }

  function _artifactTitle(lang, content) {
    const typeLabel = (lang || "code").toLowerCase();
    const firstLine = (content || "").split("\n").find(l => l.trim() && !l.trim().startsWith("#") && !l.trim().startsWith("--") && !l.trim().startsWith("//")) || "";
    const snippet   = firstLine.trim().slice(0, 50);
    return snippet ? `${typeLabel}: ${snippet}` : `${typeLabel} artifact`;
  }

  function _artifactExtension(lang) {
    const map = { sql:"sql", html:"html", python:"py", javascript:"js", typescript:"ts",
                  css:"css", json:"json", yaml:"yaml", markdown:"md", md:"md",
                  bash:"sh", shell:"sh", r:"r", ruby:"rb", go:"go", rust:"rs" };
    return map[(lang || "").toLowerCase()] || "txt";
  }

  function downloadArtifact() {
    if (!_artifactContent) return;
    const blob = new Blob([_artifactContent], { type: "text/plain;charset=utf-8" });
    const url  = URL.createObjectURL(blob);
    const a    = Object.assign(document.createElement("a"), {
      href: url,
      download: `artifact.${_artifactExtension(_artifactLang)}`,
    });
    a.click();
    URL.revokeObjectURL(url);
  }

  function copyCode(btn) {
    const code = btn.closest(".ws-code-block")?.querySelector("code")?.textContent || "";
    if (code) {
      navigator.clipboard.writeText(code);
      btn.innerHTML = '<i class="fas fa-check"></i>';
      setTimeout(() => { btn.innerHTML = '<i class="fas fa-copy"></i>'; }, 1500);
    }
  }

  function openArtifactFromBlock(btn) {
    const block = btn.closest(".ws-code-block");
    const code  = block?.querySelector("code")?.textContent || "";
    const lang  = block?.querySelector(".ws-code-header span")?.textContent || "text";
    openArtifactEnhanced(code, lang);
  }

  /* ──────────────────────────────────────────
     New conversation
  ────────────────────────────────────────── */

  function newConversation() {
    _convId = null;
    const container = document.getElementById("wsMessages");
    container.innerHTML = "";
    const welcome = document.getElementById("wsWelcome");
    if (welcome) {
      container.appendChild(welcome);
      welcome.style.display = "";
    }
    document.getElementById("currentConvTitle").textContent = "New conversation";
    document.querySelectorAll(".ws-conv-item").forEach(el => el.classList.remove("active"));
    document.getElementById("wsCitationsBar")?.classList.add("d-none");
    document.getElementById("wsInput")?.focus();
  }

  async function updateConvListTitle(convId) {
    try {
      const resp = await fetch(`/api/workspace/conversations/${convId}`);
      const data = await resp.json();
      if (data.status !== "ok") return;
      const conv  = data.conversation;
      const title = conv?.title || "New conversation";
      const creator = conv?.creator_username || "";

      // Update list item if present
      let item = document.querySelector(`.ws-conv-item[data-conv-id="${convId}"]`);
      if (!item) {
        // Create new list item
        item = document.createElement("div");
        item.className = "ws-conv-item active";
        item.dataset.convId = convId;
        item.onclick = function() { loadConversation(convId, this); };
        const creatorBadge = (_workspaceId && creator)
          ? `<div class="ws-conv-item-creator"><i class="fas fa-user me-1"></i>${escHtml(creator)}</div>`
          : "";
        item.innerHTML = `
          <div class="ws-conv-item-title">${escHtml(title)}</div>
          ${creatorBadge}
          <div class="ws-conv-item-meta">
            <span>${escHtml(conv?.model || _model)}</span>
            <span>${new Date().toISOString().slice(0,10)}</span>
          </div>`;
        const list = document.getElementById("convList");
        if (list) {
          const empty = document.getElementById("emptyConvMsg");
          if (empty) empty.remove();
          list.prepend(item);
        }
        document.querySelectorAll(".ws-conv-item").forEach(el => el.classList.remove("active"));
        item.classList.add("active");
      } else {
        const titleEl = item.querySelector(".ws-conv-item-title");
        if (titleEl) titleEl.textContent = title;
      }
      document.getElementById("currentConvTitle").textContent = title;
    } catch(e) {}
  }

  /* ──────────────────────────────────────────
     Star / delete current conversation
  ────────────────────────────────────────── */

  async function starCurrentConv() {
    if (!_convId) return;
    const starBtn   = document.getElementById("btnStarConv");
    const icon      = starBtn?.querySelector("i");
    const isStarred = icon?.classList.contains("fas");
    await fetch(`/api/workspace/conversations/${_convId}`, {
      method: "PATCH",
      headers: {"Content-Type":"application/json"},
      body: JSON.stringify({ is_starred: isStarred ? 0 : 1 })
    });
    if (icon) {
      icon.className = isStarred ? "far fa-star" : "fas fa-star";
    }
    starBtn?.classList.toggle("text-warning", !isStarred);
    starBtn?.classList.toggle("text-muted",    isStarred);
  }

  async function deleteCurrentConv() {
    if (!_convId) return;
    if (!confirm("Delete this conversation permanently?")) return;
    await fetch(`/api/workspace/conversations/${_convId}`, { method: "DELETE" });
    document.querySelector(`.ws-conv-item[data-conv-id="${_convId}"]`)?.remove();
    newConversation();
  }

  /* ──────────────────────────────────────────
     System prompt
  ────────────────────────────────────────── */

  function saveSystemPrompt() {
    const prompt = document.getElementById("systemPromptInput")?.value || "";
    if (_convId) {
      fetch(`/api/workspace/conversations/${_convId}`, {
        method: "PATCH",
        headers: {"Content-Type":"application/json"},
        body: JSON.stringify({ system_prompt: prompt })
      });
    }
    bootstrap.Modal.getInstance("#systemPromptModal")?.hide();
  }

  /* ──────────────────────────────────────────
     Starter suggestions
  ────────────────────────────────────────── */

  function useStarter(text) {
    const input = document.getElementById("wsInput");
    if (input) {
      input.value = text;
      autoResizeTextarea(input);
      input.focus();
    }
    // If the starter implies a web search, auto-enable the search toggle
    const searchKeywords = ["search", "latest", "news", "current", "today", "recent", "browse"];
    const impliesSearch = searchKeywords.some(k => text.toLowerCase().includes(k));
    if (impliesSearch) {
      const toggle = document.getElementById("toolWebSearch");
      if (toggle && !toggle.disabled) toggle.checked = true;
    }
    const welcome = document.getElementById("wsWelcome");
    if (welcome) welcome.style.display = "none";
  }

  /* ──────────────────────────────────────────
     File attachment
  ────────────────────────────────────────── */

  const _SERVER_EXTRACT_EXTS = new Set([
    "pdf","docx","doc","xlsx","xls","xlsm","pptx","ppt","html","htm","eml","msg"
  ]);

  function _isTextFile(file) {
    if (file.type && (file.type.startsWith("text/") || ["application/json","application/xml","application/csv"].includes(file.type))) return true;
    const ext = file.name.split(".").pop().toLowerCase();
    return ["txt","csv","json","xml","yaml","yml","md","py","js","ts","css","sql","sh","r","rb","log","toml","ini","conf"].includes(ext);
  }

  async function handleFileAttach(input) {
    const files = Array.from(input.files || []);
    input.value = "";
    if (!files.length) return;

    for (const file of files) {
      const isImg    = file.type.startsWith("image/");
      const isTxt    = _isTextFile(file);
      const ext      = file.name.split(".").pop().toLowerCase();
      const needsSrv = _SERVER_EXTRACT_EXTS.has(ext);

      if (!isImg && !isTxt && !needsSrv) {
        showToast(`"${file.name}" is not a supported type`, "error");
        continue;
      }
      if ((isTxt || needsSrv) && file.size > 20 * 1024 * 1024) {
        showToast(`"${file.name}" is too large (max 20 MB)`, "error");
        continue;
      }
      if (isImg && file.size > 10 * 1024 * 1024) {
        showToast(`"${file.name}" is too large (max 10 MB for images)`, "error");
        continue;
      }

      try {
        if (isImg) {
          const dataUrl = await _readAsDataURL(file);
          _attachedFiles.push({ name: file.name, mimeType: file.type, textContent: null, dataUrl, size: file.size });
        } else if (needsSrv) {
          showToast(`Extracting "${file.name}"…`, "info");
          const form = new FormData();
          form.append("file", file);
          const res  = await fetch("/api/workspace/extract-file", { method: "POST", body: form });
          const data = await res.json();
          if (data.status !== "ok") {
            showToast(`Could not extract "${file.name}": ${data.message}`, "error");
            continue;
          }
          _attachedFiles.push({ name: file.name, mimeType: file.type, textContent: data.text, dataUrl: null, size: file.size });
        } else {
          const textContent = await _readAsText(file);
          _attachedFiles.push({ name: file.name, mimeType: file.type, textContent, dataUrl: null, size: file.size });
        }
      } catch (e) {
        showToast(`Failed to read "${file.name}"`, "error");
      }
    }
    _renderFileChips();
  }

  function _readAsText(file) {
    return new Promise((resolve, reject) => {
      const r = new FileReader();
      r.onload  = e => resolve(e.target.result);
      r.onerror = reject;
      r.readAsText(file);
    });
  }

  function _readAsDataURL(file) {
    return new Promise((resolve, reject) => {
      const r = new FileReader();
      r.onload  = e => resolve(e.target.result);
      r.onerror = reject;
      r.readAsDataURL(file);
    });
  }

  function _fileIcon(name) {
    const ext = (name.split(".").pop() || "").toLowerCase();
    const map = { csv:"fa-file-csv", json:"fa-file-code", xml:"fa-file-code", yaml:"fa-file-code", yml:"fa-file-code",
                  py:"fa-file-code", js:"fa-file-code", ts:"fa-file-code", html:"fa-file-code", css:"fa-file-code",
                  sql:"fa-database", sh:"fa-terminal", md:"fa-file-alt", txt:"fa-file-alt",
                  pdf:"fa-file-pdf", xlsx:"fa-file-excel", xls:"fa-file-excel",
                  docx:"fa-file-word", doc:"fa-file-word", log:"fa-file-alt" };
    return map[ext] || "fa-file";
  }

  function _formatFileSize(bytes) {
    if (bytes < 1024) return `${bytes} B`;
    if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
    return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
  }

  function _renderFileChips() {
    const container = document.getElementById("wsAttachedFiles");
    if (!container) return;
    if (_attachedFiles.length === 0) {
      container.innerHTML = "";
      container.classList.add("d-none");
      return;
    }
    container.classList.remove("d-none");
    container.innerHTML = _attachedFiles.map((f, i) => {
      const size = _formatFileSize(f.size);
      if (f.dataUrl) {
        return `<div class="ws-file-chip" data-idx="${i}">
          <img src="${escHtml(f.dataUrl)}" class="ws-file-chip-thumb" alt="">
          <span class="ws-file-chip-name" title="${escHtml(f.name)}">${escHtml(f.name)}</span>
          <span class="ws-file-chip-size">${size}</span>
          <button class="ws-file-chip-remove" onclick="removeAttachedFile(${i})" title="Remove">
            <i class="fas fa-times"></i></button></div>`;
      }
      const icon = _fileIcon(f.name);
      return `<div class="ws-file-chip" data-idx="${i}">
        <i class="fas ${icon}"></i>
        <span class="ws-file-chip-name" title="${escHtml(f.name)}">${escHtml(f.name)}</span>
        <span class="ws-file-chip-size">${size}</span>
        <button class="ws-file-chip-remove" onclick="removeAttachedFile(${i})" title="Remove">
          <i class="fas fa-times"></i></button></div>`;
    }).join("");
  }

  function removeAttachedFile(idx) {
    _attachedFiles.splice(idx, 1);
    _renderFileChips();
  }

  // Strip "--- Attached File: NAME ---\n...\n--- End of File ---" blocks from stored
  // user messages so history renders chips instead of raw file content.
  function _parseStoredUserMessage(content) {
    const FILE_BLOCK = /\n*--- Attached File: ([^\n]+) ---\n[\s\S]*?--- End of File ---/g;
    const attachments = [];
    let match;
    while ((match = FILE_BLOCK.exec(content)) !== null) {
      attachments.push({ name: match[1], dataUrl: null, textContent: "", size: 0 });
    }
    const displayText = content.replace(FILE_BLOCK, "").trim();
    return { displayText, attachments };
  }

  /* ──────────────────────────────────────────
     UI helpers
  ────────────────────────────────────────── */

  function setStreamingState(active) {
    _streaming = active;
    const btn  = document.getElementById("wsSendBtn");
    const inp  = document.getElementById("wsInput");
    if (btn) {
      btn.disabled = active;
      btn.innerHTML = active
        ? '<span class="spinner-border spinner-border-sm"></span>'
        : '<i class="fas fa-paper-plane"></i>';
    }
    if (inp) inp.disabled = active;
  }

  function showTyping(label) {
    const el = document.getElementById("wsTyping");
    if (el) el.classList.remove("d-none");
    const lbl = document.getElementById("wsTypingLabel");
    if (lbl) lbl.textContent = label || "AI is thinking…";
  }

  function hideTyping() {
    document.getElementById("wsTyping")?.classList.add("d-none");
  }

  let _toolIndicatorEl = null;
  function showToolIndicator(tool, query) {
    hideToolIndicator();
    const el = document.createElement("div");
    el.className = "ws-tool-indicator";
    el.id = "wsToolIndicator";
    const toolIcon  = tool === "web_search" ? "fa-globe" : tool === "get_outlook_emails" ? "fa-envelope" : "fa-comments";
    const toolLabel = tool === "web_search" ? "Searching" : tool === "get_outlook_emails" ? "Reading emails" : "Reading Teams";
    el.innerHTML = `<div class="ws-tool-spinner"></div>
      <span><i class="fas ${toolIcon} me-1"></i>
      ${escHtml(toolLabel)}
      ${query ? ': <em>' + escHtml(query) + '</em>' : ''}</span>`;
    document.getElementById("wsMessages")?.appendChild(el);
    _toolIndicatorEl = el;
    scrollToBottom();
  }

  function hideToolIndicator() {
    document.getElementById("wsToolIndicator")?.remove();
    _toolIndicatorEl = null;
  }

  function showCitationsBar(citations) {
    const bar = document.getElementById("wsCitationsBar");
    const cnt = document.getElementById("citationsContainer");
    if (!bar || !cnt) return;

    cnt.innerHTML = '<span class="small text-muted me-1"><i class="fas fa-link me-1"></i>Sources:</span>';
    citations.forEach((c, i) => {
      const chip = document.createElement("a");
      chip.href      = c.url;
      chip.target    = "_blank";
      chip.className = "ws-citation-chip";
      chip.textContent = c.title || `Source ${i+1}`;
      cnt.appendChild(chip);
    });
    bar.classList.remove("d-none");
  }

  function showError(msg) {
    showToast(msg, "danger");
  }

  function showToast(msg, type = "info") {
    const id   = "toast-" + Date.now();
    const html = `<div id="${id}" class="toast align-items-center text-bg-${type} border-0 show"
                       style="position:fixed;bottom:20px;right:20px;z-index:9999;min-width:260px;"
                       role="alert">
      <div class="d-flex">
        <div class="toast-body">${escHtml(msg)}</div>
        <button type="button" class="btn-close btn-close-white me-2 m-auto"
                onclick="document.getElementById('${id}').remove()"></button>
      </div>
    </div>`;
    document.body.insertAdjacentHTML("beforeend", html);
    setTimeout(() => document.getElementById(id)?.remove(), 4000);
  }

  function scrollToBottom() {
    const el = document.getElementById("wsMessages");
    if (el) el.scrollTop = el.scrollHeight;
  }

  function updateTokenCounter() {
    const el = document.getElementById("wsTokenCounter");
    if (el) el.textContent = `${_sessionTokens.toLocaleString()} tokens used this session`;
  }

  /* ──────────────────────────────────────────
     Sidebar toggle
  ────────────────────────────────────────── */

  function toggleConvSidebar() {
    const sidebar = document.getElementById("wsConvSidebar");
    if (!sidebar) return;
    sidebar.classList.toggle("collapsed");
    localStorage.setItem("ws_sidebar_collapsed",
      sidebar.classList.contains("collapsed") ? "1" : "0");
  }

  /* ──────────────────────────────────────────
     Util
  ────────────────────────────────────────── */

  function escHtml(s) {
    return String(s ?? "")
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  /* ──────────────────────────────────────────
     Artifact tab switching (Code | Preview)
  ────────────────────────────────────────── */

  function switchArtifactTab(tab, btn) {
    const codeView    = document.getElementById("artifactCodeView");
    const previewView = document.getElementById("artifactPreviewView");
    document.querySelectorAll(".ws-artifact-tab").forEach(t => t.classList.remove("active"));
    if (btn) btn.classList.add("active");

    if (tab === "preview") {
      codeView.style.display    = "none";
      previewView.style.display = "";
      const frame = document.getElementById("artifactPreviewFrame");
      if (frame && (_artifactLang || "").toLowerCase() === "html") {
        frame.srcdoc = _artifactContent;
      }
    } else {
      codeView.style.display    = "";
      previewView.style.display = "none";
    }
  }

  // Enhanced artifact opener — shows/hides Preview tab for HTML content
  function openArtifactEnhanced(content, lang) {
    _artifactContent = content;
    _artifactLang    = lang;
    _savedArtifactId = null;

    document.getElementById("artifactModalTitle").textContent = `Artifact — ${lang || "code"}`;
    document.getElementById("artifactContent").textContent = content;

    // Show Preview tab only for HTML
    const previewTabBtn = document.getElementById("artTabPreview");
    if (previewTabBtn) {
      const isHtml = (lang || "").toLowerCase() === "html";
      previewTabBtn.style.display = isHtml ? "" : "none";
    }
    // Always start on Code tab
    const codeTabBtn = document.getElementById("artTabCode");
    if (codeTabBtn) {
      codeTabBtn.classList.add("active");
      document.getElementById("artTabPreview")?.classList.remove("active");
    }
    document.getElementById("artifactCodeView").style.display    = "";
    document.getElementById("artifactPreviewView").style.display = "none";

    const saveBtn = document.getElementById("artifactSaveBtn");
    if (saveBtn) {
      saveBtn.disabled = false;
      saveBtn.innerHTML = '<i class="fas fa-save me-1"></i>Save';
    }
    new bootstrap.Modal("#artifactModal").show();
  }

  /* ──────────────────────────────────────────
     Public API
  ────────────────────────────────────────── */
  return {
    init,
    loadConversation,
    sendMessage,
    newConversation,
    starCurrentConv,
    deleteCurrentConv,
    saveSystemPrompt,
    useStarter,
    handleFileAttach,
    removeAttachedFile,
    toggleConvSidebar,
    openArtifact: openArtifactEnhanced,
    openArtifactFromBlock,
    copyArtifact,
    saveArtifact,
    downloadArtifact,
    copyCode,
    showToast,
    switchArtifactTab,
  };
})();

/* ──────────────────────────────────────────
   Global shims (called from inline HTML)
────────────────────────────────────────── */
function loadConversation(id, el)       { WS.loadConversation(id, el); }
function sendMessage()                  { WS.sendMessage(); }
function toggleConvSidebar()            { WS.toggleConvSidebar(); }
function useStarter(text)               { WS.useStarter(text); }
function starCurrentConv()              { WS.starCurrentConv(); }
function deleteCurrentConv()            { WS.deleteCurrentConv(); }
function saveSystemPrompt()             { WS.saveSystemPrompt(); }
function handleFileAttach(el)           { WS.handleFileAttach(el); }
function removeAttachedFile(idx)        { WS.removeAttachedFile(idx); }
function copyArtifact()                 { WS.copyArtifact(); }
function saveArtifact()                 { WS.saveArtifact(); }
function downloadArtifact()             { WS.downloadArtifact(); }
function switchArtifactTab(tab, btn)    { WS.switchArtifactTab(tab, btn); }

/* ──────────────────────────────────────────
   Textarea auto-resize
────────────────────────────────────────── */
function autoResizeTextarea(el) {
  if (!el) return;
  el.style.height = "auto";
  el.style.height = Math.min(el.scrollHeight, 180) + "px";
}

/* ──────────────────────────────────────────
   New chat button handler
────────────────────────────────────────── */
document.addEventListener("DOMContentLoaded", () => {
  const newBtn = document.getElementById("btnNewChat");
  if (newBtn) newBtn.addEventListener("click", () => WS.newConversation());
});

/* ──────────────────────────────────────────
   Keyboard: Enter sends, Shift+Enter newline
────────────────────────────────────────── */
function wsInputKeydown(e) {
  const enterSends = true; // could read from settings
  if (e.key === "Enter" && !e.shiftKey && enterSends) {
    e.preventDefault();
    WS.sendMessage();
  }
}
