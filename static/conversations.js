/* ===== 认证 & 工具函数 ===== */
const API = '';
let authKey = '';
let liveEs = null;
let liveEnabled = true;

function togglePwd(id) {
  const el = document.getElementById(id);
  el.type = el.type === 'password' ? 'text' : 'password';
}

function toast(msg, ok) {
  if (ok === undefined) ok = true;
  const area = document.getElementById('toasts');
  const el = document.createElement('div');
  el.className = 'toast ' + (ok ? 'toast-ok' : 'toast-err');
  el.textContent = msg;
  area.appendChild(el);
  setTimeout(function () { el.remove(); }, 3000);
}

async function api(path, opts) {
  opts = opts || {};
  var headers = { 'Content-Type': 'application/json' };
  if (authKey) headers['Authorization'] = 'Bearer ' + authKey;
  var res = await fetch(API + path, Object.assign({}, opts, { headers: headers }));
  var ct = res.headers.get('content-type') || '';
  if (!ct.includes('application/json')) {
    var text = await res.text();
    if (!res.ok) throw new Error('HTTP ' + res.status + ': ' + text.substring(0, 100));
    throw new Error('服务器返回了非 JSON 响应');
  }
  var data = await res.json();
  if (!res.ok) {
    var e = data.error;
    var msg = (typeof e === 'object' && e !== null) ? (e.message || JSON.stringify(e)) : (e || data.message || 'HTTP ' + res.status);
    throw new Error(msg);
  }
  return data;
}

/* ===== 登录 ===== */
async function doLogin() {
  var key = document.getElementById('loginKey').value.trim();
  if (!key) { toast('请输入密钥', false); return; }
  try {
    var r = await api('/api/admin/login', { method: 'POST', body: JSON.stringify({ key: key }) });
    if (r.ok) {
      authKey = key;
      sessionStorage.setItem('_ak', key);
      document.getElementById('login').style.display = 'none';
      document.getElementById('dashboard').style.display = 'block';
      loadConversationList();
      startLiveSSE();
    }
  } catch (e) {
    toast('密钥无效', false);
  }
}

function doLogout() {
  authKey = '';
  sessionStorage.removeItem('_ak');
  if (liveEs) { try { liveEs.close(); } catch (e) {} liveEs = null; }
  document.getElementById('dashboard').style.display = 'none';
  document.getElementById('login').style.display = 'flex';
}

(function () {
  var saved = sessionStorage.getItem('_ak');
  if (saved) {
    authKey = saved;
    document.getElementById('login').style.display = 'none';
    document.getElementById('dashboard').style.display = 'block';
    loadConversationList();
    startLiveSSE();
  }
})();

/* ===== 会话列表 ===== */
var CONVERSATIONS = [];
var currentSort = { field: 'updated_at', dir: 'desc' };
var currentConvId = null;
var currentDate = null;

function setSort(field) {
  if (currentSort.field === field) {
    currentSort.dir = currentSort.dir === 'asc' ? 'desc' : 'asc';
  } else {
    currentSort.field = field;
    currentSort.dir = 'desc';
  }
  updateSortButtons();
  renderConversationList();
}

function updateSortButtons() {
  var buttons = document.querySelectorAll('.sort-btn');
  buttons.forEach(function (btn) {
    var f = btn.getAttribute('data-sort');
    btn.classList.remove('active', 'asc', 'desc');
    if (f === currentSort.field) {
      btn.classList.add('active');
      btn.classList.add(currentSort.dir === 'asc' ? 'asc' : 'desc');
    }
  });
}

function onSearch() {
  renderConversationList();
}

async function loadConversationList() {
  console.time('[前端] 加载会话列表');
  try {
    var t0 = performance.now();
    var data = await api('/api/admin/logs?limit=200');
    var apiMs = (performance.now() - t0).toFixed(0);
    console.log('[前端] API /api/admin/logs 返回 %d 条, 耗时 %s ms', (data.items || []).length, apiMs);

    CONVERSATIONS = data.items || [];
    renderConversationList();
    console.timeEnd('[前端] 加载会话列表');
  } catch (e) {
    console.timeEnd('[前端] 加载会话列表');
    toast('加载列表失败: ' + e.message, false);
    document.getElementById('convList').innerHTML = '<div class="empty">加载失败</div>';
  }
}

function sortConversations(items) {
  var field = currentSort.field;
  var dir = currentSort.dir;
  return items.slice().sort(function (a, b) {
    var va = a[field], vb = b[field];
    if (va === undefined || va === null) va = '';
    if (vb === undefined || vb === null) vb = '';
    if (field === 'turn_count') {
      va = parseInt(va) || 0; vb = parseInt(vb) || 0;
      return dir === 'asc' ? va - vb : vb - va;
    }
    va = String(va).toLowerCase();
    vb = String(vb).toLowerCase();
    if (va < vb) return dir === 'asc' ? -1 : 1;
    if (va > vb) return dir === 'asc' ? 1 : -1;
    return 0;
  });
}

function renderConversationList() {
  var t0 = performance.now();
  var listEl = document.getElementById('convList');
  var search = (document.getElementById('convSearch').value || '').toLowerCase();

  var filtered = CONVERSATIONS.filter(function (c) {
    if (!search) return true;
    var hay = [c.conversation_id, c.route, c.last_client_model,
               c.last_backend, c.date, c.note].join(' ').toLowerCase();
    return hay.indexOf(search) !== -1;
  });

  var sorted = sortConversations(filtered);

  if (!sorted.length) {
    listEl.innerHTML = '<div class="empty">暂无数据</div>';
    return;
  }

  var html = '';
  sorted.forEach(function (c) {
    var hasErr = c.turn_count > 0 && c.last_client_model === ''; // 近似判断
    var cls = 'conv-item';
    if (c.conversation_id === currentConvId && c.date === currentDate) {
      cls += ' active';
    }
    html += '<div class="' + cls + '" onclick="openConversation(\'' +
      escAttr(c.conversation_id) + '\',\'' + escAttr(c.date) + '\')">';
    html += '<div class="conv-item-top">';
    html += '<span class="conv-id">' + escHtml(c.conversation_id) + '</span>';
    // 检查 turns 是否有 error — 通过后端传来的数据无法直接判断，
    // 这里根据 last_client_model 为空且 route 存在来推断可能有问题
    html += '</div>';
    html += '<div class="conv-item-meta">';
    html += '<span>' + escHtml(c.last_client_model || 'unknown') + '</span>';
    html += '<span>via ' + escHtml(c.last_backend || '?') + '</span>';
    html += '<span>' + c.turn_count + ' 轮</span>';
    if (c.date) html += '<span>' + escHtml(c.date) + '</span>';
    html += '</div>';
    html += '</div>';
  });
  listEl.innerHTML = html;
  console.log('[前端] 渲染列表 %d 条, 耗时 %.0f ms', sorted.length, performance.now() - t0);
}

function escHtml(s) {
  if (!s) return '';
  return String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
}

function escAttr(s) {
  if (!s) return '';
  return String(s).replace(/'/g, "\\'").replace(/"/g, '&quot;');
}

/* ===== 打开会话 ===== */
var currentDoc = null;
var currentTurnIdx = 0;
var contrastOpen = false;

async function openConversation(convId, date) {
  console.time('[前端] 打开会话');
  currentConvId = convId;
  currentDate = date;
  currentTurnIdx = 0;

  // 高亮列表项
  renderConversationList();

  // 显示加载状态
  var viewerContent = document.getElementById('viewerContent');
  var viewerEmpty = document.getElementById('viewerEmpty');
  viewerEmpty.style.display = 'none';
  viewerContent.style.display = 'flex';

  document.getElementById('chatMessages').innerHTML =
    '<div class="empty" style="padding:40px">加载中…</div>';
  document.getElementById('turnBar').innerHTML = '';
  document.getElementById('convMeta').innerHTML = '';

  try {
    var t0 = performance.now();
    // 先用 summary 模式获取整体结构（轻量），同时加载第一个 turn 的详情
    var params = '?turn=0';
    if (date) params += '&date=' + encodeURIComponent(date);
    var data = await api('/api/admin/logs/' + encodeURIComponent(convId) + params);
    var apiMs = (performance.now() - t0).toFixed(0);
    var doc = data.conversation;
    var totalTurns = doc._total_turns || 1;
    console.log('[前端] API /api/admin/logs/%s?turn=0 总turns=%d, 耗时 %s ms', convId, totalTurns, apiMs);
    // 补全 turn 数组以便 turn bar 渲染
    currentDoc = doc;
    currentDoc._allTurnCount = totalTurns;
    // 为其他未加载的 turn 创建占位
    while (currentDoc.turns.length < totalTurns) {
      currentDoc.turns.push(null);
    }
    loadTurn(0);
    console.timeEnd('[前端] 打开会话');
  } catch (e) {
    console.timeEnd('[前端] 打开会话');
    document.getElementById('chatMessages').innerHTML =
      '<div class="chat-messages"><div class="chat-msg error"><div class="chat-bubble">加载失败: ' +
      escHtml(e.message) + '</div></div></div>';
  }
}

function loadTurn(idx) {
  console.time('[前端] 渲染Turn');
  if (!currentDoc || !currentDoc.turns || !currentDoc.turns.length) {
    document.getElementById('chatMessages').innerHTML =
      '<div class="empty">该会话没有 turn 数据</div>';
    console.timeEnd('[前端] 渲染Turn');
    return;
  }
  var totalTurns = currentDoc._allTurnCount || currentDoc.turns.length;
  if (idx < 0) idx = 0;
  if (idx >= totalTurns) idx = totalTurns - 1;
  currentTurnIdx = idx;
  if (!currentDoc.turns[idx]) {
    document.getElementById('chatMessages').innerHTML =
      '<div class="empty" style="padding:40px">加载 Turn ' + (idx + 1) + '…</div>';
    fetchTurn(idx);
    return;
  }

  var turn = currentDoc.turns[idx];
  renderTurnBar();
  renderConvMeta(turn);
  renderMessages(turn);

  // 滚动到顶部
  document.getElementById('chatViewport').scrollTop = 0;
  console.timeEnd('[前端] 渲染Turn');
}

async function fetchTurn(idx) {
  var params = '?turn=' + idx;
  if (currentDate) params += '&date=' + encodeURIComponent(currentDate);
  try {
    var t0 = performance.now();
    var data = await api('/api/admin/logs/' + encodeURIComponent(currentConvId) + params);
    var apiMs = (performance.now() - t0).toFixed(0);
    var newDoc = data.conversation;
    var fetchedTurn = newDoc.turns[0];
    currentDoc.turns[idx] = fetchedTurn;
    if (newDoc._allTurnCount) currentDoc._allTurnCount = newDoc._allTurnCount;
    console.log('[前端] fetchTurn(%d) 耗时 %s ms', idx, apiMs);
    loadTurn(idx);
  } catch (e) {
    document.getElementById('chatMessages').innerHTML =
      '<div class="chat-messages"><div class="chat-msg error"><div class="chat-bubble">加载 Turn ' +
      (idx + 1) + ' 失败: ' + escHtml(e.message) + '</div></div></div>';
    console.timeEnd('[前端] 渲染Turn');
  }
}

function renderTurnBar() {
  var totalTurns = currentDoc._allTurnCount || currentDoc.turns.length;
  var html = '';
  for (var i = 0; i < totalTurns; i++) {
    var t = currentDoc.turns[i];
    var cls = 'turn-tab';
    if (i === currentTurnIdx) cls += ' active';
    if (t && t.error) cls += ' turn-error';
    var label = 'Turn ' + (i + 1);
    if (!t) label += ' …';
    else if (t.error) label += ' ⚠';
    html += '<button class="' + cls + '" onclick="loadTurn(' + i + ')">' + label + '</button>';
  }
  document.getElementById('turnBar').innerHTML = html;
}

function renderConvMeta(turn) {
  var html = '';
  html += '<span>模型: <strong>' + escHtml(turn.client_model || '?') + '</strong></span>';
  html += '<span>后端: <strong>' + escHtml(turn.backend || '?') + '</strong></span>';
  html += '<span>上游: <strong>' + escHtml(turn.upstream_model || '?') + '</strong></span>';
  html += '<span>流式: <strong>' + (turn.stream ? '是' : '否') + '</strong></span>';
  if (turn.usage) {
    html += '<span>Token: <strong>' +
      (turn.usage.prompt_tokens || 0) + ' in / ' +
      (turn.usage.completion_tokens || 0) + ' out</strong></span>';
  }
  if (turn.duration_ms) {
    html += '<span>耗时: <strong>' + (turn.duration_ms / 1000).toFixed(1) + 's</strong></span>';
  }
  if (turn.error) {
    html += '<span class="meta-err">⚠ 有错误</span>';
  }
  document.getElementById('convMeta').innerHTML = html;
}

/* ===== 消息渲染 ===== */
function renderMessages(turn) {
  var t0 = performance.now();
  var chatEl = document.getElementById('chatMessages');
  try {
  var html = '<div class="chat-messages">';

  // 1. 渲染 client_request.messages
  var msgs = (turn.client_request && turn.client_request.messages) || [];
  msgs.forEach(function (msg, i) {
    try {
      html += renderMessageBubble(msg, i);
    } catch(e) {
      console.error('[前端] renderMessageBubble msg[' + i + '] 出错:', e, msg);
    }
  });

  // 2. 如果有非流式响应，渲染
  if (turn.client_response && !turn.stream) {
    html += renderNonStreamResponse(turn.client_response);
  }

  // 3. 错误
  if (turn.error) {
    html += renderErrorBubble(turn.error);
  }

  // 4. 流式数据直接折叠渲染（不再需要手动播放）
  if (turn.stream && turn.stream_trace && turn.stream_trace.client_events &&
      turn.stream_trace.client_events.length > 0) {
    var folded = foldStreamEvents(turn.stream_trace.client_events);
    if (folded.content || folded.reasoning) {
      html += '<div class="chat-msg assistant">';
      html += '<div class="chat-avatar">🤖</div>';
      html += '<div class="chat-bubble">';
      if (folded.reasoning) {
        html += '<details style="margin-bottom:8px"><summary style="cursor:pointer;color:var(--muted);font-size:12px">💭 思考过程 (' +
          folded.reasoning.length + ' 字符)</summary>';
        html += '<div style="margin-top:4px;color:var(--muted);font-style:italic;white-space:pre-wrap;font-size:12px">' +
          escHtml(folded.reasoning) + '</div></details>';
      }
      if (folded.content) {
        html += renderMarkdown(folded.content);
      }
      if (folded.toolCalls.length > 0) {
        html += '<div class="tool-calls-block">';
        folded.toolCalls.forEach(function(tc) {
          var func = tc.function || {};
          var args = '';
          try { args = JSON.stringify(JSON.parse(func.arguments || '{}'), null, 2); } catch(e) { args = func.arguments || ''; }
          html += '<div class="tool-call-card">';
          html += '<div class="tool-call-header"><span class="tool-call-icon">🔧</span>';
          html += '<span class="tool-call-name">' + escHtml(func.name || 'unknown') + '</span>';
          html += '<span class="tool-call-id">' + escHtml(tc.id || '') + '</span></div>';
          html += '<div class="tool-call-body">' + escHtml(args) + '</div></div>';
        });
        html += '</div>';
      }
      html += '</div></div>';
    }
  }

  // 5. 流式摘要
  if (turn.stream && turn.stream_trace && turn.stream_trace.summary) {
    var sum = turn.stream_trace.summary;
    html += '<div class="chat-msg system" style="margin-top:8px">';
    html += '<div class="chat-bubble" style="max-height:none;font-size:11px;padding:6px 10px">';
    html += '📊 流式摘要: ' + (sum.chunk_count || sum.event_count || 0) + ' 个片段';
    if (sum.usage) {
      html += ' | Token: ' + (sum.usage.prompt_tokens || 0) + ' in / ' +
        (sum.usage.completion_tokens || 0) + ' out';
    }
    if (sum.truncated) {
      html += ' | ⚠ 数据被截断（开启 VERBOSE_FULL_STREAM=1 可保留完整数据）';
    }
    html += '</div></div>';
  }

  html += '</div>';
  chatEl.innerHTML = html;

  // 高亮代码块
  if (typeof hljs !== 'undefined') {
    try {
      chatEl.querySelectorAll('pre code').forEach(function (block) {
        hljs.highlightElement(block);
      });
    } catch(e) {}
  }

  // 绑定工具调用的点击展开
  chatEl.querySelectorAll('.tool-call-header').forEach(function (el) {
    el.addEventListener('click', function () {
      this.classList.toggle('open');
      this.nextElementSibling.classList.toggle('open');
    });
  });
  chatEl.querySelectorAll('.tool-result-header').forEach(function (el) {
    el.addEventListener('click', function () {
      this.nextElementSibling.classList.toggle('open');
    });
  });

  console.log('[前端] 渲染消息 %d 条 (含%s流式), 耗时 %.0f ms',
    msgs.length,
    (turn.stream && turn.stream_trace && turn.stream_trace.client_events &&
     turn.stream_trace.client_events.length > 0) ? '' : '无',
    performance.now() - t0);
  } catch(e) {
    console.error('[前端] renderMessages 崩溃:', e);
  }
}

function renderMessageBubble(msg, idx) {
  var role = msg.role || 'unknown';
  var content = msg.content;
  var html = '';

  // System prompt → 折叠
  if (role === 'system' || role === 'developer') {
    var text = extractText(content);
    var short = text.substring(0, 200);
    html += '<div class="chat-msg system">';
    html += '<div class="chat-avatar">⚙</div>';
    html += '<div>';
    html += '<button class="system-toggle" onclick="var b=this.nextElementSibling;var v=b.style.display===\'block\';b.style.display=v?\'none\':\'block\';this.textContent=v?\'展开 System Prompt\':\'收起 System Prompt\'">展开 System Prompt</button>';
    html += '<div class="chat-bubble" style="display:none">' +
      escHtml(text) + '</div>';
    html += '<div style="font-size:11px;color:var(--muted);padding:2px 0">' +
      escHtml(short) + '… (' + text.length + ' 字符)</div>';
    html += '</div></div>';
    return html;
  }

  // Tool 结果
  if (role === 'tool') {
    var resultText = extractText(content);
    var shortResult = resultText.substring(0, 150);
    html += '<div class="chat-msg tool">';
    html += '<div class="chat-avatar">📦</div>';
    html += '<div>';
    html += '<div class="tool-result-card">';
    html += '<div class="tool-result-header">';
    html += '📋 工具结果 <span class="tool-result-id">' +
      escHtml(msg.tool_call_id || '') + '</span>';
    html += '</div>';
    html += '<div class="tool-result-body">' + escHtml(resultText) + '</div>';
    html += '</div>';
    html += '<div style="font-size:11px;color:var(--muted);padding:2px 0">' +
      escHtml(shortResult) + (resultText.length > 150 ? '…' : '') + '</div>';
    html += '</div></div>';
    return html;
  }

  // User & Assistant
  var avatar = role === 'user' ? '👤' : '🤖';
  var cls = 'chat-msg ' + (role === 'user' ? 'user' : 'assistant');
  html += '<div class="' + cls + '">';
  html += '<div class="chat-avatar">' + avatar + '</div>';
  html += '<div style="min-width:0">';

  // 内容
  if (typeof content === 'string') {
    html += '<div class="chat-bubble">' + renderMarkdown(content) + '</div>';
  } else if (Array.isArray(content)) {
    // 多模态内容
    var textParts = [];
    content.forEach(function (part) {
      if (part.type === 'text') {
        textParts.push(part.text || '');
      } else if (part.type === 'image_url') {
        textParts.push('[🖼 图片: ' + (part.image_url && part.image_url.url ?
          part.image_url.url.substring(0, 50) : '?') + '…]');
      } else if (part.type === 'input_audio') {
        textParts.push('[🎤 音频输入]');
      } else if (part.type === 'tool_use') {
        textParts.push('[🔧 工具调用: ' + (part.name || '?') + ']');
      } else if (part.type === 'tool_result') {
        textParts.push('[📋 工具结果]');
      } else {
        textParts.push('[' + (part.type || 'unknown') + ']');
      }
    });
    html += '<div class="chat-bubble">' + renderMarkdown(textParts.join('\n\n')) + '</div>';
  } else {
    html += '<div class="chat-bubble">' + escHtml(String(content || '')) + '</div>';
  }

  // 工具调用
  if (msg.tool_calls && msg.tool_calls.length > 0) {
    html += '<div class="tool-calls-block">';
    msg.tool_calls.forEach(function (tc) {
      var func = tc.function || {};
      var args = '';
      try {
        args = JSON.stringify(JSON.parse(func.arguments || '{}'), null, 2);
      } catch (e) {
        args = func.arguments || '';
      }
      html += '<div class="tool-call-card">';
      html += '<div class="tool-call-header">';
      html += '<span class="tool-call-icon">🔧</span>';
      html += '<span class="tool-call-name">' + escHtml(func.name || 'unknown') + '</span>';
      html += '<span class="tool-call-id">' + escHtml(tc.id || '') + '</span>';
      html += '</div>';
      html += '<div class="tool-call-body">' + escHtml(args) + '</div>';
      html += '</div>';
    });
    html += '</div>';
  }

  html += '</div></div>';
  return html;
}

function extractText(content) {
  if (typeof content === 'string') return content;
  if (Array.isArray(content)) {
    return content.map(function (p) {
      if (p.type === 'text') return p.text || '';
      if (p.type === 'image_url') return '[图片]';
      return '[' + (p.type || '?') + ']';
    }).join('');
  }
  if (content === null || content === undefined) return '';
  return String(content);
}

function renderNonStreamResponse(response) {
  if (!response || !response.choices) return '';
  var html = '<div class="chat-msg assistant"><div class="chat-avatar">🤖</div><div class="chat-bubble">';
  response.choices.forEach(function (choice) {
    var msg = choice.message || {};
    if (msg.content) {
      html += renderMarkdown(typeof msg.content === 'string' ? msg.content : JSON.stringify(msg.content));
    }
    if (msg.tool_calls) {
      msg.tool_calls.forEach(function (tc) {
        var func = tc.function || {};
        html += '<div class="tool-call-card" style="margin:4px 0"><div class="tool-call-header">';
        html += '🔧 ' + escHtml(func.name || '?');
        html += '</div></div>';
      });
    }
  });
  html += '</div></div>';
  return html;
}

function renderErrorBubble(error) {
  var html = '<div class="chat-msg error"><div class="chat-avatar">⚠️</div>';
  html += '<div class="chat-bubble">';
  html += '<strong>错误</strong>\n\n';
  if (typeof error === 'string') {
    html += escHtml(error);
  } else if (error && typeof error === 'object') {
    html += escHtml('阶段: ' + (error.stage || 'unknown') + '\n');
    html += escHtml('消息: ' + (error.message || JSON.stringify(error)));
  }
  html += '</div></div>';
  return html;
}

function renderMarkdown(text) {
  if (!text) return '';
  // Cursor 消息中包含大量 XML 标签（<user_info>, <agent_skill> 等），
  // marked.js 会将其当作 HTML 吃掉内容。检测到 XML 标签时直接用纯文本渲染。
  var hasXmlTags = /<[a-zA-Z_][a-zA-Z0-9_.-]*(\s[^>]*)?>/.test(text);
  if (hasXmlTags) {
    return '<pre style="white-space:pre-wrap;margin:0;font-family:Consolas,Monaco,monospace;font-size:12px;max-height:400px;overflow-y:auto">' +
      escHtml(text) + '</pre>';
  }
  if (typeof marked !== 'undefined') {
    try {
      var rendered = marked.parse(text);
      return rendered;
    } catch (e) {
      // fall through
    }
  }
  return '<pre style="white-space:pre-wrap;margin:0;font-family:inherit">' +
    escHtml(text) + '</pre>';
}

/* ===== 流式事件折叠为完整文本 ===== */
function foldStreamEvents(events) {
  var content = '';
  var reasoning = '';
  var toolCalls = [];
  events.forEach(function(event) {
    var data = (event && event.data) ? event.data : event;
    if (!data) return;
    var chunk = data;
    if (typeof chunk === 'string') {
      try { chunk = JSON.parse(chunk); } catch(e) { return; }
    }
    if (!chunk || !chunk.choices || !chunk.choices.length) return;
    var delta = chunk.choices[0].delta || {};
    if (delta.reasoning_content) reasoning += delta.reasoning_content;
    if (delta.content) content += delta.content;
    if (delta.tool_calls) {
      delta.tool_calls.forEach(function(tc) { toolCalls.push(tc); });
    }
  });
  return { content: content, reasoning: reasoning, toolCalls: toolCalls };
}

/* ===== 对比视图 ===== */
function toggleContrast() {
  var turn = currentDoc.turns[currentTurnIdx];
  if (!turn) return;

  var panel = document.getElementById('contrastPanel');

  if (contrastOpen) {
    panel.style.display = 'none';
    contrastOpen = false;
    document.getElementById('btnContrast').innerHTML = '△ 对比视图';
    return;
  }

  contrastOpen = true;
  panel.style.display = 'flex';
  document.getElementById('btnContrast').innerHTML = '▽ 收起对比';

  var clientReq = turn.client_request || {};
  var upstreamReq = turn.upstream_request || {};

  document.getElementById('contrastClient').textContent = JSON.stringify(
    { model: clientReq.model, messages: clientReq.messages, tools: clientReq.tools },
    null, 2);
  document.getElementById('contrastUpstream').textContent = JSON.stringify(
    { body: upstreamReq.body, headers: upstreamReq.headers }, null, 2);
}

/* ===== SSE 实时更新 ===== */
function startLiveSSE() {
  if (liveEs) { try { liveEs.close(); } catch (e) {} }

  var url = '/api/admin/logs/live';
  if (authKey) url += '?key=' + encodeURIComponent(authKey);
  liveEs = new EventSource(url);

  liveEs.onmessage = function (e) {
    if (!liveEnabled) return;
    try {
      var evt = JSON.parse(e.data);
      if (evt.type === 'turn_started' || evt.type === 'turn_done') {
        // 有新 turn，刷新列表
        loadConversationList();
      }
    } catch (err) {}
  };

  liveEs.onerror = function () {
    document.getElementById('statusBadge').textContent = '已断开';
    document.getElementById('statusBadge').style.color = 'var(--red)';
  };

  document.getElementById('statusBadge').textContent = '已连接';
  document.getElementById('statusBadge').style.color = 'var(--green)';
}

function toggleLiveUpdates() {
  liveEnabled = !liveEnabled;
  // 只用 class 切换，不用 innerHTML（避免 DOM 引用失效）
  var dot = document.getElementById('liveDot');
  if (dot) {
    if (liveEnabled) {
      dot.classList.add('live-on');
    } else {
      dot.classList.remove('live-on');
    }
  }
}

/* ===== 复制对话 ===== */
function copyConversation() {
  if (!currentDoc) { toast('请先选择一条会话', false); return; }
  var turn = currentDoc.turns[currentTurnIdx];
  if (!turn) { toast('当前 turn 数据为空', false); return; }

  var lines = [];
  lines.push('=== 会话: ' + currentConvId + ' ===');
  lines.push('模型: ' + (turn.client_model || '?') +
    ' | 后端: ' + (turn.backend || '?') +
    ' | 上游: ' + (turn.upstream_model || '?') +
    ' | 流式: ' + (turn.stream ? '是' : '否'));
  if (turn.duration_ms) lines.push('耗时: ' + (turn.duration_ms / 1000).toFixed(1) + 's');
  if (turn.usage) {
    lines.push('Token: ' + (turn.usage.prompt_tokens || 0) + ' in / ' +
      (turn.usage.completion_tokens || 0) + ' out');
  }
  lines.push('Turn ' + (currentTurnIdx + 1) + ' / ' +
    (currentDoc._allTurnCount || currentDoc.turns.length));
  lines.push('');

  // 消息
  var msgs = (turn.client_request && turn.client_request.messages) || [];
  msgs.forEach(function(msg, i) {
    var role = (msg.role || 'unknown').toUpperCase();
    var content = msg.content;
    var text = '';
    if (typeof content === 'string') {
      text = content;
    } else if (Array.isArray(content)) {
      text = content.map(function(p) {
        if (p.type === 'text') return p.text || '';
        if (p.type === 'image_url') return '[图片: ' + (p.image_url && p.image_url.url ? p.image_url.url.substring(0, 80) : '') + ']';
        return '[' + (p.type || '?') + ']';
      }).join('\n');
    } else if (content) {
      text = JSON.stringify(content);
    }
    lines.push('--- ' + role + ' ---');
    // 工具调用
    if (msg.tool_calls && msg.tool_calls.length) {
      lines.push('[工具调用: ' + msg.tool_calls.map(function(tc) {
        return (tc.function || {}).name || '?';
      }).join(', ') + ']');
    }
    if (msg.tool_call_id) {
      lines.push('[tool_call_id: ' + msg.tool_call_id + ']');
    }
    lines.push(text.substring(0, 2000)); // 每条消息最多 2000 字符
    if (text.length > 2000) lines.push('... (截断, 完整长度: ' + text.length + ' 字符)');
    lines.push('');
  });

  // 流式响应
  if (turn.stream && turn.stream_trace) {
    var folded = foldStreamEvents(turn.stream_trace.client_events || []);
    if (folded.reasoning) {
      lines.push('--- ASSISTANT (思考) ---');
      lines.push(folded.reasoning.substring(0, 3000));
      if (folded.reasoning.length > 3000) lines.push('... (截断, 完整: ' + folded.reasoning.length + ' 字符)');
      lines.push('');
    }
    if (folded.content) {
      lines.push('--- ASSISTANT (回复) ---');
      lines.push(folded.content.substring(0, 5000));
      if (folded.content.length > 5000) lines.push('... (截断, 完整: ' + folded.content.length + ' 字符)');
      lines.push('');
    }
    if (folded.toolCalls.length) {
      lines.push('--- ASSISTANT (工具调用) ---');
      folded.toolCalls.forEach(function(tc) {
        var func = tc.function || {};
        lines.push('  ' + (func.name || '?') + ': ' +
          (func.arguments || '').substring(0, 500));
      });
      lines.push('');
    }
  }

  // 错误
  if (turn.error) {
    lines.push('--- ERROR ---');
    if (typeof turn.error === 'string') {
      lines.push(turn.error);
    } else if (turn.error && typeof turn.error === 'object') {
      lines.push('阶段: ' + (turn.error.stage || 'unknown'));
      lines.push('消息: ' + (turn.error.message || JSON.stringify(turn.error)));
    }
    lines.push('');
  }

  // 流式摘要
  if (turn.stream && turn.stream_trace && turn.stream_trace.summary) {
    var sum = turn.stream_trace.summary;
    lines.push('--- 流式摘要 ---');
    lines.push('事件数: ' + (sum.chunk_count || sum.event_count || 0));
    if (sum.usage) lines.push('Token: ' + (sum.usage.prompt_tokens || 0) +
      ' in / ' + (sum.usage.completion_tokens || 0) + ' out');
    lines.push('');
  }

  var text = lines.join('\n');
  navigator.clipboard.writeText(text).then(function() {
    toast('已复制 ' + lines.length + ' 行对话信息');
  }).catch(function() {
    // Fallback: 选中文本让用户手动复制
    var ta = document.createElement('textarea');
    ta.value = text;
    ta.style.position = 'fixed';
    ta.style.left = '-9999px';
    document.body.appendChild(ta);
    ta.select();
    document.execCommand('copy');
    document.body.removeChild(ta);
    toast('已复制对话信息');
  });
}

/* ===== 辅助 ===== */
function scrollToBottom() {
  var viewport = document.getElementById('chatViewport');
  viewport.scrollTop = viewport.scrollHeight;
}
