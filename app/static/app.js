// ========== 客户跟进提醒系统 - 应用逻辑 ==========
// ========== Utility ==========
function escapeHtml(str) {
  if (!str) return '';
  // 历史备注可能带有旗帜、星级等 Emoji。仅在显示层移除，数据库原文保持不变。
  return String(str).replace(/[\p{Extended_Pictographic}\p{Regional_Indicator}\uFE0F\u200D]/gu, '').replace(/[&<>"']/g, function(m) {
    return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":"&#39;"}[m];
  });
}

// 富文本渲染：仅允许高亮 mark 与换行 br，其他标签全部转义。
// 用于沟通记录 content / result / next_plan 与周报正文的展示。
// 输入是数据库里原始的 HTML 字符串（可能含 <mark>），输出是可直接 innerHTML 的安全字符串。
var _RICH_TEXT_ALLOWED_COLORS = { yellow: 1, green: 1, pink: 1 };
function renderRichText(str) {
  if (!str) return '';
  var s = String(str);
  // 第一步：先移除 Emoji（与 escapeHtml 一致）
  s = s.replace(/[\p{Extended_Pictographic}\p{Regional_Indicator}\uFE0F\u200D]/gu, '');
  // 第二步：用占位符把合法的 <mark ...>、</mark> 暂存
  var marks = [];
  s = s.replace(/<mark\b[^>]*>/gi, function(m) {
    var colorMatch = /class=["']\s*hl-(yellow|green|pink)\s*["']/i.exec(m);
    var color = colorMatch ? colorMatch[1].toLowerCase() : 'yellow';
    if (!_RICH_TEXT_ALLOWED_COLORS[color]) color = 'yellow';
    marks.push('<mark class="hl-' + color + '">');
    return '\u0000MARK_OPEN_' + (marks.length - 1) + '\u0000';
  });
  s = s.replace(/<\/mark\s*>/gi, function() {
    return '\u0000MARK_CLOSE\u0000';
  });
  s = s.replace(/<br\s*\/?\s*>/gi, '\u0000RICH_BR\u0000');
  // 第三步：转义剩余 HTML（不转义 &：输入已是浏览器转义后的 entity，再转义会双重）
  s = s.replace(/[<>"']/g, function(m) {
    return { '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[m];
  });
  // 第四步：还原占位符为合法 mark 标签
  s = s.replace(/\u0000MARK_OPEN_(\d+)\u0000/g, function(_, i) { return marks[+i]; });
  s = s.replace(/\u0000MARK_CLOSE\u0000/g, '</mark>');
  s = s.replace(/\u0000RICH_BR\u0000/g, '<br>');
  // 第五步：把被错误转义的 &lt;mark&gt; 也清理掉（旧数据可能存过 HTML）
  s = s.replace(/&lt;mark\b[^&]*?&gt;/gi, '').replace(/&lt;\/mark&gt;/gi, '');
  s = s.replace(/&lt;br\s*\/?\s*&gt;/gi, '<br>');
  return s;
}

// 把 textarea 文本转成可存 HTML（保留换行，不做任何高亮）
function plainTextToHtml(str) {
  if (!str) return '';
  return escapeHtml(str).replace(/\n/g, '<br>');
}

function richTextPlain(el) {
  return el ? (el.innerText || el.textContent || '').trim() : '';
}

function richTextHtml(el) {
  if (!el) return '';
  // contenteditable 在不同浏览器中会用 div/p 包裹换行；存储层只需要 br 与 mark。
  return el.innerHTML.replace(/<(?:div|p)\b[^>]*>/gi, '<br>').replace(/<\/(?:div|p)>/gi, '').trim();
}

function setRichText(el, value, isHtml) {
  if (!el) return;
  el.innerHTML = isHtml ? renderRichText(value || '') : plainTextToHtml(value || '');
}

function uiIcon(name) {
  return '<span class="ui-icon ui-icon-' + name + '" aria-hidden="true"></span>';
}

function applyHoverLabels(root) {
  var scope = root && root.querySelectorAll ? root : document;
  var selector = 'button[aria-label], a[aria-label], [role="button"][aria-label], [role="menuitem"][aria-label], input[aria-label]';
  var controls = [];
  if (scope.matches && scope.matches(selector)) controls.push(scope);
  scope.querySelectorAll(selector).forEach(function(control) { controls.push(control); });
  controls.forEach(function(control) {
    if (control.getAttribute('title')) return;
    var label = (control.getAttribute('aria-label') || '').replace(/\s+/g, ' ').trim();
    if (label) control.setAttribute('title', label);
  });
}

var _ICON_ONLY_ACTIONS = {
  '关闭': ['close', '关闭'], '取消': ['close', '取消'], '删除': ['trash', '删除'],
  '编辑': ['edit', '编辑'], '快速编辑': ['edit', '快速编辑'], '导出邮箱': ['mail', '导出邮箱'],
  '导出全部沟通': ['export', '导出全部沟通'], '导出日历': ['export', '导出日历'],
  '同步 Apple 日历': ['calendar', '同步 Apple 日历'], '完整日历': ['calendar', '打开完整日历'],
  '复制链接': ['link', '复制链接'], '使用说明': ['info', '使用说明'], '操作日志': ['list', '操作日志'],
  '一键诊断': ['settings', '一键诊断'], '重新加载': ['refresh', '重新加载'],
  '重新检测': ['refresh', '重新检测'], '查看原文': ['open', '查看原文'], '清空': ['trash', '清空'],
  '返回修改': ['left', '返回修改'], '上一页': ['left', '上一页'], '下一页': ['right', '下一页']
};

var _INLINE_ICON_ACTIONS = {
  '打开导航': 'list',
  '关闭导航': 'close',
  '搜索客户': 'search',
  '记录沟通': 'message',
  '添加客户': 'plus',
  '批量记录': 'list',
  '打开完整日历': 'calendar',
  '同步 Apple 日历': 'calendar'
};

function applyIconButtons(root) {
  var scope = root && root.querySelectorAll ? root : document;
  scope.querySelectorAll('button:not([data-iconified])').forEach(function(button) {
    // 更多工具菜单已经在 HTML 中提供了同一套图标和文字标签；只在
    // 紧凑侧栏用 CSS 隐藏文字，避免动态 iconify 把菜单变成图标/文字混排。
    if (button.closest && button.closest('.sidebar-tools-menu')) return;
    var accessibleLabel = (button.getAttribute('aria-label') || button.getAttribute('title') || '').trim();
    var inlineIcon = _INLINE_ICON_ACTIONS[accessibleLabel];
    if (inlineIcon && button.querySelector('svg')) {
      button.querySelectorAll('svg').forEach(function(svg) {
        svg.outerHTML = uiIcon(inlineIcon);
      });
      button.dataset.iconified = 'true';
      return;
    }
    var label = (button.textContent || '').replace(/\s+/g, ' ').trim();
    var iconAction = _ICON_ONLY_ACTIONS[label];
    if (!iconAction) return;
    button.dataset.iconified = 'true';
    button.classList.add('icon-action-button');
    button.setAttribute('aria-label', iconAction[1]);
    button.setAttribute('title', iconAction[1]);
    button.innerHTML = uiIcon(iconAction[0]);
  });
}

function initIconButtons() {
  applyIconButtons(document);
  applyHoverLabels(document);
  // Coalesce DOM work from bursts of insertions (large list rebuilds) into a
  // single frame instead of running selector scans synchronously per mutation.
  var pendingNodes = [];
  var flushScheduled = false;
  function flushIconWork() {
    flushScheduled = false;
    var nodes = pendingNodes;
    pendingNodes = [];
    for (var i = 0; i < nodes.length; i++) {
      var node = nodes[i];
      if (!node || !node.isConnected) continue;
      if (node.matches && node.matches('button')) applyIconButtons({ querySelectorAll: function() { return [node]; } });
      if (node.querySelector && node.querySelector('button')) applyIconButtons(node);
      applyHoverLabels(node);
    }
  }
  var observer = new MutationObserver(function(mutations) {
    mutations.forEach(function(mutation) {
      mutation.addedNodes.forEach(function(node) {
        if (node.nodeType !== 1) return;
        pendingNodes.push(node);
      });
    });
    if (pendingNodes.length && !flushScheduled) {
      flushScheduled = true;
      requestAnimationFrame(flushIconWork);
    }
  });
  observer.observe(document.body, { childList: true, subtree: true });
}

// ========== Global State ==========
let currentPage = 'dashboard';
let _pageNavigationToken = 0;
let calendarData = {};
let calendarYear, calendarMonth;
let selectedCustomers = new Set();
let selectedNewPool = new Set();
let selectedTodayCustomers = new Set();
let currentUser = null;
let overviewWeekOffset = 0;
var _loginViewToken = 0;
var _loginUsersController = null;
let _batchCompleteTargets = [];
let _batchCompleteMode = ''; // today
let dashboardReminders = [];
let todayScheduleData = {};
let inboxItems = [];
let inboxQuestions = [];
var _inboxQuestionsContractLoaded = false;
let inboxFilter = 'all';
let userPreferences = null;
let customerFilters = {};
var _inboxReplyAnalysis = null;
var _inboxReplyAnalysisTimer = null;
var _inboxReplyAnalysisToken = 0;
var _inboxReplyRawContent = '';

// ========== User Colors ==========
const USER_COLORS = { 'hamid': '#4A90D9', 'amy': '#E8726A', 'kelley': '#5BB881' };
const USER_LABELS = { 'hamid': 'Hamid', 'amy': 'Amy', 'kelley': 'Kelley' };

// ========== Initialization ==========
document.addEventListener('DOMContentLoaded', function() {
  initMotionSystem();
  initMotionDiagnostics();
  checkLogin();
  initLiquidGlassPrototype();
  initIconButtons();
  initGlobalPageTools();
  initCustomerFileInput();
});

// ========== Motion System ==========
var _motionReduced = false;
var _autoLowPowerDevice = false;
var _runtimePerformanceSlow = false;
var _interfacePerformanceMode = 'auto';
var _performanceProbePromise = null;
var _performanceMonitorFrame = null;
var _performanceMonitorState = null;

function isMotionLite() {
  return document.documentElement.classList.contains('motion-lite');
}

function shouldAnimateLists() {
  return !_motionReduced && !isMotionLite();
}

function configuredInterfacePerformanceMode() {
  var mode = userPreferences && userPreferences.interface_performance;
  return ['auto', 'performance', 'full'].indexOf(mode) >= 0 ? mode : 'auto';
}

function applyInterfacePerformance(mode) {
  mode = ['auto', 'performance', 'full'].indexOf(mode) >= 0 ? mode : 'auto';
  _interfacePerformanceMode = mode;
  var probeSaysSlow = !!(userPreferences && userPreferences.performance_probe && userPreferences.performance_probe.slow);
  var useLiteMaterials = mode === 'performance' ||
    (mode === 'auto' && (_autoLowPowerDevice || _runtimePerformanceSlow || probeSaysSlow));
  var html = document.documentElement;
  html.classList.toggle('motion-lite', useLiteMaterials);
  html.classList.toggle('performance-priority', useLiteMaterials);
  html.dataset.interfacePerformance = mode;
  if (mode === 'auto') initRuntimePerformanceMonitor();
}

function initMotionSystem() {
  var reducedQuery = window.matchMedia('(prefers-reduced-motion: reduce)');
  _autoLowPowerDevice = (navigator.hardwareConcurrency && navigator.hardwareConcurrency <= 4) ||
    (navigator.deviceMemory && navigator.deviceMemory <= 4) ||
    (navigator.connection && navigator.connection.saveData);
  function syncMotionPreference() {
    _motionReduced = reducedQuery.matches;
    document.documentElement.classList.toggle('motion-reduced', _motionReduced);
  }
  syncMotionPreference();
  if (reducedQuery.addEventListener) reducedQuery.addEventListener('change', syncMotionPreference);
  else reducedQuery.addListener(syncMotionPreference);
  document.documentElement.classList.toggle('supports-view-transitions', !!document.startViewTransition);
  applyInterfacePerformance('auto');
}

// Sample a representative, off-screen list for less than one second.  Hardware
// hints do not describe old integrated GPUs reliably, so Auto mode also uses
// this local result.  It never reads customer data or sends page contents.
function runPerformanceProbe() {
  if (_performanceProbePromise) return _performanceProbePromise;
  if (!window.requestAnimationFrame || !window.performance || document.hidden) return Promise.resolve(null);
  _performanceProbePromise = new Promise(function(resolve) {
    var probe = document.createElement('div');
    probe.className = 'performance-probe';
    probe.setAttribute('aria-hidden', 'true');
    probe.innerHTML = Array.from({ length: 24 }, function(_, index) {
      return '<article><span></span><div><b>客户工作 ' + (index + 1) + '</b><i>最近沟通与下一步安排</i></div><em>今天</em></article>';
    }).join('');
    document.body.appendChild(probe);
    var frameCount = 0;
    var slowFrames = 0;
    var longestFrame = 0;
    var longTasks = 0;
    var previousFrame = performance.now();
    var frameId = null;
    var settled = false;
    var observer = null;
    if (window.PerformanceObserver) {
      try {
        observer = new PerformanceObserver(function(list) {
          list.getEntries().forEach(function(entry) { if (entry.duration >= 100) longTasks++; });
        });
        observer.observe({ entryTypes: ['longtask'] });
      } catch (ignore) { observer = null; }
    }
    function finish() {
      if (settled) return;
      settled = true;
      if (frameId) cancelAnimationFrame(frameId);
      if (observer) observer.disconnect();
      probe.remove();
      var slowRatio = frameCount ? slowFrames / frameCount : 1;
      resolve({
        version: 1,
        sampled_at: new Date().toISOString(),
        frame_count: frameCount,
        slow_frames: slowFrames,
        slow_ratio: Number(slowRatio.toFixed(3)),
        long_tasks: longTasks,
        longest_frame: Math.round(longestFrame),
        slow: frameCount < 18 || slowRatio > 0.08 || longTasks > 0
      });
    }
    function sample(now) {
      var delta = now - previousFrame;
      previousFrame = now;
      frameCount++;
      longestFrame = Math.max(longestFrame, delta);
      if (delta > 25) slowFrames++;
      frameId = requestAnimationFrame(sample);
    }
    frameId = requestAnimationFrame(sample);
    window.setTimeout(finish, 760);
  }).finally(function() { _performanceProbePromise = null; });
  return _performanceProbePromise;
}

function startInitialPerformanceProbe() {
  if (!currentUser || !userPreferences || userPreferences.performance_probe || document.hidden) return;
  window.setTimeout(async function() {
    if (!currentUser || !userPreferences || userPreferences.performance_probe || document.hidden) return;
    var result = await runPerformanceProbe();
    if (!result || !userPreferences || !currentUser) return;
    userPreferences.performance_probe = result;
    if (configuredInterfacePerformanceMode() === 'auto' && result.slow) _runtimePerformanceSlow = true;
    applyInterfacePerformance(configuredInterfacePerformanceMode());
    // Persist silently so the same account does not repeat a startup probe.
    persistUserPreferences(true);
  }, 350);
}

function initRuntimePerformanceMonitor() {
  if (_performanceMonitorFrame || document.hidden || !currentUser || configuredInterfacePerformanceMode() !== 'auto') return;
  _performanceMonitorState = { startedAt: performance.now(), previousFrame: performance.now(), frames: 0, slowFrames: 0 };
  function reset(now) {
    _performanceMonitorState = { startedAt: now, previousFrame: now, frames: 0, slowFrames: 0 };
  }
  function sample(now) {
    if (document.hidden || !currentUser || configuredInterfacePerformanceMode() !== 'auto') {
      _performanceMonitorFrame = null;
      return;
    }
    var state = _performanceMonitorState;
    var delta = now - state.previousFrame;
    state.previousFrame = now;
    // A tab returning from sleep is not evidence that the active UI is slow.
    if (delta > 1000) reset(now);
    else {
      state.frames++;
      if (delta > 30) state.slowFrames++;
      if (now - state.startedAt >= 3000) {
        if (state.frames >= 30 && state.slowFrames / state.frames > 0.1) {
          _runtimePerformanceSlow = true;
          applyInterfacePerformance('auto');
        }
        reset(now);
      }
    }
    _performanceMonitorFrame = requestAnimationFrame(sample);
  }
  _performanceMonitorFrame = requestAnimationFrame(sample);
}

document.addEventListener('visibilitychange', function() {
  if (document.hidden) {
    if (_performanceMonitorFrame) cancelAnimationFrame(_performanceMonitorFrame);
    _performanceMonitorFrame = null;
    return;
  }
  initRuntimePerformanceMonitor();
});

function runViewUpdate(update, type) {
  if (_motionReduced || isMotionLite() || !document.startViewTransition) {
    update();
    return null;
  }
  document.documentElement.dataset.motionType = type || 'content';
  var transition = document.startViewTransition(update);
  transition.finished.finally(function() {
    delete document.documentElement.dataset.motionType;
  });
  return transition;
}

// Opt-in runtime measurements for release checks. They stay dormant in normal
// customer sessions and can be opened with ?motion_debug=1 during QA.
function initMotionDiagnostics() {
  if (!new URLSearchParams(window.location.search).has('motion_debug')) return;
  var metrics = { frames: 0, slowFrames: 0, longestFrame: 0, longTasks: 0, longestTask: 0, startedAt: performance.now(), collecting: false };
  var output = document.createElement('output');
  output.id = 'motionDiagnostics';
  output.hidden = true;
  document.documentElement.dataset.motionDiagnostics = 'active';
  document.body.appendChild(output);
  window.getMotionDiagnostics = function() {
    return { frames: metrics.frames, slowFrames: metrics.slowFrames, longestFrame: Math.round(metrics.longestFrame), longTasks: metrics.longTasks, longestTask: Math.round(metrics.longestTask), elapsed: metrics.collecting ? Math.round(performance.now() - metrics.startedAt) : 0 };
  };
  function publishMetrics() { output.textContent = JSON.stringify(window.getMotionDiagnostics()); }
  publishMetrics();
  setInterval(publishMetrics, 500);
  if (window.PerformanceObserver) {
    try {
      var longTaskObserver = new PerformanceObserver(function(list) {
        if (!metrics.collecting) return;
        list.getEntries().forEach(function(entry) {
          metrics.longTasks++;
          metrics.longestTask = Math.max(metrics.longestTask, entry.duration);
        });
      });
      longTaskObserver.observe({ entryTypes: ['longtask'] });
    } catch (error) { /* Frame sampling remains available. */ }
  }
  var previousFrame = performance.now();
  setTimeout(function() {
    metrics.frames = 0;
    metrics.slowFrames = 0;
    metrics.longestFrame = 0;
    metrics.longTasks = 0;
    metrics.longestTask = 0;
    metrics.startedAt = performance.now();
    metrics.collecting = true;
  }, 500);
  function sampleFrame(now) {
    var delta = now - previousFrame;
    previousFrame = now;
    if (!metrics.collecting) {
      requestAnimationFrame(sampleFrame);
      return;
    }
    metrics.frames++;
    metrics.longestFrame = Math.max(metrics.longestFrame, delta);
    if (delta > 20) metrics.slowFrames++;
    requestAnimationFrame(sampleFrame);
  }
  requestAnimationFrame(sampleFrame);
}

// Keep list elements stable across data refreshes. This preserves reading
// position and lets changed rows move on the compositor instead of flashing.
function createElementFromHtml(html) {
  var template = document.createElement('template');
  template.innerHTML = String(html).trim();
  return template.content.firstElementChild;
}

function syncElementFromTemplate(target, fresh) {
  var transientMotionClasses = ['motion-list-enter', 'motion-list-leave', 'motion-updated'].filter(function(className) {
    return target.classList.contains(className);
  });
  Array.from(target.attributes).forEach(function(attribute) {
    if (!fresh.hasAttribute(attribute.name)) target.removeAttribute(attribute.name);
  });
  Array.from(fresh.attributes).forEach(function(attribute) {
    target.setAttribute(attribute.name, attribute.value);
  });
  // A fast background reconciliation must not cancel the confirmation that was
  // just painted by the write path. These classes are ephemeral UI state, not
  // part of the server template, so restore them after syncing attributes.
  transientMotionClasses.forEach(function(className) { target.classList.add(className); });
  target.innerHTML = fresh.innerHTML;
}

function animateKeyedReflow(nodes, previousRects) {
  if (!shouldAnimateLists()) return;
  var duration = 200;
  nodes.forEach(function(node) {
    var previous = previousRects.get(node.dataset.motionKey);
    if (!previous || !node.isConnected) return;
    var current = node.getBoundingClientRect();
    var deltaX = previous.left - current.left;
    var deltaY = previous.top - current.top;
    if (Math.abs(deltaX) < 1 && Math.abs(deltaY) < 1) return;
    node.style.transition = 'none';
    node.style.transform = 'translate3d(' + Math.round(deltaX) + 'px,' + Math.round(deltaY) + 'px,0)';
    node.style.willChange = 'transform';
    requestAnimationFrame(function() {
      node.style.transition = 'transform ' + duration + 'ms var(--motion-ease-out)';
      node.style.transform = '';
      setTimeout(function() {
        node.style.transition = '';
        node.style.willChange = '';
      }, duration + 30);
    });
  });
}

// A new row keeps its enter animation until it has actually run.  Dropping the
// class on the next frame cancels the keyframes before a single animated frame
// is painted, so new records used to appear with no motion at all.
function playListEnter(node) {
  if (!shouldAnimateLists()) return;
  node.classList.add('motion-list-enter');
  var cleanup = function() { node.classList.remove('motion-list-enter'); };
  node.addEventListener('animationend', cleanup, { once: true });
  window.setTimeout(cleanup, 400);
}

// Opt-in exit motion for lists where a record leaving is itself the message (a
// removed communication, a completed task).  The node is flagged while it
// collapses, so a re-render that arrives meanwhile neither reuses nor re-animates
// a row that is already on its way out.
function collapseListElement(node) {
  if (!shouldAnimateLists() || !node.isConnected) { node.remove(); return; }
  node.setAttribute('data-motion-leaving', '');
  node.classList.add('motion-list-leave');
  var remove = function() { node.remove(); };
  node.addEventListener('animationend', remove, { once: true });
  window.setTimeout(remove, 300);
}

function reconcileKeyedElements(container, records, options) {
  if (!container) return;
  var selector = options.selector;
  var shouldAnimate = shouldAnimateLists();
  var previousRects = new Map();
  var existing = new Map();
  Array.from(container.querySelectorAll(selector)).forEach(function(node) {
    var key = node.dataset.motionKey;
    if (!key || node.hasAttribute('data-motion-leaving')) return;
    if (shouldAnimate) previousRects.set(key, node.getBoundingClientRect());
    existing.set(key, node);
  });
  if (!existing.size) {
    if (options.chunkSize && records.length > options.chunkSize * 2) {
      container.textContent = '';
      var cursor = 0;
      return new Promise(function(resolve) {
        function appendChunk() {
          var end = Math.min(records.length, cursor + options.chunkSize);
          container.insertAdjacentHTML('beforeend', records.slice(cursor, end).map(function(record, offset) {
            return options.render(record, cursor + offset);
          }).join(''));
          var renderedNodes = container.querySelectorAll(selector);
          for (var index = cursor; index < end; index++) renderedNodes[index].dataset.motionKey = String(options.key(records[index]));
          cursor = end;
          if (cursor < records.length) requestAnimationFrame(appendChunk);
          else resolve(Array.from(renderedNodes));
        }
        appendChunk();
      });
    }
    // The first paint of a list stays still: motion explains a change, and at
    // this point nothing has changed yet.
    container.innerHTML = records.map(function(record, index) { return options.render(record, index); }).join('');
    var initialNodes = Array.from(container.querySelectorAll(selector));
    initialNodes.forEach(function(node, index) {
      node.dataset.motionKey = String(options.key(records[index]));
    });
    return Promise.resolve(initialNodes);
  }
  var nextNodes = records.map(function(record, index) {
    var key = String(options.key(record));
    var fresh = createElementFromHtml(options.render(record, index));
    fresh.dataset.motionKey = key;
    var node = existing.get(key);
    if (node) {
      var previousHtml = node.innerHTML;
      syncElementFromTemplate(node, fresh);
      if (options.changed && node.innerHTML !== previousHtml) options.changed(record, node, false);
    } else {
      node = fresh;
      playListEnter(node);
      if (options.changed) options.changed(record, node, true);
    }
    existing.delete(key);
    return node;
  });
  existing.forEach(function(node) {
    if (options.leave) collapseListElement(node);
    else node.remove();
  });
  var fragment = document.createDocumentFragment();
  nextNodes.forEach(function(node) { fragment.appendChild(node); });
  container.appendChild(fragment);
  animateKeyedReflow(nextNodes, previousRects);
  return Promise.resolve(nextNodes);
}

// Small-scope Liquid Glass prototype. It only coordinates visual state for the
// command shelf, the global AI lens and the quick-reply modal.
function initLiquidGlassPrototype() {
  var commandShelf = document.querySelector('.command-shelf');
  // The command shelf is a stable frame of reference. It keeps one material
  // treatment while the user scrolls, avoiding decorative glass movement.
  if (commandShelf) commandShelf.classList.remove('is-content-under');

  // 筛选器指示器跟随窗口变化重新定位
  window.addEventListener('resize', function() {
    updateFilterIndicator(document.getElementById('inboxFilters'));
    updateFilterIndicator(document.querySelector('.customer-view-chips'));
  }, { passive: true });

  var replyModal = document.querySelector('#inboxReplyModal .modal');
  var replyBody = document.querySelector('#inboxReplyModal .modal-body');
  if (replyModal && replyBody) {
    var replyGlassFrame = null;
    function syncReplyModalGlass() {
      replyModal.classList.toggle('is-body-scrolled', replyBody.scrollTop > 4);
      replyModal.classList.toggle('is-body-scrollable', replyBody.scrollHeight > replyBody.clientHeight + 2);
    }
    function scheduleReplyModalGlass() {
      if (replyGlassFrame) return;
      replyGlassFrame = requestAnimationFrame(function() {
        replyGlassFrame = null;
        syncReplyModalGlass();
      });
    }
    replyBody.addEventListener('scroll', scheduleReplyModalGlass, { passive: true });
    window.addEventListener('resize', syncReplyModalGlass, { passive: true });
    syncReplyModalGlass();
  }
}

// ========== Toast ==========
function showToast(message, type) {
  type = type || 'info';
  var container = document.getElementById('toastContainer');
  var toast = document.createElement('div');
  toast.className = 'toast ' + type;
  var icons = { success: "check", error: "close", info: "info", warning: "alert" };
  toast.innerHTML = uiIcon(icons[type] || "info") + "<span>" + message + "</span>";
  container.appendChild(toast);
  setTimeout(function() {
    toast.classList.add('is-leaving');
    setTimeout(function() { toast.remove(); }, _motionReduced ? 0 : 170);
  }, 3500);
}

function showToastAction(message, type, label, action) {
  var container = document.getElementById('toastContainer');
  var toast = document.createElement('div');
  toast.className = 'toast ' + (type || 'info');
  toast.appendChild(document.createTextNode(message + ' '));
  var button = document.createElement('button');
  button.type = 'button'; button.className = 'toast-action'; button.textContent = label;
  button.onclick = function() { action(); toast.remove(); };
  toast.appendChild(button); container.appendChild(toast);
  setTimeout(function() { if (toast.isConnected) toast.remove(); }, 6000);
}

// ========== Page Navigation ==========
var _globalSearchResults = [];
var _customerSearchResultById = {};
var _globalSearchActiveIndex = 0;
var _globalSearchTotal = 0;
var _globalSearchPreviewTimer = null;
var _globalSearchPreviewToken = 0;
var _globalSearchPreviewController = null;

function hideGlobalSearchPreview() {
  var preview = document.getElementById('globalSearchPreview');
  var input = document.getElementById('globalPageSearch');
  if (preview) preview.classList.remove('show');
  if (input) {
    input.setAttribute('aria-expanded', 'false');
    input.removeAttribute('aria-activedescendant');
  }
}

function escapeSearchRegExp(value) {
  return String(value || '').replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
}

function highlightSearchText(value, query) {
  var text = String(value || '');
  var tokens = Array.from(new Set(String(query || '').trim().split(/\s+/).filter(Boolean)))
    .sort(function(a, b) { return b.length - a.length; });
  if (!text || !tokens.length) return escapeHtml(text);
  var matcher;
  try {
    matcher = new RegExp(tokens.map(escapeSearchRegExp).join('|'), 'gi');
  } catch (ignore) {
    return escapeHtml(text);
  }
  var result = '';
  var lastIndex = 0;
  text.replace(matcher, function(match, offset) {
    result += escapeHtml(text.slice(lastIndex, offset));
    result += '<mark class="search-hit">' + escapeHtml(match) + '</mark>';
    lastIndex = offset + match.length;
    return match;
  });
  return result + escapeHtml(text.slice(lastIndex));
}

function renderGlobalSearchPreview(customers, total) {
  var preview = document.getElementById('globalSearchPreview');
  var input = document.getElementById('globalPageSearch');
  if (!preview || !input) return;
  preview.innerHTML = '';
  _globalSearchResults = customers || [];
  _globalSearchTotal = Math.max(Number(total || 0), _globalSearchResults.length);
  _customerSearchResultById = {};
  _globalSearchResults.forEach(function(customer) {
    _customerSearchResultById[Number(customer.id)] = customer;
  });
  _globalSearchActiveIndex = 0;
  if (!_globalSearchResults.length) {
    var empty = document.createElement('div');
    empty.className = 'global-search-empty';
    empty.textContent = '没有找到匹配客户，回车查看完整搜索结果';
    preview.appendChild(empty);
  } else {
    _globalSearchResults.forEach(function(customer, index) {
      var button = document.createElement('button');
      button.type = 'button';
      button.className = 'global-search-result' + (index === 0 ? ' is-active' : '');
      button.id = 'global-search-result-' + customer.id;
      button.setAttribute('role', 'option');
      button.setAttribute('aria-selected', index === 0 ? 'true' : 'false');
      button.setAttribute('aria-posinset', String(index + 1));
      button.setAttribute('aria-setsize', String(_globalSearchTotal || _globalSearchResults.length));
      var company = customer.company || customer.name || '未命名客户';
      var secondary = customer.country || customer.field || '';
      var detail = [customer.field, customer.primary_contact_name, customer.primary_contact_email].filter(Boolean).join(' · ');
      var match = customer.match_context || {};
      var matchText = match.content || match.title || match.contact_name || detail;
      var name = document.createElement('strong');
      name.innerHTML = highlightSearchText(company, input.value);
      var meta = document.createElement('span');
      meta.textContent = secondary;
      var small = document.createElement('small');
      var smallParts = [];
      if (matchText) smallParts.push(highlightSearchText(matchText, input.value));
      if (match.date) smallParts.push(escapeHtml(formatDate(match.date)));
      small.innerHTML = smallParts.join(' · ') || '打开客户详情';
      button.appendChild(name);
      button.appendChild(meta);
      button.appendChild(small);
      button.addEventListener('mouseenter', function() { setGlobalSearchActive(index); });
      button.addEventListener('click', function() { openGlobalSearchResult(customer.id); });
      preview.appendChild(button);
    });
  }
  if (_globalSearchResults.length) {
    var summary = document.createElement('div');
    summary.className = 'global-search-preview-summary';
    summary.textContent = '共 ' + _globalSearchTotal + ' 个客户 · 按回车查看全部';
    preview.appendChild(summary);
  }
  preview.classList.add('show');
  input.setAttribute('aria-expanded', 'true');
  if (_globalSearchResults.length) input.setAttribute('aria-activedescendant', 'global-search-result-' + _globalSearchResults[0].id);
}

function setGlobalSearchActive(index) {
  if (!_globalSearchResults.length) return;
  _globalSearchActiveIndex = (index + _globalSearchResults.length) % _globalSearchResults.length;
  document.querySelectorAll('.global-search-result').forEach(function(item, itemIndex) {
    var active = itemIndex === _globalSearchActiveIndex;
    item.classList.toggle('is-active', active);
    item.setAttribute('aria-selected', active ? 'true' : 'false');
    if (active) {
      var input = document.getElementById('globalPageSearch');
      if (input) input.setAttribute('aria-activedescendant', item.id);
    }
  });
}

async function loadGlobalSearchPreview(query) {
  var token = ++_globalSearchPreviewToken;
  if (_globalSearchPreviewController) _globalSearchPreviewController.abort();
  if (!query) {
    _globalSearchResults = [];
    hideGlobalSearchPreview();
    return;
  }
  _globalSearchPreviewController = typeof AbortController === 'function' ? new AbortController() : null;
  try {
    var params = new URLSearchParams({ search: query, view: 'all', page: 1, per_page: 5, sort: 'updated_at', order: 'desc' });
    var data = await api('/api/customers?' + params.toString(), {
      signal: _globalSearchPreviewController ? _globalSearchPreviewController.signal : undefined
    });
    if (token !== _globalSearchPreviewToken) return;
    renderGlobalSearchPreview((data && data.customers) || [], data && data.total);
  } catch (error) {
    if (token === _globalSearchPreviewToken && (!error || error.name !== 'AbortError')) renderGlobalSearchPreview([]);
  }
}

function scheduleGlobalSearchPreview() {
  var input = document.getElementById('globalPageSearch');
  var query = input ? input.value.trim() : '';
  var form = input && input.closest('.global-page-search');
  if (form) form.classList.toggle('has-query', !!query);
  _globalSearchResults = [];
  _globalSearchTotal = 0;
  _globalSearchActiveIndex = 0;
  clearTimeout(_globalSearchPreviewTimer);
  _globalSearchPreviewTimer = setTimeout(function() { loadGlobalSearchPreview(query); }, 180);
}

function openGlobalSearchResult(customerId) {
  hideGlobalSearchPreview();
  switchPage('customers');
  setTimeout(function() { openEditModal(customerId); }, 0);
}

function openSearchMatchContext(customer) {
  var context = customer && customer.match_context;
  var customerId = customer && Number(customer.id);
  if (!context || !customerId) {
    switchPage('customers');
    setTimeout(function() { openEditModal(customerId); }, 0);
    return;
  }
  if (context.type === 'inbox' && context.action === 'record') {
    openCommunicationConfirm({
      source: context.source || 'search', inboxItemId: context.id, customerId: customerId,
      customerName: customer.company || customer.name || '当前客户',
      contactId: context.contact_id || '', contactName: context.contact_name || '',
      content: context.content || context.title || '', rawContent: context.content || '', followDate: context.date || '',
      direction: context.direction || 'unknown', activityType: context.activity_type || 'follow_up',
      sourceLabel: context.source_label || 'Search 命中', sourceDetail: context.source_url || '',
      subtitle: 'Search 已带入匹配的 Inbox 原文。核对后保存，必要时再安排下一步。'
    });
    return;
  }
  switchPage('customers');
  setTimeout(function() {
    openEditModal(customerId).then(function() {
      if (context.type === 'communication') switchCustomerTab('editTabOutreach');
    });
  }, 0);
}

function submitGlobalPageSearch(event) {
  if (event) event.preventDefault();
  var customerModal = document.getElementById('customerEditModal');
  if (customerModal && customerModal.classList.contains('show') && !customerModalIsDirty('customerEditModal')) {
    closeModal('customerEditModal');
  }
  hideGlobalSearchPreview();
  switchPage('customers');
  setTimeout(function() {
    var input = document.getElementById('globalPageSearch');
    if (input) input.focus();
  }, 0);
}

function initGlobalPageTools() {
  var switcher = document.getElementById('pageActionSwitcher');
  var trigger = switcher && switcher.querySelector('.page-action-trigger');
  var actionMenu = document.getElementById('pageActionMenu');
  var searchInput = document.getElementById('globalPageSearch');
  if (trigger) {
    trigger.addEventListener('click', function() {
      var open = switcher.classList.toggle('is-open');
      trigger.setAttribute('aria-expanded', open ? 'true' : 'false');
    });
  }
  document.addEventListener('click', function(event) {
    if (!switcher || switcher.contains(event.target)) return;
    switcher.classList.remove('is-open');
    if (trigger) trigger.setAttribute('aria-expanded', 'false');
  });
  document.addEventListener('keydown', function(event) {
    if (event.key !== 'Escape' || !switcher) return;
    switcher.classList.remove('is-open');
    if (trigger) {
      trigger.setAttribute('aria-expanded', 'false');
      trigger.blur();
    }
  });
  if (searchInput) {
    searchInput.addEventListener('input', scheduleGlobalSearchPreview);
    searchInput.addEventListener('focus', function() {
      if (searchInput.value.trim()) scheduleGlobalSearchPreview();
    });
    searchInput.addEventListener('blur', function() {
      var form = searchInput.closest('.global-page-search');
      if (form && !searchInput.value.trim()) form.classList.remove('has-query');
    });
    searchInput.addEventListener('keydown', function(event) {
      if (event.key === 'ArrowDown' && _globalSearchResults.length) {
        event.preventDefault();
        setGlobalSearchActive(_globalSearchActiveIndex + 1);
      } else if (event.key === 'ArrowUp' && _globalSearchResults.length) {
        event.preventDefault();
        setGlobalSearchActive(_globalSearchActiveIndex - 1);
      } else if (event.key === 'Enter') {
        event.preventDefault();
        submitGlobalPageSearch();
      } else if (event.key === 'Escape') {
        hideGlobalSearchPreview();
      }
    });
  }
  document.addEventListener('click', function(event) {
    var form = document.querySelector('.global-page-search');
    if (form && !form.contains(event.target)) hideGlobalSearchPreview();
  });
}

function switchPage(page) {
  if (_cancelTodayTaskDrag) _cancelTodayTaskDrag();
  if (currentUser && page === 'overview' && userPreferences && userPreferences.modules && userPreferences.modules.weekly_overview === false) page = 'dashboard';
  var nextPage = page;
  var navigationToken = ++_pageNavigationToken;
  var updatePageState = function() {
    if (navigationToken !== _pageNavigationToken) return;
    currentPage = nextPage;
    document.querySelectorAll('.page-section').forEach(function(s) { s.classList.remove('active'); });
    var nextSection = document.getElementById('page-' + nextPage);
    if (nextSection) nextSection.classList.add('active');
    document.querySelectorAll('.nav-item').forEach(function(n) { n.classList.remove('active'); });
    var activeNav = document.querySelector('.nav-item[data-page="' + nextPage + '"]');
    if (activeNav) activeNav.classList.add('active');
    closeSidebar();
    window.scrollTo({ top: 0, behavior: 'auto' });
  };
  if (nextPage === currentPage) updatePageState();
  else runViewUpdate(updatePageState, 'page');
  switch(nextPage) {
    case 'dashboard': loadDashboard(); break;
    case 'inbox': loadInbox(); break;
    case 'customers': loadCustomers(); break;
    case 'calendar': loadCalendar(); initIcalUrl(); break;
    case 'history': loadHistory(); break;
    case 'logs': loadLogs(); break;
    case 'settings': loadSettings(); break;
    case 'overview': loadOverview(); break;
  }
  // 切换页面后定位筛选器指示器（延迟确保布局完成）
  setTimeout(function() {
    if (nextPage === 'inbox') updateFilterIndicator(document.getElementById('inboxFilters'));
    else if (nextPage === 'customers') updateFilterIndicator(document.querySelector('.customer-view-chips'));
  }, 80);
}

function setSidebarOpen(isOpen) {
  var sidebar = document.getElementById('sidebar');
  var toggle = document.getElementById('sidebarToggle');
  var scrim = document.getElementById('sidebarScrim');
  var main = document.querySelector('.main-content');
  if (!sidebar) return;
  sidebar.classList.toggle('open', isOpen);
  if (scrim) {
    scrim.hidden = !isOpen;
    scrim.classList.toggle('show', isOpen);
  }
  // A drawer is a navigation surface: while it is open the page behind it must
  // not receive clicks or keyboard focus, so it can never be edited by mistake.
  if (main) {
    if (isOpen) main.setAttribute('inert', '');
    else main.removeAttribute('inert');
  }
  if (toggle) {
    toggle.setAttribute('aria-expanded', isOpen ? 'true' : 'false');
    var label = isOpen ? '关闭导航' : '打开导航';
    toggle.setAttribute('aria-label', label);
    toggle.setAttribute('title', label);
  }
}

function closeSidebar() {
  setSidebarOpen(false);
}

function toggleSidebar() {
  var sidebar = document.getElementById('sidebar');
  if (!sidebar) return;
  setSidebarOpen(!sidebar.classList.contains('open'));
}

(function bindSidebarScrim() {
  var scrim = document.getElementById('sidebarScrim');
  if (scrim) scrim.addEventListener('click', closeSidebar);
})();

document.addEventListener('keydown', function(e) {
  if (e.key !== 'Enter' && e.key !== ' ' && e.key !== 'Spacebar') return;
  var item = e.target && e.target.closest ? e.target.closest('.nav-item[role="button"]') : null;
  if (!item) return;
  e.preventDefault();
  item.click();
});

function openGlobalSearch() {
  var input = document.getElementById('globalPageSearch');
  if (input) input.focus();
}

function openTodayPrimaryAction() {
  var selected = document.querySelector('#todayReminders .today-task-row.selected');
  var reminderId = selected ? Number(selected.dataset.reminderId) : Number((dashboardReminders[0] || {}).id || 0);
  var reminder = (dashboardReminders || []).find(function(item) { return Number(item.id) === reminderId; });
  if (reminder) { openReminderCommunicationConfirm(reminder); return; }
  showToast('今天没有需要记录的到期事项', 'info');
}

function updateSidebarIdentity() {
  var name = currentUser && (currentUser.name || currentUser.label || currentUser.id) || 'Trade OS';
  var avatar = document.getElementById('sidebarAvatar');
  var label = document.getElementById('sidebarUserName');
  var role = document.getElementById('sidebarUserRole');
  if (avatar) avatar.textContent = String(name).substring(0, 2).toUpperCase();
  if (label) label.textContent = name;
  if (role) role.textContent = currentUser ? '个人工作区' : '团队工作区';
}

// ========== API Helper ==========
var _pageVersion = '';
var _lastVersionCheck = 0;
var _pendingVersionRefresh = false;
var _connectionState = 'online';
var _connectionRestoreTimer = null;
var _connectionOfflineTimer = null;
var _networkFailureCount = 0;
var _connectionRecoveryPromise = null;
var _connectionHiddenAt = 0;
var _connectionLastViewRefreshAt = 0;
function setConnectionStatus(state, message) {
  var el = document.getElementById('connectionStatus'); var text = document.getElementById('connectionStatusText');
  if (!el || !text) return;
  if (state === 'offline') {
    clearTimeout(_connectionOfflineTimer);
    _connectionOfflineTimer = setTimeout(function() {
      text.textContent = message || '网络中断，正在重连…'; el.hidden = false; el.classList.remove('is-restored');
      clearTimeout(_connectionRestoreTimer);
      _connectionRestoreTimer = setTimeout(function() { el.hidden = true; }, 6000);
    }, 1200);
  }
  else if (state === 'restored') { text.textContent = '连接已恢复'; el.hidden = false; el.classList.add('is-restored'); clearTimeout(_connectionRestoreTimer); _connectionRestoreTimer = setTimeout(function() { el.hidden = true; }, 1800); }
  else { clearTimeout(_connectionOfflineTimer); el.hidden = true; }
  _connectionState = state;
}
window.addEventListener('offline', function() { setConnectionStatus('offline'); });
window.addEventListener('online', function() { verifyConnectionAfterResume(false); });

// ---- 统一操作状态（用户主动写入的全局反馈）----
// 所有用户触发的写操作，无论有没有专属按钮，都必须让用户看到“处理中 → 已保存 /
// 失败 + 重试”。这里只维护全局状态位，复用 connection-status 的视觉与
// aria-live；按钮态仍由 setActionFeedback 负责，成功/失败文案与重试在 finish 时
// 收口。失败时状态位保持可见并由调用方回滚本地 UI，避免前端长期停留在乐观假象。
var _actionStatusPending = {};
var _actionStatusResult = null;
var _actionStatusSeq = 0;
var _actionStatusTimer = null;

function renderActionStatus() {
  var el = document.getElementById('actionStatus');
  var text = document.getElementById('actionStatusText');
  var retry = document.getElementById('actionStatusRetry');
  if (!el || !text || !retry) return;
  el.classList.remove('is-success', 'is-error');
  retry.hidden = true;
  retry.onclick = null;
  var pendingIds = Object.keys(_actionStatusPending);
  if (pendingIds.length) {
    text.textContent = pendingIds.length > 1
      ? '正在保存 ' + pendingIds.length + ' 项…'
      : (_actionStatusPending[pendingIds[0]] || '正在保存…');
    el.hidden = false;
    return;
  }
  if (!_actionStatusResult) { el.hidden = true; return; }
  el.classList.add(_actionStatusResult.state === 'error' ? 'is-error' : 'is-success');
  text.textContent = _actionStatusResult.message || (_actionStatusResult.state === 'error' ? '未保存' : '已保存');
  if (_actionStatusResult.retry) {
    retry.hidden = false;
    retry.onclick = function() {
      var retryAction = _actionStatusResult.retry;
      _actionStatusResult = null;
      renderActionStatus();
      if (typeof retryAction === 'function') retryAction();
    };
  }
  el.hidden = false;
}

// 开始一个用户操作：返回操作 id，供 finishActionStatus 结算。多个操作并行时
// 显示聚合计数，只有最后一个完成后才展示成功。
function beginActionStatus(id, message) {
  if (!id) id = 'action-' + (++_actionStatusSeq);
  _actionStatusPending[id] = message || '正在保存…';
  _actionStatusResult = null;
  if (_actionStatusTimer) { clearTimeout(_actionStatusTimer); _actionStatusTimer = null; }
  renderActionStatus();
  return id;
}

// 结算一个用户操作。state: 'success' | 'error'。失败时保持可见并可选提供重试动作；
// 成功在最后一个待处理操作结束后短暂展示后自动隐藏。
function finishActionStatus(id, state, message, retry) {
  if (id) delete _actionStatusPending[id];
  var stillPending = Object.keys(_actionStatusPending).length > 0;
  if (state === 'error') {
    _actionStatusResult = { state: 'error', message: message || '未保存', retry: retry || null };
  } else if (!stillPending && !(_actionStatusResult && _actionStatusResult.state === 'error')) {
    // 并行操作里只要有一个失败，就不能被后到的成功掩盖。
    _actionStatusResult = { state: 'success', message: message || '已保存', retry: null };
    if (_actionStatusTimer) clearTimeout(_actionStatusTimer);
    _actionStatusTimer = setTimeout(function() {
      _actionStatusResult = null;
      _actionStatusTimer = null;
      renderActionStatus();
    }, 2200);
  }
  renderActionStatus();
}

// 退出登录 / 切换账号时清空，避免把上一个用户的失败状态留在屏幕上。
function resetActionStatus() {
  _actionStatusPending = {};
  _actionStatusResult = null;
  if (_actionStatusTimer) { clearTimeout(_actionStatusTimer); _actionStatusTimer = null; }
  renderActionStatus();
}

function refreshViewAfterConnectionRestore() {
  if (!currentUser || document.hidden || document.querySelector('.modal-overlay.show')) return;
  var now = Date.now();
  if (now - _connectionLastViewRefreshAt < 5000) return;
  _connectionLastViewRefreshAt = now;
  scheduleGlobalSync();
}

function verifyConnectionAfterResume(refreshView) {
  if (document.hidden) return Promise.resolve(false);
  if (_connectionRecoveryPromise) return _connectionRecoveryPromise;
  _connectionRecoveryPromise = (async function() {
    var controller = typeof AbortController === 'function' ? new AbortController() : null;
    var timeout = setTimeout(function() { if (controller) controller.abort(); }, 5000);
    try {
      // A fresh lightweight request verifies the server after a laptop wake;
      // navigator.onLine alone cannot detect a stale Wi-Fi or Tunnel route.
      var response = await fetch('/api/network/ping', {
        credentials: 'include', cache: 'no-store', signal: controller ? controller.signal : undefined
      });
      if (!response.ok) throw new Error('HTTP ' + response.status);
      await response.json();
      var hadInterruptedConnection = _connectionState === 'offline' || _networkFailureCount > 0;
      _networkFailureCount = 0;
      if (hadInterruptedConnection) setConnectionStatus('restored');
      if (refreshView) refreshViewAfterConnectionRestore();
      return true;
    } catch (error) {
      _networkFailureCount = Math.max(1, _networkFailureCount + 1);
      setConnectionStatus('offline', '服务正在恢复，请稍候…');
      return false;
    } finally {
      clearTimeout(timeout);
      _connectionRecoveryPromise = null;
    }
  })();
  return _connectionRecoveryPromise;
}

document.addEventListener('visibilitychange', function() {
  if (document.hidden) {
    _connectionHiddenAt = Date.now();
    return;
  }
  var hiddenFor = _connectionHiddenAt ? Date.now() - _connectionHiddenAt : 0;
  _connectionHiddenAt = 0;
  // A short tab switch does not need a full page refresh. A display/system
  // sleep does, and it is safe because open editing dialogs are left intact.
  verifyConnectionAfterResume(hiddenFor >= 3000);
});

async function checkVersion() {
  var now = Date.now();
  if (now - _lastVersionCheck < 5000) return;
  _lastVersionCheck = now;
  var meta = document.querySelector('meta[name="app-version"]');
  _pageVersion = meta ? meta.content : '';
  try {
    var controller = typeof AbortController === 'function' ? new AbortController() : null;
    var timeout = setTimeout(function() { if (controller) controller.abort(); }, 800);
    var resp = await fetch('/api/version', { credentials: 'include', signal: controller ? controller.signal : undefined });
    clearTimeout(timeout);
    var data = await resp.json();
    if (data.version && _pageVersion && data.version !== _pageVersion) {
      _pendingVersionRefresh = true;
    }
  } catch(e) {}
}

function isTransientApiStatus(status) {
  return [408, 425, 429, 500, 502, 503, 504, 521, 522, 523, 524].indexOf(Number(status)) >= 0;
}

function isApiNetworkError(error) {
  return !!error && ['AbortError'].indexOf(error.name) < 0 &&
    (error.name === 'TypeError' || error.name === 'NetworkError' || error.message === 'Failed to fetch');
}

function waitForApiRetry(delay) {
  return new Promise(function(resolve) { setTimeout(resolve, delay); });
}

// ---- 写入单飞（transport-level dedupe）----
// 所有写请求都经过 api()，这里是防重复提交的统一咽喉：同 method+URL+body 的
// 非 GET 请求在第一个还在途中时，后续调用共享同一个 Promise，不再发出第二个
// 网络请求。这样“双击 / 回车+点击 / 无按钮路径重复触发”在任何现有与未来新增
// 的写入口上都只会产生一次写入；handler 层的 beginWrite/setActionFeedback 仍
// 负责按钮态与提示，不再单独承担正确性。
var _pendingMutations = {};
function api(url, options) {
  options = options || {};
  var method = String(options.method || 'GET').toUpperCase();
  if (method === 'GET') return apiOnce(url, options);
  var mutationKey = method + ' ' + url + ' ' + (typeof options.body === 'string' ? options.body : '');
  if (_pendingMutations[mutationKey]) return _pendingMutations[mutationKey];
  var promise = apiOnce(url, options);
  _pendingMutations[mutationKey] = promise;
  var cleanup = function() { if (_pendingMutations[mutationKey] === promise) delete _pendingMutations[mutationKey]; };
  promise.then(cleanup, cleanup);
  return promise;
}

async function apiOnce(url, options) {
  options = options || {};
  var method = String(options.method || 'GET').toUpperCase();
  var attempts = method === 'GET' ? 3 : 1;
  if (method === 'GET' && options.retryAttempts != null) {
    attempts = Math.max(1, Math.min(4, Math.floor(Number(options.retryAttempts) || 1)));
  }
  var retryDelayMs = Math.max(150, Math.min(2000, Number(options.retryDelayMs) || 450));
  try {
    // 版本检查只在后台进行，不能阻塞当前页面的数据请求。
    checkVersion();
    var fetchOptions = Object.assign({ credentials: 'include', headers: { 'Content-Type': 'application/json' } }, options);
    delete fetchOptions.skipGlobalSync;
    delete fetchOptions.silentError;
    delete fetchOptions.retryAttempts;
    delete fetchOptions.retryDelayMs;
    var resp;
    var result;
    var hadTransientFailure = false;
    for (var attempt = 0; attempt < attempts; attempt++) {
      try {
        resp = await fetch(url, fetchOptions);
        if (resp.status === 401) {
          showToast('登录已过期，请重新登录', 'error');
          showLogin();
          return null;
        }
        if (!resp.ok) {
          var err = await resp.json().catch(function() { return {}; });
          var requestError = new Error(err.error || ('HTTP ' + resp.status + ' ' + url.split('?')[0]));
          requestError.kind = 'http';
          requestError.status = resp.status;
          requestError.url = url;
          requestError.method = method;
          Object.keys(err).forEach(function(key) { requestError[key] = err[key]; });
          if (isTransientApiStatus(resp.status) && attempt + 1 < attempts && !(fetchOptions.signal && fetchOptions.signal.aborted)) {
            hadTransientFailure = true;
            setConnectionStatus('offline', '服务响应异常，正在重试…');
            await waitForApiRetry(retryDelayMs * Math.pow(2, attempt) + Math.floor(Math.random() * 120));
            continue;
          }
          throw requestError;
        }
        try {
          result = await resp.json();
        } catch (parseError) {
          parseError.kind = 'parse';
          parseError.status = resp.status;
          parseError.url = url;
          parseError.method = method;
          if (attempt + 1 < attempts && !(fetchOptions.signal && fetchOptions.signal.aborted)) {
            hadTransientFailure = true;
            setConnectionStatus('offline', '数据传输不完整，正在重试…');
            await waitForApiRetry(retryDelayMs * Math.pow(2, attempt) + Math.floor(Math.random() * 120));
            continue;
          }
          throw parseError;
        }
        break;
      } catch (requestError) {
        if (fetchOptions.signal && fetchOptions.signal.aborted) throw requestError;
        if (!isApiNetworkError(requestError) || attempt + 1 >= attempts) {
          if (isApiNetworkError(requestError)) requestError.kind = 'network';
          throw requestError;
        }
        hadTransientFailure = true;
        setConnectionStatus('offline');
        await waitForApiRetry(retryDelayMs * Math.pow(2, attempt) + Math.floor(Math.random() * 120));
      }
    }
    if (result === undefined) throw new Error('服务没有返回有效数据');
    var hadNetworkFailure = _networkFailureCount > 0;
    _networkFailureCount = 0;
    if (method !== 'GET' && url.indexOf('/api/auth/') !== 0) {
      _lastMutationAt = Date.now();
      if (!options.skipGlobalSync) scheduleGlobalSync();
    }
    if ((hadNetworkFailure || hadTransientFailure) && _connectionState === 'offline') setConnectionStatus('restored');
    return result;
  } catch (e) {
    if (isApiNetworkError(e)) {
      _networkFailureCount++;
      if (!navigator.onLine || _networkFailureCount >= 2) setConnectionStatus('offline', '网络中断，正在重连…');
    } else if (e && e.name === 'AbortError') {
      // 请求被主动取消（如搜索输入时替换旧预览请求），是正常流程，不视为失败。
    } else {
      if (!options.silentError) showToast('请求失败: ' + e.message, 'error');
    }
    throw e;
  }
}

// ========== Badge Helpers ==========
function levelBadge(level) {
  var cls = { 'A': 'badge-level-a', 'B': 'badge-level-b', 'C': 'badge-level-c', 'C+': 'badge-level-cp', 'D': 'badge-level-d' };
  return '<span class="badge ' + (cls[level] || 'badge-level-c') + '">' + (level || '-') + '</span>';
}
function statusBadge(status) {
  var map = {
    '未建联': 'badge-status-pending', '已建联': 'badge-status-active',
    '跟进中': 'badge-status-following', '成交': 'badge-status-done', '流失': 'badge-status-lost',
    'Following': 'badge-status-following', 'Closed': 'badge-status-done', 'Lost': 'badge-status-lost',
    'pending': 'badge-status-pending', 'replied': 'badge-status-active',
    'bounced': 'badge-status-lost', 'no_reply': 'badge-status-following'
  };
  return '<span class="badge ' + (map[status] || 'badge-status-pending') + '">' + (status || '-') + '</span>';
}
function formatDate(d) { return d ? d.substring(0, 10) : '-'; }
function isOverdue(d) { return d ? d < localDateString() : false; }

// ========== DASHBOARD ==========
function localDateString(date) {
  var d = date || new Date();
  return d.getFullYear() + '-' + String(d.getMonth() + 1).padStart(2, '0') + '-' + String(d.getDate()).padStart(2, '0');
}

var _globalSyncTimer = null;
var _inboxAutoRefreshTimer = null;
var _lastMutationAt = 0;
var _pageDataLoadedAt = 0;
function scheduleGlobalSync() {
  clearTimeout(_globalSyncTimer);
  _globalSyncTimer = setTimeout(function() {
    refreshInboxBadge();
    // A mutation handler that already reloaded the active page makes this
    // refetch redundant; only resync when the visible page predates the change.
    if (_pageDataLoadedAt >= _lastMutationAt) return;
    if (currentPage === 'inbox') loadInbox();
    else if (currentPage === 'dashboard') loadDashboard();
    else if (currentPage === 'customers') loadCustomers({ preservePosition: true });
    else if (currentPage === 'overview') loadOverview();
    else if (currentPage === 'calendar') { loadCalendar(); initIcalUrl(); }
  }, 180);
}

function startInboxAutoRefresh() {
  if (_inboxAutoRefreshTimer) return;
  _inboxAutoRefreshTimer = setInterval(function() {
    if (!currentUser) return;
    // Keep the count current without rebuilding the visible list or interrupting edits.
    refreshInboxBadge();
  }, 60000);
}

function stopInboxAutoRefresh() {
  if (!_inboxAutoRefreshTimer) return;
  clearInterval(_inboxAutoRefreshTimer);
  _inboxAutoRefreshTimer = null;
}

document.addEventListener('visibilitychange', function() {
  if (document.hidden) stopInboxAutoRefresh();
  else if (currentUser) {
    startInboxAutoRefresh();
    refreshInboxBadge();
  }
});

// ========== INBOX ==========
var _inboxLoadToken = 0;
var _dashboardLoadToken = 0;
var _listPendingTimers = {};

function setListPending(element, pending) {
  if (!element || !element.id) return;
  clearTimeout(_listPendingTimers[element.id]);
  if (!pending) {
    element.classList.remove('is-refreshing');
    return;
  }
  _listPendingTimers[element.id] = setTimeout(function() {
    element.classList.add('is-refreshing');
  }, 180);
}

async function loadInbox() {
  _pageDataLoadedAt = Date.now();
  var token = ++_inboxLoadToken;
  var list = document.getElementById('inboxList');
  setListPending(list, true);
  try {
    if (new URLSearchParams(window.location.search).get('motion_state') === 'inbox_error') throw new Error('QA simulated Inbox failure');
    var data = await api('/api/inbox');
    if (token !== _inboxLoadToken) return;
    inboxItems = data.items || [];
    inboxQuestions = data.questions || [];
    _inboxQuestionsContractLoaded = Array.isArray(data.questions);
    _captureMatches = {};
    // 服务端问题模型已带归属建议，直接种入行内一键归属，无需二次请求。
    inboxQuestions.forEach(function(question) {
      var suggested = question.suggested_customer;
      if (suggested && suggested.customer_id && question.primary_item_id) {
        _captureMatches[Number(question.primary_item_id)] = {
          item_id: question.primary_item_id,
          customer_id: suggested.customer_id,
          company: suggested.company || '',
          reason: suggested.reason || '',
        };
      }
    });
    renderInbox(data.counts || {});
  } catch (e) {
    if (token === _inboxLoadToken && list) {
      list.innerHTML = '<div class="empty-state list-error-state"><p>Inbox 暂时无法加载</p><button class="btn btn-sm" type="button" onclick="loadInbox()">重新加载</button></div>';
    }
  } finally {
    if (token === _inboxLoadToken) {
      setListPending(list, false);
      setTimeout(function() { updateFilterIndicator(document.getElementById('inboxFilters')); }, 60);
    }
  }
}

// 待归属沟通的行内快速处理：服务端问题模型已给出归属建议，这里只缓存建议。
var _captureMatches = {};

function truncateCaptureSummary(text, limit) {
  var value = String(text || '').replace(/\s+/g, ' ').trim();
  if (value.length <= limit) return value;
  return value.substring(0, limit) + '…';
}

// 无用信号（如自动通知邮件）直接删除：不写任何客户记录，从 Inbox 移除。
async function deleteInboxCapture(itemId) {
  var item = inboxItems.find(function(candidate) { return Number(candidate.id) === Number(itemId); });
  if (!isInboxCommunicationCapture(item)) { showToast('这条待归属沟通已不存在，请刷新 Inbox', 'warning'); loadInbox(); return; }
  var sender = item.capture_identity || item.capture_sender || item.customer_company || item.title || '这条沟通';
  var confirmed = await showAppConfirm({
    title: '删除待归属沟通',
    message: '来自 ' + sender + ' 的内容不会写入任何客户记录，直接从 Inbox 移除。',
    submitLabel: '删除'
  });
  if (!confirmed) return;
  try {
    await api('/api/inbox/archive', {
      method: 'POST',
      body: JSON.stringify({ dedupe_key: item.dedupe_key, customer_id: item.customer_id || null, item_type: item.item_type })
    });
    _captureMatches = {};
    inboxItems = inboxItems.filter(function(entry) { return Number(entry.id) !== Number(itemId); });
    renderInbox();
    loadInbox();
    showToast('已删除，不会写入任何记录', 'success');
  } catch (e) {
    loadInbox();
  }
}

async function quickConfirmCapture(itemId) {
  var item = inboxItems.find(function(candidate) { return Number(candidate.id) === Number(itemId); });
  if (!isInboxCommunicationCapture(item)) { showToast('这条待归属沟通已不存在，请刷新 Inbox', 'warning'); loadInbox(); return; }
  var match = _captureMatches[Number(itemId)];
  if (!match || !match.customer_id) return;
  var customerId = match.customer_id;
  var followDate = item.capture_date || localDateString();
  var remaining = inboxItems.filter(function(entry) { return isInboxCommunicationCapture(entry); }).length - 1;
  try {
    await api('/api/customers/' + customerId + '/follow_history', {
      method: 'POST',
      body: JSON.stringify({
        activity_content: item.capture_content || '', activity_result: '',
        activity_type: item.capture_activity_type || 'follow_up',
        direction: item.capture_direction || 'unknown',
        follow_date: followDate, source: item.item_type === 'gmail_capture' ? 'gmail' : 'browser_extension',
        inbox_item_id: item.id, contact_id: null
      })
    });
    _captureMatches = {};
    inboxItems = inboxItems.filter(function(entry) { return Number(entry.id) !== Number(itemId); });
    renderInbox();
    loadInbox();
    showToast(remaining > 0 ? '已归属 ' + (match.company || '客户') + '，待归属沟通还剩 ' + remaining + ' 条' : '已归属 ' + (match.company || '客户'), 'success');
  } catch (e) {
    loadInbox();
  }
}

async function refreshInboxManually() {
  await loadInbox();
  showToast('Inbox 已更新', 'success');
}

// 统一身份自动归属：只处理有唯一、无冲突确定性证据的待归属沟通。
// 返回的每条都带 undo_token，撤销即恢复时间线与 Inbox 条目。
async function autoAttributeInbox() {
  var button = document.querySelector('.inbox-auto-attribute');
  if (button) button.disabled = true;
  try {
    var data = await api('/api/inbox/auto-attribute', { method: 'POST', body: JSON.stringify({}) });
    var resolved = data.resolved || [];
    var held = data.held || [];
    await loadInbox();
    if (!resolved.length) {
      showToast(held.length ? '没有可自动确定的归属，仍需人工判断' : '没有待归属沟通', 'info');
      return;
    }
    var tokens = resolved.map(function(entry) { return entry.undo_token; }).filter(Boolean);
    showToastAction('已自动归属 ' + resolved.length + ' 条，Inbox 只剩需人工判断的项目', 'success', '撤销', async function() {
      for (var i = tokens.length - 1; i >= 0; i--) {
        try { await api('/api/undo/' + encodeURIComponent(tokens[i]), { method: 'POST', body: JSON.stringify({}) }); } catch (e) {}
      }
      showToast('已撤销自动归属', 'success');
      loadInbox();
    });
  } catch (e) {
    if (button) button.disabled = false;
  }
}

async function refreshInboxBadge() {
  try {
    var data = await api('/api/inbox/counts');
    var navCount = document.getElementById('inboxNavCount');
    if (navCount) navCount.textContent = (data && data.all) || '';
  } catch (e) {}
}

// 更新筛选器滑动指示器位置（陶土色胶囊跟随 active 按钮）
function updateFilterIndicator(container) {
  if (!container) return;
  var active = container.querySelector('.inbox-filter.active, .customer-view-chip.active');
  if (!active) { container.classList.remove('has-indicator'); return; }
  var containerRect = container.getBoundingClientRect();
  var activeRect = active.getBoundingClientRect();
  var left = activeRect.left - containerRect.left - 5; // 减去容器 padding
  var width = activeRect.width;
  container.classList.add('has-indicator');
  container.style.setProperty('--indicator-x', left + 'px');
  container.style.setProperty('--indicator-w', width + 'px');
}

function setInboxFilter(filter) {
  inboxFilter = filter;
  runViewUpdate(function() {
    document.querySelectorAll('.inbox-filter').forEach(function(button) {
      button.classList.toggle('active', button.dataset.inboxFilter === filter);
    });
    updateFilterIndicator(document.getElementById('inboxFilters'));
    renderInbox();
  }, 'list');
}

// 按待办动作/情况分类，不按客户个体平铺。
function isInboxCommunicationCapture(item) {
  return !!item && (item.item_type === 'browser_capture' || item.item_type === 'gmail_capture');
}

function inboxQuestionTypeLabel(question) {
  if (!question) return 'Inbox';
  if (question.source_type === 'gmail') return 'Gmail';
  if (question.source_type === 'browser') return '浏览器采集';
  if (question.source_type === 'sela') return 'Sela';
  return 'Inbox';
}

var INBOX_QUESTION_FILTER_LABELS = {
  all: '全部',
  identity: '待归属',
  reply: '客户回复',
  approval: '待批准',
  identity_review: '身份待确认',
};
var INBOX_QUESTION_FILTER_ORDER = ['all', 'identity', 'reply', 'approval', 'identity_review'];

var _inboxExpanded = new Set();
var inboxState = { questions: [], counts: {}, activeFilter: 'all', expandedQuestionId: '', draftResponses: {}, pendingQuestionId: '', uploadStates: {}, analysisStates: {}, inlineErrors: {}, responseAttempts: {}, analysisResolved: false };

function toggleInboxItem(key) {
  if (_inboxExpanded.has(key)) _inboxExpanded.delete(key);
  else _inboxExpanded.add(key);
  renderInbox();
}

function renderInbox(counts) {
  if (_inboxQuestionsContractLoaded) return renderInboxQuestionWorkspace(counts || {});
  counts = counts || {};
  var navCount = document.getElementById('inboxNavCount');
  if (navCount) navCount.textContent = counts.all || inboxQuestions.length || '';
  var overview = document.getElementById('inboxOverview');
  if (overview) {
    var identityCount = inboxQuestions.filter(function(question) { return question.kind === 'identity'; }).length;
    overview.innerHTML = '<strong>' + (counts.all || inboxQuestions.length || 0) + '</strong><span>个问题需要判断</span>' +
      (identityCount ? '<button class="btn btn-sm inbox-auto-attribute" type="button" onclick="autoAttributeInbox()">自动归属可确定项</button>' : '') +
      '<p>系统能自行处理的噪声、退信、技术冲突和重复证据不会出现在这里；这里只留下必须由你决定的问题。</p>';
  }
  var filtersEl = document.getElementById('inboxFilters');
  if (filtersEl) {
    var kinds = INBOX_QUESTION_FILTER_ORDER.filter(function(key) {
      return key === 'all' || (counts[key] || 0) > 0;
    });
    filtersEl.innerHTML = kinds.map(function(key) {
      var countText = key === 'all' ? inboxQuestions.length : (counts[key] || 0);
      return '<button class="inbox-filter' + (key === inboxFilter ? ' active' : '') + '" data-inbox-filter="' + key +
        '" onclick="setInboxFilter(\'' + key + '\')">' + INBOX_QUESTION_FILTER_LABELS[key] +
        (countText ? '<span class="inbox-filter-count">' + countText + '</span>' : '') + '</button>';
    }).join('');
    updateFilterIndicator(filtersEl);
  }
  var list = document.getElementById('inboxList');
  if (!list) return;
  var questions = inboxQuestions.filter(function(question) {
    return inboxFilter === 'all' || question.kind === inboxFilter;
  });
  if (!questions.length) {
    list.innerHTML = '<div class="inbox-empty"><strong>' +
      (inboxQuestions.length ? '这个分类已清空' : 'Inbox 已清空') +
      '</strong><span>当前没有需要你作出判断的问题。新的客户回复或无法自动确定的归属会出现在这里。</span></div>';
    return;
  }
  list.innerHTML = questions.map(renderInboxQuestionHtml).join('');
}

function inboxQuestionFilter(kind) {
  return ({ identity: '确认归属', reply: '处理回复', fact_request: '补充资料', investigation_request: '提交调查', approval: '批准动作', identity_review: '确认主体' })[kind] || '补充资料';
}
function renderInboxQuestionWorkspace(counts) {
  inboxState.questions = (inboxQuestions || []).map(function(q) { if (!q.id) q.id = String(q.primary_item_id || q.key); return q; }); inboxState.counts = counts || inboxState.counts;
  var nav = document.getElementById('inboxNavCount'); if (nav) nav.textContent = counts.all || inboxState.questions.length || '';
  var overview = document.getElementById('inboxOverview');
  if (overview) overview.innerHTML = '<strong>' + (counts.all || inboxState.questions.length) + '</strong><span>个需要你判断的问题</span><p>新证据会先由系统自动匹配、去重、分析和归类；只有无法安全继续的事项才会出现在这里。</p>';
  var kinds = ['all'].concat(Array.from(new Set(inboxState.questions.map(function(q) { return q.kind; }))));
  var filters = document.getElementById('inboxFilters');
  if (filters) filters.innerHTML = kinds.map(function(kind) { var n = kind === 'all' ? inboxState.questions.length : inboxState.questions.filter(function(q) { return q.kind === kind; }).length; return '<button class="inbox-filter' + (inboxState.activeFilter === kind ? ' active' : '') + '" data-inbox-filter="' + kind + '" onclick="setInboxQuestionFilter(\'' + kind + '\')">' + (kind === 'all' ? '全部' : inboxQuestionFilter(kind)) + '<span class="inbox-filter-count">' + n + '</span></button>'; }).join('');
  var list = document.getElementById('inboxList'); if (!list) return;
  var questions = inboxState.questions.filter(function(q) { return inboxState.activeFilter === 'all' || q.kind === inboxState.activeFilter; });
  if (!questions.length) { list.innerHTML = '<div class="inbox-empty"><strong>当前没有需要你判断的问题</strong><span>新证据会先由系统自动匹配、去重、分析和归类；只有无法安全继续的事项才会出现在这里。</span></div>'; return; }
  list.innerHTML = '<div class="inbox-workspace">' + questions.map(renderInboxQuestionCard).join('') + '</div>';
  hydrateInboxPickers();
}
function setInboxQuestionFilter(kind) { inboxState.activeFilter = kind; renderInboxQuestionWorkspace(inboxState.counts); }
function renderInboxQuestionCard(q) {
  var open = inboxState.expandedQuestionId === q.id, subject = (q.subject && q.subject.company) || '待确认主体';
  if (!open) return '<article class="inbox-question-row"><button class="inbox-question-open" onclick="openInboxQuestion(\'' + escapeHtml(q.id) + '\')"><strong>' + escapeHtml(q.headline || q.question) + '</strong><span>' + escapeHtml(subject) + ' · ' + escapeHtml(q.why_human || q.why || '') + '</span><small>' + (q.evidence_count || 0) + ' 条证据 · ' + escapeHtml(formatDate(q.updated_at || q.created_at)) + ' · 查看详情</small></button></article>';
  var evidence = (q.evidence || []).map(function(e, i) { return inboxEvidenceHtml(e, i); }).join('');
  var draft = inboxState.draftResponses[q.id] || {}, upload = inboxState.uploadStates[q.id] || {};
  var fields = ((q.response_schema || {}).fields || []).map(function(f) { return inboxResponseFieldHtml(q, f, draft); }).join('');
  var analyses = (inboxState.analysisStates[q.id] || []).map(function(a) { var citations = (a.citations || []).map(function(c) { return '<li>' + escapeHtml(c.source || '') + ' ' + escapeHtml(String(c.row_or_page || '')) + '：' + escapeHtml(c.excerpt || '') + '</li>'; }).join(''); return '<div class="inbox-analysis-result" data-analysis-status="' + escapeHtml(a.status || '') + '"><b>分析结论：' + escapeHtml(a.status || '') + '</b>' + (a.error ? '<p>' + escapeHtml(a.error) + '</p>' : '') + (citations ? '<ol>' + citations + '</ol>' : '') + '</div>'; }).join('');
  var attachment = q.response_schema && q.response_schema.attachments && q.response_schema.attachments.allowed && q.subject && q.subject.customer_id ? '<label>补充证据<input type="file" multiple accept=".xlsx,.xls,.csv,.pdf,.png,.jpg,.jpeg" onchange="uploadInboxEvidence(\'' + escapeHtml(q.id) + '\',' + Number(q.subject.customer_id) + ',this)"' + (upload.pending ? ' disabled' : '') + '></label><p class="inbox-upload-status" aria-live="polite">' + escapeHtml(upload.message || '') + (upload.error ? ' <button type="button" class="text-action" onclick="retryInboxEvidence(\'' + escapeHtml(q.id) + '\',' + Number(q.subject.customer_id) + ')">重试</button>' : '') + '</p>' + analyses : '';
  var transactional = q.kind === 'identity' || q.kind === 'reply';
  var optionsHtml = (q.options || []).map(function(option) {
    var cls = 'btn btn-sm' + (option.style === 'primary' ? ' btn-primary' : (option.style === 'text' ? ' text-action' : ''));
    return '<button class="' + cls + '" onclick="runInboxOption(' + Number(q.primary_item_id) + ',\'' + escapeHtml(option.key) + '\')">' + escapeHtml(option.label) + '</button>';
  }).join('');
  var decision = transactional
    ? '<p class="inbox-field-help">选择下面一项后，系统会打开确认窗口，并把结果写入客户记录。</p><div class="inbox-options">' + optionsHtml + '</div>'
    : fields + attachment + '<p class="inbox-effects"><b>提交后：</b>' + escapeHtml((q.completion_effects || []).join(' ')) + '<br><b>不会：</b>' + escapeHtml((q.will_not_do || []).join(' ')) + '</p><p class="inbox-inline-error" aria-live="polite"></p><button class="btn btn-primary" onclick="submitInboxQuestion(\'' + escapeHtml(q.id) + '\')">提交回答</button>';
  return '<article class="inbox-question-active" id="inbox-question-' + escapeHtml(q.id) + '"><section class="inbox-question-queue"><button class="text-action" onclick="closeInboxQuestion()">返回队列</button><h3>' + escapeHtml(q.headline || q.question) + '</h3><p>' + escapeHtml(q.summary || '') + '</p><p><b>为什么需要你：</b>' + escapeHtml(q.why_human || q.why || '') + '</p></section><section class="inbox-question-evidence"><h4>相关证据 <small>按业务相关度排序</small></h4><ol>' + evidence + '</ol></section><section class="inbox-question-decision"><h4>请你提供</h4>' + decision + '</section></article>';
}
function inboxEvidenceHtml(e, index) {
  var ordinal = '<b>' + String(index + 1).padStart(2, '0') + '</b>';
  var source = '<small>' + escapeHtml(e.source_label || '来源') + '</small>';
  var s = e.structured || null;
  if (s) {
    var tags = [s.company, s.kind, s.severity].filter(function(v) { return v; }).map(function(v) {
      return '<span class="inbox-evidence-tag">' + escapeHtml(v) + '</span>';
    }).join('');
    var proposal = s.proposal ? '<p class="inbox-evidence-proposal"><span>建议</span>' + escapeHtml(s.proposal) + '</p>' : '';
    var fields = (s.fields || []).map(function(f) {
      return '<div class="inbox-evidence-field"><span>' + escapeHtml(f.label) + '</span><strong>' + escapeHtml(f.value) + '</strong></div>';
    }).join('');
    var context = s.context && !fields ? '<p class="inbox-evidence-context">' + escapeHtml(s.context) + '</p>' : '';
    return '<li>' + ordinal + '<div class="inbox-evidence-body">' +
      (tags ? '<div class="inbox-evidence-tags">' + tags + '</div>' : '') + proposal +
      (fields ? '<div class="inbox-evidence-fields">' + fields + '</div>' : '') + context +
      '</div>' + source + '</li>';
  }
  return '<li>' + ordinal + '<span>' + escapeHtml(e.detail || '原始证据') + '</span>' + source + '</li>';
}

function inboxResponseFieldHtml(q, f, draft) {
  var current = draft[f.key] || '';
  var required = f.required ? ' <em>*</em>' : '';
  var help = f.help ? '<small class="inbox-field-help">' + escapeHtml(f.help) + '</small>' : '';
  if (f.input_type === 'choice' || f.input_type === 'investigation_conclusion') {
    var buttons = (f.choices || []).map(function(choice) {
      var selected = String(current) === String(choice.value);
      return '<button type="button" class="inbox-choice' + (selected ? ' is-selected' : '') +
        '" data-inbox-choice="' + escapeHtml(choice.value) + '" aria-pressed="' + (selected ? 'true' : 'false') +
        '" onclick="selectInboxChoice(\'' + escapeHtml(q.id) + '\', this)">' + escapeHtml(choice.label) + '</button>';
    }).join('');
    return '<div class="inbox-field-block"><span class="inbox-field-label">' + escapeHtml(f.label) + required + '</span>' +
      '<input type="hidden" data-inbox-field="' + escapeHtml(f.key) + '" value="' + escapeHtml(current) + '">' +
      '<div class="inbox-choice-group" role="group" aria-label="' + escapeHtml(f.label) + '">' + buttons + '</div>' + help + '</div>';
  }
  if (f.input_type === 'customer_picker') {
    var source = (f.validation && f.validation.options_source) || (f.key === 'contact_id' ? 'contacts' : 'customers');
    var customerId = q.subject && q.subject.customer_id ? Number(q.subject.customer_id) : 0;
    var placeholder = source === 'contacts' ? '加载联系人…' : '加载客户…';
    return '<label>' + escapeHtml(f.label) + required +
      '<select data-inbox-field="' + escapeHtml(f.key) + '" data-inbox-picker="' + escapeHtml(source) +
      '" data-customer-id="' + customerId + '" data-inbox-value="' + escapeHtml(current) +
      '" onchange="rememberInboxDraft(\'' + escapeHtml(q.id) + '\',this)"><option value="">' +
      escapeHtml(placeholder) + '</option></select>' + help + '</label>';
  }
  var type = f.input_type === 'textarea' || f.input_type === 'investigation_conclusion' ? 'textarea' : 'input';
  var value = escapeHtml(current);
  var change = ' oninput="rememberInboxDraft(\'' + escapeHtml(q.id) + '\',this)"';
  var control = type === 'textarea'
    ? '<textarea data-inbox-field="' + escapeHtml(f.key) + '" placeholder="' + escapeHtml(f.help || '') + '"' + change + '>' + value + '</textarea>'
    : '<input data-inbox-field="' + escapeHtml(f.key) + '" type="' + (f.input_type === 'email' ? 'email' : 'text') + '" value="' + value + '" placeholder="' + escapeHtml(f.label) + '"' + change + '>';
  return '<label>' + escapeHtml(f.label) + required + control + help + '</label>';
}

function selectInboxChoice(id, button) {
  var group = button.parentNode;
  if (!group) return;
  Array.prototype.forEach.call(group.querySelectorAll('.inbox-choice'), function(btn) {
    btn.classList.remove('is-selected');
    btn.setAttribute('aria-pressed', 'false');
  });
  button.classList.add('is-selected');
  button.setAttribute('aria-pressed', 'true');
  var input = group.parentNode ? group.parentNode.querySelector('input[data-inbox-field]') : null;
  if (input) { input.value = button.getAttribute('data-inbox-choice') || ''; rememberInboxDraft(id, input); }
}

var _inboxPickerCustomers = null;
async function hydrateInboxPickers() {
  var selects = document.querySelectorAll('#inboxList select[data-inbox-picker]');
  for (var i = 0; i < selects.length; i++) {
    var select = selects[i];
    if (select.dataset.hydrated === '1') continue;
    select.dataset.hydrated = '1';
    var current = select.getAttribute('data-inbox-value') || '';
    try {
      if (select.dataset.inboxPicker === 'contacts') {
        var customerId = Number(select.dataset.customerId || 0);
        if (!customerId) { select.innerHTML = '<option value="">该问题暂未关联客户</option>'; continue; }
        var contacts = await api('/api/customers/' + customerId + '/contacts');
        select.innerHTML = '<option value="">选择联系人</option>' + (contacts || []).map(function(c) {
          return '<option value="' + escapeHtml(String(c.id)) + '">' +
            escapeHtml((c.name || '未命名联系人') + (c.email ? ' · ' + c.email : '')) + '</option>';
        }).join('');
      } else {
        if (!_inboxPickerCustomers) _inboxPickerCustomers = await api('/api/customers?view=all&per_page=100&sort=updated_at&order=desc');
        var list = (_inboxPickerCustomers && _inboxPickerCustomers.customers) || [];
        select.innerHTML = '<option value="">选择客户</option>' + list.map(function(c) {
          return '<option value="' + escapeHtml(String(c.id)) + '">' +
            escapeHtml(c.company || c.name || ('客户 #' + c.id)) + (c.country ? ' · ' + c.country : '') + '</option>';
        }).join('');
      }
      if (current) select.value = current;
    } catch (e) {
      select.innerHTML = '<option value="">加载失败，请重试</option>';
      select.dataset.hydrated = '';
    }
  }
}

function rememberInboxDraft(id, input) { var draft = inboxState.draftResponses[id] || (inboxState.draftResponses[id] = {}); draft[input.dataset.inboxField] = input.value; }
function inboxResponseAttempt(id) { var key = 'trosa-inbox-response-attempt-' + id; var saved = Number(sessionStorage.getItem(key) || 0); return Math.max(Number(inboxState.responseAttempts[id] || 0), saved); }
function advanceInboxResponseAttempt(id) { var next = inboxResponseAttempt(id) + 1; inboxState.responseAttempts[id] = next; sessionStorage.setItem('trosa-inbox-response-attempt-' + id, String(next)); }
function openInboxQuestion(id) { inboxState.expandedQuestionId = String(id); renderInboxQuestionWorkspace(inboxState.counts); }
function closeInboxQuestion() { inboxState.expandedQuestionId = ''; renderInboxQuestionWorkspace(inboxState.counts); if (inboxState.analysisResolved) { inboxState.analysisResolved = false; loadInbox(); } }
async function submitInboxQuestion(id) { var q = inboxState.questions.find(function(x) { return String(x.id) === String(id); }), card = document.getElementById('inbox-question-' + id); if (!q || !card || inboxState.pendingQuestionId) return; var answer = inboxState.draftResponses[id] || {}; card.querySelectorAll('[data-inbox-field]').forEach(function(el) { answer[el.dataset.inboxField] = el.value.trim(); }); inboxState.draftResponses[id] = answer; inboxState.pendingQuestionId = id; card.querySelectorAll('button,input,textarea').forEach(function(el) { el.disabled = true; }); try { var result = await api('/api/inbox/questions/' + encodeURIComponent(id) + '/respond', { method: 'POST', body: JSON.stringify({ revision: q.revision, answer: answer, attachment_ids: (inboxState.uploadStates[id] || {}).attachmentIds || [], idempotency_key: 'inbox-' + id + '-' + q.revision + '-' + inboxResponseAttempt(id) }) }); delete inboxState.draftResponses[id]; inboxState.questions = inboxState.questions.filter(function(x) { return String(x.id) !== String(id); }); inboxQuestions = inboxState.questions; inboxState.expandedQuestionId = ''; inboxState.pendingQuestionId = ''; renderInboxQuestionWorkspace({ all: inboxState.questions.length }); refreshInboxBadge(); setTimeout(loadInbox, 250); if (result.undo_token) showToastAction('邮箱已更正；系统将继续准备后续工作。', 'success', '撤销', async function() { await api('/api/undo/' + encodeURIComponent(result.undo_token), { method: 'POST', body: '{}' }); advanceInboxResponseAttempt(id); await loadInbox(); showToast('已恢复旧邮箱和 Inbox 问题。', 'success'); }); else showToast('回答已保存；系统将继续准备后续工作。', 'success'); } catch (e) { inboxState.pendingQuestionId = ''; card.querySelectorAll('button,input,textarea').forEach(function(el) { el.disabled = false; }); var error = card.querySelector('.inbox-inline-error'); if (error) error.textContent = e && e.message ? e.message : '提交失败，草稿已保留。'; } }
async function uploadInboxEvidence(questionId, customerId, input) { var files = Array.from(input.files || []); if (!files.length) return; inboxState.uploadStates[questionId] = { pending: true, files: files, message: '正在上传并分析证据…', attachmentIds: (inboxState.uploadStates[questionId] || {}).attachmentIds || [] }; renderInboxQuestionWorkspace(inboxState.counts); try { var form = new FormData(); files.forEach(function(file) { form.append('files', file); }); form.append('category', 'inbox_evidence'); var response = await fetch('/api/customers/' + customerId + '/files', { method: 'POST', body: form, credentials: 'same-origin' }); var uploaded = await response.json(); if (!response.ok) throw new Error(uploaded.error || '上传失败'); var ids = (uploaded.created || []).map(function(file) { return file.id; }), analyses = []; for (var i = 0; i < ids.length; i++) { var result = await api('/api/inbox/questions/' + questionId + '/attachments/' + ids[i] + '/analyze', { method: 'POST', body: '{}' }); analyses.push(result.analysis || {}); } inboxState.analysisStates[questionId] = analyses; inboxState.uploadStates[questionId] = { attachmentIds: ids, message: '证据已分析并附上可审计引用。' }; renderInboxQuestionWorkspace(inboxState.counts); if (analyses.some(function(a) { return a && (a.status === 'supported' || a.status === 'not_supported'); })) { inboxState.analysisResolved = true; _pageDataLoadedAt = Date.now(); } } catch (e) { inboxState.uploadStates[questionId] = { files: files, error: true, attachmentIds: (inboxState.uploadStates[questionId] || {}).attachmentIds || [], message: (e && e.message) || '上传或分析失败；文件已保留，可重试。' }; renderInboxQuestionWorkspace(inboxState.counts); } }
function retryInboxEvidence(questionId, customerId) { var state = inboxState.uploadStates[questionId] || {}; if (!state.files || !state.files.length) return; uploadInboxEvidence(questionId, customerId, { files: state.files }); }

function inboxQuestionCustomerName(question) {
  var customer = question.customer || null;
  if (customer) return customer.company || customer.name || '已关联客户';
  var suggested = question.suggested_customer || null;
  if (suggested && suggested.company) return suggested.company;
  var evidence = (question.evidence || [])[0] || {};
  return evidence.identity || question.source_label || '待确认';
}

function renderInboxEvidence(question) {
  var evidence = question.evidence || [];
  if (!evidence.length) return '';
  return '<div class="inbox-question-evidence">' + evidence.map(function(entry) {
    var detail = String(entry.detail || '').trim();
    return '<div class="inbox-evidence-item">' +
      '<div class="inbox-evidence-meta">' + escapeHtml(entry.source_label || '') +
        (entry.date ? ' · ' + escapeHtml(formatDate(entry.date)) : '') + '</div>' +
      (detail ? '<p>' + escapeHtml(detail) + '</p>' : '') +
    '</div>';
  }).join('') + '</div>';
}

function renderInboxSelaContext(review) {
  if (!review) return '';
  var rows = [];
  if (review.company) rows.push('<div><span>主体</span><strong>' + escapeHtml(review.company) + '</strong></div>');
  if (review.website) rows.push('<div><span>官网</span><strong>' + escapeHtml(review.website) + '</strong></div>');
  if (review.email) rows.push('<div><span>邮箱</span><strong>' + escapeHtml(review.email) + '</strong></div>');
  return '<div class="inbox-question-sela"><span>' + escapeHtml(review.reason_label || '身份待确认') + '</span>' +
    rows.join('') +
    (review.explanation ? '<p>' + escapeHtml(review.explanation) + '</p>' : '') + '</div>';
}

function renderInboxQuestionHtml(question) {
  var key = question.key;
  var expanded = _inboxExpanded.has(key);
  var questionId = Number(question.primary_item_id);
  var name = inboxQuestionCustomerName(question);
  var customer = question.customer || null;
  var customerId = customer ? Number(customer.id || 0) : 0;
  var nameHtml = customerId
    ? '<button type="button" class="inbox-customer-link" onclick="openInboxCustomer(' + customerId + ')">' + escapeHtml(name) + '</button>'
    : escapeHtml(name);
  var kindChip = '<span class="inbox-item-type-chip inbox-type-' + escapeHtml(question.kind) + '">' +
    escapeHtml(question.kind_label || '待处理') + '</span>';
  var typeChip = '<span class="inbox-item-type-chip">' + escapeHtml(inboxQuestionTypeLabel(question)) + '</span>';
  var metaParts = [];
  if (customer) {
    if (customer.country) metaParts.push(customer.country);
    if (customer.contact_name) metaParts.push(customer.contact_name);
  } else if (question.suggested_customer && question.suggested_customer.company) {
    metaParts.push('建议：' + question.suggested_customer.company);
  }
  var meta = metaParts.length ? '<span class="inbox-item-meta">' + escapeHtml(metaParts.join(' · ')) + '</span>' : '';
  var firstEvidence = (question.evidence || [])[0] || {};
  var identitySpan = (!customer && question.kind === 'identity' && firstEvidence.identity)
    ? '<span class="inbox-item-identity">' + escapeHtml(firstEvidence.identity) + '</span>' : '';
  var options = question.options || [];
  var primaryOption = options.filter(function(option) { return option.style === 'primary'; })[0] || options[0];
  var quickAction = '';
  if (primaryOption && !expanded) {
    quickAction = '<span class="inbox-item-quick-action">' +
      '<button class="btn btn-sm' + (primaryOption.style === 'primary' ? ' btn-primary' : '') +
      '" onclick="runInboxOption(' + questionId + ',\'' + escapeHtml(primaryOption.key) + '\')">' +
      escapeHtml(primaryOption.label) + '</button></span>';
  }
  var detail = '';
  if (expanded) {
    var known = (question.known_facts || []).map(function(fact) {
      return '<li>' + escapeHtml(fact) + '</li>';
    }).join('');
    detail = '<div class="inbox-item-detail">' +
      '<div class="inbox-question-why"><span>为什么需要你</span><p>' + escapeHtml(question.why || '') + '</p></div>' +
      (question.sela_review ? renderInboxSelaContext(question.sela_review) : '') +
      (known ? '<div class="inbox-question-known"><span>系统已知</span><ul>' + known + '</ul></div>' : '') +
      renderInboxEvidence(question) +
      '<div class="inbox-actions">' + options.map(function(option) {
        var cls = 'btn btn-sm' + (option.style === 'primary' ? ' btn-primary' : (option.style === 'text' ? ' text-action' : ''));
        return '<button class="' + cls + '" onclick="runInboxOption(' + questionId + ',\'' + escapeHtml(option.key) + '\')">' +
          escapeHtml(option.label) + '</button>';
      }).join('') + '</div>' +
    '</div>';
  }
  var toggleIcon = expanded ? '▾' : '▸';
  var toggleBtn = '<button class="inbox-toggle" onclick="toggleInboxItem(\'' + escapeHtml(key) + '\')" aria-label="' +
    (expanded ? '收起' : '展开') + '">' + toggleIcon + '</button>';
  return '<article class="inbox-item inbox-question inbox-' + escapeHtml(question.kind) +
    (expanded ? ' inbox-item-expanded' : ' inbox-item-collapsed') + '">' +
    '<div class="inbox-item-row">' +
      '<span class="inbox-item-date">' + escapeHtml(formatDate(question.created_at)) + '</span>' +
      '<div class="inbox-item-main">' +
        '<h3 class="inbox-item-name">' + nameHtml + '</h3>' +
        '<div class="inbox-item-meta-row">' + kindChip + typeChip + meta + identitySpan + '</div>' +
      '</div>' +
      '<span class="inbox-summary-why">' + escapeHtml(question.headline || '') + '</span>' +
      quickAction + toggleBtn +
    '</div>' + detail +
    '</article>';
}

async function runInboxOption(questionId, optionKey) {
  var question = inboxQuestions.find(function(candidate) {
    return Number(candidate.primary_item_id) === Number(questionId);
  });
  if (!question) { showToast('该问题已处理，请刷新 Inbox', 'warning'); loadInbox(); return; }
  var option = (question.options || []).filter(function(candidate) { return candidate.key === optionKey; })[0] || {};
  var action = option.action || optionKey;
  if (action === 'record') {
    if (question.kind === 'identity' && optionKey === 'assign_suggested') return quickConfirmCapture(questionId);
    if (question.kind === 'reply') return recordInboxReply(questionId);
    return recordInboxCapture(questionId);
  }
  if (action === 'archive') return decideInboxQuestion(questionId, 'archive');
  if (action === 'approve' || action === 'skip') return resolveSelaAgentRequest(questionId, action === 'skip' ? 'skip' : 'approve');
  if (action === 'identity_same') return decideInboxQuestion(questionId, 'same');
  if (action === 'identity_different') return decideInboxQuestion(questionId, 'different');
  return decideInboxQuestion(questionId, action);
}

async function decideInboxQuestion(questionId, decision) {
  try {
    await api('/api/inbox/' + Number(questionId) + '/decide', {
      method: 'POST',
      body: JSON.stringify({ decision: decision })
    });
    showToast(decision === 'archive' ? '已确认不需要处理' : '已记录你的判断', 'success');
    loadInbox();
  } catch (e) {}
}


function parseSelaIdentityReview(item) {
  if (!item || item.item_type !== 'sela_identity_review') return null;
  var raw = String(item.content || '').trim();
  if (!raw) return null;
  try {
    var parsed = JSON.parse(raw);
    return parsed && typeof parsed === 'object' ? parsed : null;
  } catch (e) {
    return null;
  }
}

function selaIdentityReasonLabel(reason) {
  var labels = {
    CUSTOMER_ALREADY_LINKED: '可能已关联现有客户',
    CUSTOMER_ALREADY_HAS_SELA_PROSPECT: '已有 Sela 线索',
    TROSA_REVISION_CONFLICT: '客户资料或来源有冲突',
    SOURCE_IDENTITY_CONFLICT: '来源身份与客户资料不一致',
    EXTERNAL_IDENTITY_CONFLICT: '外部身份发生冲突',
    MULTIPLE_TROSA_MATCHES: '匹配到多个客户',
    MULTIPLE_EXTERNAL_LINKS: '匹配到多个外部线索',
  };
  return labels[String(reason || '').trim()] || '需要确认客户身份';
}

function selaIdentityQualificationLabel(status) {
  var labels = {
    READY_TO_CONTACT: '资料初步通过',
    'RESEARCH MORE': '需要补充核验',
  };
  return labels[String(status || '').trim()] || '尚未判断';
}

function selaIdentityConfidenceLabel(confidence) {
  var labels = { HIGH: '高', MEDIUM: '中', LOW: '低' };
  return labels[String(confidence || '').trim().toUpperCase()] || '未提供';
}

function selaIdentityMatchLabel(matchType) {
  var labels = { DOMAIN: '官网域名一致', NAME: '名称相似', NAME_VARIANT: '名称变体' };
  return labels[String(matchType || '').trim().toUpperCase()] || '存在匹配线索';
}

function renderSelaIdentityReview(review, item) {
  var research = review.research || {};
  var agentState = research.agent_state || {};
  var match = agentState.exclusion_match || {};
  var reason = selaIdentityReasonLabel(review.reason);
  var qualification = selaIdentityQualificationLabel(research.qualification_status);
  var confidence = selaIdentityConfidenceLabel(research.confidence);
  var hygiene = String(research.site_hygiene || '').toUpperCase();
  var hygieneLabel = hygiene === 'CONCERN' ? '官网或域名需复核' : hygiene === 'CLEAN' ? '官网资料正常' : '';
  var explanation = research.research_reason || research.reason || research.qualification_reason || 'Sela 暂停了自动处理，等待人工确认。';
  var action = '先核对官网、邮箱与现有客户资料；确认无误后再决定是否进入联系准备。';
  if (review.reason === 'CUSTOMER_ALREADY_LINKED') action = '沿用现有客户档案，不要新建重复客户，也不要从这条线索直接发信。';
  if (review.reason === 'CUSTOMER_ALREADY_HAS_SELA_PROSPECT') action = '沿用已有 Sela 线索，不要重复建立或重复联系。';
  if (hygiene === 'CONCERN') action = '先核对官网和域名关系；在身份确认前不要联系。';
  var html = '<div class="sela-review-card">' +
    '<div class="sela-review-lead"><strong>这条记录需要判断，不是待发送邮件。</strong><span>系统没有自动新建客户或发送外联。</span></div>' +
    '<div class="sela-review-statuses"><span class="sela-review-status sela-review-status-reason">' + escapeHtml(reason) + '</span><span class="sela-review-status">' + escapeHtml(qualification) + '</span><span class="sela-review-status">置信度：' + escapeHtml(confidence) + '</span>' + (hygieneLabel ? '<span class="sela-review-status sela-review-status-warning">' + escapeHtml(hygieneLabel) + '</span>' : '') + '</div>' +
    '<div class="sela-review-facts">' +
      '<div><span>官网</span>' + (review.website ? '<a href="' + escapeHtml(review.website) + '" target="_blank" rel="noopener">' + escapeHtml(String(review.website).replace(/^https?:\/\//i, '').replace(/\/$/, '')) + '</a>' : '<strong>未提供</strong>') + '</div>' +
      '<div><span>邮箱</span><strong>' + escapeHtml(review.email || '未提供') + '</strong></div>' +
      (match.canonical_name ? '<div><span>匹配记录</span><strong>' + escapeHtml(match.canonical_name) + ' · ' + escapeHtml(selaIdentityMatchLabel(match.match_type)) + '</strong></div>' : '') +
    '</div>' +
    '<div class="sela-review-explanation"><span>为什么停在这里</span><p>' + escapeHtml(explanation) + '</p></div>' +
    '<div class="sela-review-next"><span>建议处理</span><p>' + escapeHtml(action) + '</p></div>' +
    '<details class="sela-review-raw"><summary>查看原始来源数据</summary><pre>' + escapeHtml(item.content || '') + '</pre></details>' +
  '</div>';
  return html;
}

async function createInboxTaskFromPanel(button, customerId) {
  var panel = button.closest('.inbox-inline-decision');
  if (!panel) return;
  var inputs = panel.querySelectorAll('input');
  var title = (inputs[0] && inputs[0].value || '').trim();
  var dueDate = inputs[1] && inputs[1].value;
  if (!title || !dueDate) { showToast('请填写具体动作和日期', 'warning'); return; }
  button.disabled = true;
  try {
    var result = await api('/api/customers/' + customerId + '/tasks', { method: 'POST', body: JSON.stringify({ title: title, due_date: dueDate }) });
    showInboxTaskUndoToast(result.id);
    await loadInbox();
  } catch (e) {
    button.disabled = false;
  }
}

function showInboxTaskUndoToast(taskId) {
  var container = document.getElementById('toastContainer');
  var toast = document.createElement('div');
  toast.className = 'toast success toast-with-action';
  toast.innerHTML = uiIcon('check') + '<span>下一步已安排，信号已从 Inbox 移除</span><button type="button">撤销</button>';
  var timer = setTimeout(function() { if (toast.isConnected) toast.remove(); }, 12000);
  toast.querySelector('button').onclick = async function() {
    try {
      toast.querySelector('button').disabled = true;
      await api('/api/reminders/' + taskId, { method: 'DELETE' });
      clearTimeout(timer);
      toast.remove();
      showToast('已撤销下一步安排', 'success');
      await loadInbox();
    } catch (e) {
      toast.querySelector('button').disabled = false;
    }
  };
  container.appendChild(toast);
}

async function archiveInboxItem(key, customerId, itemType) {
  try {
    await api('/api/inbox/archive', { method: 'POST', body: JSON.stringify({ dedupe_key: key, customer_id: customerId || null, item_type: itemType }) });
    showToast('已归档', 'success');
    loadInbox();
  } catch (e) {}
}

async function resolveSelaAgentRequest(itemId, action) {
  var item = inboxItems.find(function(candidate) { return Number(candidate.id) === Number(itemId); });
  if (!item) { showToast('这条 Agent 请求已不存在，请刷新 Inbox', 'warning'); return; }
  var resolution = '';
  if (action === 'skip') {
    resolution = '已跳过，本轮不决策';
  } else {
    var suggestion = item.content || item.title || '';
    resolution = window.prompt('请记录对这条 Agent 请求的处理结果：', suggestion) || '';
    if (!resolution.trim()) return;
  }
  try {
    await api('/api/integrations/sela/needs/' + Number(itemId) + '/resolve', {
      method: 'POST',
      body: JSON.stringify({ action: action || 'edit', resolution: resolution.trim() })
    });
    showToast('已记录到 Inbox 和客户时间线', 'success');
    loadInbox();
  } catch (e) {}
}

async function recordInboxReply(itemId) {
  var item = inboxItems.find(function(candidate) { return Number(candidate.id) === Number(itemId); });
  if (!item || !item.customer_id) { showToast('这条回复缺少客户归属，请先重新关联', 'warning'); return; }
  openCommunicationConfirm({
    source: 'inbox', inboxItemId: item.id, customerId: item.customer_id,
    customerName: item.customer_company || item.customer_name || '当前客户',
    contactId: item.contact_id || '', contactName: item.contact_name || '',
    content: item.content || '', rawContent: item.content || '', followDate: item.follow_date || '',
    direction: item.direction || 'inbound', activityType: item.activity_type || 'customer_reply',
    sourceLabel: item.source_label || 'Inbox 客户回复',
    autoAnalyze: true,
    subtitle: 'AI 已整理摘要，请核对后保存为客户事实；原始回复会一并保留。'
  });
}

function recordInboxCapture(itemId) {
  var item = inboxItems.find(function(candidate) { return Number(candidate.id) === Number(itemId); });
  if (!isInboxCommunicationCapture(item)) { showToast('这条待归属沟通已不存在，请刷新 Inbox', 'warning'); return; }
  var remaining = inboxItems.filter(function(entry) { return isInboxCommunicationCapture(entry); }).length;
  openCommunicationConfirm({
    source: item.item_type === 'gmail_capture' ? 'gmail' : 'browser_extension', inboxItemId: item.id,
    // A capture can remain open after its identity was reliably resolved.
    // Keep that confirmed context; only genuinely unassigned captures need the picker.
    customerId: item.customer_id || '', customerName: item.customer_company || item.customer_name || '',
    contactId: item.contact_id || '', contactName: item.contact_name || '',
    content: item.capture_content || '', rawContent: item.capture_content || '', followDate: item.capture_date || '',
    direction: item.capture_direction || 'unknown', activityType: item.capture_activity_type || 'follow_up',
    sourceLabel: item.capture_platform || item.capture_channel || (item.item_type === 'gmail_capture' ? 'Gmail' : '浏览器采集'),
    sourceDetail: item.capture_source_url || item.capture_identity || '',
    autoAnalyze: true, autoPipeline: true, queueSize: remaining,
    subtitle: 'AI 已整理摘要，请核对归属与内容；原始记录会一并保留。'
  });
}

async function openInboxCustomer(customerId) {
  await openEditModal(customerId);
}

var _communicationConfirmContext = null;
var _agentTaskProposalId = null;

async function openAgentProposalConfirmation(proposalId) {
  var response = await api('/api/agent/proposals/' + proposalId);
  var proposal = response.proposal || {};
  var payload = proposal.payload || {};
  var action = proposal.proposal_action || (proposal.proposal_type === 'task' ? 'create_task' : 'record_communication');
  if (action === 'create_task') {
    await openEditModal(proposal.customer_id);
    openCustomerTaskModal('create');
    _agentTaskProposalId = proposal.id;
    document.getElementById('customerTaskTitle').value = payload.title || '';
    document.getElementById('customerTaskDate').value = payload.due_date || '';
    return;
  }
  openCommunicationConfirm({
    agentProposalId: proposal.id, agentProposalAction: action, agentProposalPayload: payload,
    customerId: proposal.customer_id, content: payload.content || payload.activity_content || '',
    followDate: payload.follow_date || localDateString(), direction: payload.direction || 'unknown',
    activityType: payload.activity_type || 'follow_up', source: payload.source || 'agent_gateway',
    inboxItemId: payload.inbox_item_id || '', reminderId: action === 'complete_task' ? payload.task_id : '',
    sourceLabel: proposal.source_reference || 'Agent 提议', subtitle: '你可以编辑后再确认；确认后才会写入 CRM。'
  });
}

async function openCommunicationConfirm(options) {
  var context = Object.assign({ source: 'manual', direction: 'unknown', activityType: 'follow_up' }, options || {});
  _communicationConfirmContext = context;
  var knownCustomer = document.getElementById('communicationConfirmKnownCustomer');
  var knownCustomerName = document.getElementById('communicationConfirmKnownCustomerName');
  var pickerWrap = document.getElementById('communicationConfirmCustomerPicker');
  var contextPanel = document.getElementById('communicationConfirmContext');
  var contextCustomer = document.getElementById('communicationConfirmContextCustomer');
  var contextContact = document.getElementById('communicationConfirmContextContact');
  var contextSource = document.getElementById('communicationConfirmContextSource');
  var contextDate = document.getElementById('communicationConfirmContextDate');
  var rawDetails = document.getElementById('inboxReplyRawSource');
  var rawText = document.getElementById('inboxReplyRawSourceText');
  var title = document.getElementById('communicationConfirmTitle');
  var kicker = document.getElementById('communicationConfirmKicker');
  var subtitle = document.getElementById('communicationConfirmSubtitle');
  var contentLabel = document.getElementById('communicationConfirmContentLabel');
  if (title) title.textContent = context.source === 'today' ? '完成并记录' : '记录沟通';
  if (kicker) kicker.textContent = context.source === 'today' ? '今日待办' : '沟通确认';
  if (subtitle) subtitle.textContent = context.subtitle || '确认实际发生的事实；需要时再安排明确的下一步。';
  if (contentLabel) contentLabel.innerHTML = '这次发生了什么 <span class="required">*</span>';
  document.getElementById('inboxReplyContent').value = context.content || '';
  _inboxReplyRawContent = context.content || '';
  _inboxReplyAnalysis = null;
  document.getElementById('inboxReplyAnalysis').hidden = true;
  document.getElementById('inboxReplyDate').value = context.followDate || localDateString();
  if (rawDetails && rawText) {
    var hasRaw = !!(context.rawContent && context.rawContent !== context.content);
    rawDetails.hidden = !hasRaw;
    rawText.textContent = context.rawContent || '';
  }
  var hasContext = context.source !== 'manual' || context.inboxItemId || context.contactId || context.sourceLabel || context.followDate;
  if (contextPanel) contextPanel.hidden = !hasContext;
  if (hasContext) {
    if (contextCustomer) contextCustomer.textContent = context.customerName || (context.customerId ? '当前客户' : '待选择');
    if (contextContact) contextContact.textContent = context.contactName || '未关联';
    if (contextSource) contextSource.textContent = [context.sourceLabel || context.source || '手动记录', context.sourceDetail || ''].filter(Boolean).join(' · ');
    if (contextDate) contextDate.textContent = context.followDate ? formatChineseDate(context.followDate) : '未记录';
  }
  if (context.customerId) {
    if (knownCustomer) knownCustomer.hidden = false;
    if (knownCustomerName) knownCustomerName.textContent = context.customerName || '当前客户';
    if (pickerWrap) pickerWrap.hidden = true;
    document.getElementById('inboxReplyCustomer').value = context.customerId;
  } else {
    if (knownCustomer) knownCustomer.hidden = true;
    if (pickerWrap) pickerWrap.hidden = false;
    document.getElementById('inboxReplyCustomer').value = '';
    // 已有规则匹配建议时先用它，避免每次都重新拉全部客户。
    var match = context.inboxItemId ? _captureMatches[Number(context.inboxItemId)] : null;
    if (match && match.customer_id) {
      try {
        var state = _customerPickerRegistry.inboxReplyCustomerPicker;
        if (state && state.customers) {
          var suggested = state.customers.find(function(entry) { return Number(entry.id) === Number(match.customer_id); });
          if (suggested) chooseInboxReplyCandidate(suggested.id);
        }
      } catch (e) {}
    }
    try {
      var data = await api('/api/customers?view=all&sort=updated_at&order=desc');
      initializeCustomerPicker('inboxReplyCustomerPicker', data.customers || []);
      if (match && match.customer_id && !document.getElementById('inboxReplyCustomer').value) {
        var customers2 = (_customerPickerRegistry.inboxReplyCustomerPicker || {}).customers || [];
        var suggested2 = customers2.find(function(entry) { return Number(entry.id) === Number(match.customer_id); });
        if (suggested2) chooseInboxReplyCandidate(suggested2.id);
      }
    } catch (e) { return; }
  }
  openModal('inboxReplyModal');
  setTimeout(function() { document.getElementById('inboxReplyContent').focus(); }, 0);
  if (context.autoAnalyze && context.content) analyzeInboxReply(true, { autoFillSummary: true });
}

function openInboxReplyModal() {
  return openCommunicationConfirm({ source: 'manual', direction: 'inbound', activityType: 'customer_reply' });
}

function clearInboxReplyAnalysis() {
  clearTimeout(_inboxReplyAnalysisTimer);
  _inboxReplyAnalysis = null;
  var panel = document.getElementById('inboxReplyAnalysis');
  if (panel) panel.hidden = true;
}

function extractInboxReplyImage(input) {
  var file = input.files && input.files[0];
  if (!file) return;
  if (file.size > 8 * 1024 * 1024) { showToast('图片请控制在 8MB 以内', 'warning'); input.value = ''; return; }
  var panel = document.getElementById('inboxReplyAnalysis');
  panel.hidden = false;
  panel.innerHTML = '<div class="quick-analysis-loading">正在识别截图文字…</div>';
  var reader = new FileReader();
  reader.onload = async function() {
    try {
      var result = await api('/api/inbox/extract-image', { method: 'POST', body: JSON.stringify({ image: reader.result }) });
      var textarea = document.getElementById('inboxReplyContent');
      textarea.value = result.text || '';
      showToast('截图文字已识别，请核对原文', 'success');
      analyzeInboxReply(false);
    } catch(e) {
      panel.innerHTML = '<div class="quick-analysis-error">截图识别暂不可用：' + escapeHtml(e.message || '请检查视觉模型配置') + '</div>';
    } finally {
      input.value = '';
    }
  };
  reader.readAsDataURL(file);
}

async function analyzeInboxReply(force, options) {
  var autoOptions = options || {};
  clearTimeout(_inboxReplyAnalysisTimer);
  var content = document.getElementById('inboxReplyContent').value.trim();
  if (!content) { if (force) showToast('请先粘贴客户回复', 'warning'); return; }
  var panel = document.getElementById('inboxReplyAnalysis');
  var token = ++_inboxReplyAnalysisToken;
  panel.hidden = false;
  if (!autoOptions.autoFillSummary) panel.innerHTML = '<div class="quick-analysis-loading">AI 正在识别客户和关键信息…</div>';
  else panel.innerHTML = '<div class="quick-analysis-loading">AI 正在整理这次沟通的摘要…</div>';
  try {
    var context = _communicationConfirmContext || {};
    var requestedDirection = ['auto', 'outbound', 'inbound', 'two_way'].indexOf(context.direction) >= 0
      ? context.direction : 'auto';
    var result = await api('/api/inbox/analyze-reply', { method: 'POST', body: JSON.stringify({
      content: content, direction: requestedDirection, customer_id: context.customerId || ''
    }) });
    if (token !== _inboxReplyAnalysisToken) return;
    _inboxReplyAnalysis = result.analysis || null;
    renderInboxReplyAnalysis(result);
    var candidates = result.candidates || [];
    if (candidates.length && candidates[0].score >= 70 && !context.customerId && !document.getElementById('inboxReplyCustomer').value) {
      chooseInboxReplyCandidate(candidates[0].id);
    }
    var analysis = result.analysis || {};
    if (analysis.message_date && /^\d{4}-\d{2}-\d{2}$/.test(analysis.message_date) && !document.getElementById('inboxReplyDate').value) {
      document.getElementById('inboxReplyDate').value = analysis.message_date;
    }
    // 摘要直接进记录框（可编辑），原始文本保留在下方“原始记录”里。
    var summary = String(analysis.summary || '').trim();
    var textarea = document.getElementById('inboxReplyContent');
    if (autoOptions.autoFillSummary && summary && textarea.value.trim() === _inboxReplyRawContent.trim()) {
      textarea.value = summary;
      _inboxReplyRawContent = context.rawContent || _inboxReplyRawContent;
    }
  } catch(e) {
    panel.innerHTML = '<div class="quick-analysis-error">暂时无法使用 AI 分析，仍可手动选择客户并保存原文。</div>';
  }
}

function renderInboxReplyAnalysis(result) {
  var panel = document.getElementById('inboxReplyAnalysis');
  var analysis = result.analysis || {};
  var facts = (analysis.key_facts || []).concat(analysis.needs || []).slice(0, 6);
  var candidates = (_communicationConfirmContext && _communicationConfirmContext.customerId) ? [] : (result.candidates || []);
  var html = '<div class="quick-analysis-head"><strong>AI 整理</strong><span>' + escapeHtml(analysis.intent || '未知') + '</span></div>';
  html += '<p>' + escapeHtml(analysis.summary || '已读取原文') + '</p>';
  if (facts.length) html += '<div class="quick-analysis-facts">' + facts.map(function(f) { return '<span>' + escapeHtml(f) + '</span>'; }).join('') + '</div>';
  if (candidates.length) {
    html += '<div class="quick-analysis-matches"><small>可能对应</small>' + candidates.map(function(c) {
      return '<button type="button" onclick="chooseInboxReplyCandidate(' + c.id + ')"><strong>' + escapeHtml(c.company || c.name) + '</strong><span>' + escapeHtml([c.contact_name, c.country, c.reason].filter(Boolean).join(' · ')) + '</span><em>' + c.score + '%</em></button>';
    }).join('') + '</div>';
  } else html += '<div class="quick-analysis-no-match">未从原文找到明确客户，请手动搜索确认。</div>';
  panel.innerHTML = html;
}

var _communicationAnalyses = {};

function selectedCommunicationDirection(context) {
  var select = document.getElementById(context === 'complete' ? 'completeDirectionOverride' : 'followHistoryDirectionOverride');
  return select ? select.value : 'auto';
}

function communicationDirectionLabel(direction) {
  return { auto: '自动识别', outbound: '我发给客户', inbound: '客户发给我', two_way: '双方沟通', unknown: '方向不明确' }[direction] || '方向不明确';
}

function communicationDirectionClass(direction) {
  return { outbound: 'is-outbound', inbound: 'is-inbound', two_way: 'is-two-way', unknown: 'is-unknown' }[direction] || 'is-unknown';
}

function attentionStateMessage(attention) {
  if (!attention || attention.state === 'planned') return '';
  var labels = {
    waiting_reply: '等待客户回复', no_response: '日常跟进后仍未回复',
    no_near_term_need: '近期无需求', monitoring: '暂时观察',
    no_next_plan: '最近还没有下一步计划', custom: '按实际情况观察',
    not_investing_now: '当前不投入'
  };
  return '系统已判断为“' + (labels[attention.state] || '暂时观察') + '”' +
    (attention.review_date ? '，' + formatChineseDate(attention.review_date) + '再复查' : '');
}

function normalizeSpeakerName(value) {
  return String(value || '').toLowerCase().replace(/[^a-z0-9\u00c0-\u024f\u4e00-\u9fff]+/g, '');
}

function communicationContextData(context) {
  if (context === 'complete') {
    var modal = document.getElementById('completeModal');
    return {
      customer_id: modal ? modal.dataset.customerId || '' : '',
      customer_name: (document.getElementById('completeCustomerName') || {}).textContent || ''
    };
  }
  return {
    customer_id: (document.getElementById('editCustomerId') || {}).value || '',
    customer_name: [(document.getElementById('editName') || {}).value, (document.getElementById('editCompany') || {}).value].filter(Boolean).join(' / ')
  };
}

function inferCommunicationDirectionFromText(content) {
  var userName = currentUser && (currentUser.name || currentUser.label || currentUser.id);
  var userKey = normalizeSpeakerName(userName);
  var ours = false;
  var theirs = false;
  var pattern = /^\s*(?:\[[^\]\n]{1,100}\]\s*)?([^:\n]{1,60}?)\s*:\s+/gm;
  var match;
  while ((match = pattern.exec(String(content || ''))) !== null) {
    var speaker = normalizeSpeakerName(match[1]);
    if (!speaker) continue;
    if (userKey && (speaker === userKey || speaker.endsWith(userKey) || userKey.endsWith(speaker))) ours = true;
    else theirs = true;
  }
  if (ours && theirs) return 'two_way';
  if (ours) return 'outbound';
  if (theirs) return 'inbound';

  var text = String(content || '').replace(/\s+/g, ' ').trim();
  var inboundPatterns = [
    /(?<!向)(?<!给)(?<!问)(?<!询)(?<!进)(?<!系)(?<!醒)(?<!复)(?:客户|买家|对方|联系人)[^。；;\n]{0,28}(?:回复|表示|确认|询问|问道|要求|希望|发送|提供|同意|拒绝|反馈|告知|提出|需要|接受)/i,
    /(?:收到|等待|暂无|暂未|没有|未有)(?:客户|买家|对方)[^。；;\n]{0,10}(?:回复|反馈|确认)/i
  ];
  var outboundPatterns = [
    /(?:我方|我们|本人|销售)[^。；;\n]{0,28}(?:回复|表示|确认|询问|问|提供|发送|报价|建议|告知|提醒|跟进|联系)/i,
    /(?:向|给)(?:客户|买家|对方|联系人)[^。；;\n]{0,28}(?:回复|确认|询问|提供|发送|报价|建议|告知|提醒)/i,
    /(?:询问|问|回复|提醒|跟进|联系)(?:了)?(?:客户|买家|对方|联系人)/i,
    /(?:二次|再次|继续)?(?:开发|跟进)(?:客户|需求)?/i,
    /(?:^|[。；;，,])(?:已|再次|继续)?(?:提供|发送|解释|介绍|询问|告知|提醒|跟进|报价|确认)(?:了)?/i
  ];
  var inbound = inboundPatterns.some(function(pattern) { return pattern.test(text); });
  var outbound = outboundPatterns.some(function(pattern) { return pattern.test(text); });
  if (inbound && outbound) return 'two_way';
  if (outbound) return 'outbound';
  if (inbound) return 'inbound';
  return 'unknown';
}

function resolvedCommunicationDirection(context) {
  var override = selectedCommunicationDirection(context);
  if (override !== 'auto') return override;
  var analysis = _communicationAnalyses[context] || {};
  if (['outbound', 'inbound', 'two_way'].indexOf(analysis.direction) >= 0) return analysis.direction;
  var input = document.getElementById(context === 'complete' ? 'completeResult' : 'followHistoryContent');
  return inferCommunicationDirectionFromText(input ? (input.isContentEditable ? richTextPlain(input) : input.value) : '');
}

function updateAutoDirectionPreview(context) {
  var override = selectedCommunicationDirection(context);
  var direction = override === 'auto' ? resolvedCommunicationDirection(context) : override;
  var hint = document.getElementById(context === 'complete' ? 'completeDirectionHint' : 'historyDirectionHint');
  if (!hint) return;
  hint.textContent = direction === 'unknown'
    ? '系统将在 AI 整理时结合发言人和客户信息判断'
    : (override === 'auto' ? '系统识别：' : '手动指定：') + communicationDirectionLabel(direction);
}

async function analyzeCommunication(context) {
  var isComplete = context === 'complete';
  var contentId = isComplete ? 'completeResult' : 'followHistoryContent';
  var panelId = isComplete ? 'completeAnalysis' : 'followHistoryAnalysis';
  var contentEl = document.getElementById(contentId);
  var panel = document.getElementById(panelId);
  var button = document.querySelector('button[onclick="analyzeCommunication(\'' + context + '\')"]');
  var content = contentEl ? (contentEl.isContentEditable ? richTextPlain(contentEl) : contentEl.value.trim()) : '';
  var direction = selectedCommunicationDirection(context);
  if (!content) { showToast('请先粘贴或输入沟通内容', 'warning'); if (contentEl) contentEl.focus(); return; }
  if (!panel) return;
  if (button) { button.disabled = true; button.textContent = 'AI 正在整理…'; button.setAttribute('aria-busy', 'true'); }
  panel.hidden = false;
  panel.innerHTML = '<div class="quick-analysis-loading">AI 正在后台整理沟通内容，你可以继续填写其他内容…</div>';
  // 整理结果写到共享槽位并渲染进当前表单，必须在响应回来时确认还是同一个
  // 上下文：客户工作区用 scope 校验，记录跟进弹窗用 fillToken 校验。
  var workspaceScope = context === 'history' ? beginCustomerScope() : null;
  var completeFillToken = _completeModalFillToken;
  try {
    var customerContext = communicationContextData(context);
    var result = await api('/api/inbox/analyze-reply', { method: 'POST', body: JSON.stringify({
      content: content,
      direction: direction,
      customer_id: customerContext.customer_id,
      customer_name: customerContext.customer_name
    }) });
    var contextChanged = workspaceScope ? !liveCustomerCache(workspaceScope) : completeFillToken !== _completeModalFillToken;
    if (contextChanged) return;
    var analysis = result.analysis || {};
    _communicationAnalyses[context] = analysis;
    var resolvedDirection = analysis.direction || resolvedCommunicationDirection(context);
    updateAutoDirectionPreview(context);
    var facts = (analysis.key_facts || []).concat(analysis.needs || []).filter(Boolean);
    panel.innerHTML = '<div class="quick-analysis-head"><strong>AI 整理草稿 · ' + escapeHtml(communicationDirectionLabel(resolvedDirection)) + '</strong><span>' + escapeHtml(analysis.intent || '未知') + '</span></div>' +
      '<p>' + escapeHtml(analysis.summary || '已读取原文') + '</p>' +
      (facts.length ? '<div class="quick-analysis-facts">' + facts.map(function(item) { return '<span>' + escapeHtml(item) + '</span>'; }).join('') + '</div>' : '') +
      '<div class="quick-analysis-actions"><button type="button" class="text-action" onclick="applyCommunicationAnalysis(\'' + context + '\',\'summary\')">采用摘要</button>' +
      (analysis.suggested_next_action ? '<button type="button" class="text-action" onclick="applyCommunicationAnalysis(\'' + context + '\',\'next\')">采用下一步建议</button>' : '') + '</div>';
  } catch (e) {
    panel.innerHTML = '<div class="quick-analysis-error">AI 暂时无法整理，原文已保留，可以继续手动保存。</div>';
  } finally {
    if (button) { button.disabled = false; button.textContent = 'AI 帮我整理'; button.removeAttribute('aria-busy'); }
  }
}

function applyCommunicationAnalysis(context, field) {
  var analysis = _communicationAnalyses[context] || {};
  var isComplete = context === 'complete';
  if (field === 'summary') {
    var target = document.getElementById(isComplete ? 'completeResult' : 'followHistoryContent');
    if (target) {
      if (target.isContentEditable) setRichText(target, analysis.summary || '', false);
      else target.value = analysis.summary || '';
      target.dispatchEvent(new Event('input', { bubbles: true }));
      target.focus();
    }
    showToast('摘要已填入记录框，可继续修改后保存', 'success');
    return;
  }
  var suggestion = analysis.suggested_next_action || '';
  if (!suggestion) return;
  if (isComplete) {
    document.getElementById('completeHasNext').checked = true;
    toggleCompleteNext();
    document.getElementById('completeNextTask').value = suggestion;
    updateCompleteSaveLabel();
  } else {
    document.getElementById('followHistoryNextTask').value = suggestion;
    updateFollowHistorySaveLabel();
  }
  showToast('下一步建议已填入，请确认动作和日期', 'success');
}

function chooseInboxReplyCandidate(customerId) {
  var state = _customerPickerRegistry.inboxReplyCustomerPicker;
  var picker = document.getElementById('inboxReplyCustomerPicker');
  if (!state || !picker) return;
  var customer = state.customers.find(function(c) { return Number(c.id) === Number(customerId); });
  if (!customer) return;
  picker.querySelector('input[type="hidden"]').value = customer.id;
  picker.querySelector('input[type="search"]').value = customerPickerLabel(customer) + (customer.country ? ' · ' + customer.country : '');
  picker.querySelector('.customer-picker-results').classList.remove('show');
}

async function saveInboxReply() {
  var context = _communicationConfirmContext || {};
  var customerId = context.customerId || document.getElementById('inboxReplyCustomer').value;
  var content = document.getElementById('inboxReplyContent').value.trim();
  var followDate = document.getElementById('inboxReplyDate').value || localDateString();
  if (!content) { showToast('请先记录沟通内容', 'warning'); document.getElementById('inboxReplyContent').focus(); return; }
  if (!customerId) { showToast('请搜索并选择客户', 'warning'); document.getElementById('inboxReplyCustomerSearch').focus(); return; }
  var button = document.getElementById('saveInboxReplyButton');
  button.disabled = true;
  button.textContent = '正在记录…';
  try {
    var saved = null;
    if (context.agentProposalId) {
      var proposalPayload = Object.assign({}, context.agentProposalPayload || {}, {
        content: content, activity_content: content, follow_date: followDate,
        activity_result: (_inboxReplyAnalysis && _inboxReplyAnalysis.summary) || '',
        activity_type: context.activityType || 'follow_up', direction: context.direction || 'unknown',
        next_task: '', next_follow_up: '',
        inbox_item_id: context.inboxItemId || '', contact_id: context.contactId || null
      });
      await api('/api/agent/proposals/' + context.agentProposalId, { method: 'PUT', body: JSON.stringify(proposalPayload) });
      saved = await api('/api/agent/proposals/' + context.agentProposalId + '/confirm', { method: 'POST' });
    } else if (context.reminderId) {
      saved = await api('/api/reminders/' + context.reminderId, {
        method: 'PUT', body: JSON.stringify({
          activity_content: content, activity_result: (_inboxReplyAnalysis && _inboxReplyAnalysis.summary) || '',
          activity_type: context.activityType || 'follow_up', direction: context.direction || 'unknown',
          next_task: '', next_follow_up: '', is_reported: 0
        })
      });
    } else {
      saved = await api('/api/customers/' + customerId + '/follow_history', {
        method: 'POST', body: JSON.stringify({
          activity_content: content, activity_result: (_inboxReplyAnalysis && _inboxReplyAnalysis.summary) || '',
          activity_type: context.activityType || 'follow_up', direction: context.direction || 'unknown',
          follow_date: followDate, next_task: '', next_follow_up: '',
          source: context.source || 'manual', inbox_item_id: context.inboxItemId || '', contact_id: context.contactId || null
        })
      });
    }
    if (liveCustomerCache(beginCustomerScope(customerId))) {
      var activity = saved && saved.activity ? Object.assign({ type: 'follow' }, saved.activity) : Object.assign({
        type: 'follow', id: saved && (saved.activity_id || (!context.agentProposalId && saved.id)),
        follow_date: followDate, content: content,
        result: (_inboxReplyAnalysis && _inboxReplyAnalysis.summary) || '',
        next_plan: '', activity_type: context.activityType || 'customer_reply',
        direction: context.direction || 'unknown', is_reported: false
      }, saved && saved.activity ? saved.activity : {});
      if (activity.id) {
        var changedKey = upsertCustomerTimelineEntry(activity);
        var reminders = (_customerDetailCache.reminders || []).slice();
        if (saved && saved.completed_task) reminders = reminders.filter(function(task) { return Number(task.id) !== Number(saved.completed_task.id); });
        if (saved && saved.next_step) reminders = reminders.filter(function(task) { return Number(task.id) !== Number(saved.next_step.id); }).concat([saved.next_step]);
        if (saved && (saved.completed_task || saved.next_step)) applyCustomerTaskSnapshot(reminders);
        syncCustomerWorkspaceAfterCommunication(customerId, activity, changedKey);
      }
      reconcileCustomerTimeline({ includeSummary: true }).catch(function() {});
    }
    // 待归属沟通流水线：确认一条后直接载入下一条，不回列表。
    if (context.autoPipeline) {
      loadInbox();
      if (currentPage === 'dashboard') loadDashboard();
      var nextItem = inboxItems.find(function(entry) {
        return isInboxCommunicationCapture(entry) && Number(entry.id) !== Number(context.inboxItemId);
      });
      var total = context.queueSize || 1;
      showToast('已记录，待归属沟通还剩 ' + (nextItem ? Math.max(total - 1, 1) : 0) + ' 条', 'success');
      if (nextItem) {
        button.disabled = false;
        button.textContent = '确认并记录';
        recordInboxCapture(nextItem.id);
      } else {
        closeModal('inboxReplyModal', true);
        button.disabled = false;
        button.textContent = '确认并记录';
      }
      return;
    }
    closeModal('inboxReplyModal', true);
    showToast('沟通已保存到时间线', 'success');
    loadInbox();
    if (currentPage === 'dashboard') loadDashboard();
  } catch (e) {
  } finally {
    if (!context.autoPipeline) {
      button.disabled = false;
      button.textContent = '确认并记录';
    }
  }
}

// 可复用的客户搜索选择器。使用隐藏 input 保持现有提交逻辑不变。
var _customerPickerRegistry = {};

function customerPickerLabel(customer) {
  return customer.company || customer.name || '未命名客户';
}

function initializeCustomerPicker(pickerId, customers) {
  var picker = document.getElementById(pickerId);
  if (!picker) return;
  var hidden = picker.querySelector('input[type="hidden"]');
  var input = picker.querySelector('input[type="search"]');
  var results = picker.querySelector('.customer-picker-results');
  _customerPickerRegistry[pickerId] = { customers: customers || [], filtered: [] };
  hidden.value = '';
  input.value = '';
  results.innerHTML = '';
  results.classList.remove('show');
  if (input.dataset.pickerReady === '1') return;
  input.dataset.pickerReady = '1';
  input.addEventListener('focus', function() { renderCustomerPicker(pickerId); });
  input.addEventListener('input', function() {
    hidden.value = '';
    renderCustomerPicker(pickerId);
  });
  input.addEventListener('keydown', function(event) {
    var state = _customerPickerRegistry[pickerId];
    if (event.key === 'Enter' && state && state.filtered.length) {
      event.preventDefault();
      selectCustomerPicker(pickerId, 0);
    }
    if (event.key === 'Escape') results.classList.remove('show');
  });
  input.addEventListener('blur', function() {
    setTimeout(function() { results.classList.remove('show'); }, 120);
  });
}

function renderCustomerPicker(pickerId) {
  var picker = document.getElementById(pickerId);
  var state = _customerPickerRegistry[pickerId];
  if (!picker || !state) return;
  var input = picker.querySelector('input[type="search"]');
  var results = picker.querySelector('.customer-picker-results');
  var q = input.value.trim().toLowerCase();
  state.filtered = state.customers.filter(function(customer) {
    return [customer.company, customer.name, customer.primary_contact_name, customer.primary_contact_email, customer.country, customer.field, customer.tags]
      .some(function(value) { return String(value || '').toLowerCase().indexOf(q) >= 0; });
  }).slice(0, 8);
  if (!state.filtered.length) {
    results.innerHTML = '<div class="customer-picker-empty">没有找到匹配的客户</div>';
  } else {
    results.innerHTML = state.filtered.map(function(customer, index) {
      var primary = customerPickerLabel(customer);
      var secondary = [customer.name !== primary ? customer.name : '', customer.primary_contact_name, customer.country, customer.field].filter(Boolean).join(' · ');
      return '<button type="button" class="customer-picker-option" onmousedown="event.preventDefault()" onclick="selectCustomerPicker(\'' + pickerId + '\',' + index + ')"><strong>' + escapeHtml(primary) + '</strong>' +
        (secondary ? '<span>' + escapeHtml(secondary) + '</span>' : '') + '</button>';
    }).join('');
  }
  results.classList.add('show');
}

function selectCustomerPicker(pickerId, index) {
  var picker = document.getElementById(pickerId);
  var state = _customerPickerRegistry[pickerId];
  var customer = state && state.filtered[index];
  if (!picker || !customer) return;
  picker.querySelector('input[type="hidden"]').value = customer.id;
  picker.querySelector('input[type="search"]').value = customerPickerLabel(customer) + (customer.country ? ' · ' + customer.country : '');
  picker.querySelector('.customer-picker-results').classList.remove('show');
}

function formatChineseDate(dateStr) {
  if (!dateStr) return '';
  var value = String(dateStr).trim();
  var match = value.match(/^(\d{4})-(\d{1,2})-(\d{1,2})/);
  if (match) return Number(match[2]) + '月' + Number(match[3]) + '日';
  var parsed = new Date(value);
  if (!isNaN(parsed.getTime()) && !/^\d{4}$/.test(value)) {
    return (parsed.getMonth() + 1) + '月' + parsed.getDate() + '日';
  }
  return value;
}

function formatChineseToday(date) {
  var value = date || new Date();
  var weekdays = ['星期日', '星期一', '星期二', '星期三', '星期四', '星期五', '星期六'];
  return (value.getMonth() + 1) + '月' + value.getDate() + '日 · ' + weekdays[value.getDay()];
}

function updateTodaySummary(reminders) {
  var today = localDateString();
  var uniqueCustomers = new Set();
  var overdueCount = 0;
  (reminders || []).forEach(function(reminder) {
    var customerKey = reminder.customer_id || reminder.customer_company || reminder.customer_name || reminder.id;
    uniqueCustomers.add(String(customerKey));
    if ((reminder.remind_date || '').substring(0, 10) < today) overdueCount++;
  });
  document.getElementById('statPending').textContent = uniqueCustomers.size;
  document.getElementById('statOverdue').textContent = overdueCount;
}

function arrangeOverdueReminders(button) {
  var today = localDateString();
  var overdue = (dashboardReminders || []).filter(function(item) {
    return !item.is_done && String(item.reminder_type || '').indexOf('outreach_') !== 0 &&
      String(item.remind_date || '').substring(0, 10) < today;
  });
  if (!overdue.length) { showToast('没有需要重新安排的逾期待办', 'success'); return; }
  if (!window.confirm('将 ' + overdue.length + ' 项逾期待办安排到未来 9 个工作日，按当前 Today 优先顺序分配？')) return;
  var original = button && button.textContent;
  if (button) { button.disabled = true; button.setAttribute('aria-busy', 'true'); button.title = '正在整理'; }
  var nextDate = new Date();
  var businessDates = [];
  while (businessDates.length < Math.ceil(overdue.length / 5)) {
    nextDate.setDate(nextDate.getDate() + 1);
    if (nextDate.getDay() !== 0 && nextDate.getDay() !== 6) businessDates.push(localDateString(nextDate));
  }
  (async function() {
    var changed = 0;
    var failed = [];
    for (var index = 0; index < overdue.length; index++) {
      var item = overdue[index];
      try {
        await api('/api/reminders/' + item.id + '/reschedule', {
          method: 'POST',
          body: JSON.stringify({ remind_date: businessDates[Math.floor(index / 5)], customer_id: item.customer_id })
        });
        changed++;
      } catch (error) {
        failed.push(item.customer_company || item.customer_name || ('#' + item.id));
      }
    }
    if (button) { button.disabled = false; button.removeAttribute('aria-busy'); button.title = '整理逾期'; button.textContent = original; }
    await loadDashboard();
    if (failed.length) showToast('已安排 ' + changed + ' 项，' + failed.length + ' 项因客户匹配冲突保留待处理', 'warning');
    else showToast('已按工作日安排 ' + changed + ' 项逾期待办', 'success');
  })().catch(function() {
    if (button) { button.disabled = false; button.removeAttribute('aria-busy'); button.title = '整理逾期'; button.textContent = original; }
    showToast('批量安排未完成，请重新加载后重试', 'error');
  });
}

async function loadDashboard() {
  _pageDataLoadedAt = Date.now();
  var loadToken = ++_dashboardLoadToken;
  var errorEl = document.getElementById('todayDashboardError');
  var showError = function(message) {
    if (!errorEl) return;
    errorEl.hidden = false;
    errorEl.innerHTML = '<span>' + escapeHtml(message || '今天数据暂时无法加载') + '</span><button type="button" class="btn btn-sm" onclick="loadDashboard()">重新加载</button>';
  };
  var clearError = function() { if (errorEl) { errorEl.hidden = true; errorEl.innerHTML = ''; } };
  clearError();

  // Keep each panel independent. A failed optional section must not turn the
  // whole workbench into a misleading “没有待办” state.
  var requests = await Promise.allSettled([
    api('/api/stats'),
    Promise.all([api('/api/reminders/today'), api('/api/reminders/upcoming')]),
    api('/api/logs?limit=8'),
    api('/api/my-weekly-logs')
  ]);
  // A slower earlier read must not repaint today's stats/reminders over a newer
  // one (e.g. completing a task then returning to Today).
  if (loadToken !== _dashboardLoadToken) return;
  var statsResult = requests[0];
  if (statsResult.status === 'fulfilled') {
    var stats = statsResult.value || {};
    var followingEl = document.getElementById('statFollowing');
    if (followingEl) followingEl.textContent = stats.pending || 0;
    document.getElementById('dashDate').textContent = formatChineseToday();
    var distEl = document.getElementById('statusDist');
    if (distEl) {
      var total = stats.total || 1;
      var statusColors = { '未标记': '#9B8E82', '成交': '#5B7B5A', '流失': '#A0522D' };
      var sc = stats.stage_counts || {};
      var distHtml = '';
      for (var s in sc) {
        var c = sc[s];
        var pct = Math.round(c / total * 100);
        distHtml += '<div style="margin-bottom:12px;"><div style="display:flex;justify-content:space-between;font-size:0.82rem;"><span style="color:var(--fg-secondary);">' + s + '</span><span style="color:var(--fg-muted);">' + c + ' (' + pct + '%)</span></div><div class="progress-bar"><div class="progress-fill" style="width:' + pct + '%;background:' + (statusColors[s] || '#9B8E82') + ';"></div></div></div>';
      }
      if (!distHtml) distHtml = '<div class="empty-state"><p>暂无数据</p></div>';
      distEl.innerHTML = distHtml;
    }
  } else {
    showError('今天统计暂时无法加载，客户数据没有被清空。');
  }

  var reminderResult = requests[1];
  if (reminderResult.status === 'fulfilled') {
    dashboardReminders = reminderResult.value[0] || [];
    var upcoming = reminderResult.value[1] || [];
    updateTodaySummary(dashboardReminders);
    renderTodayTasks(dashboardReminders);
    renderTodaySchedule(dashboardReminders.concat(upcoming));
  } else {
    dashboardReminders = [];
    renderTodayError('今日跟进暂时无法加载，系统没有把它当作“已完成”。');
    showError('今日跟进暂时无法加载，客户数据没有被清空。');
  }

  var logsResult = requests[2];
  if (logsResult.status === 'fulfilled') {
    var logs = logsResult.value || [];
    var actEl = document.getElementById('recentActivity');
    if (actEl) actEl.innerHTML = logs.length ? logs.map(function(l) {
      return '<div class="log-item"><span class="log-time">' + escapeHtml(l.created_at || '') + '</span><span class="log-action">' + escapeHtml(l.action || '') + '</span><span class="log-detail">' + renderRichText(l.details || '') + '</span></div>';
    }).join('') : '<div class="empty-state"><p>暂无最近动态</p></div>';
  }

  var weeklyResult = requests[3];
  if (weeklyResult.status === 'fulfilled') loadWeeklyFollowList(weeklyResult.value || []);
}

function renderTodayError(message) {
  var remEl = document.getElementById('todayReminders');
  if (remEl) remEl.innerHTML = '<div class="empty-state"><strong>' + escapeHtml(message || '今日跟进暂时无法加载') + '</strong><p>请点击“重新加载”重试。</p></div>';
  var focus = document.getElementById('todayFocus');
  if (focus) focus.innerHTML = '<div class="empty-state"><p>等待今日跟进数据</p></div>';
}

function renderTodayTasks(reminders) {
  var remEl = document.getElementById('todayReminders');
  if (!reminders || reminders.length === 0) {
    remEl.innerHTML = '<div class="today-clear"><strong>今天已经处理完了</strong><span>新的提醒会自动出现在这里。</span></div>';
    document.getElementById('todayFocus').innerHTML = '<div class="empty-state"><p>今天没有待处理事项</p></div>';
    var wideEmpty = document.getElementById('todayWideDetail');
    if (wideEmpty) wideEmpty.innerHTML = '<div class="today-wide-empty"><span>今日工作已完成</span><p>新的提醒会自动出现在这里。</p></div>';
    return;
  }

  var selectedRow = remEl.querySelector('.today-task-row.selected');
  var selectedId = selectedRow ? Number(selectedRow.dataset.reminderId) : Number(reminders[0].id);
  if (!reminders.some(function(item) { return Number(item.id) === selectedId; })) selectedId = Number(reminders[0].id);
  reconcileKeyedElements(remEl, reminders, {
    selector: '.today-task-row',
    key: function(item) { return 'reminder-' + item.id; },
    render: function(item, index) { return buildTodayTaskRow(item, index, Number(item.id) === selectedId); }
  });
  initTodayTaskSorting();
  renderTodayFocus(reminders.filter(function(item) { return Number(item.id) === selectedId; })[0] || reminders[0]);
}

function buildTodayTaskRow(r, index, selected) {
    var today = localDateString();
    var overdue = (r.remind_date || '').substring(0, 10) < today;
    var name = r.customer_company || r.customer_name || '未命名客户';
    var action = r.task_title || r.title || r.content || '联系客户';
    var context = escapeHtml(r.why_today || '') + (r.last_activity ? (r.why_today ? ' · 最近：' : '最近：') + renderRichText(r.last_activity) : '');
    var customerId = Number(r.customer_id);
    var isMultiSelected = selectedTodayCustomers.has(customerId);
    return '<div class="today-task-row' + (selected ? ' selected' : '') + (isMultiSelected ? ' is-multi-selected' : '') + '" data-reminder-id="' + r.id + '" data-customer-id="' + customerId + '" role="button" tabindex="0" onclick="selectTodayReminder(' + r.id + ')" onkeydown="if(event.target===this&&(event.key===\'Enter\'||event.key===\' \')){event.preventDefault();selectTodayReminder(' + r.id + ')}">' +
      '<span class="today-task-index-wrap">' +
        '<span class="today-task-index">' + (index + 1) + '</span>' +
        '<input type="checkbox" class="table-checkbox today-task-checkbox" data-id="' + customerId + '" onclick="event.stopPropagation()" onchange="updateTodaySelection()"' + (isMultiSelected ? ' checked' : '') + ' aria-label="选择 ' + escapeHtml(name) + '">' +
      '</span>' +
      '<span class="today-task-copy"><button type="button" class="today-task-customer" onclick="event.stopPropagation();openEditModal(' + customerId + ')">' + escapeHtml(name) + '</button><strong>' + escapeHtml(action) + '</strong><span>' + context + '</span></span>' +
      '<span class="today-task-date' + (overdue ? ' overdue' : '') + '">' + (overdue ? formatChineseDate(r.remind_date) : '今天') + '</span>' +
      '</div>';
}

function todayTaskIdsFromDom() {
  return Array.from(document.querySelectorAll('#todayReminders .today-task-row')).map(function(row) {
    return Number(row.dataset.reminderId);
  });
}

function refreshTodayTaskIndexes() {
  document.querySelectorAll('#todayReminders .today-task-row').forEach(function(row, index) {
    var indexEl = row.querySelector('.today-task-index');
    if (indexEl) indexEl.textContent = index + 1;
  });
}

var _todaySortRetryIds = null;
var _todaySortStatusTimer = null;
var _todaySortSaveTimer = null;
var _todaySortSaveToken = 0;
var _todaySortEscapeHandler = null;
var _todaySortDocumentMoveHandler = null;
var _todaySortDocumentUpHandler = null;
var _todaySortWindowBlurHandler = null;
var _todaySortVisibilityHandler = null;
var _cancelTodayTaskDrag = null;

function setTodaySortStatus(state, message) {
  var status = document.getElementById('todaySortStatus');
  if (!status) return;
  clearTimeout(_todaySortStatusTimer);
  status.className = 'today-sort-status' + (state ? ' ' + state : '');
  if (state === 'error') {
    status.innerHTML = '<span>!</span><span>' + escapeHtml(message || '顺序保存失败') + '</span><button type="button" onclick="retryTodayTaskOrder()">重试</button>';
    return;
  }
  var icon = state === 'saving' ? '<i></i>' : (state === 'success' ? uiIcon('check') : '');
  status.innerHTML = icon + '<span>' + escapeHtml(message || '') + '</span>';
  if (state === 'success') {
    _todaySortStatusTimer = setTimeout(function() {
      status.classList.add('is-hiding');
      setTimeout(function() { status.className = 'today-sort-status'; status.innerHTML = ''; }, 220);
    }, 1500);
  }
}

async function persistTodayTaskOrder(explicitIds) {
  var ids = explicitIds || todayTaskIdsFromDom();
  if (!ids.length) return;
  var byId = {};
  dashboardReminders.forEach(function(reminder) { byId[Number(reminder.id)] = reminder; });
  dashboardReminders = ids.map(function(id) { return byId[id]; }).filter(Boolean);
  refreshTodayTaskIndexes();
  var saveToken = ++_todaySortSaveToken;
  setTodaySortStatus('saving', '正在保存顺序…');
  try {
    var result = await api('/api/reminders/today/order', {
      method: 'POST', skipGlobalSync: true, body: JSON.stringify({ ids: ids })
    });
    if (!result || !result.success) throw new Error('保存未完成');
    if (saveToken !== _todaySortSaveToken) return;
    _todaySortRetryIds = null;
    setTodaySortStatus('success', '顺序已保存');
  } catch (e) {
    if (saveToken !== _todaySortSaveToken) return;
    _todaySortRetryIds = ids.slice();
    setTodaySortStatus('error', '保存失败');
  }
}

function retryTodayTaskOrder() {
  if (_todaySortRetryIds) persistTodayTaskOrder(_todaySortRetryIds.slice());
}

function captureTodayTaskPositions(list) {
  var positions = {};
  list.querySelectorAll('.today-task-row').forEach(function(row) {
    positions[row.dataset.reminderId] = row.getBoundingClientRect();
  });
  return positions;
}

function animateTodayTaskReflow(list, previousPositions) {
  list.querySelectorAll('.today-task-row').forEach(function(row) {
    var previous = previousPositions[row.dataset.reminderId];
    if (!previous) return;
    var current = row.getBoundingClientRect();
    var deltaY = previous.top - current.top;
    if (!deltaY) return;
    row.style.transition = 'none';
    row.style.transform = 'translateY(' + deltaY + 'px)';
    requestAnimationFrame(function() {
      requestAnimationFrame(function() {
        row.style.transition = 'transform 190ms cubic-bezier(.2,.8,.2,1)';
        row.style.transform = '';
        setTimeout(function() { row.style.transition = ''; }, 210);
      });
    });
  });
}

function scheduleTodayTaskSave(message) {
  clearTimeout(_todaySortSaveTimer);
  clearTimeout(_globalSyncTimer);
  _todaySortSaveToken++;
  _todaySortRetryIds = null;
  setTodaySortStatus('saving', message || '正在保存顺序…');
  _todaySortSaveTimer = setTimeout(function() { persistTodayTaskOrder(); }, 260);
}

function initTodayTaskSorting() {
  if (_cancelTodayTaskDrag) _cancelTodayTaskDrag();
  var list = document.getElementById('todayReminders');
  if (!list) return;
  var drag = null;
  var lightPress = null;
  var longPressTimer = null;
  var longPressStart = null;
  var autoScrollFrame = null;

  function beginDrag(row, handle, e) {
    clearTimeout(_globalSyncTimer);
    var rect = row.getBoundingClientRect();
    var placeholder = document.createElement('div');
    placeholder.className = 'today-task-placeholder';
    placeholder.style.height = rect.height + 'px';
    list.insertBefore(placeholder, row);
    drag = { row: row, handle: handle, placeholder: placeholder, pointerId: e.pointerId,
      pointerOffsetY: e.clientY - rect.top, initialIds: todayTaskIdsFromDom(), scrollVelocity: 0,
      lastClientX: e.clientX, lastClientY: e.clientY };
    row.classList.add('is-dragging');
    row.style.position = 'fixed'; row.style.left = Math.round(rect.left) + 'px'; row.style.top = Math.round(rect.top) + 'px';
    row.style.width = Math.round(rect.width) + 'px'; row.style.height = Math.round(rect.height) + 'px'; row.style.zIndex = '10000';
    document.body.appendChild(row);
    document.body.classList.add('today-is-sorting');
    if (navigator.vibrate && e.pointerType !== 'mouse') navigator.vibrate(18);
    setTodaySortStatus('sorting', '拖动到想要的位置');
    if (!autoScrollFrame) autoScrollFrame = requestAnimationFrame(runAutoScroll);
  }

  function runAutoScroll() {
    if (!drag) { autoScrollFrame = null; return; }
    if (drag.scrollVelocity) {
      window.scrollBy(0, drag.scrollVelocity);
      updatePlaceholder(drag.lastClientX, drag.lastClientY);
    }
    autoScrollFrame = requestAnimationFrame(runAutoScroll);
  }

  function updatePlaceholder(clientX, clientY) {
    if (!drag) return;
    var target = document.elementFromPoint(clientX, clientY);
    target = target && target.closest('.today-task-row');
    if (!target || target.parentElement !== list) return;
    var rect = target.getBoundingClientRect();
    var before = clientY < rect.top + rect.height / 2;
    var reference = before ? target : target.nextSibling;
    if (reference === drag.placeholder || drag.placeholder.nextSibling === reference) return;
    var previousPositions = captureTodayTaskPositions(list);
    list.insertBefore(drag.placeholder, reference);
    animateTodayTaskReflow(list, previousPositions);
    refreshTodayTaskIndexes();
  }

  function moveFloatingRow(clientY) {
    if (!drag) return;
    drag.lastClientY = clientY;
    drag.row.style.top = Math.round(clientY - drag.pointerOffsetY) + 'px';
    var edge = 72;
    if (clientY < edge) drag.scrollVelocity = -Math.ceil((edge - clientY) / edge * 14);
    else if (clientY > window.innerHeight - edge) drag.scrollVelocity = Math.ceil((clientY - (window.innerHeight - edge)) / edge * 14);
    else drag.scrollVelocity = 0;
  }

  function finishDrag(cancelled) {
    if (!drag) return;
    var active = drag;
    drag = null;
    if (autoScrollFrame) { cancelAnimationFrame(autoScrollFrame); autoScrollFrame = null; }
    document.body.classList.remove('today-is-sorting');
    var floatingRect = active.row.getBoundingClientRect();
    list.insertBefore(active.row, active.placeholder);
    active.placeholder.remove();
    active.row.removeAttribute('style');
    active.row.classList.remove('is-dragging');
    if (cancelled) {
      var rowMap = {};
      list.querySelectorAll('.today-task-row').forEach(function(item) { rowMap[Number(item.dataset.reminderId)] = item; });
      active.initialIds.forEach(function(id) { if (rowMap[id]) list.appendChild(rowMap[id]); });
    }
    refreshTodayTaskIndexes();
    var landingRect = active.row.getBoundingClientRect();
    active.row.style.transition = 'none';
    active.row.style.transform = 'translate(' + (floatingRect.left - landingRect.left) + 'px,' + (floatingRect.top - landingRect.top) + 'px) scale(1.018)';
    requestAnimationFrame(function() {
      requestAnimationFrame(function() {
        active.row.classList.add('is-settling');
        active.row.style.transform = '';
        active.row.style.transition = '';
        setTimeout(function() { active.row.classList.remove('is-settling'); active.row.removeAttribute('style'); }, 280);
      });
    });
    if (active.handle.hasPointerCapture && active.handle.hasPointerCapture(active.pointerId)) active.handle.releasePointerCapture(active.pointerId);
    var finalIds = todayTaskIdsFromDom();
    if (!cancelled && finalIds.join(',') !== active.initialIds.join(',')) {
      if (navigator.vibrate) navigator.vibrate(16);
      var finalIndex = finalIds.indexOf(Number(active.row.dataset.reminderId));
      var badge = active.row.querySelector('.today-task-index');
      if (badge) {
        badge.classList.remove('rank-changed');
        void badge.offsetWidth;
        badge.classList.add('rank-changed');
      }
      scheduleTodayTaskSave('已移动到第 ' + (finalIndex + 1) + ' 位…');
    } else if (cancelled) {
      setTodaySortStatus('success', '已取消移动');
    } else {
      setTodaySortStatus('success', '顺序未改变');
    }
  }

  _cancelTodayTaskDrag = function() { if (drag) finishDrag(true); };

  function onDocumentKeydown(e) {
    if (drag && e.key === 'Escape') { e.preventDefault(); finishDrag(true); }
  }
  if (_todaySortEscapeHandler) document.removeEventListener('keydown', _todaySortEscapeHandler);
  _todaySortEscapeHandler = onDocumentKeydown;
  document.addEventListener('keydown', onDocumentKeydown);

  if (_todaySortDocumentMoveHandler) document.removeEventListener('pointermove', _todaySortDocumentMoveHandler);
  if (_todaySortDocumentUpHandler) {
    document.removeEventListener('pointerup', _todaySortDocumentUpHandler);
    document.removeEventListener('pointercancel', _todaySortDocumentUpHandler);
  }
  if (_todaySortWindowBlurHandler) window.removeEventListener('blur', _todaySortWindowBlurHandler);
  if (_todaySortVisibilityHandler) document.removeEventListener('visibilitychange', _todaySortVisibilityHandler);
  _todaySortDocumentMoveHandler = function(e) {
    if (longPressTimer && longPressStart && longPressStart.pointerId === e.pointerId) {
      clearTimeout(longPressTimer);
      longPressTimer = null;
      longPressStart = null;
    }
    if (lightPress && lightPress.pointerId === e.pointerId) {
      var moved = Math.abs(e.clientX - lightPress.startX) + Math.abs(e.clientY - lightPress.startY) > 7;
      if (moved) {
        var press = lightPress;
        lightPress = null;
        press.handle.classList.remove('is-pressing');
        beginDrag(press.row, press.handle, e);
      }
    }
    if (!drag || drag.pointerId !== e.pointerId) return;
    e.preventDefault();
    drag.lastClientX = e.clientX;
    moveFloatingRow(e.clientY);
    updatePlaceholder(e.clientX, e.clientY);
  };
  _todaySortDocumentUpHandler = function(e) {
    if (longPressTimer) { clearTimeout(longPressTimer); longPressTimer = null; longPressStart = null; }
    if (lightPress && lightPress.pointerId === e.pointerId) {
      var press = lightPress;
      lightPress = null;
      press.handle.classList.remove('is-pressing');
      if (press.toggle && e.type !== 'pointercancel') {
        e.preventDefault();
        e.stopPropagation();
        updateCustomerPriority(Number(press.handle.dataset.customerLight), press.handle.classList.contains('is-on') ? 'unpin' : 'pin');
      }
      return;
    }
    if (!drag || drag.pointerId !== e.pointerId) return;
    e.preventDefault();
    finishDrag(e.type === 'pointercancel');
  };
  _todaySortWindowBlurHandler = function() { if (longPressTimer) { clearTimeout(longPressTimer); longPressTimer = null; longPressStart = null; } if (lightPress) { lightPress.handle.classList.remove('is-pressing'); lightPress = null; } if (drag) finishDrag(true); };
  _todaySortVisibilityHandler = function() { if (longPressTimer) { clearTimeout(longPressTimer); longPressTimer = null; longPressStart = null; } if (document.hidden && lightPress) { lightPress.handle.classList.remove('is-pressing'); lightPress = null; } if (document.hidden && drag) finishDrag(true); };
  document.addEventListener('pointermove', _todaySortDocumentMoveHandler, { passive: false });
  document.addEventListener('pointerup', _todaySortDocumentUpHandler, { passive: false });
  document.addEventListener('pointercancel', _todaySortDocumentUpHandler, { passive: false });
  window.addEventListener('blur', _todaySortWindowBlurHandler);
  document.addEventListener('visibilitychange', _todaySortVisibilityHandler);

  list.querySelectorAll('.today-task-row').forEach(function(row) {
    if (row._todaySortCleanup) row._todaySortCleanup();
    var onRowPointerDown = function(e) {
      if (e.button !== undefined && e.button !== 0) return;
      if (e.target.closest('button, input, textarea, select, a')) return;
      if (list.classList.contains('is-selection-mode')) return;
      lightPress = { row: row, handle: row, pointerId: e.pointerId, startX: e.clientX, startY: e.clientY, toggle: false };
      if (e.pointerType === 'touch') {
        clearTimeout(longPressTimer);
        longPressStart = { row: row, pointerId: e.pointerId };
        longPressTimer = setTimeout(function() {
          longPressTimer = null;
          if (longPressStart) {
            var targetRow = longPressStart.row;
            longPressStart = null;
            lightPress = null;
            enterTodaySelectionMode(targetRow);
            if (navigator.vibrate) navigator.vibrate(15);
          }
        }, 500);
      }
    };
    row.addEventListener('pointerdown', onRowPointerDown);
    row._todaySortCleanup = function() {
      row.removeEventListener('pointerdown', onRowPointerDown);
      row._todaySortCleanup = null;
    };
  });
}

function selectTodayReminder(reminderId) {
  var list = document.getElementById('todayReminders');
  if (list && list.classList.contains('is-selection-mode')) {
    var row = list.querySelector('.today-task-row[data-reminder-id="' + reminderId + '"]');
    if (row) {
      var cb = row.querySelector('.today-task-checkbox');
      if (cb) { cb.checked = !cb.checked; updateTodaySelection(); }
    }
    return;
  }
  var selected = dashboardReminders.find(function(r) { return Number(r.id) === Number(reminderId); });
  if (!selected) return;
  document.querySelectorAll('#todayReminders .today-task-row').forEach(function(row) {
    row.classList.toggle('selected', Number(row.dataset.reminderId) === Number(reminderId));
  });
  renderTodayFocus(selected);
}

function renderTodayFocus(r) {
  var el = document.getElementById('todayFocus');
  if (!r) return;
  el.dataset.reminderId = r.id;
  el.dataset.customerId = r.customer_id;
  el.dataset.customerName = r.customer_company || r.customer_name || '当前客户';
  var name = r.customer_company || r.customer_name || '未命名客户';
  var contact = r.customer_company && r.customer_name && r.customer_company !== r.customer_name ? r.customer_name : '';
  var meta = [r.country, r.field].filter(Boolean).join(' · ');
  var website = r.website || '';
  if (website && !/^https?:\/\//i.test(website)) website = 'https://' + website;
  el.innerHTML = '<div class="today-focus-label">下一步</div>' +
    '<h3>' + escapeHtml(name) + '</h3>' +
    (meta ? '<div class="today-focus-meta">' + escapeHtml(meta) + '</div>' : '') +
    '<div class="today-focus-task">' + escapeHtml(r.task_title || r.title || r.content || '联系客户') + '</div>' +
    '<div class="today-focus-context"><div><span>为什么今天</span><strong>' + escapeHtml(r.why_today || '今天到期') + '</strong></div>' +
    '<div><span>最近动态</span><strong>' + renderRichText(r.last_activity || '暂无沟通记录') + '</strong></div></div>' +
    (contact ? '<div class="today-focus-contact"><span class="contact-avatar">' + escapeHtml(contact.substring(0, 1).toUpperCase()) + '</span><span>' + escapeHtml(contact) + '</span></div>' : '') +
    '<div class="today-focus-actions"><button class="btn btn-primary today-focus-primary" onclick="openTodayCommunicationConfirm()">完成并记录</button>' +
    '<div class="today-focus-links"><button class="text-action" onclick="openEditModal(' + r.customer_id + ')">查看客户</button>' +
    (website ? '<a class="text-action" href="' + escapeHtml(website) + '" target="_blank" rel="noopener">访问网站</a>' : '') + '</div></div>';
  renderTodayWideDetail(r, name, meta, website);
}

function renderTodayWideDetail(r, name, meta, website) {
  var el = document.getElementById('todayWideDetail');
  if (!el) return;
  el.dataset.reminderId = r.id;
  el.dataset.customerId = r.customer_id;
  el.dataset.customerName = name;
  var action = r.task_title || r.title || r.content || '联系客户';
  var need = r.why_today || '今天到期，需要推进下一步';
  var activity = r.last_activity || '暂无沟通记录';
  el.innerHTML =
    '<div class="today-wide-detail-head">' +
      '<div class="today-wide-kicker">客户档案 · 今日焦点</div>' +
      '<div class="today-wide-head-row"><div><h2>' + escapeHtml(name) + '</h2>' + (meta ? '<p>' + escapeHtml(meta) + '</p>' : '') + '</div></div>' +
      '<div class="today-wide-actions">' +
        '<button type="button" class="today-wide-icon" onclick="focusTodayWideComposer()" aria-label="记录沟通" title="记录沟通"><span class="ui-icon ui-icon-message" aria-hidden="true"></span></button>' +
        '<button type="button" class="today-wide-icon" onclick="openEditModal(' + r.customer_id + ')" aria-label="查看客户" title="查看客户"><span class="ui-icon ui-icon-open" aria-hidden="true"></span></button>' +
        (website ? '<a class="today-wide-icon" href="' + escapeHtml(website) + '" target="_blank" rel="noopener" aria-label="访问官网" title="访问官网"><span class="ui-icon ui-icon-external" aria-hidden="true"></span></a>' : '') +
      '</div>' +
    '</div>' +
    '<div class="today-wide-facts">' +
      '<div><span>当前等待</span><strong>' + escapeHtml(need) + '</strong></div>' +
      '<div><span>下一步</span><strong class="is-clay">' + escapeHtml(action) + '</strong></div>' +
      '<div><span>关键需求</span><strong>' + escapeHtml(r.field || '待补充客户需求') + '</strong></div>' +
      '<div><span>最近发生</span><strong>' + renderRichText(activity) + '</strong></div>' +
    '</div>' +
    '<div class="today-wide-compose">' +
      '<div class="today-wide-compose-title"><span>沟通记录</span><small>在确认面板中记录事实，并按需安排下一步。</small></div>' +
      '<div class="today-wide-compose-footer"><span>当前待办会在确认后完成</span><button type="button" class="btn btn-primary" onclick="openTodayCommunicationConfirm()"><span class="ui-icon ui-icon-check" aria-hidden="true"></span><span>完成并记录</span></button></div>' +
    '</div>';
}

function focusTodayWideComposer() {
  openTodayCommunicationConfirm();
}

function openTodayCommunicationConfirm() {
  var detail = document.getElementById('todayWideDetail');
  if (!detail || !detail.dataset.reminderId || !detail.dataset.customerId) return;
  openReminderCommunicationConfirm({
    id: Number(detail.dataset.reminderId), customer_id: Number(detail.dataset.customerId),
    customer_company: detail.dataset.customerName || '当前客户'
  });
}

function openReminderCommunicationConfirm(reminder) {
  if (!reminder || !reminder.id || !reminder.customer_id) return;
  openCommunicationConfirm({
    source: 'today', sourceLabel: 'Today 待办', reminderId: Number(reminder.id), customerId: Number(reminder.customer_id),
    customerName: reminder.customer_company || reminder.customer_name || '当前客户', direction: 'unknown', activityType: 'follow_up',
    subtitle: '记录这次实际沟通；确认后会完成当前这条待办，必要时再安排下一步。'
  });
}

function revealTodayWideNextPanel(panel, transitionToken) {
  if (!panel || panel.hidden || panel.dataset.transitionToken !== transitionToken || !panel.classList.contains('is-open')) return;
  var detail = panel.closest('.today-wide-detail');
  if (!detail) return;
  var panelRect = panel.getBoundingClientRect();
  var detailRect = detail.getBoundingClientRect();
  // Reveal the fields, but do not chase the footer. Keeping the customer name
  // and facts in view makes the expansion feel connected to the context.
  var contentBottom = panelRect.bottom;
  var visibleBottom = detailRect.bottom - 22;
  var needed = contentBottom - visibleBottom;
  if (needed <= 1) return;
  detail.scrollTo({
    top: Math.min(detail.scrollHeight - detail.clientHeight, detail.scrollTop + needed),
    behavior: document.documentElement.classList.contains('motion-reduced') || document.documentElement.classList.contains('motion-lite') ? 'auto' : 'smooth'
  });
}

function toggleTodayWideNext(button) {
  var panel = document.getElementById('todayWideNextPanel');
  if (!panel || !button) return;
  var shouldOpen = panel.hidden || !panel.classList.contains('is-open');
  var transitionToken = String((parseInt(panel.dataset.transitionToken || '0', 10) || 0) + 1);
  panel.dataset.transitionToken = transitionToken;
  button.setAttribute('aria-expanded', shouldOpen ? 'true' : 'false');
  button.classList.toggle('is-open', shouldOpen);
  var icon = button.querySelector('.ui-icon');
  if (icon) icon.className = 'ui-icon ui-icon-' + (shouldOpen ? 'close' : 'plus');
  button.setAttribute('aria-label', shouldOpen ? '关闭下一步' : '添加下一步');
  button.setAttribute('title', shouldOpen ? '关闭下一步' : '添加下一步');
  if (shouldOpen) {
    panel.hidden = false;
    panel.inert = false;
    panel.setAttribute('aria-hidden', 'false');
    window.requestAnimationFrame(function() {
      if (panel.dataset.transitionToken === transitionToken) panel.classList.add('is-open');
    });
    var task = document.getElementById('todayWideNextTask');
    if (task) task.focus({ preventScroll: true });
    window.setTimeout(function() { revealTodayWideNextPanel(panel, transitionToken); }, document.documentElement.classList.contains('motion-reduced') ? 0 : 240);
  } else {
    panel.setAttribute('aria-hidden', 'true');
    panel.inert = true;
    panel.classList.remove('is-open');
    var taskInput = document.getElementById('todayWideNextTask');
    var dateInput = document.getElementById('todayWideNextDate');
    if (taskInput) taskInput.value = '';
    if (dateInput) dateInput.value = '';
    var finishClose = function() {
      if (panel.dataset.transitionToken === transitionToken && !panel.classList.contains('is-open')) panel.hidden = true;
    };
    panel.addEventListener('transitionend', finishClose, { once: true });
    window.setTimeout(finishClose, 260);
  }
}

async function submitTodayWideNote(reminderId, button) {
  var note = document.getElementById('todayWideFeedback');
  var content = note && note.value.trim();
  if (!content) {
    if (note) note.focus();
    showToast('请先记录沟通内容', 'warning');
    return;
  }
  var nextPanel = document.getElementById('todayWideNextPanel');
  var nextTask = nextPanel && !nextPanel.hidden ? (document.getElementById('todayWideNextTask').value || '').trim() : '';
  var nextDate = nextPanel && !nextPanel.hidden ? (document.getElementById('todayWideNextDate').value || '').trim() : '';
  if (nextPanel && !nextPanel.hidden && (!nextTask || !nextDate)) {
    showToast('安排下一步时需要填写动作和日期', 'warning');
    if (!nextTask) document.getElementById('todayWideNextTask').focus();
    else document.getElementById('todayWideNextDate').focus();
    return;
  }
  var reset = setActionFeedback(button, 'pending', '正在记录…');
  try {
    await api('/api/reminders/' + reminderId, {
      method: 'PUT',
      body: JSON.stringify({ activity_type: 'follow_up', direction: 'unknown', activity_content: content, activity_result: '', next_task: nextTask, next_follow_up: nextTask ? nextDate : '', is_reported: 0 })
    });
    // The selected focus and the task list refresh immediately; that visible
    // state change is the confirmation, so no redundant success toast.
    await loadDashboard();
    reset();
  } catch (e) {
    setActionFeedback(button, 'error', '保存失败');
    showToast('这条更新没有保存，请重试', 'error');
    reset(1800);
  }
}

function renderTodaySchedule(reminders) {
  todayScheduleData = {};
  calendarData = {};
  (reminders || []).forEach(function(r) {
    var dateStr = (r.remind_date || '').substring(0, 10);
    if (!dateStr) return;
    if (!todayScheduleData[dateStr]) todayScheduleData[dateStr] = [];
    if (!calendarData[dateStr]) calendarData[dateStr] = [];
    todayScheduleData[dateStr].push(r);
    calendarData[dateStr].push(r);
  });

  var grid = document.getElementById('todayScheduleGrid');
  var weekdays = ['日','一','二','三','四','五','六'];
  var html = '';
  var firstTaskDate = '';
  for (var i = 1; i <= 14; i++) {
    var date = new Date();
    date.setHours(12, 0, 0, 0);
    date.setDate(date.getDate() + i);
    var dateStr = localDateString(date);
    var tasks = todayScheduleData[dateStr] || [];
    if (!firstTaskDate && tasks.length) firstTaskDate = dateStr;
    html += '<button type="button" class="today-schedule-day" data-schedule-date="' + dateStr + '" onclick="showTodayScheduleDetail(\'' + dateStr + '\')">' +
      '<span class="schedule-weekday">' + weekdays[date.getDay()] + '</span><strong>' + date.getDate() + '</strong>' +
      (tasks.length ? '<span class="schedule-count">' + tasks.length + ' 项</span>' : '<span class="schedule-empty">—</span>') + '</button>';
  }
  grid.innerHTML = html;
  var initialDate = firstTaskDate || localDateString(new Date(Date.now() + 86400000));
  showTodayScheduleDetail(initialDate);
}

function showTodayScheduleDetail(dateStr) {
  document.querySelectorAll('.today-schedule-day').forEach(function(day) {
    day.classList.toggle('selected', day.dataset.scheduleDate === dateStr);
  });
  var tasks = todayScheduleData[dateStr] || [];
  var el = document.getElementById('todayScheduleDetail');
  if (!tasks.length) {
    el.innerHTML = '<strong>' + formatChineseDate(dateStr) + '</strong><span>没有安排</span>';
    return;
  }
  var html = '<strong>' + formatChineseDate(dateStr) + '</strong><div class="schedule-detail-items">';
  tasks.forEach(function(r) {
    html += '<div class="schedule-detail-item"><span><b>' + escapeHtml(r.customer_company || r.customer_name || '客户') + '</b>' + escapeHtml(' · ' + (r.task_title || r.title || r.content || '联系客户')) + '</span><button class="text-action" onclick="openCompleteModal(' + r.id + ')">记录</button></div>';
  });
  el.innerHTML = html + '</div>';
}

function openCalendarSync() {
  switchPage('calendar');
  initIcalUrl();
}

async function loadWeeklyFollowList(prefetchedData) {
  try {
    var data = prefetchedData || await api('/api/my-weekly-logs');
    var el = document.getElementById('weeklyFollowList');
    
    // Merge and sort
    var items = [];
    (data.follow_logs || []).forEach(function(f) {
      items.push({
        type: 'follow',
        id: f.id,
        date: f.follow_date || '',
        customer_id: f.customer_id,
        customer: f.customer_company || f.customer_name || '客户',
        content: f.content || '',
        result: f.result || '',
        is_reported: f.is_reported || false,
        meta: (f.result || f.content || '').substring(0, 60),
      });
    });
    (data.outreach_logs || []).forEach(function(o) {
      items.push({
        type: 'outreach',
        id: o.id,
        date: o.sent_date || '',
        customer_id: o.customer_id,
        customer: o.customer_company || o.customer_name || '客户',
        content: o.subject || '',
        result: o.reply_status || '',
        is_reported: o.is_reported || false,
        meta: (o.content || '').substring(0, 60),
      });
    });
    items.sort(function(a, b) { return b.date.localeCompare(a.date); });
    
    if (items.length === 0) {
      el.innerHTML = '<div class="empty-state"><p>本周暂无跟进记录</p></div>';
      return;
    }
    
    var html = '';
    items.forEach(function(item) {
      var icon = item.type === 'follow' ? uiIcon('message') : uiIcon('mail');
      var iconClass = item.type === 'follow' ? 'follow' : 'outreach';
      var star = uiIcon('star');
      var starClass = item.is_reported ? 'reported' : '';
      var starTitle = item.is_reported ? '从本周工作中移除' : '加入本周工作';
      
      html += '<div class="dash-wl-item">';
      html += '<div class="dash-wl-icon ' + iconClass + '">' + icon + '</div>';
      html += '<div class="dash-wl-info">';
      html += '<div class="dash-wl-cust"><a href="javascript:void(0)" onclick="openEditModal(' + item.customer_id + ')">' + escapeHtml(item.customer) + '</a></div>';
      html += '<div class="dash-wl-text" title="' + escapeHtml(item.content) + '">' + (item.type === 'follow' ? renderRichText(item.meta || item.content) : escapeHtml(item.meta || item.content)) + '</div>';
      html += '<div class="dash-wl-date">' + formatDate(item.date) + '</div>';
      html += '</div>';
      html += '<div class="dash-wl-report"><button class="' + starClass + '" onclick="toggleReportFromDashboard(\'' + item.type + '\',' + item.id + ')" title="' + starTitle + '">' + star + '</button></div>';
      html += '</div>';
    });
    el.innerHTML = html;
  } catch(e) { console.error(e); }
}

async function toggleReportFromDashboard(type, id) {
  try {
    var url = type === 'follow'
      ? '/api/follow-history/' + id + '/report'
      : '/api/outreach/' + id + '/report';
    var res = await api(url, { method: 'POST' });
    showToast(res.is_reported ? '已加入本周工作' : '已从本周工作中移除', 'success');
    loadWeeklyFollowList();
  } catch(e) {}
}

async function batchCompleteToday() {
  try {
    var reminders = await api('/api/reminders/today');
    if (reminders.length === 0) { showToast('今日暂无任务', 'info'); return; }
    _batchCompleteTargets = reminders;
    _batchCompleteMode = 'today';
    document.getElementById('batchCompleteGroup').value = 'today';
    document.getElementById('batchCompleteTitle').textContent = '将完成今日 ' + reminders.length + ' 条跟进提醒';
    document.getElementById('batchCompleteResult').value = '';
    document.getElementById('batchCompleteNext').value = '';
    openModal('batchCompleteModal');
  } catch(e) {}
}

// ========== TODAY MULTI-SELECT ==========
function enterTodaySelectionMode(initialRow) {
  var list = document.getElementById('todayReminders');
  if (!list) return;
  list.classList.add('is-selection-mode');
  if (initialRow) {
    var cb = initialRow.querySelector('.today-task-checkbox');
    if (cb) cb.checked = true;
  }
  updateTodaySelection();
}

function exitTodaySelectionMode() {
  var list = document.getElementById('todayReminders');
  if (list) list.classList.remove('is-selection-mode');
  clearTodaySelection();
}

function updateTodaySelection() {
  selectedTodayCustomers.clear();
  document.querySelectorAll('#todayReminders .today-task-checkbox').forEach(function(cb) {
    var row = cb.closest('.today-task-row');
    if (cb.checked) {
      selectedTodayCustomers.add(parseInt(cb.dataset.id));
      if (row) row.classList.add('is-multi-selected');
    } else {
      if (row) row.classList.remove('is-multi-selected');
    }
  });
  var bar = document.getElementById('todayBatchBar');
  if (!bar) return;
  if (selectedTodayCustomers.size > 0) {
    bar.classList.add('show');
    document.getElementById('todayBatchCount').textContent = '已选 ' + selectedTodayCustomers.size + ' 项';
  } else {
    bar.classList.remove('show');
  }
}

function clearTodaySelection() {
  selectedTodayCustomers.clear();
  document.querySelectorAll('#todayReminders .today-task-checkbox').forEach(function(cb) { cb.checked = false; });
  document.querySelectorAll('#todayReminders .today-task-row.is-multi-selected').forEach(function(row) { row.classList.remove('is-multi-selected'); });
  var list = document.getElementById('todayReminders');
  if (list) list.classList.remove('is-selection-mode');
  var bar = document.getElementById('todayBatchBar');
  if (bar) bar.classList.remove('show');
}

async function batchExportTodayEmails() {
  if (selectedTodayCustomers.size === 0) { showToast('请先选择客户', 'warning'); return; }
  await doExportEmails(Array.from(selectedTodayCustomers));
}

function openTodayQuickEdit() {
  if (selectedTodayCustomers.size === 0) { showToast('请先选择客户', 'warning'); return; }
  document.getElementById('todayQuickEditTitle').textContent = '将编辑 ' + selectedTodayCustomers.size + ' 个客户';
  document.getElementById('todayQuickEditLevel').value = '';
  document.getElementById('todayQuickEditBusinessStage').value = '';
  document.getElementById('todayQuickEditNextFollowUp').value = '';
  document.getElementById('todayQuickEditActivityType').value = 'follow_up';
  document.getElementById('todayQuickEditDirection').value = 'unknown';
  document.getElementById('todayQuickEditContent').value = '';
  document.getElementById('todayQuickEditResult').value = '';
  openModal('todayQuickEditModal');
}

async function submitTodayQuickEdit() {
  if (!beginWrite('todayQuickEdit')) return;
  try {
    var ids = Array.from(selectedTodayCustomers);
    if (ids.length === 0) { closeModal('todayQuickEditModal'); return; }
    var level = document.getElementById('todayQuickEditLevel').value;
    var businessStage = document.getElementById('todayQuickEditBusinessStage').value;
    var nextFollowUp = document.getElementById('todayQuickEditNextFollowUp').value;
    var content = document.getElementById('todayQuickEditContent').value.trim();
    var result = document.getElementById('todayQuickEditResult').value.trim();
    var activityType = document.getElementById('todayQuickEditActivityType').value;
    var direction = document.getElementById('todayQuickEditDirection').value;
    if (!level && !businessStage && !nextFollowUp && !content) { showToast('请至少选择一项要修改的字段，或填写跟进内容', 'warning'); return; }
    try {
      if (level) {
        await api('/api/customers/batch/level', { method: 'POST', body: JSON.stringify({ ids: ids, value: level }) });
      }
      if (businessStage) {
        await api('/api/customers/batch/business-stage', { method: 'POST', body: JSON.stringify({ ids: ids, value: businessStage }) });
      }
      if (nextFollowUp) {
        await api('/api/customers/batch/next_follow_up', { method: 'POST', body: JSON.stringify({ ids: ids, value: nextFollowUp }) });
      }
      if (content) {
        await api('/api/customers/batch/follow_history', {
          method: 'POST',
          body: JSON.stringify({ ids: ids, content: content, result: result, activity_type: activityType, direction: direction })
        });
      }
      showToast('已更新 ' + ids.length + ' 个客户', 'success');
      closeModal('todayQuickEditModal', true);
      clearTodaySelection();
      loadDashboard();
    } catch(e) {}
  } finally {
    endWrite('todayQuickEdit');
  }
}

// ========== PERSONAL PREFERENCES ==========
var MODULE_LABELS = {
  ai_assistant: ['沟通整理', '按需总结沟通记录'],
  email_validation: ['邮箱验证', '邮箱检查与批量导入'],
  calendar_sync: ['日历同步', '完整日历与 Apple 日历'],
  weekly_overview: ['本周工作', '周度工作汇总'],
  outreach: ['开发邮件', '开发信与回复记录'],
  excel_import: ['Excel 导入', '历史表格导入与恢复']
};
var NAV_LABELS = { dashboard: '今天', inbox: 'Inbox', customers: '客户', overview: '本周工作' };
var CUSTOMER_COLUMN_LABELS = { country: '国家', type: '客户类型', field: '行业领域', level: '客户等级', last_activity: '最近发生', next_step: '下一步', website: '网站' };

function defaultUserPreferences() {
  var modules = {};
  Object.keys(MODULE_LABELS).forEach(function(key) { modules[key] = true; });
  return {
    modules: modules,
    nav_order: ['dashboard', 'inbox', 'customers', 'overview'],
    default_page: 'dashboard',
    customer_columns: ['country', 'last_activity', 'next_step', 'website'],
    saved_customer_views: [],
    font_size: 'standard',
    interface_performance: 'auto',
    performance_probe: null,
    inbox: { priority_silent_days: 45, regular_silent_days: 75, max_reactivation_items: 5 }
  };
}

var FONT_SIZE_CLASSES = { small: 'font-size-small', standard: 'font-size-standard', large: 'font-size-large', xl: 'font-size-xl' };
function applyFontSize(fontSize) {
  var html = document.documentElement;
  Object.values(FONT_SIZE_CLASSES).forEach(function(cls) { html.classList.remove(cls); });
  html.classList.add(FONT_SIZE_CLASSES[fontSize] || FONT_SIZE_CLASSES.standard);
}

async function loadUserPreferences() {
  try {
    userPreferences = await api('/api/preferences');
  } catch(e) {
    userPreferences = defaultUserPreferences();
  }
  var oldViews = [];
  try { oldViews = JSON.parse(localStorage.getItem('tradeos_saved_customer_views') || '[]'); } catch(e) {}
  if (oldViews.length && !(userPreferences.saved_customer_views || []).length) {
    userPreferences.saved_customer_views = oldViews;
    await persistUserPreferences(true);
    localStorage.removeItem('tradeos_saved_customer_views');
  }
  applyUserPreferences();
  startInitialPerformanceProbe();
  return userPreferences;
}

async function persistUserPreferences(silent) {
  try {
    var result = await api('/api/preferences', {
      method: 'PUT', skipGlobalSync: true,
      body: JSON.stringify(userPreferences || defaultUserPreferences())
    });
    userPreferences = result.preferences || userPreferences;
    applyUserPreferences();
    if (!silent) showToast('个人设置已保存', 'success');
    return true;
  } catch(e) { return false; }
}

function applyUserPreferences() {
  if (!userPreferences) userPreferences = defaultUserPreferences();
  var modules = userPreferences.modules || {};
  document.querySelectorAll('[data-module]').forEach(function(element) {
    element.classList.toggle('module-hidden', modules[element.dataset.module] === false);
  });
  document.querySelectorAll('[onclick*="analyzeCommunication"]').forEach(function(element) {
    element.classList.toggle('module-hidden', modules.ai_assistant === false);
  });
  document.querySelectorAll('.draft-email-import').forEach(function(element) {
    element.classList.toggle('module-hidden', modules.email_validation === false);
  });
  var navContainer = document.querySelector('.nav-personal');
  if (navContainer) {
    (userPreferences.nav_order || []).forEach(function(page) {
      var item = navContainer.querySelector('[data-nav-page="' + page + '"]');
      if (item) navContainer.appendChild(item);
    });
  }
  applyCustomerColumnVisibility();
  applyFontSize(userPreferences.font_size);
  applyInterfacePerformance(configuredInterfacePerformanceMode());
}

function performanceProbeStatusText(probe) {
  if (!probe || !probe.sampled_at) return '自动模式会参考设备状态与一次不到一秒的本地帧率测试；不会上传页面内容或客户数据。';
  var sampledAt = new Date(probe.sampled_at);
  var dateText = isNaN(sampledAt.getTime()) ? '已完成' : ('上次检测：' + sampledAt.toLocaleDateString());
  var ratio = Math.round(Number(probe.slow_ratio || 0) * 100);
  var result = probe.slow ? '检测到较多慢帧，自动模式会优先保证操作流畅。' : '当前设备可以保留完整视觉。';
  return dateText + ' · 慢帧 ' + ratio + '% · ' + result + ' 测试只在本机运行，不上传页面内容或客户数据。';
}

function previewInterfacePerformance(mode) {
  applyInterfacePerformance(mode);
}

function applyCustomerColumnVisibility() {
  if (!userPreferences) return;
  var visible = userPreferences.customer_columns || [];
  var workspace = document.getElementById('customerCardWorkspace');
  if (!workspace) return;
  var tracks = ['38px', 'minmax(250px, 1.8fr)'];
  var trackMap = { country: 'minmax(88px, .7fr)', type: 'minmax(88px, .72fr)', field: 'minmax(110px, .86fr)', level: '58px', last_activity: 'minmax(150px, 1fr)', next_step: 'minmax(178px, 1.2fr)', website: '66px' };
  Object.keys(CUSTOMER_COLUMN_LABELS).forEach(function(column) {
    var isVisible = visible.indexOf(column) >= 0;
    workspace.classList.toggle('customer-hide-' + column, !isVisible);
    if (isVisible) tracks.push(trackMap[column]);
  });
  tracks.push('116px');
  workspace.style.setProperty('--customer-grid-template', tracks.join(' '));
}

function renderPersonalSettings() {
  if (!userPreferences) userPreferences = defaultUserPreferences();
  var modules = userPreferences.modules || {};
  var moduleGrid = document.getElementById('moduleSettingsGrid');
  if (moduleGrid) moduleGrid.innerHTML = Object.keys(MODULE_LABELS).map(function(key) {
    var label = MODULE_LABELS[key];
    return '<label class="module-setting"><span><strong>' + label[0] + '</strong><small>' + label[1] + '</small></span><input type="checkbox" data-setting-module="' + key + '" ' + (modules[key] !== false ? 'checked' : '') + '></label>';
  }).join('');
  renderNavOrderSettings();
  var defaultPage = document.getElementById('preferenceDefaultPage');
  if (defaultPage) defaultPage.value = userPreferences.default_page || 'dashboard';
  var columnGrid = document.getElementById('customerColumnSettings');
  if (columnGrid) columnGrid.innerHTML = Object.keys(CUSTOMER_COLUMN_LABELS).map(function(key) {
    return '<label class="column-setting"><span>' + CUSTOMER_COLUMN_LABELS[key] + '</span><input type="checkbox" data-setting-column="' + key + '" ' + ((userPreferences.customer_columns || []).indexOf(key) >= 0 ? 'checked' : '') + '></label>';
  }).join('');
  var inbox = userPreferences.inbox || {};
  document.getElementById('preferencePrioritySilentDays').value = inbox.priority_silent_days || 45;
  document.getElementById('preferenceRegularSilentDays').value = inbox.regular_silent_days || 75;
  document.getElementById('preferenceMaxReactivation').value = inbox.max_reactivation_items || 5;
  var fontSize = document.getElementById('preferenceFontSize');
  if (fontSize) fontSize.value = userPreferences.font_size || 'standard';
  var performance = document.getElementById('preferenceInterfacePerformance');
  if (performance) performance.value = configuredInterfacePerformanceMode();
  var probeStatus = document.getElementById('performanceProbeStatus');
  if (probeStatus) probeStatus.textContent = performanceProbeStatusText(userPreferences.performance_probe);
}

function renderNavOrderSettings() {
  var container = document.getElementById('navOrderSettings');
  if (!container || !userPreferences) return;
  container.innerHTML = (userPreferences.nav_order || []).map(function(page, index) {
    return '<div class="nav-order-item"><span>' + (NAV_LABELS[page] || page) + '</span><button type="button" title="上移" aria-label="上移" onclick="movePreferenceNav(' + index + ',-1)">' + uiIcon('up') + '</button><button type="button" title="下移" aria-label="下移" onclick="movePreferenceNav(' + index + ',1)">' + uiIcon('down') + '</button></div>';
  }).join('');
}

function movePreferenceNav(index, delta) {
  var order = (userPreferences.nav_order || []).slice();
  var target = index + delta;
  if (target < 0 || target >= order.length) return;
  var item = order[index]; order[index] = order[target]; order[target] = item;
  userPreferences.nav_order = order;
  renderNavOrderSettings();
}

async function savePersonalSettings() {
  document.querySelectorAll('[data-setting-module]').forEach(function(input) {
    userPreferences.modules[input.dataset.settingModule] = input.checked;
  });
  userPreferences.customer_columns = Array.from(document.querySelectorAll('[data-setting-column]:checked')).map(function(input) { return input.dataset.settingColumn; });
  userPreferences.default_page = document.getElementById('preferenceDefaultPage').value || 'dashboard';
  if (userPreferences.modules.weekly_overview === false && userPreferences.default_page === 'overview') userPreferences.default_page = 'dashboard';
  var fontSizeSelect = document.getElementById('preferenceFontSize');
  if (fontSizeSelect) userPreferences.font_size = fontSizeSelect.value;
  var performanceSelect = document.getElementById('preferenceInterfacePerformance');
  if (performanceSelect) userPreferences.interface_performance = performanceSelect.value;
  userPreferences.inbox = {
    priority_silent_days: Number(document.getElementById('preferencePrioritySilentDays').value || 45),
    regular_silent_days: Number(document.getElementById('preferenceRegularSilentDays').value || 75),
    max_reactivation_items: Number(document.getElementById('preferenceMaxReactivation').value || 5)
  };
  if (await persistUserPreferences(false)) {
    renderPersonalSettings();
    refreshInboxBadge();
  }
}

var AI_CONFIG_PROVIDER_DEFAULTS = {
  auto: { label: '自动选择', base_url: '', model: '', local: false },
  deepseek: { label: 'DeepSeek', base_url: 'https://api.deepseek.com', model: 'deepseek-chat', local: false },
  qwen: { label: '通义千问', base_url: 'https://dashscope.aliyuncs.com/compatible-mode/v1', model: 'qwen-plus', local: false },
  glm: { label: '智谱 GLM', base_url: 'https://open.bigmodel.cn/api/paas/v4', model: 'glm-4-flash', local: false },
  openai: { label: 'OpenAI / 兼容接口', base_url: 'https://api.openai.com/v1', model: 'gpt-4o-mini', local: false },
  lmstudio: { label: 'LM Studio（本地）', base_url: 'http://localhost:1234', model: 'local-model', local: true },
  ollama: { label: 'Ollama（本地）', base_url: 'http://localhost:11434', model: 'qwen2.5:7b', local: true }
};
var _aiConfigStatus = null;
var _aiConfigBusy = false;

function aiConfigProviderInfo(provider) {
  provider = provider || 'auto';
  var fallback = AI_CONFIG_PROVIDER_DEFAULTS[provider] || AI_CONFIG_PROVIDER_DEFAULTS.auto;
  var current = (_aiConfigStatus && _aiConfigStatus.providers || []).find(function(item) {
    return item.id === provider;
  });
  return Object.assign({}, fallback, current || {});
}

function renderAiConfigStatus(status) {
  var container = document.getElementById('aiConfigStatus');
  if (!container || !status) return;
  var provider = status.active_backend_label || status.backend_label || '自动选择';
  var configured = status.configured;
  var state = configured ? '已就绪' : '尚未配置';
  var stateClass = configured ? 'is-ready' : 'is-pending';
  if (status.backend === 'auto') {
    var active = (status.providers || []).find(function(item) { return item.id === status.active_backend; });
    state = active ? (active.local ? '共用当前本地连接 · 请测试' : '共用当前连接 · 已就绪') : '等待配置';
    stateClass = active && !active.local ? 'is-ready' : 'is-pending';
  } else if (status.providers) {
    var selected = status.providers.find(function(item) { return item.id === status.backend; });
    if (selected && selected.local) {
      state = '本地服务 · 无需 API Key，请测试连接';
      stateClass = 'is-pending';
    }
  }
  var keyState = status.api_key_configured ? 'API Key 已保存（不会显示明文）' :
    (status.backend === 'auto' ? '当前共享连接无需显示 Key' : 'API Key 尚未配置');
  var visionState = status.vision_configured ? '截图识别复用当前模型（需支持图片）' : '截图识别等待共享接口';
  container.innerHTML = '<div class="ai-config-status-main"><span class="ai-config-status-dot ' + stateClass + '" aria-hidden="true"></span><strong>当前接口：' + escapeHtml(provider) + '</strong><span>' + escapeHtml(state) + '</span></div>' +
    '<div class="ai-config-status-meta"><span>' + escapeHtml(keyState) + '</span><span>' + escapeHtml(visionState) + '</span><span>来源：' + escapeHtml(status.config_source || '环境变量') + '</span></div>' +
    (!status.can_edit ? '<p class="settings-help ai-config-admin-note">只有管理员可以修改共享 AI 接口配置。</p>' : '');
}

function setAiModelStatus(message, type) {
  var status = document.getElementById('aiConfigModelStatus');
  if (!status) return;
  status.className = 'ai-config-model-status' + (type ? ' is-' + type : '');
  status.textContent = message || '';
}

function clearAiModelPicker() {
  var picker = document.getElementById('aiConfigModelPicker');
  if (!picker) return;
  picker.innerHTML = '<option value="">读取后可选择模型</option>';
  picker.value = '';
  picker.hidden = true;
  setAiModelStatus('');
}

function syncAiModelPicker() {
  var model = document.getElementById('aiConfigModel');
  var picker = document.getElementById('aiConfigModelPicker');
  if (!model || !picker || picker.hidden) return;
  var value = (model.value || '').trim();
  picker.value = Array.prototype.some.call(picker.options, function(option) {
    return option.value === value;
  }) ? value : '';
}

function chooseAiModelFromList() {
  var model = document.getElementById('aiConfigModel');
  var picker = document.getElementById('aiConfigModelPicker');
  if (model && picker && picker.value) model.value = picker.value;
}

function updateAiConfigProviderFields() {
  var backend = document.getElementById('aiConfigBackend');
  var apiKey = document.getElementById('aiConfigApiKey');
  var apiKeyField = document.getElementById('aiConfigApiKeyField');
  var baseUrl = document.getElementById('aiConfigBaseUrl');
  var model = document.getElementById('aiConfigModel');
  var modelButton = document.getElementById('aiConfigModelsButton');
  var modelPicker = document.getElementById('aiConfigModelPicker');
  var help = document.getElementById('aiConfigHelp');
  if (!backend || !apiKey || !baseUrl || !model) return;
  var provider = backend.value || 'auto';
  var info = aiConfigProviderInfo(provider);
  var isAuto = provider === 'auto';
  clearAiModelPicker();
  apiKey.disabled = isAuto || !!info.local;
  baseUrl.disabled = isAuto;
  model.disabled = isAuto;
  if (modelButton) modelButton.disabled = _aiConfigBusy || isAuto;
  if (modelPicker) modelPicker.disabled = _aiConfigBusy || isAuto;
  if (apiKeyField) apiKeyField.hidden = isAuto;
  apiKey.value = '';
  apiKey.placeholder = info.local ? '本地服务无需 API Key' :
    (info.api_key_configured ? '已配置，留空表示保留现有 Key' : '粘贴服务商 API Key');
  baseUrl.value = isAuto ? '' : (info.base_url || AI_CONFIG_PROVIDER_DEFAULTS[provider].base_url);
  model.value = isAuto ? '' : (info.model || AI_CONFIG_PROVIDER_DEFAULTS[provider].model);
  if (help) {
    help.textContent = isAuto
      ? '自动模式只会从已配置连接中选择一套共享连接；如需新增或替换 Key，请选择具体服务商。'
      : (info.local
        ? '本地服务不会离开当前设备；请先启动 ' + info.label.replace('（本地）', '') + '，再测试连接。'
        : 'API Key 只保存在服务端的独立权限文件中，页面不会回显明文；修改后立即对新的 AI 请求生效。');
  }
}

function renderAiConfigForm(status) {
  var form = document.getElementById('aiConfigForm');
  var backend = document.getElementById('aiConfigBackend');
  if (!form || !backend || !status) return;
  form.hidden = !status.can_edit;
  backend.value = AI_CONFIG_PROVIDER_DEFAULTS[status.backend] ? status.backend : 'auto';
  updateAiConfigProviderFields();
}

function aiConfigFormValue() {
  return {
    backend: (document.getElementById('aiConfigBackend') || {}).value || 'auto',
    api_key: ((document.getElementById('aiConfigApiKey') || {}).value || '').trim(),
    base_url: ((document.getElementById('aiConfigBaseUrl') || {}).value || '').trim(),
    model: ((document.getElementById('aiConfigModel') || {}).value || '').trim()
  };
}

async function loadAiModels() {
  var backend = document.getElementById('aiConfigBackend');
  var provider = backend && backend.value || 'auto';
  if (provider === 'auto') {
    setAiModelStatus('请先选择具体的 AI 服务商，再读取模型列表。', 'error');
    return;
  }
  var picker = document.getElementById('aiConfigModelPicker');
  var model = document.getElementById('aiConfigModel');
  clearAiModelPicker();
  setAiModelStatus('正在读取模型列表…');
  setAiConfigBusy(true);
  try {
    var result = await api('/api/ai/config/models', {
      method: 'POST', silentError: true, body: JSON.stringify(aiConfigFormValue())
    });
    if (!result || !result.success) throw new Error((result && result.error) || '读取模型列表失败');
    var models = Array.isArray(result.models) ? result.models : [];
    if (!picker || !models.length) throw new Error('接口没有返回可用模型，请手动填写模型名');
    var currentModel = model && (model.value || '').trim();
    models.forEach(function(item) {
      var id = typeof item === 'string' ? item : item && item.id;
      if (!id) return;
      var option = document.createElement('option');
      option.value = String(id);
      option.textContent = String(id);
      picker.appendChild(option);
    });
    picker.hidden = false;
    if (currentModel && Array.prototype.some.call(picker.options, function(option) {
      return option.value === currentModel;
    })) picker.value = currentModel;
    var suffix = result.truncated ? '，仅显示前 200 个' : '';
    setAiModelStatus('已读取 ' + models.length + ' 个模型' + suffix + '；可从下拉框选择，也可继续手动填写。', 'success');
  } catch (error) {
    if (picker) picker.hidden = true;
    setAiModelStatus((error && error.message) || '读取模型列表失败，请手动填写模型名。', 'error');
  } finally {
    setAiConfigBusy(false);
  }
}

function setAiConfigFeedback(message, type) {
  var feedback = document.getElementById('aiConfigFeedback');
  if (!feedback) return;
  feedback.className = 'ai-config-feedback' + (type ? ' is-' + type : '');
  feedback.textContent = message || '';
}

function setAiConfigBusy(busy) {
  _aiConfigBusy = !!busy;
  var backend = document.getElementById('aiConfigBackend');
  var isAuto = !backend || (backend.value || 'auto') === 'auto';
  if (backend) backend.disabled = _aiConfigBusy;
  document.querySelectorAll('#aiConfigForm button').forEach(function(button) {
    button.disabled = _aiConfigBusy || (button.id === 'aiConfigModelsButton' && isAuto);
  });
  var picker = document.getElementById('aiConfigModelPicker');
  if (picker) picker.disabled = _aiConfigBusy || isAuto;
}

async function loadAiConfig() {
  var status = document.getElementById('aiConfigStatus');
  try {
    var result = await api('/api/ai/config', { silentError: true });
    _aiConfigStatus = result || null;
    renderAiConfigStatus(_aiConfigStatus);
    renderAiConfigForm(_aiConfigStatus);
  } catch (error) {
    if (status) status.textContent = (error && error.message) || 'AI 配置暂时无法读取';
    var form = document.getElementById('aiConfigForm');
    if (form) form.hidden = true;
  }
}

async function testAiConfig() {
  setAiConfigFeedback('正在发送最小连接测试，不会携带客户资料…');
  setAiConfigBusy(true);
  try {
    var result = await api('/api/ai/config/test', {
      method: 'POST', silentError: true, body: JSON.stringify(aiConfigFormValue())
    });
    if (!result || !result.success) throw new Error((result && result.error) || '连接测试失败');
    setAiConfigFeedback('连接成功：' + (result.provider || '接口') + ' · ' + (result.model || '模型已响应'), 'success');
  } catch (error) {
    setAiConfigFeedback((error && error.message) || '连接测试失败，请检查配置', 'error');
  } finally {
    setAiConfigBusy(false);
  }
}

async function saveAiConfig() {
  setAiConfigFeedback('正在保存并刷新 AI 运行时配置…');
  setAiConfigBusy(true);
  try {
    var result = await api('/api/ai/config', {
      method: 'PUT', body: JSON.stringify(aiConfigFormValue())
    });
    _aiConfigStatus = Object.assign({}, result && result.config || {}, { can_edit: true });
    var key = document.getElementById('aiConfigApiKey');
    if (key) key.value = '';
    renderAiConfigStatus(_aiConfigStatus);
    renderAiConfigForm(_aiConfigStatus);
    setAiConfigFeedback('已保存，新的 AI 请求会立即使用这套配置。', 'success');
    showToast('AI API 配置已保存并生效', 'success');
  } catch (error) {
    setAiConfigFeedback((error && error.message) || 'AI 配置保存失败', 'error');
  } finally {
    setAiConfigBusy(false);
  }
}

async function clearAiConfig() {
  if (!await showAppConfirm({
    title: '清除快速接入配置',
    message: '只删除设置页保存的独立配置文件；环境文件中的 AI 配置不受影响。确定继续？',
    submitLabel: '清除配置', danger: true
  })) return;
  setAiConfigBusy(true);
  try {
    var result = await api('/api/ai/config', { method: 'DELETE' });
    _aiConfigStatus = Object.assign({}, result && result.config || {}, { can_edit: true });
    renderAiConfigStatus(_aiConfigStatus);
    renderAiConfigForm(_aiConfigStatus);
    setAiConfigFeedback('已清除设置页配置；如仍有环境变量，系统会继续使用环境变量。', 'success');
    showToast('快速接入配置已清除', 'success');
  } catch (error) {
    setAiConfigFeedback((error && error.message) || 'AI 配置清除失败', 'error');
  } finally {
    setAiConfigBusy(false);
  }
}

// ========== CUSTOMERS LIST ==========
var customerView = 'all';
var customerPage = 1;
var _customerSearchTimer = null;
var _customerLoadToken = 0;

function getCustomerSearchQuery() {
  var input = document.getElementById('globalPageSearch');
  return input ? input.value.trim() : '';
}

function setCustomerView(view) {
  customerView = view || 'all';
  customerPage = 1;
  document.querySelectorAll('.customer-view-chip').forEach(function(chip) {
    chip.classList.toggle('active', chip.dataset.view === customerView);
  });
  updateFilterIndicator(document.querySelector('.customer-view-chips'));
  loadCustomers();
}

function openUncontactedCustomers() {
  switchPage('customers');
  setCustomerView('uncontacted');
}

function getSavedCustomerViews() {
  return (userPreferences && userPreferences.saved_customer_views) || [];
}

function renderSavedCustomerViews() {
  var el = document.getElementById('savedCustomerViews');
  if (!el) return;
  var views = getSavedCustomerViews();
  el.innerHTML = views.map(function(view, index) {
    return '<button onclick="applySavedCustomerView(' + index + ')">' + escapeHtml(view.name) + '</button><button class="remove" title="删除视图" aria-label="删除视图" onclick="removeSavedCustomerView(' + index + ')">' + uiIcon('close') + '</button>';
  }).join('');
}

async function saveCurrentCustomerView() {
  var search = getCustomerSearchQuery();
  var suggested = search || ({uncontacted:'未获回复',communicated:'已有联系',waiting:'等待回复',silent:'长期未联系',no_next:'尚无下一步',data_quality:'资料待整理',archived:'已归档'}[customerView] || '我的客户视图');
  var name = await showAppPrompt({ title: '保存客户视图', message: '为这组筛选条件起一个方便回看的名字。', label: '视图名称', value: suggested, submitLabel: '保存' });
  if (!name) return;
  var views = getSavedCustomerViews();
  views.push({ name: name.trim(), view: customerView, search: search, filters: getCustomerFilterValues() });
  userPreferences.saved_customer_views = views;
  persistUserPreferences(true);
  renderSavedCustomerViews();
  showToast('视图已保存', 'success');
}

function applySavedCustomerView(index) {
  var view = getSavedCustomerViews()[index];
  if (!view) return;
  var globalSearch = document.getElementById('globalPageSearch');
  if (globalSearch) globalSearch.value = view.search || '';
  customerFilters = view.filters || {};
  setCustomerFilterInputs(customerFilters);
  setCustomerView(view.view || 'all');
}

function removeSavedCustomerView(index) {
  var views = getSavedCustomerViews();
  views.splice(index, 1);
  userPreferences.saved_customer_views = views;
  persistUserPreferences(true);
  renderSavedCustomerViews();
}

function toggleCustomerFilters() {
  var panel = document.getElementById('customerAdvancedFilters');
  if (panel) panel.hidden = !panel.hidden;
}

function getCustomerFilterValues() {
  var mapping = {
    country: 'filterCountry', business_role: 'filterBusinessRole', field: 'filterField', level: 'filterLevel',
    has_judgment: 'filterJudgment', next_state: 'filterNextState', days_min: 'filterDaysMin',
    days_max: 'filterDaysMax', last_from: 'filterLastFrom', last_to: 'filterLastTo', tag: 'filterTag',
    source: 'filterSource'
  };
  var filters = {};
  Object.keys(mapping).forEach(function(key) {
    var element = document.getElementById(mapping[key]);
    var value = element ? String(element.value || '').trim() : '';
    if (value) filters[key] = value;
  });
  return filters;
}

function setCustomerFilterInputs(filters) {
  var mapping = {
    country: 'filterCountry', business_role: 'filterBusinessRole', field: 'filterField', level: 'filterLevel',
    has_judgment: 'filterJudgment', next_state: 'filterNextState', days_min: 'filterDaysMin',
    days_max: 'filterDaysMax', last_from: 'filterLastFrom', last_to: 'filterLastTo', tag: 'filterTag',
    source: 'filterSource'
  };
  Object.keys(mapping).forEach(function(key) {
    var element = document.getElementById(mapping[key]);
    if (element) element.value = (filters || {})[key] || '';
  });
}

function applyCustomerFilters() {
  customerFilters = getCustomerFilterValues();
  customerPage = 1;
  loadCustomers();
}

function clearCustomerFilters() {
  customerFilters = {};
  setCustomerFilterInputs({});
  customerPage = 1;
  loadCustomers();
}

function renderCustomerActiveFilters(filters) {
  var element = document.getElementById('customerActiveFilters');
  if (!element) return;
  element.innerHTML = (filters || []).map(function(item) { return '<span>' + escapeHtml(item) + '</span>'; }).join('');
}

async function loadCustomers(options) {
  _pageDataLoadedAt = Date.now();
  options = options || {};
  var loadToken = ++_customerLoadToken;
  var requestedView = customerView;
  var scrollY = options.preservePosition ? window.scrollY : null;
  var search = getCustomerSearchQuery();
  var customerTable = document.getElementById('customerTableBody');
  setListPending(customerTable, true);
  try {
    renderSavedCustomerViews();
    var params = new URLSearchParams({ search: search, view: customerView, page: customerPage, per_page: customerView === 'priority' ? 100 : 30, sort: 'updated_at', order: 'desc' });
    var debugParams = new URLSearchParams(window.location.search);
    var debugRowCount = debugParams.has('motion_debug') ? Math.min(300, Math.max(0, Number(debugParams.get('motion_rows')) || 0)) : 0;
    if (debugRowCount) {
      params.set('page', '1');
      params.set('per_page', String(Math.min(100, debugRowCount)));
    }
    Object.keys(customerFilters || {}).forEach(function(key) { if (customerFilters[key] !== '') params.set(key, customerFilters[key]); });
    if (debugParams.get('motion_state') === 'customer_error') throw new Error('QA simulated customer failure');
    var data = await api('/api/customers?' + params.toString());
    if (debugRowCount > 100 && Number(data.total || 0) > 100) {
      var pageRequests = [];
      for (var debugPage = 2; debugPage <= Math.ceil(debugRowCount / 100); debugPage++) {
        var pageParams = new URLSearchParams(params);
        pageParams.set('page', String(debugPage));
        pageRequests.push(api('/api/customers?' + pageParams.toString()));
      }
      var extraPages = await Promise.all(pageRequests);
      extraPages.forEach(function(pageData) { data.customers = data.customers.concat(pageData.customers || []); });
      data.customers = data.customers.slice(0, debugRowCount);
      data.total = data.customers.length;
      data.page = 1;
      data.pages = 1;
    }
    if (loadToken !== _customerLoadToken || requestedView !== customerView) return;
    await renderCustomerTable('customerTableBody', data.customers, 'existing');
    initCustomerPrioritySorting();
    renderCustomerActiveFilters(data.interpreted_filters || []);
    renderCustomerPagination(data);
    applyCustomerColumnVisibility();
    if (scrollY !== null) requestAnimationFrame(function() { window.scrollTo(0, scrollY); });
  } catch(e) {
    if (loadToken === _customerLoadToken && customerTable) {
      customerTable.innerHTML = '<div class="empty-state list-error-state"><p>客户列表暂时无法加载</p><button class="btn btn-sm" type="button" onclick="loadCustomers()">重新加载</button></div>';
      renderCustomerPagination({ total: 0 });
    }
  } finally {
    if (loadToken === _customerLoadToken) {
      setListPending(customerTable, false);
      setTimeout(function() { updateFilterIndicator(document.querySelector('.customer-view-chips')); }, 60);
    }
  }
}

function renderCustomerPagination(data) {
  var el = document.getElementById('customerPagination');
  if (!el) return;
  var total = Number(data.total || 0), page = Number(data.page || 1), pages = Number(data.pages || 1);
  if (!total) { el.innerHTML = ''; return; }
  el.innerHTML = '<span>共 ' + total + ' 个客户 · 第 ' + page + '/' + pages + ' 页</span><div>' +
    '<button class="btn btn-sm" onclick="changeCustomerPage(-1)" ' + (page <= 1 ? 'disabled' : '') + '>上一页</button>' +
    '<button class="btn btn-sm" onclick="changeCustomerPage(1)" ' + (page >= pages ? 'disabled' : '') + '>下一页</button></div>';
}

function changeCustomerPage(delta) {
  customerPage = Math.max(1, customerPage + delta);
  loadCustomers();
  var page = document.getElementById('page-customers');
  if (page) page.scrollTo({ top: 0, behavior: 'smooth' });
}

function renderCustomerTable(tbodyId, customers, type) {
  var tbody = document.getElementById(tbodyId);
  if (!customers || customers.length === 0) {
    tbody.innerHTML = '<div class="empty-state"><p>暂未找到客户</p></div>';
    return;
  }
  if (type === 'existing') {
    customers.forEach(function(customer) {
      _customerSearchResultById[Number(customer.id)] = customer;
    });
  }
  var rows = [];
  var searchQuery = type === 'existing' ? getCustomerSearchQuery() : '';
  customers.forEach(function(c) {
    var selId = type === 'existing' ? 'custSel_' + c.id : 'newSel_' + c.id;
    var company = c.company || c.name || '未命名公司';
    var person = c.company && c.name && c.name !== c.company ? c.name : '';
    var hasContact = !!c.has_contact;
    var lastDate = c.last_contact || c.latest_outreach_date || '';
    var lastText = hasContact ? formatDate(lastDate) : '尚未获得客户回复';
    if (c.waiting_reply) lastText += ' · 等待回复';
    else if (c.days_since_contact !== null && c.days_since_contact >= 30) lastText += ' · ' + c.days_since_contact + '天未联系';
    var relationshipBadge = '<span class="customer-contact-state ' + (hasContact ? 'contacted' : 'uncontacted') + '">' + (hasContact ? '已有联系' : '未获回复') + '</span>';
    var updatedText = c.updated_at ? '<small class="customer-updated">更新于 ' + formatDate(c.updated_at) + '</small>' : '';
    var nextText = c.next_task_date ? (escapeHtml(c.next_task_title || '已安排') + '<small>' + formatDate(c.next_task_date) + '</small>') : '<span class="customer-no-next">尚未安排</span>';
    var tags = (c.tags || '').split(/[,，]/).map(function(tag){ return tag.trim(); }).filter(Boolean);
    var tagsHtml = tags.length ? '<div class="customer-tags">' + tags.slice(0, 4).map(function(tag){ return '<span>' + escapeHtml(tag) + '</span>'; }).join('') + '</div>' : '';
    var matchReasons = (c.match_reasons || []).length ? '<div class="customer-match-reasons">' + c.match_reasons.map(function(reason) { return '<span>' + escapeHtml(reason) + '</span>'; }).join('') + '</div>' : '';
    var issuesHtml = (c.data_quality_issues || []).length ? '<div class="customer-quality-issues">' + c.data_quality_issues.map(function(issue) { return '<span>' + escapeHtml(issue) + '</span>'; }).join('') + '</div>' : '';
    var matchContext = c.match_context || {};
    var matchContextDetail = [matchContext.content || matchContext.title || matchContext.contact_name, matchContext.date ? formatDate(matchContext.date) : ''].filter(Boolean).join(' · ');
    var matchContextHtml = matchContextDetail ? '<div class="customer-match-context"><span>' + highlightSearchText(matchContextDetail, searchQuery) + '</span></div>' : '';
    var matchContextAction = matchContext.type === 'inbox' && matchContext.action === 'record'
      ? '<button type="button" class="text-action customer-search-context-action" onclick="openCustomerSearchContext(' + c.id + ')">记录进展</button>' : '';
    var selected = type === 'existing' && selectedCustomers.has(Number(c.id));
    var isPinned = Number(c.is_pinned || 0) === 1;
    var selectCell = customerView === 'archived' ? '<div class="customer-select-cell"></div>' : '<div class="customer-select-cell"><input type="checkbox" class="table-checkbox" id="' + selId + '" data-id="' + c.id + '" data-type="' + type + '" onchange="updateSelection(\'' + type + '\')"' + (selected ? ' checked' : '') + '></div>';
    var dragHandle = '';
    var actions = customerView === 'archived'
      ? '<button class="customer-action" onclick="restoreArchivedCustomer(' + c.id + ')" title="恢复" aria-label="恢复">' + uiIcon('archive') + '</button>'
      : '<button class="customer-action" onclick="openEditModal(' + c.id + ')" title="查看与编辑" aria-label="查看与编辑">' + uiIcon('open') + '</button><button class="customer-action customer-action-archive" onclick="deleteCustomer(' + c.id + ')" title="归档" aria-label="归档">' + uiIcon('archive') + '</button>';
    var companyHtml = searchQuery ? highlightSearchText(company, searchQuery) : escapeHtml(company);
    rows.push({ id: c.id, html: '<article class="customer-row-card' + (isPinned ? ' customer-row-priority customer-light-on' : ' customer-light-off') + '" data-customer-id="' + c.id + '" tabindex="0">' +
      selectCell +
      '<div class="customer-company-cell" data-label="客户"><div class="customer-company-heading">' + dragHandle + customerLightControl(c.id, isPinned, 'customer-list-light') + '<button class="customer-name-button" onclick="openEditModal(' + c.id + ')">' + companyHtml + '</button></div>' + relationshipBadge + (person ? '<small>' + (searchQuery ? highlightSearchText(person, searchQuery) : escapeHtml(person)) + '</small>' : '') + updatedText + tagsHtml + issuesHtml + matchReasons + matchContextHtml + matchContextAction + '</div>' +
      '<div class="customer-muted" data-label="国家" data-customer-column="country">' + escapeHtml(c.country || '-') + '</div>' +
      '<div class="customer-muted" data-label="类型" data-customer-column="type">' + escapeHtml(c.type || '-') + '</div>' +
      '<div class="customer-muted" data-label="行业" data-customer-column="field">' + escapeHtml(c.field || c.industry || '-') + '</div>' +
      '<div class="customer-muted" data-label="等级" data-customer-column="level">' + escapeHtml(c.level || '-') + '</div>' +
      '<div class="customer-recent" data-label="最近发生" data-customer-column="last_activity">' + lastText + '</div>' +
      '<div class="customer-next" data-label="下一步" data-customer-column="next_step">' + nextText + '</div>' +
      '<div data-label="网站" data-customer-column="website">' + (c.website ? '<a href="' + escapeHtml(c.website) + '" target="_blank" rel="noopener" class="customer-website-action" title="' + escapeHtml(c.website) + '" aria-label="访问网站">' + uiIcon('external') + '</a>' : '<span class="customer-no-website">暂无</span>') + '</div>' +
      '<div class="customer-actions-cell" data-label="操作"><div class="customer-action-group">' + actions + '</div></div></article>' });
  });
  return reconcileKeyedElements(tbody, rows, {
    selector: '.customer-row-card',
    // Keep large priority lists responsive without stretching first paint
    // across dozens of animation frames. Constrained devices use smaller,
    // predictable batches; ordinary pages stay a single DOM update.
    chunkSize: isMotionLite() && rows.length > 20 ? 10 : (rows.length > 60 ? 30 : 0),
    key: function(row) { return 'customer-' + row.id; },
    render: function(row) { return row.html; }
  });
}

function openCustomerSearchContext(customerId) {
  var customer = _customerSearchResultById[Number(customerId)];
  if (customer && customer.match_context) {
    openSearchMatchContext(customer);
    return;
  }
  openEditModal(customerId);
}

async function updateCustomerPriority(customerId, action) {
  document.querySelectorAll('[data-customer-light="' + customerId + '"]').forEach(function(control) {
    control.classList.remove('is-pressing', 'is-turning-on', 'is-turning-off');
    void control.offsetWidth;
    control.classList.add('is-pressing', action === 'pin' ? 'is-turning-on' : 'is-turning-off');
  });
  try {
    await api('/api/customers/' + customerId + '/priority', {
      method: 'POST',
      body: JSON.stringify({ action: action })
    });
    var messages = { pin: '已标记客户', unpin: '已取消标记', up: '客户顺序已调整', down: '客户顺序已调整' };
    showToast(messages[action] || '已更新', 'success');
    setTimeout(function() {
      if (currentPage === 'customers') loadCustomers({ preservePosition: true });
      else if (currentPage === 'dashboard') loadDashboard();
      else if (currentPage === 'inbox') loadInbox();
    }, 360);
  } catch (e) {}
}

function customerLightControl(customerId, isLit, extraClass) {
  if (!customerId) return '';
  var action = isLit ? 'unpin' : 'pin';
  var label = isLit ? '取消标记客户' : '标记客户';
  var isTodayControl = (extraClass || '').indexOf('today-customer-light') >= 0;
  var title = isTodayControl ? label + '；按住拖动可调整今日顺序' : label;
  var clickAction = isTodayControl ? '' : ' onclick="event.stopPropagation(); updateCustomerPriority(' + Number(customerId) + ', \'' + action + '\')"';
  return '<button type="button" data-customer-light="' + Number(customerId) + '" class="customer-light-toggle ' + (isLit ? 'is-on' : '') + ' ' + (extraClass || '') + '" title="' + title + '" aria-label="' + title + '"' + clickAction + '><span class="customer-light-lens"><i class="customer-light-filament"></i></span></button>';
}

function customerPriorityIdsFromDom() {
  return Array.from(document.querySelectorAll('#customerTableBody .customer-row-priority')).map(function(row) {
    return Number(row.querySelector('.table-checkbox').dataset.id);
  });
}

async function persistCustomerPriorityOrder() {
  var ids = customerPriorityIdsFromDom();
  if (!ids.length) return;
  try {
    await api('/api/customers/priority/order', {
      method: 'POST',
      body: JSON.stringify({ ids: ids })
    });
    showToast('客户顺序已保存', 'success');
  } catch (e) {
    loadCustomers({ preservePosition: true });
  }
}

function initCustomerPrioritySorting() {
  return initCustomerCardSorting();
  var body = document.getElementById('customerTableBody');
  if (!body) return;
  function capturePositions() {
    var positions = {};
    body.querySelectorAll('.customer-row-priority:not([style*="display: none"])').forEach(function(item) {
      positions[item.querySelector('.table-checkbox').dataset.id] = item.getBoundingClientRect();
    });
    return positions;
  }
  function animateReflow(previous) {
    body.querySelectorAll('.customer-row-priority:not([style*="display: none"])').forEach(function(item) {
      var id = item.querySelector('.table-checkbox').dataset.id;
      var before = previous[id];
      if (!before) return;
      var now = item.getBoundingClientRect();
      var offset = before.top - now.top;
      if (!offset) return;
      item.style.transition = 'none';
      item.style.transform = 'translateY(' + offset + 'px)';
      requestAnimationFrame(function() {
        requestAnimationFrame(function() {
          item.style.transition = 'transform 190ms cubic-bezier(.2,.8,.2,1)';
          item.style.transform = '';
          setTimeout(function() { item.style.transition = ''; }, 210);
        });
      });
    });
  }
  body.querySelectorAll('.customer-row-priority').forEach(function(row) {
    var handle = row;
    handle.addEventListener('keydown', function(e) {
      if (e.key !== 'ArrowUp' && e.key !== 'ArrowDown') return;
      e.preventDefault();
      e.stopPropagation();
      var sibling = e.key === 'ArrowUp' ? row.previousElementSibling : row.nextElementSibling;
      if (!sibling || !sibling.classList.contains('customer-row-priority')) return;
      if (e.key === 'ArrowUp') body.insertBefore(row, sibling);
      else body.insertBefore(sibling, row);
      persistCustomerPriorityOrder();
      handle.focus();
    });
    handle.addEventListener('pointerdown', function(e) {
      if (e.button !== undefined && e.button !== 0) return;
      if (e.target.closest('button, input, textarea, select, a, label')) return;
      e.preventDefault();
      e.stopPropagation();
      if (handle.setPointerCapture) handle.setPointerCapture(e.pointerId);
      var initialIds = customerPriorityIdsFromDom();
      var rowRect = row.getBoundingClientRect();
      var placeholder = document.createElement('tr');
      placeholder.className = 'customer-priority-placeholder';
      placeholder.innerHTML = '<td colspan="' + row.children.length + '"></td>';
      placeholder.querySelector('td').style.height = rowRect.height + 'px';
      body.insertBefore(placeholder, row);
      var ghost = document.createElement('div');
      ghost.className = 'customer-priority-drag-ghost';
      ghost.style.left = Math.round(rowRect.left) + 'px';
      ghost.style.top = Math.round(rowRect.top) + 'px';
      ghost.style.width = Math.round(rowRect.width) + 'px';
      ghost.style.height = Math.round(rowRect.height) + 'px';
      var dragHeading = row.querySelector('.customer-company-heading').cloneNode(true);
      dragHeading.querySelectorAll('button').forEach(function(button) { button.removeAttribute('onclick'); button.tabIndex = -1; });
      ghost.appendChild(dragHeading);
      var dragHint = document.createElement('span');
      dragHint.className = 'customer-priority-drag-hint';
      dragHint.textContent = '调整关注顺序';
      ghost.appendChild(dragHint);
      document.body.appendChild(ghost);
      row.classList.add('is-dragging');
      row.style.display = 'none';
      document.body.classList.add('customer-is-sorting');
      var moveFrame = null;
      var pendingMove = null;
      function applyMove() {
        moveFrame = null;
        if (!pendingMove) return;
        var current = pendingMove;
        pendingMove = null;
        var target = document.elementFromPoint(current.clientX, current.clientY);
        target = target && target.closest('.customer-row-priority');
        if (!target || target.parentElement !== body) return;
        var rect = target.getBoundingClientRect();
        var reference = current.clientY < rect.top + rect.height / 2 ? target : target.nextSibling;
        if (reference === placeholder || placeholder.nextSibling === reference) return;
        var previous = capturePositions();
        body.insertBefore(placeholder, reference);
        animateReflow(previous);
      }
      function move(event) {
        if (event.pointerId !== e.pointerId) return;
        ghost.style.transform = 'translate3d(0,' + Math.round(event.clientY - e.clientY) + 'px,0) scale(1.006)';
        pendingMove = { clientX: event.clientX, clientY: event.clientY };
        if (!moveFrame) moveFrame = requestAnimationFrame(applyMove);
      }
      function finish(event) {
        if (event.pointerId !== e.pointerId) return;
        if (moveFrame) { cancelAnimationFrame(moveFrame); moveFrame = null; }
        if (pendingMove) applyMove();
        document.removeEventListener('pointermove', move);
        document.removeEventListener('pointerup', finish);
        document.removeEventListener('pointercancel', finish);
        if (handle.hasPointerCapture && handle.hasPointerCapture(e.pointerId)) handle.releasePointerCapture(e.pointerId);
        document.body.classList.remove('customer-is-sorting');
        var ghostRect = ghost.getBoundingClientRect();
        body.insertBefore(row, placeholder);
        placeholder.remove();
        ghost.remove();
        row.style.display = '';
        row.classList.remove('is-dragging');
        if (event.type === 'pointercancel') {
          var rowsById = {};
          body.querySelectorAll('.customer-row-priority').forEach(function(item) { rowsById[item.querySelector('.table-checkbox').dataset.id] = item; });
          initialIds.forEach(function(id) { if (rowsById[id]) body.appendChild(rowsById[id]); });
        }
        var landingRect = row.getBoundingClientRect();
        row.style.transition = 'none';
        row.style.transform = 'translate(' + (ghostRect.left - landingRect.left) + 'px,' + (ghostRect.top - landingRect.top) + 'px) scale(1.018)';
        requestAnimationFrame(function() {
          requestAnimationFrame(function() {
            row.classList.add('is-settling');
            row.style.transform = '';
            row.style.transition = '';
            setTimeout(function() { row.classList.remove('is-settling'); }, 300);
          });
        });
        var finalIds = customerPriorityIdsFromDom();
        if (event.type !== 'pointercancel' && finalIds.join(',') !== initialIds.join(',')) persistCustomerPriorityOrder();
      }
      document.addEventListener('pointermove', move);
      document.addEventListener('pointerup', finish);
      document.addEventListener('pointercancel', finish);
    });
  });
}

var _customerCardSortMoveHandler = null;
var _customerCardSortUpHandler = null;
var _customerCardSortCancelHandler = null;
var _cancelCustomerCardDrag = null;

function initCustomerCardSorting() {
  if (_cancelCustomerCardDrag) _cancelCustomerCardDrag();
  var list = document.getElementById('customerTableBody');
  if (!list) return;
  var drag = null;
  var pendingPress = null;
  var autoScrollFrame = null;

  function visibleRows() { return Array.from(list.querySelectorAll('.customer-row-priority')); }
  function capturePositions() {
    var positions = {};
    visibleRows().forEach(function(item) { positions[item.dataset.customerId] = item.getBoundingClientRect(); });
    return positions;
  }
  function animateReflow(previous) {
    visibleRows().forEach(function(item) {
      var before = previous[item.dataset.customerId];
      if (!before) return;
      var now = item.getBoundingClientRect();
      var offset = before.top - now.top;
      if (!offset) return;
      item.style.transition = 'none';
      item.style.transform = 'translateY(' + offset + 'px)';
      requestAnimationFrame(function() {
        requestAnimationFrame(function() {
          item.style.transition = 'transform 190ms cubic-bezier(.18,.9,.24,1.08)';
          item.style.transform = '';
          setTimeout(function() { item.style.transition = ''; }, 220);
        });
      });
    });
  }
  function updatePlaceholder(clientX, clientY) {
    if (!drag) return;
    var target = document.elementFromPoint(clientX, clientY);
    target = target && target.closest('.customer-row-priority');
    if (!target || target.parentElement !== list) return;
    var rect = target.getBoundingClientRect();
    var reference = clientY < rect.top + rect.height / 2 ? target : target.nextSibling;
    if (reference === drag.placeholder || drag.placeholder.nextSibling === reference) return;
    var previous = capturePositions();
    list.insertBefore(drag.placeholder, reference);
    animateReflow(previous);
  }
  function moveFloatingCard(clientY) {
    if (!drag) return;
    drag.lastClientY = clientY;
    drag.row.style.top = Math.round(clientY - drag.pointerOffsetY) + 'px';
    var edge = 72;
    drag.scrollVelocity = clientY < edge ? -Math.ceil((edge - clientY) / edge * 14) : (clientY > window.innerHeight - edge ? Math.ceil((clientY - (window.innerHeight - edge)) / edge * 14) : 0);
  }
  function runAutoScroll() {
    if (!drag) { autoScrollFrame = null; return; }
    if (drag.scrollVelocity) {
      window.scrollBy(0, drag.scrollVelocity);
      updatePlaceholder(drag.lastClientX, drag.lastClientY);
    }
    autoScrollFrame = requestAnimationFrame(runAutoScroll);
  }
  function beginDrag(row, event) {
    var rect = row.getBoundingClientRect();
    var computed = window.getComputedStyle(row);
    var workspace = list.closest('.customer-card-workspace');
    var gridTemplate = workspace ? window.getComputedStyle(workspace).getPropertyValue('--customer-grid-template').trim() : '';
    var initialIds = visibleRows().map(function(item) { return Number(item.dataset.customerId); });
    var placeholder = document.createElement('div');
    placeholder.className = 'customer-card-placeholder';
    placeholder.style.height = Math.round(rect.height) + 'px';
    list.insertBefore(placeholder, row);
    drag = {
      row: row,
      placeholder: placeholder,
      pointerId: event.pointerId,
      pointerOffsetY: event.clientY - rect.top,
      initialIds: initialIds,
      scrollVelocity: 0,
      lastClientX: event.clientX,
      lastClientY: event.clientY
    };
    row.classList.add('is-dragging');
    // The card is temporarily mounted on body. Preserve its inherited grid
    // definition before leaving the customer workspace so every column stays put.
    row.style.setProperty('--customer-grid-template', gridTemplate || computed.gridTemplateColumns);
    row.style.gridTemplateColumns = computed.gridTemplateColumns;
    row.style.columnGap = computed.columnGap;
    row.style.position = 'fixed';
    row.style.left = Math.round(rect.left) + 'px';
    row.style.top = Math.round(rect.top) + 'px';
    row.style.width = Math.round(rect.width) + 'px';
    row.style.height = Math.round(rect.height) + 'px';
    row.style.zIndex = '10000';
    document.body.appendChild(row);
    document.body.classList.add('customer-is-sorting');
    if (navigator.vibrate && event.pointerType !== 'mouse') navigator.vibrate(18);
    if (!autoScrollFrame) autoScrollFrame = requestAnimationFrame(runAutoScroll);
  }
  function finishDrag(cancelled) {
    if (!drag) return;
    var active = drag;
    drag = null;
    if (autoScrollFrame) { cancelAnimationFrame(autoScrollFrame); autoScrollFrame = null; }
    document.body.classList.remove('customer-is-sorting');
    var floatingRect = active.row.getBoundingClientRect();
    list.insertBefore(active.row, active.placeholder);
    active.placeholder.remove();
    active.row.removeAttribute('style');
    active.row.classList.remove('is-dragging');
    if (cancelled) {
      var byId = {};
      visibleRows().forEach(function(item) { byId[item.dataset.customerId] = item; });
      active.initialIds.forEach(function(id) { if (byId[id]) list.appendChild(byId[id]); });
    }
    var landingRect = active.row.getBoundingClientRect();
    active.row.style.transition = 'none';
    active.row.style.transform = 'translate(' + (floatingRect.left - landingRect.left) + 'px,' + (floatingRect.top - landingRect.top) + 'px) scale(1.018)';
    requestAnimationFrame(function() {
      requestAnimationFrame(function() {
        active.row.classList.add('is-settling');
        active.row.style.transform = '';
        active.row.style.transition = '';
        setTimeout(function() { active.row.classList.remove('is-settling'); active.row.removeAttribute('style'); }, 280);
      });
    });
    if (active.row.hasPointerCapture && active.row.hasPointerCapture(active.pointerId)) active.row.releasePointerCapture(active.pointerId);
    var finalIds = visibleRows().map(function(item) { return Number(item.dataset.customerId); });
    if (!cancelled && finalIds.join(',') !== active.initialIds.join(',')) persistCustomerPriorityOrder();
  }
  _cancelCustomerCardDrag = function() { pendingPress = null; if (drag) finishDrag(true); };

  list.querySelectorAll('.customer-row-priority').forEach(function(row) {
    if (row._customerSortCleanup) row._customerSortCleanup();
    var onRowKeydown = function(e) {
      if (e.key !== 'ArrowUp' && e.key !== 'ArrowDown') return;
      e.preventDefault();
      var sibling = e.key === 'ArrowUp' ? row.previousElementSibling : row.nextElementSibling;
      if (!sibling || !sibling.classList.contains('customer-row-priority')) return;
      if (e.key === 'ArrowUp') list.insertBefore(row, sibling); else list.insertBefore(sibling, row);
      persistCustomerPriorityOrder();
    };
    var onRowPointerdown = function(e) {
      if (e.button !== undefined && e.button !== 0) return;
      // On touch screens, a card belongs to page scrolling. Reordering remains
      // a desktop mouse/keyboard action so ordinary swipes never become drags.
      var touchLayout = window.matchMedia && window.matchMedia('(hover: none), (pointer: coarse)').matches;
      if (touchLayout || e.pointerType !== 'mouse') return;
      if (e.target.closest('button, input, textarea, select, a, label')) return;
      pendingPress = { row: row, pointerId: e.pointerId, startX: e.clientX, startY: e.clientY };
    };
    row.addEventListener('keydown', onRowKeydown);
    row.addEventListener('pointerdown', onRowPointerdown);
    row._customerSortCleanup = function() {
      row.removeEventListener('keydown', onRowKeydown);
      row.removeEventListener('pointerdown', onRowPointerdown);
      row._customerSortCleanup = null;
    };
  });
  if (_customerCardSortMoveHandler) document.removeEventListener('pointermove', _customerCardSortMoveHandler);
  if (_customerCardSortUpHandler) document.removeEventListener('pointerup', _customerCardSortUpHandler);
  if (_customerCardSortCancelHandler) document.removeEventListener('pointercancel', _customerCardSortCancelHandler);
  _customerCardSortMoveHandler = function(e) {
    if (pendingPress && pendingPress.pointerId === e.pointerId) {
      if (Math.abs(e.clientX - pendingPress.startX) + Math.abs(e.clientY - pendingPress.startY) > 7) {
        var press = pendingPress;
        pendingPress = null;
        e.preventDefault();
        beginDrag(press.row, e);
      }
    }
    if (!drag || drag.pointerId !== e.pointerId) return;
    e.preventDefault();
    drag.lastClientX = e.clientX;
    moveFloatingCard(e.clientY);
    updatePlaceholder(e.clientX, e.clientY);
  };
  _customerCardSortUpHandler = function(e) {
    if (pendingPress && pendingPress.pointerId === e.pointerId) { pendingPress = null; return; }
    if (drag && drag.pointerId === e.pointerId) { e.preventDefault(); finishDrag(false); }
  };
  _customerCardSortCancelHandler = function(e) {
    if (pendingPress && pendingPress.pointerId === e.pointerId) pendingPress = null;
    if (drag && drag.pointerId === e.pointerId) finishDrag(true);
  };
  document.addEventListener('pointermove', _customerCardSortMoveHandler, { passive: false });
  document.addEventListener('pointerup', _customerCardSortUpHandler);
  document.addEventListener('pointercancel', _customerCardSortCancelHandler);
}

async function restoreArchivedCustomer(id) {
  try {
    await api('/api/customers/' + id + '/restore', { method: 'POST' });
    showToast('客户已恢复', 'success');
    loadCustomers();
  } catch(e) {}
}

// Selection management
function updateSelection(type) {
  if (type === 'existing') {
    selectedCustomers.clear();
    document.querySelectorAll('#customerTableBody input[type=checkbox]:checked').forEach(function(cb) { selectedCustomers.add(parseInt(cb.dataset.id)); });
    var bar = document.getElementById('customerBatchBar');
    if (selectedCustomers.size > 0) { bar.classList.add('show'); document.getElementById('customerBatchCount').textContent = '已选 ' + selectedCustomers.size + ' 项'; }
    else { bar.classList.remove('show'); }
  } else {
    selectedNewPool.clear();
    document.querySelectorAll('#newPoolTableBody input[type=checkbox]:checked').forEach(function(cb) { selectedNewPool.add(parseInt(cb.dataset.id)); });
    var bar = document.getElementById('newPoolBatchBar');
    if (selectedNewPool.size > 0) { bar.classList.add('show'); document.getElementById('newPoolBatchCount').textContent = '已选 ' + selectedNewPool.size + ' 项'; }
    else { bar.classList.remove('show'); }
  }
}
function toggleAllCustomers(cb) { document.querySelectorAll('#customerTableBody input[type=checkbox]').forEach(function(c) { c.checked = cb.checked; }); updateSelection('existing'); }
function clearCustomerSelection() { selectedCustomers.clear(); document.getElementById('customerSelectAll').checked = false; document.querySelectorAll('#customerTableBody input[type=checkbox]').forEach(function(c) { c.checked = false; }); document.getElementById('customerBatchBar').classList.remove('show'); }
function clearNewPoolSelection() { selectedNewPool.clear(); document.querySelectorAll('.pool-cb').forEach(function(c) { c.checked = false; }); document.querySelectorAll('.pool-group thead input[type=checkbox]').forEach(function(c) { c.checked = false; }); document.getElementById('newPoolBatchBar').classList.remove('show'); }

// Batch operations
function batchSetLevelNew() { openBatchSetModal('level', 'new'); }
function batchSetStatusNew() { openBatchSetModal('business-stage', 'new'); }

function openBatchSetModal(field, type) {
  document.getElementById('batchSetField').value = field;
  document.getElementById('batchSetType').value = type;
  var sel = document.getElementById('batchSetValue');
  sel.innerHTML = '';
  if (field === 'level') {
    document.getElementById('batchSetTitle').textContent = '设置等级';
    document.getElementById('batchSetLabel').textContent = '等级';
    CUSTOMER_LEVEL_OPTIONS.forEach(function(v) { sel.innerHTML += '<option value="' + v + '">' + v + '</option>'; });
  } else {
    document.getElementById('batchSetTitle').textContent = '设置业务阶段';
    document.getElementById('batchSetLabel').textContent = '业务阶段';
    ['','成交','流失'].forEach(function(v) { sel.innerHTML += '<option value="' + v + '">' + (v || '未标记') + '</option>'; });
  }
  openModal('batchSetModal');
}

async function submitBatchSet() {
  if (!beginWrite('batchSet')) return;
  try {
    var field = document.getElementById('batchSetField').value;
    var type = document.getElementById('batchSetType').value;
    var value = document.getElementById('batchSetValue').value;
    var ids = type === 'existing' ? Array.from(selectedCustomers) : Array.from(selectedNewPool);
    if (ids.length === 0) { showToast('请选择要操作的项', 'warning'); return; }
    var endpoint = field === 'level' ? '/api/customers/batch/level' : '/api/customers/batch/business-stage';
    var button = document.getElementById('batchSetSubmit');
    var reset = setActionFeedback(button, 'pending', '更新中…');
    var actionId = beginActionStatus('batch-set', '正在批量更新…');
    try {
      await api(endpoint, { method: 'POST', body: JSON.stringify({ ids: ids, value: value }), silentError: true });
      setActionFeedback(button, 'success', '已更新');
      showToast('批量更新成功', 'success');
      finishActionStatus(actionId, 'success', '批量更新已保存');
      closeModal('batchSetModal', true);
      if (type === 'existing') { clearCustomerSelection(); loadCustomers(); }
      else { clearNewPoolSelection(); loadNewPool(); }
    } catch(e) {
      // 批量写入失败时保持弹窗打开，让用户可以直接重试，而不是误报成功。
      setActionFeedback(button, 'error', '更新失败');
      showToast('批量更新未保存，请重试', 'error');
      finishActionStatus(actionId, 'error', '批量更新未保存', function() { submitBatchSet(); });
      reset(1800);
    }
  } finally {
    endWrite('batchSet');
  }
}

async function batchDeleteCustomers() {
  if (!await showAppConfirm({ title: '归档客户', message: '将 ' + selectedCustomers.size + ' 个客户归档？之后仍可恢复。', submitLabel: '归档' })) return;
  try {
    await api('/api/customers/batch/delete', { method: 'POST', body: JSON.stringify({ ids: Array.from(selectedCustomers) }) });
    showToast('客户已归档', 'success');
    clearCustomerSelection(); loadCustomers();
  } catch(e) {}
}
async function batchDeleteNew() {
  if (!await showAppConfirm({ title: '归档选中客户', message: '确认归档 ' + selectedNewPool.size + ' 个选中客户？之后仍可恢复。', submitLabel: '归档' })) return;
  try {
    await api('/api/customers/batch/delete', { method: 'POST', body: JSON.stringify({ ids: Array.from(selectedNewPool) }) });
    showToast('批量删除成功', 'success');
    clearNewPoolSelection(); loadNewPool();
  } catch(e) {}
}

async function batchExportEmails() {
  if (selectedCustomers.size === 0) { showToast('请先选择客户', 'warning'); return; }
  await doExportEmails(Array.from(selectedCustomers));
}
async function batchExportEmailsNew() {
  if (selectedNewPool.size === 0) { showToast('请先选择客户', 'warning'); return; }
  await doExportEmails(Array.from(selectedNewPool));
}
async function doExportEmails(customerIds) {
  var emails = [];
  for (var i = 0; i < customerIds.length; i++) {
    try {
      var c = await api('/api/customers/' + customerIds[i]);
      (c.contacts || []).forEach(function(ct) { if (ct.email) emails.push(ct.email); });
    } catch(e) {}
  }
  if (emails.length === 0) { showToast('所选客户暂无联系人邮箱', 'info'); return; }
  var unique = emails.filter(function(v, i, a) { return a.indexOf(v) === i; });
  var text = unique.join(', ');
  try {
    await navigator.clipboard.writeText(text);
    showToast('已复制 ' + unique.length + ' 个邮箱到剪贴板', 'success');
  } catch(e) {
    await showAppPrompt({ title: '客户邮箱', message: '浏览器未能直接复制，邮箱已整理在下方。', label: '邮箱地址', value: text, readonly: true, submitLabel: '完成' });
  }
}

async function exportCurrentCustomerEmails() {
  var customerId = Number((document.getElementById('editCustomerId') || {}).value || 0);
  if (!customerId) {
    showToast('请先打开一位客户', 'warning');
    return;
  }
  await doExportEmails([customerId]);
}

async function exportGroupEmails(groupKey) {
  var search = document.getElementById('newPoolSearch').value;
  var level = document.getElementById('newPoolLevelFilter').value;
  var businessStage = document.getElementById('newPoolStageFilter').value;
  try {
    var params = new URLSearchParams({ view: 'uncontacted', search: search, level: level, business_stage: businessStage });
    var data = await api('/api/customers?' + params.toString());
    var customers = data.customers || [];
    var today = new Date().toISOString().split('T')[0];
    var tomorrow = new Date(Date.now() + 86400000).toISOString().split('T')[0];
    var dayAfter = new Date(Date.now() + 172800000).toISOString().split('T')[0];
    var weekEnd = getWeekEndDate();

    var ids = [];
    customers.forEach(function(c) {
      var nf = (c.next_follow_up || '').trim();
      var match = false;
      if (groupKey === 'overdue' && nf && nf < today) match = true;
      else if (groupKey === 'today' && nf === today) match = true;
      else if (groupKey === 'tomorrow' && nf === tomorrow) match = true;
      else if (groupKey === 'week' && nf >= dayAfter && nf <= weekEnd) match = true;
      else if (groupKey === 'none' && !nf) match = true;
      if (match) ids.push(c.id);
    });
    if (ids.length === 0) { showToast('该组暂无客户', 'info'); return; }
    await doExportEmails(ids);
  } catch(e) {}
}

// ========== NEW CLIENT POOL ==========
async function loadNewPool() {
  var search = document.getElementById('newPoolSearch').value;
  var level = document.getElementById('newPoolLevelFilter').value;
  var businessStage = document.getElementById('newPoolStageFilter').value;
  try {
    var params = new URLSearchParams({ view: 'uncontacted', search: search, level: level, business_stage: businessStage });
    var data = await api('/api/customers?' + params.toString());
    renderNewPoolGroups(data.customers || []);
  } catch(e) {}
}

function renderNewPoolGroups(customers) {
  var today = new Date().toISOString().split('T')[0];
  var tomorrow = new Date(Date.now() + 86400000).toISOString().split('T')[0];
  var dayAfter = new Date(Date.now() + 172800000).toISOString().split('T')[0];
  var weekEnd = getWeekEndDate();

  var groups = { overdue: [], today: [], tomorrow: [], week: [], none: [] };

  customers.forEach(function(c) {
    var nf = (c.next_follow_up || '').trim();
    if (!nf) { groups.none.push(c); return; }
    if (nf < today) { groups.overdue.push(c); }
    else if (nf === today) { groups.today.push(c); }
    else if (nf === tomorrow) { groups.tomorrow.push(c); }
    else if (nf >= dayAfter && nf <= weekEnd) { groups.week.push(c); }
    else { groups.none.push(c); }
  });

  renderPoolGroup('overdue', groups.overdue, true);
  renderPoolGroup('today', groups.today, true);
  renderPoolGroup('tomorrow', groups.tomorrow, true);
  renderPoolGroup('week', groups.week, true);
  renderPoolGroup('none', groups.none, false);
}

function getWeekEndDate() {
  var d = new Date();
  var day = d.getDay(); // 0=Sun, 1=Mon
  var offset = day === 0 ? 0 : 7 - day;
  var end = new Date(d.getTime() + offset * 86400000);
  return end.toISOString().split('T')[0];
}

function renderPoolGroup(key, customers, showBatch) {
  var countEl = document.getElementById('poolCount' + key.charAt(0).toUpperCase() + key.slice(1));
  var bodyEl = document.getElementById('poolBody' + key.charAt(0).toUpperCase() + key.slice(1));
  var groupEl = document.getElementById('poolGroup' + key.charAt(0).toUpperCase() + key.slice(1));
  if (countEl) countEl.textContent = customers.length;
  if (groupEl) groupEl.style.display = customers.length === 0 ? 'none' : 'block';

  if (!customers || customers.length === 0) {
    if (bodyEl) bodyEl.innerHTML = '<div class="empty-state"><p>暂无客户</p></div>';
    return;
  }

  var html = '<table class="pool-table"><thead><tr><th style="width:30px;"><input type="checkbox" class="table-checkbox" onchange="toggleAllPoolGroup(\'' + key + '\', this)"></th><th>客户</th><th>公司</th><th>国家</th><th>等级</th><th>状态</th><th>下次跟进</th><th>操作</th></tr></thead><tbody>';
  customers.forEach(function(c) {
    var selId = 'poolSel_' + c.id;
    var checked = selectedNewPool.has(c.id) ? ' checked' : '';
    var nf = c.next_follow_up || '';
    var dateClass = '';
    if (key === 'overdue') dateClass = 'overdue';
    if (key === 'today') dateClass = 'today';
    html += '<tr>';
    html += '<td><input type="checkbox" class="table-checkbox pool-cb" id="' + selId + '" data-id="' + c.id + '"' + checked + ' onchange="updatePoolSelection()"></td>';
    html += '<td><span class="cust-name">' + escapeHtml(c.name || '') + '</span></td>';
    html += '<td><span class="cust-company">' + escapeHtml(c.company || '') + '</span></td>';
    html += '<td>' + escapeHtml(c.country || '') + '</td>';
    html += '<td>' + (c.level ? levelBadge(c.level) : '') + '</td>';
    html += '<td>' + statusBadge(c.business_stage || '未标记') + '</td>';
    html += '<td class="next-date ' + dateClass + '">' + (nf || '-') + '</td>';
    html += '<td><button class="btn btn-sm" onclick="openEditModal(' + c.id + ')">编辑</button></td>';
    html += '</tr>';
  });
  html += '</tbody></table>';
  if (bodyEl) bodyEl.innerHTML = html;
}

function toggleAllPoolGroup(key, masterCb) {
  var bodyEl = document.getElementById('poolBody' + key.charAt(0).toUpperCase() + key.slice(1));
  if (!bodyEl) return;
  bodyEl.querySelectorAll('input.pool-cb').forEach(function(cb) {
    cb.checked = masterCb.checked;
    var id = parseInt(cb.dataset.id);
    if (masterCb.checked) selectedNewPool.add(id);
    else selectedNewPool.delete(id);
  });
  updatePoolSelection();
}

function updatePoolSelection() {
  selectedNewPool.clear();
  document.querySelectorAll('.pool-cb:checked').forEach(function(cb) { selectedNewPool.add(parseInt(cb.dataset.id)); });
  var bar = document.getElementById('newPoolBatchBar');
  if (selectedNewPool.size > 0) { bar.classList.add('show'); document.getElementById('newPoolBatchCount').textContent = '已选 ' + selectedNewPool.size + ' 项'; }
  else { bar.classList.remove('show'); }
}

async function batchCompleteGroup(groupKey) {
  var search = document.getElementById('newPoolSearch').value;
  var level = document.getElementById('newPoolLevelFilter').value;
  var businessStage = document.getElementById('newPoolStageFilter').value;
  try {
    var params = new URLSearchParams({ view: 'uncontacted', search: search, level: level, business_stage: businessStage });
    var data = await api('/api/customers?' + params.toString());
    var customers = data.customers || [];
    var today = new Date().toISOString().split('T')[0];
    var tomorrow = new Date(Date.now() + 86400000).toISOString().split('T')[0];
    var dayAfter = new Date(Date.now() + 172800000).toISOString().split('T')[0];
    var weekEnd = getWeekEndDate();

    var targets = [];
    customers.forEach(function(c) {
      var nf = (c.next_follow_up || '').trim();
      if (groupKey === 'overdue' && nf && nf < today) targets.push(c);
      if (groupKey === 'today' && nf === today) targets.push(c);
      if (groupKey === 'tomorrow' && nf === tomorrow) targets.push(c);
      if (groupKey === 'week' && nf >= dayAfter && nf <= weekEnd) targets.push(c);
    });

    if (targets.length === 0) { showToast('该组暂无客户', 'info'); return; }

    _batchCompleteTargets = targets;
    _batchCompleteMode = 'newpool';
    document.getElementById('batchCompleteGroup').value = groupKey;
    document.getElementById('batchCompleteTitle').textContent = '将完成 ' + targets.length + ' 个客户的跟进';
    document.getElementById('batchCompleteResult').value = '';
    document.getElementById('batchCompleteNext').value = '';
    openModal('batchCompleteModal');
  } catch(e) {}
}

async function submitBatchComplete() {
  if (!beginWrite('batchComplete')) return;
  try {
    var result = document.getElementById('batchCompleteResult').value.trim() || '继续跟进';
    var nextDate = document.getElementById('batchCompleteNext').value.trim();
    var targets = _batchCompleteTargets || [];
    if (targets.length === 0) { closeModal('batchCompleteModal'); return; }

    var today = new Date().toISOString().split('T')[0];
    var completed = 0;

    for (var i = 0; i < targets.length; i++) {
      try {
        var item = targets[i];
        if (_batchCompleteMode === 'newpool') {
          var cid = item.id;
          await api('/api/customers/' + cid + '/follow_history', {
            method: 'POST',
            body: JSON.stringify({ content: result, follow_date: today, result: result, next_plan: nextDate, is_reported: false })
          });
          if (nextDate) {
            await api('/api/customers/' + cid, { method: 'PUT', body: JSON.stringify({ next_follow_up: nextDate }) });
          }
        } else if (_batchCompleteMode === 'today') {
          await api('/api/reminders/' + item.id, { method: 'PUT', body: JSON.stringify({ result: result, next_follow_up: nextDate }) });
        }
        completed++;
      } catch(e) {}
    }
    closeModal('batchCompleteModal', true);
    showToast('已完成 ' + completed + ' 条跟进', 'success');
    if (_batchCompleteMode === 'newpool') loadNewPool();
    else loadDashboard();
    _batchCompleteTargets = [];
  } finally {
    endWrite('batchComplete');
  }
}

// ========== CUSTOMER EDIT MODAL ==========
var _customerDetailCache = null;
var _editingContactId = null;
var _editingContactOriginal = null;
var _customerDetailLoadToken = 0;
var _customerDetailLoadingId = null;
var _customerDetailController = null;
var _customerTimelinePage = 1;
var _customerTimelinePerPage = 3;
var _customerTimelineLoading = false;
var _customerSectionLoads = {};
var _customerWorkspaceCache = {};
var _customerWorkspaceCacheTtl = 15000;

// ---- 客户工作区 scope ----
// “哪个客户的工作区正开着”用 generation 表达：openEditModal 每次装载（新开或
// 重开同一客户）以及关闭工作区都会递增。异步操作在第一个 await 之前用
// beginCustomerScope() 捕获 scope，await 之后只允许通过 liveCustomerCache(scope)
// 触碰共享的工作区状态；期间切换、重开或关闭客户都会让 scope 失效，旧响应自然
// 被丢弃。逐点位的 id 比较（旧方案）有一个盲区：同一客户关闭后立刻重开时 id
// 相同，旧加载的响应仍会覆盖新一次加载的数据 —— generation 不受此影响。
var _customerScopeGeneration = 0;
function beginCustomerScope(customerId) {
  return {
    generation: _customerScopeGeneration,
    customerId: Number(customerId || (_customerDetailCache && _customerDetailCache.id) || 0)
  };
}
function customerScopeIsActive(scope) {
  return !!scope && !!_customerDetailCache &&
    scope.generation === _customerScopeGeneration &&
    Number(_customerDetailCache.id) === scope.customerId;
}

// await 之后触碰共享工作区状态的唯一合法入口：scope 已失效时返回 null，
// 调用方拿到 null 就直接丢弃这次响应。
function liveCustomerCache(scope) {
  return customerScopeIsActive(scope) ? _customerDetailCache : null;
}

function normalizeCustomerTimelineItems(items) {
  return (items || []).map(function(item) {
    if (item.type === 'outreach') {
      return Object.assign({}, item, {
        sent_date: item.sent_date || item.date || '',
        subject: item.subject || item.content || '开发邮件',
        content: item.content || '',
        reply_content: item.reply_content || item.result || ''
      });
    }
    return Object.assign({}, item, {
      follow_date: item.follow_date || item.date || ''
    });
  });
}

function updateCustomerWorkspaceIdentity(customer) {
  customer = customer || {};
  var title = document.getElementById('customerEditTitle');
  var meta = document.getElementById('customerWorkspaceMeta');
  if (title) title.textContent = customer.company || customer.name || '客户详情';
  if (!meta) return;
  var website = (customer.website || '').trim();
  var websiteUrl = website && !/^https?:\/\//i.test(website) ? 'https://' + website : website;
  var websiteHost = '';
  try { websiteHost = websiteUrl ? new URL(websiteUrl).hostname.replace(/^www\./, '') : ''; } catch (ignore) { websiteHost = website; }
  var contextMeta = [customer.country, customer.industry || customer.field, customer.owner ? '归属：' + customer.owner : ''].filter(Boolean).join(' · ');
  meta.innerHTML =
    (contextMeta ? '<span class="workspace-location">' + escapeHtml(contextMeta) + '</span>' : '') +
    (websiteUrl ? '<a class="workspace-website-link" href="' + escapeHtml(websiteUrl) + '" target="_blank" rel="noopener" title="在新窗口访问 ' + escapeHtml(websiteHost) + '">' +
      '<span>' + escapeHtml(websiteHost) + '</span><span class="workspace-site-arrow" aria-hidden="true">↗</span></a>' : '');
}

function setCustomerWorkspaceState(state, title, message, progress) {
  var modal = document.getElementById('customerEditModal');
  var loading = document.getElementById('customerWorkspaceLoading');
  var titleEl = document.getElementById('customerWorkspaceLoadingTitle');
  var messageEl = document.getElementById('customerWorkspaceLoadingMessage');
  var progressEl = document.getElementById('customerWorkspaceLoadingProgress');
  var retryButton = document.getElementById('customerWorkspaceRetryButton');
  if (!modal || !loading) return;
  var blocked = state === 'loading' || state === 'error';
  modal.classList.toggle('is-loading', blocked);
  modal.classList.toggle('is-error', state === 'error');
  modal.setAttribute('aria-busy', state === 'loading' ? 'true' : 'false');
  loading.hidden = !blocked;
  if (titleEl) titleEl.textContent = title || (state === 'error' ? '客户资料暂时无法加载' : '正在加载客户资料…');
  if (messageEl) messageEl.textContent = message || (state === 'error' ? '可以重试；当前不会覆盖你正在查看的其他客户。' : '正在读取客户资料和最近沟通。');
  if (progressEl) progressEl.style.width = Math.max(8, Math.min(100, Number(progress) || (state === 'error' ? 100 : 24))) + '%';
  if (retryButton) retryButton.hidden = state !== 'error';
}

function retryCustomerDetail() {
  var id = _customerDetailLoadingId || Number(document.getElementById('editCustomerId').value || 0);
  if (id) openEditModal(id);
}

function customerWorkspaceShellKey(customerId) {
  return 'tradeos.customer-shell.v1:' + String(currentUser && currentUser.id || '') + ':' + Number(customerId);
}

function readCustomerWorkspaceShell(customerId) {
  try {
    var cached = JSON.parse(sessionStorage.getItem(customerWorkspaceShellKey(customerId)) || 'null');
    return cached && cached.id === Number(customerId) ? cached : null;
  } catch (ignore) { return null; }
}

function writeCustomerWorkspaceShell(customer) {
  if (!customer || !customer.id) return;
  var shell = {
    id: Number(customer.id), name: customer.name || '', company: customer.company || '',
    country: customer.country || '', field: customer.field || customer.industry || '',
    website: customer.website || '', owner: customer.owner || ''
  };
  try { sessionStorage.setItem(customerWorkspaceShellKey(customer.id), JSON.stringify(shell)); } catch (ignore) {}
}

async function openEditModal(id) {
  id = Number(id);
  if (!id) return;
  var modal = document.getElementById('customerEditModal');
  if (!modal) return;
  _editingContactId = null;
  _editingContactOriginal = null;
  if (_customerDetailLoadingId === id && modal.classList.contains('is-loading') && !modal.classList.contains('is-error')) return;

  var requestToken = ++_customerDetailLoadToken;
  _customerDetailLoadingId = id;
  // A new workspace load supersedes every in-flight echo of the previous one —
  // including a reload of the same customer, whose id would be identical.
  _customerScopeGeneration++;
  if (_customerDetailController) _customerDetailController.abort();
  _customerDetailController = typeof AbortController === 'function' ? new AbortController() : null;
  _customerDetailCache = null;
  document.getElementById('customerEditTitle').textContent = '客户详情';
  document.getElementById('customerWorkspaceMeta').textContent = '正在读取资料…';
  var cachedShell = readCustomerWorkspaceShell(id);
  if (cachedShell) updateCustomerWorkspaceIdentity(cachedShell);
  setCustomerWorkspaceState('loading', '正在加载客户资料…', '先读取客户身份和最近沟通，联系人、文件与分析在打开页签时加载。', 24);
  openModal('customerEditModal');
  var detailTimedOut = false;
  var detailSlowNotice = setTimeout(function() {
    if (requestToken !== _customerDetailLoadToken || !_customerDetailController) return;
    setCustomerWorkspaceState('loading', '正在连接本地服务…', '响应时间较长，系统会自动重试，请先保持这个窗口打开。', 58);
  }, 4500);
  var detailTimeout = setTimeout(function() {
    if (requestToken !== _customerDetailLoadToken || !_customerDetailController) return;
    detailTimedOut = true;
    setCustomerWorkspaceState('error', '客户资料加载时间较长', '服务响应超过 15 秒。可以重试，或先关闭这个窗口。', 100);
    _customerDetailController.abort();
  }, 15000);

  try {
    var requestOptions = {
      signal: _customerDetailController ? _customerDetailController.signal : undefined,
      silentError: true,
      retryAttempts: 3,
      retryDelayMs: 500
    };
    var cachedWorkspace = _customerWorkspaceCache[id];
    var useCachedWorkspace = cachedWorkspace && (Date.now() - cachedWorkspace.savedAt < _customerWorkspaceCacheTtl);
    var parts = await Promise.all([
      useCachedWorkspace ? Promise.resolve(cachedWorkspace.summary) : api('/api/customers/' + id + '/summary', requestOptions),
      useCachedWorkspace ? Promise.resolve(cachedWorkspace.timeline) : api('/api/customers/' + id + '/timeline?page=1&per_page=' + _customerTimelinePerPage, requestOptions)
    ]);
    if (!useCachedWorkspace) _customerWorkspaceCache[id] = { savedAt: Date.now(), summary: parts[0], timeline: parts[1] };
    var c = parts[0] || {};
    var timelineItems = normalizeCustomerTimelineItems((parts[1] && parts[1].items) || []);
    if (!Array.isArray(c.recent_facts)) {
      c.recent_facts = timelineItems.slice(0, 3).map(function(item) {
        return {
          type: item.type, id: item.id, date: item.date || item.follow_date || item.sent_date || '',
          activity_type: item.activity_type || '', content: item.content || item.subject || '',
          result: item.result || item.reply_content || '', source: item.type === 'outreach' ? '开发邮件' : '沟通记录',
          source_detail: item.source || item.activity_type || ''
        };
      });
    }
    c.follow_history = timelineItems.filter(function(item) { return item.type === 'follow'; });
    c.outreach_emails = timelineItems.filter(function(item) { return item.type === 'outreach'; });
    c.timeline_items = timelineItems;
    c.timeline_pagination = (parts[1] && parts[1].pagination) || {};
    // The summary contains only the nearest task.  The complete task and
    // automatic-node lists are fetched only when their tab is opened.
    c.reminders = c.next_task ? [c.next_task] : [];
    c.tasks = null;
    c.contacts = null;
    c.files = null;
    if (requestToken !== _customerDetailLoadToken) return;
    if (!c || !c.id) throw new Error('客户资料为空');
    writeCustomerWorkspaceShell(c);
    document.getElementById('editCustomerId').value = c.id;
    updateCustomerWorkspaceIdentity(c);
    document.getElementById('editName').value = c.name || '';
    document.getElementById('editCompany').value = c.company || '';
    document.getElementById('editCountry').value = c.country || '';
    setCustomerLevelFieldValue(c.level || 'C');
    document.getElementById('editBusinessRole').value = c.business_role || '';
    document.getElementById('editField').value = c.field || '';
    document.getElementById('editBusinessStage').value = c.business_stage || '';
    document.getElementById('editSource').value = c.source || '';
    document.getElementById('editSourceDetail').value = c.source_detail || '';
    document.getElementById('editNextFollowUp').value = (c.next_follow_up || '').substring(0, 10);
    document.getElementById('editWebsite').value = c.website || '';
    document.getElementById('editTags').value = c.tags || '';
    document.getElementById('editProfile').value = c.profile || '';
    document.getElementById('editNotes').value = c.notes || '';
    ['contactName', 'contactTitle', 'contactEmail', 'contactPhone', 'contactWhatsapp', 'contactLinkedin',
     'bulkContactEmails', 'followHistoryContent', 'followHistoryResult', 'followHistoryNextTask', 'followHistoryNext']
      .forEach(function(fieldId) { var field = document.getElementById(fieldId); if (field) { if (field.isContentEditable) field.innerHTML = ''; else field.value = ''; } });
    document.getElementById('followHistoryDirectionOverride').value = 'auto';
    document.getElementById('followHistoryReport').checked = false;
    _communicationAnalyses.history = null;
    updateAutoDirectionPreview('history');
    var historyAnalysis = document.getElementById('followHistoryAnalysis');
    if (historyAnalysis) { historyAnalysis.hidden = true; historyAnalysis.innerHTML = ''; }
    var bulkValidation = document.getElementById('bulkContactEmailValidation');
    if (bulkValidation) { bulkValidation.hidden = true; bulkValidation.innerHTML = ''; }
    switchCustomerCompose('history');
    var communicationComposer = document.getElementById('followCompose');
    if (communicationComposer) communicationComposer.open = false;
    renderFollowTimeline(c.follow_history || [], c.outreach_emails || []);
    renderCustomerFactsBrief(c);
    renderCustomerNextTask(c.reminders || []);
    _customerDetailCache = c;
    _customerTimelinePage = 1;
    _customerTimelineLoading = false;
    _customerSectionLoads = {};
    renderCustomerTimelineMore(c.timeline_pagination);
    document.querySelectorAll('#customerEditModal .tab-btn').forEach(function(t, i) { t.classList.toggle('active', i === 0); });
    document.querySelectorAll('#customerEditModal .tab-content').forEach(function(t, i) { t.classList.toggle('active', i === 0); });

    // 基础资料到达后立即显示详情；文件、联系人和待办等次级区块按需加载。
    setCustomerWorkspaceState('ready');
    markModalClean('customerEditModal');
  } catch(e) {
    if (requestToken !== _customerDetailLoadToken || ((e && e.name === 'AbortError') && !detailTimedOut)) return;
    var detailErrorMessage = '网络或本地服务响应较慢。请点击“重新加载”再试一次。';
    if (detailTimedOut) detailErrorMessage = '服务响应超过 15 秒。请点击“重新加载”再试一次。';
    else if (e && e.status === 404) detailErrorMessage = '这条客户资料可能已被删除或当前数据源已变化。请刷新客户列表后再试。';
    else if (e && e.status >= 500) detailErrorMessage = '本地数据服务暂时繁忙或被系统占用。请点击“重新加载”，不会影响已保存的数据。';
    else if (e && e.kind === 'parse') detailErrorMessage = '收到的数据不完整，系统已自动重试仍未成功。请点击“重新加载”。';
    setCustomerWorkspaceState('error', '客户资料加载失败', detailErrorMessage, 100);
    showToast('客户资料加载失败，可以点击“重新加载”重试', 'error');
  } finally {
    clearTimeout(detailSlowNotice);
    clearTimeout(detailTimeout);
    if (requestToken === _customerDetailLoadToken) _customerDetailController = null;
  }
}

async function copyEmailsToClipboard(emails) {
  var unique = Array.from(new Set((emails || []).map(function(email) {
    return String(email || '').trim().toLowerCase();
  }).filter(Boolean)));
  if (!unique.length) { showToast('该客户没有可复制的邮箱', 'info'); return false; }
  var text = unique.join('\n');
  try {
    await navigator.clipboard.writeText(text);
    showToast('已复制 ' + unique.length + ' 个邮箱到剪贴板', 'success');
    return true;
  } catch (e) {
    await showAppPrompt({ title: '复制邮箱', message: '浏览器未能直接复制，请从下方复制。', label: '邮箱地址', value: text, readonly: true, submitLabel: '完成' });
    return false;
  }
}
function exportAllContacts() {
  window.location.href = '/api/contacts/export.csv';
}

function switchCustomerTab(tabId) {
  var btn = document.querySelector('#customerEditModal [data-customer-tab="' + tabId + '"]');
  if (btn) switchTab(btn, tabId);
}

function openCustomerFollowComposer() {
  var customerId = (document.getElementById('editCustomerId') || {}).value || '';
  var customerName = (document.getElementById('customerEditTitle') || {}).textContent || '当前客户';
  if (!customerId) { showToast('请先打开客户', 'warning'); return; }
  openCommunicationConfirm({ source: 'manual', sourceLabel: '客户工作区', customerId: customerId, customerName: customerName,
    direction: 'unknown', activityType: 'follow_up' });
}

function openCustomerTaskModal(mode) {
  var customerName = document.getElementById('customerEditTitle').textContent || '当前客户';
  var modal = document.getElementById('customerTaskModal');
  var currentTask = mode === 'create' ? null : (_customerDetailCache && (_customerDetailCache.reminders || [])[0]);
  var isEditing = !!(currentTask && currentTask.id);
  document.getElementById('customerTaskModalCustomer').textContent = customerName;
  document.getElementById('customerTaskModalTitle').textContent = isEditing ? '调整下一步' : '安排下一步';
  document.getElementById('customerTaskTitle').value = isEditing ? (currentTask.title || currentTask.content || '') : '';
  document.getElementById('customerTaskDate').value = isEditing ? (currentTask.remind_date || '').substring(0, 10) : '';
  // Bind this dialog to the customer it was opened for. The submit handler must
  // write to this captured id, never re-read editCustomerId at submit time —
  // otherwise a workspace switch while the dialog is open writes the next step
  // to whichever customer is on screen.
  if (modal) {
    modal.dataset.customerId = String((_customerDetailCache && _customerDetailCache.id) || document.getElementById('editCustomerId').value || '');
    modal.dataset.editTaskId = isEditing ? String(currentTask.id) : '';
  }
  var submit = document.getElementById('customerTaskSubmit');
  if (submit) {
    submit.textContent = isEditing ? '保存下一步' : '创建下一步';
    submit.dataset.editTaskId = isEditing ? String(currentTask.id) : '';
  }
  _agentTaskProposalId = null;
  document.querySelectorAll('#customerTaskModal .task-date-choices button').forEach(function(choice) {
    choice.classList.remove('is-selected');
    choice.setAttribute('aria-pressed', 'false');
  });
  openModal('customerTaskModal');
  setTimeout(function() { document.getElementById('customerTaskTitle').focus(); }, 0);
}

function setCustomerTaskDate(days, button) {
  var date = new Date();
  date.setDate(date.getDate() + days);
  var value = date.toISOString().slice(0, 10);
  document.getElementById('customerTaskDate').value = value;
  document.querySelectorAll('#customerTaskModal .task-date-choices button').forEach(function(choice) {
    choice.classList.toggle('is-selected', choice === button);
    choice.setAttribute('aria-pressed', choice === button ? 'true' : 'false');
  });
  if (button) {
    var original = button.dataset.actionLabel || button.textContent.trim();
    button.dataset.actionLabel = original;
    button.textContent = '已选 ' + formatChineseDate(value);
    setTimeout(function() { if (button.classList.contains('is-selected')) button.textContent = original; }, 1100);
  }
}

function renderCustomerNextTask(reminders) {
  var el = document.getElementById('customerNextTask');
  var quickActions = document.getElementById('customerTaskQuickActions');
  var otherEl = document.getElementById('customerOtherTasks');
  var actionButton = document.getElementById('customerTaskActionButton');
  var openTasks = reminders || [];
  var next = (reminders || [])[0];
  if (!next) {
    el.innerHTML = '<span class="workspace-muted">尚未安排下一步</span>';
    el.hidden = false;
    if (actionButton) actionButton.textContent = '安排下一步';
    if (quickActions) quickActions.hidden = true;
    if (otherEl) otherEl.innerHTML = '';
    return;
  }
  var title = next.title || next.content || '联系客户';
  el.innerHTML = '<strong>' + escapeHtml(title) + '</strong><span>' + formatChineseDate(next.remind_date) + '</span>' +
    (next.reason ? '<p>' + escapeHtml(next.reason) + '</p>' : '');
  el.hidden = false;
  if (actionButton) actionButton.textContent = '调整下一步';
  if (quickActions) quickActions.hidden = false;
  if (otherEl) {
    var others = openTasks.slice(1, 4);
    otherEl.innerHTML = others.length ? '<div class="workspace-kicker">其他未完成待办</div>' + others.map(function(task) {
      return '<button type="button" class="customer-other-task" onclick="switchCustomerTab(\'editTabTasks\')"><span>' + escapeHtml(task.title || task.content || '待办') + '</span><time>' + escapeHtml(formatChineseDate(task.remind_date || '')) + '</time></button>';
    }).join('') : '';
  }
}

function customerTaskMotionKey(task) {
  return task && task.id ? 'task-' + task.id : '';
}

function customerTaskHtml(task) {
  var overdue = isOverdue((task.remind_date || '').substring(0, 10));
  return '<article class="customer-task-row' + (overdue ? ' is-overdue' : '') + '">' +
    '<div><strong>' + escapeHtml(task.title || task.content || '待办') + '</strong>' +
    (task.reason ? '<p>' + escapeHtml(task.reason) + '</p>' : '') + '</div>' +
    '<time>' + escapeHtml(formatChineseDate(task.remind_date || '')) + '</time></article>';
}

function renderCustomerTaskEmpty(list) {
  var activeRows = list && list.querySelectorAll('.customer-task-row:not([data-motion-leaving])');
  var rows = list && list.querySelectorAll('.customer-task-row');
  if (list && rows && rows.length && shouldAnimateLists()) {
    var token = String(Date.now()) + '-' + Math.random();
    list.dataset.taskEmptyToken = token;
    if (activeRows && activeRows.length) reconcileKeyedElements(list, [], {
      selector: '.customer-task-row', leave: true,
      key: customerTaskMotionKey, render: customerTaskHtml
    });
    window.setTimeout(function() {
      if (list.dataset.taskEmptyToken !== token || list.querySelector('.customer-task-row')) return;
      delete list.dataset.taskEmptyToken;
      list.innerHTML = '<div class="customer-task-empty">暂无明确的未完成待办。可以在右侧安排下一步。</div>';
    }, 330);
    return;
  }
  delete list.dataset.taskEmptyToken;
  list.innerHTML = '<div class="customer-task-empty">暂无明确的未完成待办。可以在右侧安排下一步。</div>';
}

function renderCustomerTasks(tasks, changedKeys) {
  var list = document.getElementById('customerTasksList');
  if (!list) return;
  var explicit = tasks || [];
  list.classList.add('customer-task-list');
  if (!explicit.length) {
    renderCustomerTaskEmpty(list);
    return;
  }
  delete list.dataset.taskEmptyToken;
  var changed = {};
  (changedKeys || []).forEach(function(key) { if (key) changed[key] = true; });
  reconcileKeyedElements(list, explicit, {
    selector: '.customer-task-row',
    leave: true,
    key: customerTaskMotionKey,
    render: customerTaskHtml,
    changed: function(task, node, isNew) {
      if (isNew || changed[customerTaskMotionKey(task)]) markElementConfirmed(node);
    }
  });
}

function focusCustomerGap(tabId) {
  var target = tabId || 'editTabBasic';
  switchCustomerTab(target);
  setTimeout(function() {
    var field = target === 'editTabContacts' ? 'contactName' : target === 'editTabBasic' ? 'editField' : '';
    var input = field && document.getElementById(field);
    if (input && !input.disabled) input.focus({ preventScroll: true });
  }, 180);
}

function renderCustomerFactsBrief(customer) {
  var summary = document.getElementById('customerWorkspaceSummary');
  if (!summary) return;
  customer = customer || {};
  var primaryContact = customer.primary_contact || (customer.contacts || [])[0];
  var contactCount = Number(customer.contact_count || (customer.contacts || []).length || 0);
  var gaps = customer.information_gaps || [];
  var currentStatus = customer.current_judgment || {
    label: customer.customer_judgment || '未记录人工判断',
    source: customer.customer_judgment ? '用户记录' : '待确认'
  };
  var currentNext = customer.current_next_step || {};
  var nextTask = (customer.reminders || [])[0] || customer.next_task;
  var nextLabel = currentNext.label || (nextTask && (nextTask.title || nextTask.content)) || '未安排下一步';
  var nextDate = currentNext.date || (nextTask && nextTask.remind_date) || '';
  var waitingText = customer.customer_judgment || '未记录人工判断';
  var website = (customer.website || '').trim();
  var websiteUrl = website && !/^https?:\/\//i.test(website) ? 'https://' + website : website;
  var websiteHost = website;
  try { websiteHost = websiteUrl ? new URL(websiteUrl).hostname.replace(/^www\./, '') : ''; } catch (ignore) {}
  var contactChannels = primaryContact ? [primaryContact.email, primaryContact.phone, primaryContact.whatsapp].filter(Boolean) : [];
  var contactHtml = primaryContact
    ? '<div class="customer-fact-person"><strong>' + escapeHtml(primaryContact.name || primaryContact.email || '联系人') + '</strong>' +
      (primaryContact.title ? '<span>' + escapeHtml(primaryContact.title) + '</span>' : '') +
      (primaryContact.email ? '<a href="mailto:' + escapeHtml(primaryContact.email) + '">' + escapeHtml(primaryContact.email) + '</a>' : '') +
      (contactChannels.length > 1 ? '<small>' + escapeHtml(contactChannels.slice(1).join(' · ')) + '</small>' : '') +
      (contactCount > 1 ? '<small>共 ' + contactCount + ' 位联系人</small>' : '') +
    '</div>'
    : '<div class="customer-fact-empty">暂无联系人</div>';
  var recentFacts = (customer.recent_facts || []).slice(0, 3);
  var recentHtml = recentFacts.length ? recentFacts.slice(0, 1).map(function(fact) {
    var factText = fact.content || fact.subject || '已记录沟通';
    var sourceLabel = customerFactLabel(fact.type, fact.source_detail);
    return '<article class="customer-fact-event" data-fact-key="' + escapeHtml(customerTimelineKey(fact)) + '"><div class="customer-fact-event-meta"><span>' + escapeHtml(sourceLabel) + '</span><time>' + escapeHtml(formatChineseDate(fact.date || '')) + '</time></div>' +
      '<p>' + (fact.type === 'follow' ? renderRichText(factText) : escapeHtml(factText)) + '</p>' +
      (fact.result ? '<small><b>结果</b> ' + (fact.type === 'follow' ? renderRichText(fact.result) : escapeHtml(fact.result)) + '</small>' : '') +
    '</article>';
  }).join('') : '<div class="customer-fact-empty">暂无沟通记录</div>';
  var gapsHtml = gaps.length ? gaps.map(function(gap) {
    var gapLabel = gap.label || '资料待确认';
    var gapDetail = gap.detail || '需要人工确认';
    return '<button type="button" class="customer-gap-item" title="' + escapeHtml(gapDetail) + '" aria-label="' + escapeHtml(gapLabel + '：' + gapDetail) + '" onclick="focusCustomerGap(\'' + escapeHtml(gap.target || 'editTabBasic') + '\')"><span>' + escapeHtml(gapLabel) + '</span></button>';
  }).join('') : '<div class="customer-gap-empty">暂无缺口</div>';
  var customerLevel = customerLevelForDisplay(customer.level);
  var agentProspect = customer.agent_prospect || null;
  var agentResearchHtml = '';
  if (agentProspect) {
    var researchTags = [agentProspect.qualification_status, agentProspect.research_status, agentProspect.confidence].filter(Boolean).join(' · ');
    var researchText = agentProspect.reason || agentProspect.research_reason || 'sela 已建立该客户的研究资料。';
    var researchSources = (agentProspect.source_urls || []).filter(function(url) {
      return /^https?:\/\//i.test(String(url || ''));
    }).slice(0, 3).map(function(url) {
      var label = String(url).replace(/^https?:\/\//i, '').replace(/\/$/, '');
      return '<a href="' + escapeHtml(url) + '" target="_blank" rel="noopener">' + escapeHtml(label) + '</a>';
    }).join(' · ');
    var isDnc = agentProspect.contact_permission === 'do_not_contact';
    var permission = isDnc
      ? '已停止联系' + (agentProspect.suppression_reason ? '：' + agentProspect.suppression_reason : '')
      : '';
    var exclusionReview = agentProspect.exclusion_review || null;
    var exclusionReviewHtml = exclusionReview
      ? '<small class="customer-fact-files">排除身份待确认：可能与「' + escapeHtml(exclusionReview.canonical_name || exclusionReview.matched_value || '历史记录') + '」相同；当前仅名称相似。</small><div><button type="button" class="text-action" onclick="resolveCustomerSelaExclusionReview(\'accept\')">确认是新主体</button><button type="button" class="text-action" onclick="resolveCustomerSelaExclusionReview(\'reject\')">确认同一主体并停止联系</button></div>'
      : '';
    agentResearchHtml = '<section class="customer-fact-section customer-fact-context"><div class="customer-fact-section-head"><span class="customer-fact-label">sela 研究</span><span class="customer-fact-gap-count">' + escapeHtml(researchTags || '已导入') + '</span></div><p class="customer-fact-context-copy">' + renderRichText(researchText.slice(0, 720)) + '</p>' +
      (agentProspect.angle ? '<small class="customer-fact-files">沟通角度：' + escapeHtml(agentProspect.angle) + '</small>' : '') +
      (permission ? '<small class="customer-fact-files">联系权限：' + escapeHtml(permission) + '</small><div><button type="button" class="text-action" onclick="setCustomerContactPermission(\'allowed\')">恢复联系（人工解禁）</button></div>' : '') +
      exclusionReviewHtml +
      (researchSources ? '<small class="customer-fact-files">公开来源：' + researchSources + '</small>' : '') +
    '</section>';
  }
  summary.innerHTML =
    '<div class="customer-facts-brief-head"><div><span class="workspace-kicker">客户当前工作</span><span class="customer-facts-brief-hint">先看最近发生什么，再决定下一步</span></div></div>' +
    '<div class="customer-now-next-grid">' +
      '<section class="customer-fact-section customer-fact-now"><div class="customer-fact-section-head"><div><span class="customer-fact-label">现在</span><span class="customer-fact-label-sub">最近一次重要沟通</span></div><button type="button" class="text-action" onclick="switchCustomerTab(\'editTabOutreach\')">时间线</button></div><div class="customer-fact-events">' + recentHtml + '</div><div class="customer-now-waiting"><span>当前等待</span><strong>' + escapeHtml(waitingText) + '</strong><button type="button" class="text-action" onclick="editCustomerWaiting()">调整</button></div></section>' +
      '<section class="customer-fact-section customer-fact-next"><div class="customer-fact-section-head"><div><span class="customer-fact-label">下一步</span><span class="customer-fact-label-sub">最需要执行的一个动作</span></div><button type="button" class="text-action" onclick="switchCustomerTab(\'editTabTasks\')">全部待办</button></div><strong class="customer-next-title">' + escapeHtml(nextLabel) + '</strong>' + (nextDate ? '<time class="customer-next-date">' + escapeHtml(formatChineseDate(nextDate)) + '</time>' : '<span class="customer-next-date">尚未安排日期</span>') + '</section>' +
    '</div>' +
    '<details class="customer-secondary-info"><summary><span>资料与历史信息</span><span class="customer-secondary-hint">' + (gaps.length ? gaps.length + ' 项待确认 · ' : '') + '联系人、客户资料、状态与信息缺口</span></summary><div class="customer-secondary-grid">' +
      '<section class="customer-fact-section customer-fact-identity"><div class="customer-fact-section-head"><span class="customer-fact-label">客户身份</span><button type="button" class="text-action" onclick="switchCustomerTab(\'editTabBasic\')">资料</button></div>' +
        '<strong class="customer-fact-company">' + escapeHtml(customer.company || customer.name || '未记录公司名称') + '</strong>' +
        (customer.name && customer.company && customer.name !== customer.company ? '<span class="customer-fact-subline">客户名称：' + escapeHtml(customer.name) + '</span>' : '') +
        '<div class="customer-fact-values"><span><b>国家/地区</b>' + escapeHtml(customer.country || '未记录') + '</span><span><b>行业/领域</b>' + escapeHtml(customer.industry || customer.field || '未记录') + '</span><span><b>来源</b>' + escapeHtml([customer.source, customer.source_detail].filter(Boolean).join(' · ') || '未记录') + '</span><label class="customer-level-quick" title="点击等级即可直接输入修改"><b>等级</b><span class="customer-level-quick-control"><input class="customer-level-quick-input" type="text" inputmode="text" autocapitalize="characters" aria-label="客户等级，输入 A/B/C/D，可选加号或减号" value="' + escapeHtml(customerLevel) + '" data-current-level="' + escapeHtml(customerLevel) + '" onchange="quickUpdateCustomerLevel(this)"><span class="customer-level-quick-affordance" aria-hidden="true"><span class="ui-icon ui-icon-edit"></span></span><span class="customer-level-quick-feedback" aria-live="polite"></span></span></label><span class="customer-fact-website"><b>网站</b>' + (websiteUrl ? '<a href="' + escapeHtml(websiteUrl) + '" target="_blank" rel="noopener">' + escapeHtml(websiteHost) + '</a>' : '未记录') + '</span></div>' +
      '</section>' +
      '<section class="customer-fact-section customer-fact-contact"><div class="customer-fact-section-head"><span class="customer-fact-label">沟通对象</span><button type="button" class="text-action" onclick="switchCustomerTab(\'editTabContacts\')">' + (primaryContact ? '查看' : '添加') + '</button></div>' + contactHtml + '</section>' +
      '<section class="customer-fact-section customer-fact-status"><div class="customer-fact-section-head"><span class="customer-fact-label">客户状态</span><button type="button" class="text-action" onclick="editCustomerWaiting()">编辑等待</button></div><div class="customer-fact-status-grid"><div><span>关系状态</span><strong>' + escapeHtml(currentStatus.label || '未记录') + '</strong></div><div><span>当前等待</span><strong>' + escapeHtml(waitingText) + '</strong></div><div><span>下一步</span><strong>' + escapeHtml(nextLabel) + '</strong>' + (nextDate ? '<time>' + escapeHtml(formatChineseDate(nextDate)) + '</time>' : '') + '</div></div></section>' +
      '<section class="customer-fact-section customer-fact-context"><div class="customer-fact-section-head"><span class="customer-fact-label">当前上下文</span><span class="customer-fact-gap-count">关键需求：' + escapeHtml(customer.field || customer.industry || '待补充') + '</span></div><p class="customer-fact-context-copy">' + renderRichText((customer.profile || customer.notes || '尚未记录当前背景；可在资料中补充。').slice(0, 360)) + '</p><small class="customer-fact-files">相关资料：' + Number(customer.file_count || 0) + ' 个文件</small></section>' +
      agentResearchHtml +
      '<section class="customer-fact-section customer-fact-gaps"><div class="customer-fact-section-head"><span class="customer-fact-label">信息缺口</span><span class="customer-fact-gap-count">' + (gaps.length ? gaps.length + ' 项待确认' : '基础字段完整') + '</span></div><div class="customer-gap-list">' + gapsHtml + '</div></section>' +
    '</div></details>';
}

async function resolveCustomerSelaExclusionReview(decision) {
  var customer = _customerDetailCache || {};
  var customerId = Number(customer.id || (document.getElementById('editCustomerId') || {}).value || 0);
  var review = (customer.agent_prospect || {}).exclusion_review;
  if (!customerId || !review) { showToast('没有待确认的 sela 排除身份', 'info'); return; }
  var accepting = decision === 'accept';
  if (!await showAppConfirm({
    title: accepting ? '确认是新主体' : '确认同一主体并停止联系',
    message: accepting
      ? '这会保留客户并记录“与历史名称不是同一主体”的决定。'
      : '这会在 Trosa 记录停止联系，并阻止 sela 后续外联。',
    submitLabel: accepting ? '确认新主体' : '停止联系', danger: !accepting,
  })) return;
  var note = await showAppPrompt({
    title: '补充决定依据（可选）', message: '这条说明会和决定一起保存在 Trosa。',
    label: '依据', value: '', submitLabel: '保存决定',
  });
  if (note === null) return;
  try {
    await api('/api/customers/' + customerId + '/agent-prospect/exclusion-decision', {
      method: 'POST', body: { decision: accepting ? 'accept' : 'reject', note: note }
    });
    delete _customerWorkspaceCache[customerId];
    await openEditModal(customerId);
    showToast('排除身份决定已保存到 Trosa', 'success');
  } catch (e) {
    showToast((e && e.message) || '保存排除身份决定失败', 'error');
  }
}

async function setCustomerContactPermission(permission) {
  var customer = _customerDetailCache || {};
  var customerId = Number(customer.id || (document.getElementById('editCustomerId') || {}).value || 0);
  if (!customerId) { showToast('缺少客户 ID', 'error'); return; }
  var unblocking = permission === 'allowed';
  if (!await showAppConfirm({
    title: unblocking ? '恢复联系（人工解禁）' : '停止联系',
    message: unblocking
      ? '这会解除 DNC 并恢复 sela 跟进资格。请确认已核实误标或客户已同意恢复联系。'
      : '这会在 Trosa 记录停止联系，并阻止 sela 后续外联。',
    submitLabel: unblocking ? '确认恢复联系' : '停止联系', danger: !unblocking,
  })) return;
  var note = await showAppPrompt({
    title: '填写原因（必填）', message: '解禁/停止联系的原因会写入审计记录。',
    label: '原因', value: '', submitLabel: '保存',
  });
  if (note === null) return;
  if (String(note).trim().length < 2) { showToast('请填写至少 2 个字的原因', 'error'); return; }
  try {
    await api('/api/customers/' + customerId + '/agent-prospect/contact-permission', {
      method: 'POST', body: { permission: permission, note: note }
    });
    delete _customerWorkspaceCache[customerId];
    await openEditModal(customerId);
    showToast(unblocking ? '已恢复联系，sela 可继续跟进' : '已记录停止联系', 'success');
  } catch (e) {
    showToast((e && e.message) || '保存联系权限失败', 'error');
  }
}

async function refreshCustomerWorkspace() {
  var customerId = document.getElementById('editCustomerId').value;
  if (!customerId) return;
  var scope = beginCustomerScope(customerId);
  var parts = await Promise.all([
    api('/api/customers/' + customerId + '/summary'),
    api('/api/customers/' + customerId + '/tasks')
  ]);
  var cache = liveCustomerCache(scope);
  if (!cache) return;
  var summary = parts[0] || {};
  var tasks = parts[1] || {};
  var customer = cache;
  Object.keys(summary).forEach(function(key) { customer[key] = summary[key]; });
  customer.reminders = tasks.tasks || summary.reminders || [];
  customer.tasks = customer.reminders;
  _customerDetailCache = customer;
  updateCustomerWorkspaceIdentity(customer);
  renderCustomerFactsBrief(customer);
  renderCustomerNextTask(customer.reminders || []);
  renderCustomerTasks(customer.tasks);
  document.getElementById('editNextFollowUp').value = customer.next_follow_up || '';
  if (_customerWorkspaceCache[customerId]) {
    _customerWorkspaceCache[customerId].summary = summary;
    _customerWorkspaceCache[customerId].savedAt = Date.now();
  }
}

async function copyCustomerContext(mode) {
  var customerId = document.getElementById('editCustomerId').value;
  if (!customerId) return;
  try {
    var result = await api('/api/customers/' + customerId + '/context?mode=' + encodeURIComponent(mode || 'compact'));
    await navigator.clipboard.writeText(result.content || '');
    showToast('全部沟通已导出，可直接粘贴到外部模型', 'success');
  } catch (e) {
    showToast('复制上下文失败：' + (e.message || '请稍后重试'), 'error');
  }
}

async function editCustomerWaiting() {
  var customerId = document.getElementById('editCustomerId').value;
  var scope = beginCustomerScope(customerId);
  var current = (_customerDetailCache && _customerDetailCache.customer_judgment) || '';
  var waiting = await showAppPrompt({ title: '更新当前等待', message: '写下正在等待的回复、文件或确认；留空即可清除。', label: '当前等待', value: current, submitLabel: '保存' });
  if (waiting === null) return;
  var cache = liveCustomerCache(scope);
  var previous = cache ? (cache.customer_judgment || '') : null;
  // 立即显示新的“当前等待”，失败再回滚；不依赖重新打开客户资料。
  if (cache) {
    cache.customer_judgment = waiting.trim();
    renderCustomerFactsBrief(cache);
  }
  var actionId = beginActionStatus('waiting-' + customerId, '正在保存当前等待…');
  try {
    var updated = await api('/api/customers/' + customerId + '/waiting', { method: 'PUT', body: JSON.stringify({ waiting: waiting.trim() }), silentError: true });
    if (cache) {
      cache.customer_judgment = updated.waiting || '';
      renderCustomerFactsBrief(cache);
    }
    showToast(waiting.trim() ? '当前等待已更新' : '当前等待已清除', 'success');
    finishActionStatus(actionId, 'success', waiting.trim() ? '当前等待已更新' : '当前等待已清除');
  } catch (e) {
    if (cache && previous !== null) {
      cache.customer_judgment = previous;
      renderCustomerFactsBrief(cache);
    }
    showToast('当前等待未保存，请重试', 'error');
    finishActionStatus(actionId, 'error', '当前等待未保存', function() { editCustomerWaiting(); });
  }
}

var CUSTOMER_LEVEL_BASE_OPTIONS = ['A', 'B', 'C', 'D'];
var CUSTOMER_LEVEL_VARIANT_OPTIONS = ['A+', 'A-', 'B+', 'B-', 'C+', 'C-', 'D+', 'D-'];
var CUSTOMER_LEVEL_OPTIONS = CUSTOMER_LEVEL_BASE_OPTIONS.concat(CUSTOMER_LEVEL_VARIANT_OPTIONS);

function customerLevelForDisplay(level) {
  var normalized = String(level || '').trim().toUpperCase();
  return /^[ABCD](?:[+-])?$/.test(normalized) ? normalized : 'C';
}

function setCustomerLevelFieldValue(level) {
  var field = document.getElementById('editLevel');
  if (!field) return;
  field.value = customerLevelForDisplay(level);
}

function setCustomerLevelQuickFeedback(root, message, type) {
  var feedback = root && root.querySelector ? root.querySelector('.customer-level-quick-feedback') : null;
  if (!feedback) return;
  if (feedback._clearTimer) clearTimeout(feedback._clearTimer);
  feedback.textContent = message || '';
  feedback.classList.toggle('is-visible', !!message);
  feedback.classList.toggle('is-error', type === 'error');
  if (message && type !== 'saving') {
    feedback._clearTimer = setTimeout(function() {
      feedback.classList.remove('is-visible');
    }, 1800);
  }
}

async function quickUpdateCustomerLevel(select) {
  if (!select) return;
  var quickLabel = select.closest('.customer-level-quick');
  var idField = document.getElementById('editCustomerId');
  var id = Number(idField && idField.value || 0);
  var previous = select.dataset.currentLevel || (_customerDetailCache && _customerDetailCache.level) || 'C';
  var next = String(select.value || '').trim().toUpperCase();
  if (!/^[ABCD](?:[+-])?$/.test(next)) {
    select.value = previous;
    setCustomerLevelQuickFeedback(quickLabel, '格式不正确', 'error');
    showToast('等级请输入 A、B、C 或 D，可选加号或减号', 'warning');
    return;
  }
  select.value = next;
  if (!id || !next || next === previous) return;

  if (quickLabel) quickLabel.classList.add('is-saving');
  setCustomerLevelQuickFeedback(quickLabel, '保存中…', 'saving');
  select.disabled = true;
  select.setAttribute('aria-busy', 'true');
  select.classList.add('is-saving');
  var scope = beginCustomerScope(id);
  try {
    await api('/api/customers/' + id, {
      method: 'PUT',
      body: JSON.stringify({ level: next })
    });
    var cache = liveCustomerCache(scope);
    if (cache) {
      cache.level = next;
      var editLevel = document.getElementById('editLevel');
      if (editLevel) editLevel.value = next;
      renderCustomerFactsBrief(cache);
    }
    markModalClean('customerEditModal');
    setCustomerLevelQuickFeedback(document.querySelector('.customer-level-quick'), '已保存', 'success');
    showToast('客户等级已更新为 ' + next, 'success');
    if (currentPage === 'customers') loadCustomers({ preservePosition: true });
  } catch (e) {
    select.value = previous;
    setCustomerLevelQuickFeedback(quickLabel, '未保存', 'error');
    select.disabled = false;
    select.removeAttribute('aria-busy');
    select.classList.remove('is-saving');
  } finally {
    if (quickLabel) quickLabel.classList.remove('is-saving');
  }
}

// One user intent is one write. These handlers can be reached from a button, a
// form submit and the unsaved-changes 保存并退出 at the same time, and some are
// invoked without a button element, so a synchronous keyed guard is what keeps
// a double-click or Enter+click from creating duplicate records.
var _inflightWrites = {};
function beginWrite(key) {
  if (_inflightWrites[key]) return false;
  _inflightWrites[key] = true;
  return true;
}
function endWrite(key) { delete _inflightWrites[key]; }

function setActionFeedback(button, state, label) {
  if (!button) return function() {};
  var original = button.dataset.actionLabel || button.textContent.trim();
  button.dataset.actionLabel = original;
  button.disabled = state === 'pending';
  button.classList.toggle('is-pending', state === 'pending');
  button.classList.toggle('is-confirmed', state === 'success');
  button.textContent = label || original;
  return function resetActionFeedback(delay) {
    setTimeout(function() {
      button.disabled = false;
      button.classList.remove('is-pending', 'is-confirmed');
      button.textContent = original;
    }, delay || 0);
  };
}

function applyCustomerTaskSnapshot(tasks, changedKey) {
  var cache = _customerDetailCache;
  if (!cache) return;
  // Completing/postponing a task updates 下次跟进 programmatically. That is a
  // saved server change, not a user edit, so it must not raise the
  // unsaved-changes prompt on the next close when the form was otherwise clean.
  var workspaceModal = document.getElementById('customerEditModal');
  var workspaceOpen = !!(workspaceModal && workspaceModal.classList.contains('show'));
  var workspaceWasDirty = workspaceOpen && customerModalIsDirty('customerEditModal');
  var openTasks = (tasks || []).slice().sort(function(a, b) {
    return String(a.remind_date || '').localeCompare(String(b.remind_date || '')) || Number(a.id || 0) - Number(b.id || 0);
  });
  cache.reminders = openTasks;
  if (Array.isArray(cache.tasks)) cache.tasks = openTasks;
  cache.next_task = openTasks[0] || null;
  cache.next_follow_up = cache.next_task ? (cache.next_task.remind_date || '') : '';
  cache.current_next_step = cache.next_task ? {
    label: cache.next_task.title || cache.next_task.content || '没有明确下一步',
    date: cache.next_task.remind_date || '', source: '待办记录'
  } : { label: '没有明确下一步', date: '', source: '系统事实' };
  var nextDateInput = document.getElementById('editNextFollowUp');
  if (nextDateInput) nextDateInput.value = cache.next_follow_up;
  renderCustomerNextTask(openTasks);
  if (Array.isArray(cache.tasks)) renderCustomerTasks(cache.tasks, changedKey ? [changedKey] : []);
  renderCustomerFactsBrief(cache);
  markElementConfirmed(document.getElementById('customerNextTask'));
  if (workspaceOpen && !workspaceWasDirty) markModalClean('customerEditModal');
  var cachedWorkspace = _customerWorkspaceCache[cache.id];
  if (cachedWorkspace) {
    cachedWorkspace.summary = Object.assign({}, cachedWorkspace.summary || {}, {
      next_follow_up: cache.next_follow_up,
      next_task: cache.next_task,
      current_next_step: cache.current_next_step
    });
    cachedWorkspace.savedAt = Date.now();
  }
}

async function completeCustomerNextTask(button) {
  var next = _customerDetailCache && (_customerDetailCache.reminders || [])[0];
  if (!next) return;
  var scope = beginCustomerScope();
  var reset = setActionFeedback(button, 'pending', '处理中…');
  try {
    var saved = await api('/api/reminders/' + next.id, { method: 'PUT', body: JSON.stringify({ result: '已完成', activity_type: 'task_completed' }) });
    var cache = liveCustomerCache(scope);
    if (cache) {
      var remaining = (cache.reminders || []).filter(function(task) {
        return Number(task.id) !== Number(next.id);
      });
      applyCustomerTaskSnapshot(remaining);
      if (saved && saved.activity_id) {
        var activity = {
          type: 'follow', id: saved.activity_id, follow_date: localDateString(),
          content: '已完成', result: '', next_plan: '', activity_type: 'task_completed',
          direction: 'unknown', is_reported: false, source: 'manual'
        };
        var changedKey = upsertCustomerTimelineEntry(activity);
        syncCustomerWorkspaceAfterCommunication(scope.customerId, activity, changedKey);
      }
    }
    // The confirmed local echo is already painted. These reads repair any
    // server-derived rollups without making the visible response wait on them.
    Promise.all([refreshCustomerTimeline(), refreshCustomerWorkspace()]).catch(function() {});
    // 客户详情覆盖在 Today 之上；完成详情里的待办后同步刷新底层列表，
    // 避免关闭详情后仍看到已经完成的今日事项。
    if (currentPage === 'dashboard') loadDashboard();
    // The panel itself changes to the next state, so a second success toast
    // would only repeat what the user can already see.
    reset();
  } catch (e) {
    setActionFeedback(button, 'error', '保存失败');
    showToast('完成操作未保存，请重试', 'error');
    reset(1800);
  }
}

async function postponeCustomerNextTask(days, button) {
  var next = _customerDetailCache && (_customerDetailCache.reminders || [])[0];
  if (!next) return;
  var scope = beginCustomerScope();
  var due = new Date();
  due.setDate(due.getDate() + days);
  var reset = setActionFeedback(button, 'pending', '正在调整…');
  try {
    var saved = await api('/api/reminders/' + next.id + '/reschedule', { method: 'POST', body: JSON.stringify({ remind_date: localDateString(due) }) });
    var cache = liveCustomerCache(scope);
    if (cache) {
      var updatedTask = saved && saved.reminder ? saved.reminder : Object.assign({}, next, {
        remind_date: (saved && saved.remind_date) || localDateString(due)
      });
      var remaining = (cache.reminders || []).filter(function(task) {
        return Number(task.id) !== Number(next.id) &&
          Number(task.id) !== Number(saved && saved.merged_into || 0);
      });
      if (!updatedTask.is_done) remaining.push(updatedTask);
      applyCustomerTaskSnapshot(remaining, customerTaskMotionKey(updatedTask));
    }
    Promise.all([refreshCustomerWorkspace(), refreshCustomerTimeline()]).catch(function() {});
    // 客户详情会覆盖在今日待办之上；延后后同步刷新底层列表，
    // 让已移到未来的提醒立刻离开今日待办。
    loadDashboard();
    // The refreshed next-step card now shows the new date; keep the feedback
    // local to the button and let the changed card be the confirmation.
    reset();
  } catch (e) {
    setActionFeedback(button, 'error', '保存失败');
    showToast('日期没有保存，请检查网络后重试', 'error');
    reset(1800);
  }
}

async function createCustomerTask(button) {
  // The customer this dialog was opened for is bound at open time
  // (openCustomerTaskModal stamps dataset.customerId). Never re-read the
  // workspace DOM here: a workspace switch while the dialog is open must not
  // redirect the write.
  var modal = document.getElementById('customerTaskModal');
  var customerId = (modal && modal.dataset.customerId) || document.getElementById('editCustomerId').value;
  var title = document.getElementById('customerTaskTitle').value.trim();
  var dueDate = document.getElementById('customerTaskDate').value;
  if (!title || !dueDate) { showToast('请填写具体动作和日期', 'warning'); return; }
  // The unsaved-changes 保存并退出 path calls this without a button, so the
  // visual disable alone cannot stop a second write.
  if (!beginWrite('customerTask')) return;
  var proposalId = _agentTaskProposalId;
  var scope = beginCustomerScope(customerId);
  var editTaskId = proposalId ? 0 : Number((button && button.dataset.editTaskId) || (modal && modal.dataset.editTaskId) || 0);
  var reset = setActionFeedback(button, 'pending', editTaskId ? '正在保存…' : '正在创建…');
  try {
    var created;
    if (proposalId) {
      await api('/api/agent/proposals/' + proposalId, { method: 'PUT', body: JSON.stringify({ title: title, due_date: dueDate }) });
      created = await api('/api/agent/proposals/' + proposalId + '/confirm', { method: 'POST' });
      _agentTaskProposalId = null;
    } else if (editTaskId) {
      created = await api('/api/reminders/' + editTaskId, {
        method: 'PATCH',
        body: JSON.stringify({ title: title, content: title, remind_date: dueDate })
      });
    } else created = await api('/api/customers/' + customerId + '/tasks', {
      method: 'POST',
      body: JSON.stringify({ title: title, due_date: dueDate })
    });
    document.getElementById('customerTaskTitle').value = '';
    document.getElementById('customerTaskDate').value = '';
    closeModal('customerTaskModal', true);
    // This task covers the Inbox signal that opened the scheduling flow. Refresh
    // immediately so the handled customer leaves the current list.
    if (currentPage === 'inbox') await loadInbox();
    var cache = liveCustomerCache(scope);
    if (cache) {
      var task = created && (created.task || created.next_task || created.reminder);
      var mergedInto = Number(created && created.merged_into || 0);
      var visibleTasks = Array.isArray(cache.tasks)
        ? cache.tasks.slice()
        : (cache.reminders || []).slice();
      if (task) {
        visibleTasks = visibleTasks.filter(function(item) {
          return Number(item.id) !== editTaskId && Number(item.id) !== mergedInto && Number(item.id) !== Number(task.id);
        });
        if (!task.is_done) visibleTasks.push(task);
        visibleTasks.sort(function(a, b) { return String(a.remind_date || '').localeCompare(String(b.remind_date || '')); });
      }
      applyCustomerTaskSnapshot(visibleTasks, task ? customerTaskMotionKey(task) : '');
      document.getElementById('editNextFollowUp').value = cache.next_follow_up || '';
      renderCustomerNextTask(cache.reminders || []);
      markElementConfirmed(document.getElementById('customerNextTask'));
      renderCustomerFactsBrief(cache);
      // The customer workspace may be reopened from its short-lived summary
      // cache. Keep that cache aligned with the durable write as well, or the
      // newly created task disappears again when the detail modal is opened.
      var cachedWorkspace = _customerWorkspaceCache[customerId];
      if (cachedWorkspace) {
        cachedWorkspace.summary = Object.assign({}, cachedWorkspace.summary || {}, {
          next_follow_up: cache.next_follow_up || '',
          next_task: cache.next_task || null,
          current_next_step: cache.current_next_step || (cache.next_task ? {
            label: cache.next_task.title || cache.next_task.content || '没有明确下一步',
            date: cache.next_task.remind_date || '', source: '待办记录'
          } : { label: '没有明确下一步', date: '', source: '系统事实' })
        });
        cachedWorkspace.savedAt = Date.now();
      }
    }
    // Closing the composer and rendering the new card is sufficient success
    // feedback. Toasts are reserved for problems or non-visible outcomes.
    reset();
  } catch(e) {
    setActionFeedback(button, 'error', '创建失败');
    showToast('下一步没有创建，请重试', 'error');
    reset(1800);
  } finally {
    endWrite('customerTask');
  }
}

async function saveCustomer() {
  var id = document.getElementById('editCustomerId').value;
  var name = document.getElementById('editName').value.trim();
  if (!name) { showToast('客户名称不能为空', 'warning'); return false; }
  
  var country = document.getElementById('editCountry').value.trim();
  
  var businessStage = document.getElementById('editBusinessStage').value;
  var data = {
    name: name,
    company: document.getElementById('editCompany').value.trim(),
    country: country,
    level: document.getElementById('editLevel').value,
    business_role: document.getElementById('editBusinessRole').value,
    field: document.getElementById('editField').value.trim(),
    business_stage: businessStage,
    source: document.getElementById('editSource').value,
    source_detail: document.getElementById('editSourceDetail').value.trim(),
    next_follow_up: document.getElementById('editNextFollowUp').value,
    website: document.getElementById('editWebsite').value.trim(),
    tags: document.getElementById('editTags').value.trim(),
    profile: document.getElementById('editProfile').value.trim(),
    notes: document.getElementById('editNotes').value.trim()
  };
  var scope = beginCustomerScope(id);
  var button = document.getElementById('saveCustomerFooterBtn');
  var reset = setActionFeedback(button, 'pending', '保存中…');
  var actionId = beginActionStatus('customer-save-' + id, '正在保存客户资料…');
  try {
    var previousNextFollowUp = _customerDetailCache && _customerDetailCache.next_follow_up;
    await api('/api/customers/' + id, { method: 'PUT', body: JSON.stringify(data), silentError: true });
    var cache = liveCustomerCache(scope);
    if (cache) {
      Object.keys(data).forEach(function(key) { cache[key] = data[key]; });
      updateCustomerWorkspaceIdentity(cache);
      renderCustomerFactsBrief(cache);
      if (previousNextFollowUp !== data.next_follow_up) await refreshCustomerWorkspace();
    }
    var msg = '客户更新成功';
    showToast(msg, 'success');
    finishActionStatus(actionId, 'success', '客户资料已保存');
    setActionFeedback(button, 'success', '已保存');
    markModalClean('customerEditModal');
    if (currentPage === 'customers') loadCustomers({ preservePosition: true });
    else loadDashboard();
    return true;
  } catch(e) {
    showToast('客户资料未保存，请重试', 'error');
    finishActionStatus(actionId, 'error', '客户资料未保存', function() { saveCustomer(); });
    setActionFeedback(button, 'error', '保存失败');
    reset(1800);
    return false;
  }
}

async function deleteCustomer(id) {
  if (!await showAppConfirm({ title: '归档客户', message: '将这个客户归档？之后仍可恢复。', submitLabel: '归档' })) return;
  try {
    await api('/api/customers/' + id, { method: 'DELETE' });
    showToast('客户已归档', 'success');
    if (currentPage === 'customers') loadCustomers();
    else loadDashboard();
  } catch(e) {}
}

// Contacts
var CONTACT_EDIT_FIELDS = [
  { key: 'name', label: '姓名', type: 'text' },
  { key: 'title', label: '职位', type: 'text' },
  { key: 'email', label: '邮箱', type: 'email' },
  { key: 'phone', label: '电话', type: 'tel' },
  { key: 'whatsapp', label: 'WhatsApp', type: 'tel' },
  { key: 'linkedin', label: '领英', type: 'text' }
];

function contactEditCard(contactId) {
  var id = Number(contactId);
  return Array.from(document.querySelectorAll('#contactsList [data-contact-id]')).find(function(card) {
    return Number(card.getAttribute('data-contact-id')) === id;
  }) || null;
}

function contactEditInputHtml(contact, contactId, field) {
  var inputId = 'contactEdit' + field.key.charAt(0).toUpperCase() + field.key.slice(1) + contactId;
  return '<div class="form-group contact-edit-field"><label class="form-label" for="' + inputId + '">' + field.label + '</label>' +
    '<input class="form-control" id="' + inputId + '" type="' + field.type + '" autocomplete="off" data-contact-field="' + field.key + '" value="' + escapeHtml(contact[field.key] || '') + '"></div>';
}

function contactEditFormHtml(contact) {
  var contactId = Number(contact.id);
  var firstRow = CONTACT_EDIT_FIELDS.slice(0, 2).map(function(field) { return contactEditInputHtml(contact, contactId, field); }).join('');
  var secondRow = CONTACT_EDIT_FIELDS.slice(2, 4).map(function(field) { return contactEditInputHtml(contact, contactId, field); }).join('');
  var thirdRow = CONTACT_EDIT_FIELDS.slice(4, 6).map(function(field) { return contactEditInputHtml(contact, contactId, field); }).join('');
  return '<div class="sub-item contact-item contact-item-editing" data-contact-id="' + contactId + '">' +
    '<form class="contact-edit-form" autocomplete="off" onsubmit="return saveContact(event,' + contactId + ')">' +
      '<div class="contact-edit-header"><div><strong>编辑联系人</strong><span>修改后会立即同步到客户资料</span></div><button class="text-action contact-edit-cancel" type="button" onclick="cancelContactEdit(' + contactId + ')">取消编辑</button></div>' +
      '<div class="form-row">' + firstRow + '</div>' +
      '<div class="form-row">' + secondRow + '</div>' +
      '<div class="form-row">' + thirdRow + '</div>' +
      '<div class="contact-edit-actions"><button class="btn btn-primary btn-sm" type="submit">保存修改</button><span class="contact-edit-status" role="status" aria-live="polite"></span></div>' +
  '</form>' +
  '</div>';
}

function syncContactEditInputValues(contact) {
  if (!contact) return;
  var card = contactEditCard(contact.id);
  if (!card) return;
  CONTACT_EDIT_FIELDS.forEach(function(field) {
    var input = card.querySelector('[data-contact-field="' + field.key + '"]');
    if (input) input.value = contact[field.key] || '';
  });
}

function renderContacts(contacts) {
  var el = document.getElementById('contactsList');
  if (!contacts || contacts.length === 0) { el.innerHTML = '<div class="contact-empty-state"><strong>还没有联系人</strong><span>先添加一位主要联系人，后续的沟通记录会更清晰。</span></div>'; return; }
  var html = '';
  contacts.forEach(function(c) {
    var contactId = Number(c.id);
    if (!contactId) return;
    if (Number(_editingContactId) === contactId) {
      html += contactEditFormHtml(c);
      return;
    }
    var displayName = escapeHtml(c.name || c.email || '(未命名)');
    html += '<div class="sub-item contact-item" data-contact-id="' + contactId + '"><div class="sub-item-header"><span class="sub-item-title">' + displayName + (c.is_primary ? ' <span style="font-size:0.7rem;color:var(--accent);">(主要)</span>' : '') + '</span><span class="contact-item-actions">' +
      '<button class="btn btn-sm" type="button" onclick="startContactEdit(' + contactId + ')" aria-label="编辑联系人 ' + displayName + '">编辑</button>' +
      '<button class="btn btn-sm btn-danger" type="button" onclick="deleteContact(' + contactId + ')" aria-label="删除联系人 ' + displayName + '">删除</button></span></div><div class="sub-item-detail">' +
      (c.title ? '<div>' + escapeHtml(c.title) + '</div>' : '') + (c.email ? '<div>' + escapeHtml(c.email) + '</div>' : '') +
      (c.phone ? '<div>电话：' + escapeHtml(c.phone) + '</div>' : '') + (c.whatsapp ? '<div>WhatsApp：' + escapeHtml(c.whatsapp) + '</div>' : '') + (c.linkedin ? '<div>' + escapeHtml(c.linkedin) + '</div>' : '') + '</div></div>';
  });
  el.innerHTML = html;
  if (_editingContactId) {
    var editingContact = contacts.find(function(contact) { return Number(contact.id) === Number(_editingContactId); });
    syncContactEditInputValues(editingContact);
  }
}

function setContactEditStatus(card, message, isError) {
  var status = card && card.querySelector('.contact-edit-status');
  if (!status) return;
  status.textContent = message || '';
  status.classList.toggle('is-error', !!isError);
}

function contactEditIsDirty() {
  if (!_editingContactId || !_editingContactOriginal) return false;
  var card = contactEditCard(_editingContactId);
  if (!card) return false;
  return CONTACT_EDIT_FIELDS.some(function(field) {
    var input = card.querySelector('[data-contact-field="' + field.key + '"]');
    return input && input.value !== String(_editingContactOriginal[field.key] || '');
  });
}

function startContactEdit(contactId) {
  var id = Number(contactId);
  if (!id || !_customerDetailCache || !Array.isArray(_customerDetailCache.contacts)) return;
  if (_editingContactId && Number(_editingContactId) !== id) {
    showToast('请先保存或取消当前联系人编辑', 'info');
    return;
  }
  var contact = _customerDetailCache.contacts.find(function(item) { return Number(item.id) === id; });
  if (!contact) return;
  _editingContactId = id;
  _editingContactOriginal = Object.assign({}, contact);
  renderContacts(_customerDetailCache.contacts);
  setTimeout(function() {
    var card = contactEditCard(id);
    var firstInput = card && card.querySelector('[data-contact-field="name"]');
    if (firstInput) firstInput.focus({ preventScroll: true });
  }, 0);
}

function cancelContactEdit(contactId) {
  var id = Number(contactId);
  if (!_editingContactId || Number(_editingContactId) !== id) return;
  _editingContactId = null;
  _editingContactOriginal = null;
  if (_customerDetailCache && Array.isArray(_customerDetailCache.contacts)) renderContacts(_customerDetailCache.contacts);
}

async function saveContact(event, contactId) {
  if (event && event.preventDefault) event.preventDefault();
  var id = Number(contactId);
  var card = contactEditCard(id);
  if (!id || !card || Number(_editingContactId) !== id) return false;
  var data = {};
  CONTACT_EDIT_FIELDS.forEach(function(field) {
    var input = card.querySelector('[data-contact-field="' + field.key + '"]');
    data[field.key] = input ? input.value.trim() : '';
  });
  if (!data.name && !data.email) {
    setContactEditStatus(card, '姓名和邮箱至少填一项', true);
    showToast('姓名和邮箱至少填一项', 'warning');
    var nameInput = card.querySelector('[data-contact-field="name"]');
    if (nameInput) nameInput.focus();
    return false;
  }
  var customerId = document.getElementById('editCustomerId').value;
  var scope = beginCustomerScope(customerId);
  var saveButton = card.querySelector('button[type="submit"]');
  card.classList.add('is-saving');
  if (saveButton) { saveButton.disabled = true; saveButton.textContent = '保存中…'; }
  card.querySelectorAll('input').forEach(function(input) { input.disabled = true; });
  setContactEditStatus(card, '正在保存联系人资料…', false);
  try {
    var saved = await api('/api/contacts/' + id, { method: 'PUT', body: JSON.stringify(data) });
    if (!saved) throw new Error('联系人资料未保存，请重新登录');
    _editingContactId = null;
    _editingContactOriginal = null;
    var cache = liveCustomerCache(scope);
    if (!cache) { showToast('联系人资料已保存', 'success'); return false; }
    var current = Array.isArray(cache.contacts)
      ? cache.contacts.find(function(item) { return Number(item.id) === id; }) : null;
    var updated = Object.assign({}, current || {}, data, { id: id });
    if (saved && saved.contact) updated = saved.contact;
    patchCustomerWorkspaceContact(updated);
    showToast('联系人资料已保存', 'success');
  } catch (e) {
    card.classList.remove('is-saving');
    if (saveButton) { saveButton.disabled = false; saveButton.textContent = '保存修改'; }
    card.querySelectorAll('input').forEach(function(input) { input.disabled = false; });
    setContactEditStatus(card, e.message || '保存失败，请检查后重试', true);
  }
  return false;
}

function patchCustomerWorkspaceContact(contact, options) {
  if (!_customerDetailCache || !contact) return;
  options = options || {};
  var contactsLoaded = Array.isArray(_customerDetailCache.contacts);
  if (contactsLoaded) {
    var nextContacts = _customerDetailCache.contacts.filter(function(item) { return Number(item.id) !== Number(contact.id); });
    if (!options.remove) nextContacts.push(contact);
    nextContacts.sort(function(a, b) { return Number(b.is_primary || 0) - Number(a.is_primary || 0) || Number(a.id || 0) - Number(b.id || 0); });
    _customerDetailCache.contacts = nextContacts;
    _customerDetailCache.contact_count = nextContacts.length;
    _customerDetailCache.primary_contact = nextContacts[0] || null;
    if (!options.deferRender) renderContacts(nextContacts);
  } else if (options.remove) {
    _customerDetailCache.contact_count = Math.max(0, Number(_customerDetailCache.contact_count || 0) - 1);
    if (_customerDetailCache.primary_contact && Number(_customerDetailCache.primary_contact.id) === Number(contact.id)) {
      _customerDetailCache.primary_contact = null;
    }
  } else {
    _customerDetailCache.contact_count = Math.max(Number(_customerDetailCache.contact_count || 0), 0) + (options.merged ? 0 : 1);
    if (!_customerDetailCache.primary_contact || contact.is_primary) _customerDetailCache.primary_contact = contact;
  }
  if (!options.deferRender) renderCustomerFactsBrief(_customerDetailCache);
}

async function addContact() {
  var id = document.getElementById('editCustomerId').value;
  var name = document.getElementById('contactName').value.trim();
  var email = document.getElementById('contactEmail').value.trim();
  if (!name && !email) { showToast('姓名和邮箱至少填一项', 'warning'); return false; }
  var data = { name: name, title: document.getElementById('contactTitle').value.trim(), email: email, phone: document.getElementById('contactPhone').value.trim(), whatsapp: document.getElementById('contactWhatsapp').value.trim(), linkedin: document.getElementById('contactLinkedin').value.trim() };
  var scope = beginCustomerScope(id);
  var button = document.getElementById('addContactSubmit');
  var reset = setActionFeedback(button, 'pending', '添加中…');
  var actionId = beginActionStatus('contact-add-' + id, '正在添加联系人…');
  try {
    var saved = await api('/api/customers/' + id + '/contacts', { method: 'POST', body: JSON.stringify(data), silentError: true });
    showToast('联系人已添加', 'success');
    finishActionStatus(actionId, 'success', '联系人已添加');
    if (liveCustomerCache(scope)) {
      document.getElementById('contactName').value = ''; document.getElementById('contactTitle').value = '';
      document.getElementById('contactEmail').value = ''; document.getElementById('contactPhone').value = ''; document.getElementById('contactWhatsapp').value = ''; document.getElementById('contactLinkedin').value = '';
      patchCustomerWorkspaceContact(saved && saved.contact, { merged: !!(saved && saved.merged) });
    }
    setActionFeedback(button, 'success', '已添加');
    reset(1200);
    return true;
  } catch(e) {
    // 保留已填内容，用户可以直接重试，不必重新输入。
    setActionFeedback(button, 'error', '添加失败');
    showToast('联系人未添加，请重试', 'error');
    finishActionStatus(actionId, 'error', '联系人未添加', function() { addContact(); });
    reset(1800);
    return false;
  }
}

async function addBulkContacts() {
  var id = document.getElementById('editCustomerId').value;
  var text = document.getElementById('bulkContactEmails').value.trim();
  if (!text) { showToast('请输入邮箱', 'warning'); return false; }
  var emails = normalizeEmailList(text);
  if (!emails.length) { showToast('未找到有效邮箱', 'warning'); return false; }
  var validationPanel = document.getElementById('bulkContactEmailValidation');
  validationPanel.hidden = false;
  validationPanel.innerHTML = '<strong>正在检查邮箱可发送性…</strong><span>检查格式、邮件路由、一次性邮箱、免费邮箱和部门邮箱</span>';
  var scope = beginCustomerScope(id);
  try {
    var validation = await api('/api/emails/validate', { method: 'POST', body: JSON.stringify({ emails: emails }) });
    var accepted = (validation.results || []).filter(function(item) { return item.status === 'valid' || item.status === 'suspicious'; });
    var added = 0;
    for (var i = 0; i < accepted.length; i++) {
      var email = accepted[i].normalized || accepted[i].email;
      var response = await api('/api/customers/' + id + '/contacts', { method: 'POST', body: JSON.stringify({ name: email.split('@')[0], email: email }) });
      if (!response.duplicate) added++;
      if (liveCustomerCache(scope)) {
        patchCustomerWorkspaceContact(response && response.contact, { merged: !!(response && response.merged), deferRender: true });
      }
    }
    if (liveCustomerCache(scope)) {
      renderEmailValidation('bulkContactEmailValidation', validation);
      document.getElementById('bulkContactEmails').value = '';
      if (Array.isArray(_customerDetailCache.contacts)) renderContacts(_customerDetailCache.contacts);
      renderCustomerFactsBrief(_customerDetailCache);
    }
    showToast('已验证并导入 ' + added + ' 个联系人', 'success');
    return true;
  } catch(e) {
    validationPanel.innerHTML = '<strong>邮箱验证失败</strong><span>' + escapeHtml(e.message || '请稍后重试') + '</span>';
    return false;
  }
}

async function deleteContact(contactId) {
  if (!await showAppConfirm({ title: '删除联系人', message: '确认删除该联系人？', submitLabel: '删除' })) return;
  var id = Number(contactId);
  var customerId = document.getElementById('editCustomerId').value;
  var scope = beginCustomerScope(customerId);
  var cache = liveCustomerCache(scope);
  var previous = cache && Array.isArray(cache.contacts)
    ? cache.contacts.find(function(item) { return Number(item.id) === id; }) : null;
  // 联系人列表可见时立即移除；服务端拒绝（如仍被引用）则回滚，保证前后端一致。
  if (previous) patchCustomerWorkspaceContact(previous, { remove: true });
  var actionId = beginActionStatus('contact-delete-' + id, '正在删除联系人…');
  try {
    await api('/api/contacts/' + id, { method: 'DELETE', silentError: true });
    showToast('联系人已删除', 'success');
    finishActionStatus(actionId, 'success', '联系人已删除');
  } catch(e) {
    if (previous) patchCustomerWorkspaceContact(previous);
    showToast('联系人未删除，请重试', 'error');
    finishActionStatus(actionId, 'error', '联系人未删除', function() { deleteContact(id); });
  }
}

// ========== 客户文件附件 ==========
// 打开方式：native = 浏览器原生预览（图片/PDF）；preview = 服务端渲染预览页（Office/邮件/压缩包/表格/文本）；其余类型点击即下载。
var _CUSTOMER_FILE_OPEN_MODES = {
  pdf: 'native', jpg: 'native', jpeg: 'native', png: 'native', gif: 'native', webp: 'native', bmp: 'native', heic: 'native',
  xlsx: 'preview', xls: 'preview', csv: 'preview', txt: 'preview', md: 'preview', rtf: 'preview',
  docx: 'preview', pptx: 'preview', eml: 'preview', zip: 'preview', tar: 'preview', gz: 'preview'
};

function customerFileSizeText(bytes) {
  if (!bytes && bytes !== 0) return '';
  if (bytes < 1024) return bytes + ' B';
  if (bytes < 1024 * 1024) return (bytes / 1024).toFixed(1) + ' KB';
  return (bytes / (1024 * 1024)).toFixed(1) + ' MB';
}

function _customerFileOpenMode(name) {
  var ext = String(name || '').split('.').pop().toLowerCase();
  return _CUSTOMER_FILE_OPEN_MODES[ext] || 'download';
}

function openCustomerFile(fileId, mode) {
  var id = document.getElementById('editCustomerId').value;
  if (!id) return;
  var base = '/api/customers/' + id + '/files/' + fileId;
  if (mode === 'native') window.open(base + '/download?inline=1', '_blank', 'noopener');
  else if (mode === 'preview') window.open(base + '/preview', '_blank', 'noopener');
  else downloadCustomerFile(fileId);
}

function renderCustomerFiles(files) {
  var el = document.getElementById('customerFilesList');
  if (!el) return;
  files = files || [];
  if (!files.length) {
    el.innerHTML = '<div class="customer-file-empty"><strong>还没有客户文件</strong><span>上传报价单、合同、样品图片或展会资料后，查阅这个客户时可以直接点击打开，不用再翻找本地 Excel。</span></div>';
    return;
  }
  var html = '<div class="customer-file-list">';
  files.forEach(function(f) {
    var ext = String(f.original_name || '').split('.').pop().toUpperCase().slice(0, 4) || '文件';
    var mode = f.missing ? 'download' : _customerFileOpenMode(f.original_name);
    var missing = f.missing ? '<span class="customer-file-missing">文件本体缺失</span>' : '';
    var tag = f.category ? '<span class="customer-file-tag">' + escapeHtml(f.category) + '</span>' : '';
    var uploader = f.uploaded_by ? '<span>由 ' + escapeHtml(f.uploaded_by) + ' 上传</span>' : '';
    var date = (f.created_at || '').slice(0, 10);
    var metaParts = [customerFileSizeText(f.file_size), date, uploader].filter(Boolean);
    var openHint = mode === 'preview' ? '<span class="customer-file-hint">点击预览</span>'
      : mode === 'native' ? '<span class="customer-file-hint">点击预览</span>'
      : (f.missing ? '' : '<span class="customer-file-hint">点击下载</span>');
    var actions = '<span class="customer-file-actions">' +
      (mode === 'preview' && !f.missing ? '<button class="btn btn-sm" type="button" onclick="event.stopPropagation();openCustomerFile(' + f.id + ',\'preview\')">预览</button>' : '') +
      (!f.missing ? '<button class="btn btn-sm" type="button" onclick="event.stopPropagation();downloadCustomerFile(' + f.id + ')">下载</button>' : '') +
      '<button class="btn btn-sm btn-danger" type="button" onclick="event.stopPropagation();deleteCustomerFile(' + f.id + ')">删除</button></span>';
    var rowAttr = 'role="button" tabindex="0" title="' + (f.missing ? '文件本体缺失' : (mode === 'download' ? '点击下载文件' : '点击预览文件')) + '" ' +
      'onclick="' + (f.missing ? '' : 'openCustomerFile(' + f.id + ',\'' + mode + '\')') + '" ' +
      'onkeydown="if(event.key===\'Enter\'||event.key===\' \'){event.preventDefault();' + (f.missing ? '' : 'openCustomerFile(' + f.id + ',\'' + mode + '\')') + '}"';
    html += '<div class="customer-file-item" ' + rowAttr + '>' +
      '<span class="customer-file-icon">' + uiIcon('file') + '</span>' +
      '<span class="customer-file-type">' + escapeHtml(ext) + '</span>' +
      '<span class="customer-file-main"><span class="customer-file-name" title="' + escapeHtml(f.original_name) + '">' + escapeHtml(f.original_name) + '</span>' +
      '<span class="customer-file-meta">' + metaParts.join(' · ') + ' ' + openHint + ' ' + missing + ' ' + tag + '</span></span>' +
      actions + '</div>';
  });
  el.innerHTML = html + '</div>';
}

function openCustomerFilePicker() {
  var input = document.getElementById('customerFileInput');
  if (!input) return;
  input.value = '';
  renderCustomerFilePickList();
  input.click();
}

function renderCustomerFilePickList() {
  var input = document.getElementById('customerFileInput');
  var el = document.getElementById('customerFilePickList');
  if (!el || !input) return;
  var files = Array.from(input.files || []);
  el.innerHTML = files.map(function(f) {
    return '<div class="customer-file-pick-item"><span class="customer-file-name">' + escapeHtml(f.name) + '</span>' +
      '<span class="customer-file-pick-size">' + customerFileSizeText(f.size) + '</span></div>';
  }).join('');
}

function setCustomerFileInputFiles(fileList) {
  var input = document.getElementById('customerFileInput');
  if (!input) return;
  try {
    var dt = new DataTransfer();
    Array.from(fileList || []).forEach(function(f) { dt.items.add(f); });
    input.files = dt.files;
  } catch (ignore) {}
  renderCustomerFilePickList();
  var compose = document.getElementById('customerFileCompose');
  if (compose && input.files && input.files.length) compose.open = true;
}

function handleCustomerFileDrop(event) {
  event.preventDefault();
  event.stopPropagation();
  var zone = document.getElementById('customerFileDropzone');
  if (zone) zone.classList.remove('is-dragover');
  var dt = event.dataTransfer;
  if (!dt || !dt.files || !dt.files.length) return;
  setCustomerFileInputFiles(dt.files);
  showToast('已加入 ' + dt.files.length + ' 个文件，点击“上传所选文件”', 'info');
}

function initCustomerFileInput() {
  var input = document.getElementById('customerFileInput');
  if (!input) return;
  input.addEventListener('change', function() {
    renderCustomerFilePickList();
    var compose = document.getElementById('customerFileCompose');
    if (compose && input.files && input.files.length) compose.open = true;
  });
}

async function uploadCustomerFiles() {
  var input = document.getElementById('customerFileInput');
  var files = Array.from(input && input.files ? input.files : []);
  if (!files.length) { showToast('请先选择文件', 'warning'); return false; }
  var id = document.getElementById('editCustomerId').value;
  if (!id) return false;
  var maxBytes = 25 * 1024 * 1024;
  var maxFiles = 10;
  if (files.length > maxFiles) {
    showToast('每次最多上传 ' + maxFiles + ' 个文件', 'warning');
    files = files.slice(0, maxFiles);
  }
  var oversized = files.filter(function(f) { return f.size > maxBytes; });
  if (oversized.length) {
    showToast('已跳过 ' + oversized.length + ' 个超过 25MB 的文件', 'warning');
    files = files.filter(function(f) { return f.size <= maxBytes; });
    if (!files.length) { renderCustomerFilePickList(); return false; }
  }
  var totalBytes = files.reduce(function(sum, f) { return sum + f.size; }, 0);
  if (totalBytes > maxBytes * maxFiles) {
    showToast('本次上传总量不能超过 250MB', 'warning');
    return false;
  }
  var form = new FormData();
  files.forEach(function(f) { form.append('files', f, f.name); });
  var category = (document.getElementById('customerFileCategory').value || '').trim();
  if (category) form.append('category', category);
  var btn = document.getElementById('customerFileUploadBtn');
  if (btn) { btn.disabled = true; btn.textContent = '正在上传…'; }
  var scope = beginCustomerScope(id);
  try {
    var resp = await fetch('/api/customers/' + id + '/files', { method: 'POST', credentials: 'include', body: form });
    var data = await resp.json().catch(function() { return {}; });
    if (!resp.ok) throw new Error(data.error || ('上传失败（HTTP ' + resp.status + '）'));
    var uploaded = (data.created || []).length;
    var rejected = (data.rejected || []).length;
    showToast('已上传 ' + uploaded + ' 个文件' + (rejected ? '，跳过 ' + rejected + ' 个不支持的文件' : ''), rejected ? 'warning' : 'success');
    input.value = '';
    renderCustomerFilePickList();
    var categoryInput = document.getElementById('customerFileCategory');
    if (categoryInput) categoryInput.value = '';
    var createdFiles = (data && data.created) || [];
    var cache = liveCustomerCache(scope);
    if (cache && Array.isArray(cache.files)) {
      var currentFiles = createdFiles.concat(cache.files);
      cache.files = currentFiles;
      renderCustomerFiles(currentFiles);
    }
    return true;
  } catch (e) {
    showToast(e.message || '上传失败', 'error');
    return false;
  } finally {
    if (btn) { btn.disabled = false; btn.textContent = '上传所选文件'; }
  }
}

function downloadCustomerFile(fileId) {
  var id = document.getElementById('editCustomerId').value;
  if (!id) return;
  var a = document.createElement('a');
  a.href = '/api/customers/' + id + '/files/' + fileId + '/download';
  a.rel = 'noopener';
  document.body.appendChild(a);
  a.click();
  a.remove();
}

async function deleteCustomerFile(fileId) {
  if (!await showAppConfirm({ title: '删除文件', message: '确认删除这个客户文件？文件会移入可恢复区，可立即撤销。', submitLabel: '删除' })) return;
  var id = document.getElementById('editCustomerId').value;
  if (!id) return;
  var scope = beginCustomerScope(id);
  try {
    var deleted = await api('/api/customers/' + id + '/files/' + fileId, { method: 'DELETE' });
    showToastAction('文件已移入可恢复区。', 'success', '撤销', async function() {
      // 撤销是稍后发生的独立动作：按文件归属客户重新捕获 scope，只有当工作区
      // 仍显示该客户时才做本地回显。
      var restoreScope = beginCustomerScope(id);
      try {
        var restored = await api('/api/customers/' + id + '/files/' + fileId + '/restore', { method: 'POST' });
        var restoreCache = liveCustomerCache(restoreScope);
        if (restoreCache && Array.isArray(restoreCache.files) && restored && restored.file) {
          restoreCache.files = [restored.file].concat(restoreCache.files.filter(function(file) { return Number(file.id) !== Number(fileId); }));
          renderCustomerFiles(restoreCache.files);
        }
        showToast('文件已恢复', 'success');
      } catch (e) { showToast(e.message || '恢复失败', 'error'); }
    });
    var cache = liveCustomerCache(scope);
    if (cache && Array.isArray(cache.files)) {
      cache.files = cache.files.filter(function(file) { return Number(file.id) !== Number(fileId); });
      renderCustomerFiles(cache.files);
    }
  } catch (e) {}
}

// Follow Timeline - 混排跟进记录 + 开发信
var _followTimelineCache = {};
function communicationTypeLabel(type) {
  var labels = { whatsapp:'WhatsApp', email:'邮件', phone:'电话', meeting:'会议', quote:'报价', sample:'寄样', follow_up:'其他跟进', customer_reply:'客户回复', task_completed:'完成任务' };
  return labels[type] || type || '沟通记录';
}
// The timeline stores where a fact came from as an internal key. Showing the
// raw key ("manual") next to a business label reads like a bug to the user, so
// only known import channels get named and everything else stays generic.
function interactionSourceLabel(source) {
  var labels = { manual:'手动记录', quick_reply:'快速回复', gmail:'Gmail', gmail_delivery:'Gmail', gmail_reply:'Gmail 回复', excel:'Excel 导入', excel_recovery:'Excel 恢复', import:'导入', sync:'自动同步', agent_confirmed:'AI 建议已确认', sela_agent:'Sela 建议已确认' };
  return (source && labels[source]) || '';
}
function customerFactLabel(type, sourceDetail) {
  var detail = String(sourceDetail || '');
  var contactTypes = { whatsapp:1, email:1, phone:1, meeting:1, quote:1, sample:1, follow_up:1, customer_reply:1, task_completed:1 };
  var typeLabel = type === 'outreach' ? '开发邮件'
    : (Object.prototype.hasOwnProperty.call(contactTypes, detail.toLowerCase()) ? communicationTypeLabel(detail) : '');
  var channel = interactionSourceLabel(detail);
  if (typeLabel && channel && channel !== typeLabel) return typeLabel + ' · ' + channel;
  return typeLabel || channel || '沟通记录';
}
function renderCustomerTimelineMore(pagination) {
  var el = document.getElementById('customerTimelineMore');
  if (!el) return;
  el.className = 'customer-timeline-more';
  if (!pagination || !pagination.has_next) {
    el.hidden = !((_customerDetailCache && (_customerDetailCache.timeline_items || []).length));
    if (!el.hidden) el.classList.add('is-end');
    el.innerHTML = '';
    return;
  }
  el.hidden = false;
  el.innerHTML = '<button class="btn btn-sm" type="button" onclick="loadMoreCustomerTimeline()">查看更早记录</button>';
}

async function loadMoreCustomerTimeline() {
  var customerId = _customerDetailCache && _customerDetailCache.id;
  if (!customerId || _customerTimelineLoading) return;
  var scope = beginCustomerScope(customerId);
  var more = document.getElementById('customerTimelineMore');
  _customerTimelineLoading = true;
  if (more) { more.className = 'customer-timeline-more is-loading'; more.innerHTML = ''; more.hidden = false; }
  try {
    var nextPage = _customerTimelinePage + 1;
    var data = await api('/api/customers/' + customerId + '/timeline?page=' + nextPage + '&per_page=' + _customerTimelinePerPage);
    var cache = liveCustomerCache(scope);
    if (!cache) return;
    var nextItems = normalizeCustomerTimelineItems((data && data.items) || []);
    cache.timeline_items = (cache.timeline_items || []).concat(nextItems);
    cache.timeline_pagination = (data && data.pagination) || {};
    cache.follow_history = cache.timeline_items.filter(function(item) { return item.type === 'follow'; });
    cache.outreach_emails = cache.timeline_items.filter(function(item) { return item.type === 'outreach'; });
    _customerTimelinePage = nextPage;
    renderFollowTimeline(cache.follow_history, cache.outreach_emails);
    renderCustomerTimelineMore(cache.timeline_pagination);
  } catch (e) {
    if (liveCustomerCache(scope)) renderCustomerTimelineMore(_customerDetailCache.timeline_pagination);
    showToast('更早的沟通记录暂时无法加载', 'error');
  } finally {
    _customerTimelineLoading = false;
  }
}

async function refreshCustomerTimeline(options) {
  options = options || {};
  var customerId = _customerDetailCache && _customerDetailCache.id;
  if (!customerId) return;
  var scope = beginCustomerScope(customerId);
  var data = await api('/api/customers/' + customerId + '/timeline?page=1&per_page=' + _customerTimelinePerPage,
    { silentError: !!options.silentError });
  var cache = liveCustomerCache(scope);
  if (!cache) return;
  if (_customerWorkspaceCache[customerId]) {
    _customerWorkspaceCache[customerId].timeline = data;
    _customerWorkspaceCache[customerId].savedAt = Date.now();
  }
  var items = normalizeCustomerTimelineItems((data && data.items) || []);
  cache.timeline_items = items;
  cache.timeline_pagination = (data && data.pagination) || {};
  cache.follow_history = items.filter(function(item) { return item.type === 'follow'; });
  cache.outreach_emails = items.filter(function(item) { return item.type === 'outreach'; });
  _customerTimelinePage = 1;
  renderFollowTimeline(cache.follow_history, cache.outreach_emails);
  renderCustomerTimelineMore(cache.timeline_pagination);
}

// ---------- 写后即时回显（local echo） ----------
// Every timeline write already answers with the durable row it created or
// changed.  Painting that answer is what makes the change visible at the moment
// it is saved; the timeline read afterwards only reconciles, so a slow tunnel
// or an unreachable server can no longer look like "nothing happened".

function customerTimelineKey(entry) {
  if (!entry || !entry.id) return '';
  var isOutreach = entry.type === 'outreach' || entry.type === 'email';
  return (isOutreach ? 'outreach-' : 'follow-') + entry.id;
}

// The server has already confirmed the write, so the tint is a one-shot
// confirmation of *which* record changed, never a stored state.
function markElementConfirmed(node) {
  if (!node || !node.classList || !shouldAnimateLists()) return;
  if (node._motionConfirmTimer) window.clearTimeout(node._motionConfirmTimer);
  node.classList.add('motion-updated');
  node._motionConfirmTimer = window.setTimeout(function() {
    node.classList.remove('motion-updated');
    node._motionConfirmTimer = null;
  }, 1100);
}

function markCustomerFactConfirmed(key) {
  var summary = document.getElementById('customerWorkspaceSummary');
  if (!summary || !key) return;
  markElementConfirmed(summary.querySelector('[data-fact-key="' + key + '"]'));
}

function markTimelineEntryConfirmed(key) {
  var list = document.getElementById('outreachList');
  if (!list || !key) return;
  markElementConfirmed(list.querySelector('.tl-item[data-motion-key="' + key + '"]'));
}

function syncCustomerLatestContact(cache) {
  if (!cache) return;
  var dates = (cache.timeline_items || []).filter(function(item) {
    return item.type === 'follow';
  }).map(function(item) {
    return String(item.follow_date || item.date || '').substring(0, 10);
  }).filter(Boolean).sort().reverse();
  // The first timeline page can contain only outreach rows. In that partial
  // view an empty follow-up slice is not proof that the customer has no prior
  // communication, so keep the summary value until the background summary read
  // supplies the authoritative rollup.
  if (dates.length || !(cache.timeline_pagination && cache.timeline_pagination.has_next)) {
    cache.last_contact = dates[0] || '';
  }
}

function findCustomerTimelineEntry(type, id) {
  var cache = _customerDetailCache;
  if (!cache) return null;
  var recordType = type === 'outreach' ? 'outreach' : 'follow';
  var entry = (cache.timeline_items || []).find(function(item) {
    return item.type === recordType && Number(item.id) === Number(id);
  });
  if (entry) return Object.assign({}, entry);
  if (recordType === 'follow' && _followTimelineCache[id]) {
    return Object.assign({ type: 'follow' }, _followTimelineCache[id]);
  }
  return null;
}

// Reconcile the cached workspace with the row the server just returned.  Ordering
// follows the timeline itself (newest first), so an edited date moves the record
// exactly where a later read will place it.
function upsertCustomerTimelineEntry(entry, options) {
  options = options || {};
  var cache = _customerDetailCache;
  if (!cache || !entry || !entry.id) return '';
  var isOutreach = entry.type === 'outreach' || entry.type === 'email';
  var recordType = isOutreach ? 'outreach' : 'follow';
  var key = customerTimelineKey({ type: recordType, id: entry.id });
  var raw = isOutreach ? Object.assign({}, entry, {
    type: 'outreach', id: entry.id, date: entry.sent_date || entry.date || '',
    sent_date: entry.sent_date || entry.date || '', subject: entry.subject || entry.content || '开发邮件',
    content: entry.content || '', reply_content: entry.reply_content || entry.result || '',
    reply_status: entry.reply_status || 'pending', is_reported: entry.is_reported || false
  }) : Object.assign({}, entry, {
    type: 'follow', id: entry.id,
    date: entry.follow_date || entry.date || '',
    follow_date: entry.follow_date || entry.date || ''
  });
  var items = (cache.timeline_items || []).filter(function(item) {
    return !(item.type === recordType && Number(item.id) === Number(entry.id));
  });
  items.push(raw);
  items.sort(function(a, b) {
    var dateOrder = String(b.date || b.follow_date || b.sent_date || '')
      .localeCompare(String(a.date || a.follow_date || a.sent_date || ''));
    if (dateOrder) return dateOrder;
    return String(b.created_at || '').localeCompare(String(a.created_at || '')) || Number(b.id || 0) - Number(a.id || 0);
  });
  cache.timeline_items = items;
  cache.follow_history = items.filter(function(item) { return item.type === 'follow'; });
  cache.outreach_emails = items.filter(function(item) { return item.type === 'outreach'; });
  if (!isOutreach) syncCustomerLatestContact(cache);
  if (options.skipRender !== true) renderFollowTimeline(cache.follow_history, cache.outreach_emails, [key]);
  var workspace = _customerWorkspaceCache[cache.id];
  if (workspace) {
    workspace.timeline = Object.assign({}, workspace.timeline || {}, {
      items: cache.timeline_items, pagination: cache.timeline_pagination || {}
    });
    workspace.savedAt = Date.now();
  }
  return key;
}

function removeCustomerTimelineEntry(type, id, options) {
  options = options || {};
  var cache = _customerDetailCache;
  if (!cache) return '';
  var recordType = type === 'outreach' ? 'outreach' : 'follow';
  var key = customerTimelineKey({ type: recordType, id: id });
  var items = (cache.timeline_items || []).filter(function(item) {
    return !(item.type === recordType && Number(item.id) === Number(id));
  });
  cache.timeline_items = items;
  cache.follow_history = items.filter(function(item) { return item.type === 'follow'; });
  cache.outreach_emails = items.filter(function(item) { return item.type === 'outreach'; });
  if (recordType === 'follow') syncCustomerLatestContact(cache);
  if (cache.recent_facts) {
    cache.recent_facts = cache.recent_facts.filter(function(fact) {
      return !(String(fact.type || '') === recordType && Number(fact.id) === Number(id));
    });
  }
  var workspace = _customerWorkspaceCache[cache.id];
  if (workspace) {
    workspace.timeline = Object.assign({}, workspace.timeline || {}, {
      items: cache.timeline_items, pagination: cache.timeline_pagination || {}
    });
    if (workspace.summary) workspace.summary = Object.assign({}, workspace.summary, {
      last_contact: cache.last_contact || '', recent_facts: cache.recent_facts || []
    });
    workspace.savedAt = Date.now();
  }
  if (options.skipRender !== true) {
    renderFollowTimeline(cache.follow_history, cache.outreach_emails);
    renderCustomerFactsBrief(cache);
  }
  return key;
}

// A read that is still showing earlier pages must not collapse back to the first
// page, and a failed read must not undo a write the user already confirmed.
async function reconcileCustomerTimeline(options) {
  options = options || {};
  if (!_customerDetailCache || !_customerDetailCache.id || _customerTimelinePage !== 1) return;
  try {
    await refreshCustomerTimeline({ silentError: true });
    if (options.includeSummary) await refreshCustomerWorkspace();
  } catch (error) {
    // The confirmed row stays on screen; opening the customer again reconciles
    // anything that changed meanwhile.
  }
}

function recentFactFromCommunication(activity) {
  activity = activity || {};
  return {
    type: 'follow', id: activity.id, date: activity.follow_date || activity.date || '',
    activity_type: activity.activity_type || '', content: activity.content || '',
    result: activity.result || '', source: '沟通记录', source_detail: activity.activity_type || '沟通记录'
  };
}

function recentFactFromOutreach(outreach) {
  outreach = outreach || {};
  return {
    type: 'outreach', id: outreach.id, date: outreach.sent_date || outreach.date || '',
    activity_type: '', content: outreach.subject || outreach.content || '',
    result: outreach.reply_content || outreach.result || '', source: '开发邮件', source_detail: 'outreach'
  };
}

// The workspace summary is deliberately cached while its tabs are open.
// A successful write must therefore update the summary and the "现在" card too,
// not only the timeline, otherwise those keep showing pre-save data until the
// entire browser page is reloaded.
function syncCustomerWorkspaceAfterMutation(customerId, fact, changedKey) {
  customerId = Number(customerId);
  if (!customerId || !fact || !_customerDetailCache || Number(_customerDetailCache.id) !== customerId) return;
  var existingFacts = Array.isArray(_customerDetailCache.recent_facts) ? _customerDetailCache.recent_facts : [];
  _customerDetailCache.recent_facts = [fact].concat(existingFacts.filter(function(item) {
    return !(String(item.type || '') === String(fact.type || '') && Number(item.id) === Number(fact.id));
  })).slice(0, 3);
  var cached = _customerWorkspaceCache[customerId];
  if (cached) {
    cached.summary = Object.assign({}, cached.summary || {}, {
      last_contact: _customerDetailCache.last_contact || '',
      next_follow_up: _customerDetailCache.next_follow_up || '',
      next_task: (_customerDetailCache.reminders || [])[0] || null,
      customer_judgment: _customerDetailCache.customer_judgment || '',
      current_next_step: _customerDetailCache.current_next_step || {},
      recent_facts: _customerDetailCache.recent_facts
    });
    cached.timeline = Object.assign({}, cached.timeline || {}, {
      items: _customerDetailCache.timeline_items || [],
      pagination: _customerDetailCache.timeline_pagination || {}
    });
    cached.savedAt = Date.now();
  }
  renderCustomerFactsBrief(_customerDetailCache);
  markCustomerFactConfirmed(changedKey);
  markTimelineEntryConfirmed(changedKey);
}

function syncCustomerWorkspaceAfterCommunication(customerId, activity, changedKey) {
  syncCustomerWorkspaceAfterMutation(customerId, recentFactFromCommunication(activity),
    changedKey || customerTimelineKey({ type: 'follow', id: activity && activity.id }));
}

function syncCustomerWorkspaceAfterOutreach(customerId, outreach, changedKey) {
  syncCustomerWorkspaceAfterMutation(customerId, recentFactFromOutreach(outreach),
    changedKey || customerTimelineKey({ type: 'outreach', id: outreach && outreach.id }));
}

function timelineItemHtml(item) {
  var typeLabel = item.type === 'email' ? '开发信' : communicationTypeLabel(item.activity_type);
  var typeIcon = item.type === 'email' ? uiIcon('mail') : uiIcon('message');
  var typeClass = item.type === 'email' ? 'tl-outreach' : 'tl-follow';
  var reportIcon = uiIcon('star');
  var reportTitle = item.is_reported ? '从本周工作中移除' : '加入本周工作';
  var reportClass = item.is_reported ? 'tl-report active' : 'tl-report';
  var weeklyStatus = item.is_reported
    ? '<span class="tl-weekly-status" aria-label="已纳入本周工作" title="已纳入本周工作">' + reportIcon + '<span>已纳入本周</span></span>'
    : '';

  var html = '<div class="tl-item ' + typeClass + '"><div class="tl-dot"></div><div class="tl-card">';
  var showDirection = item.type === 'activity' && item.activity_type !== 'task_completed';
  var directionClass = communicationDirectionClass(item.direction);
  var directionTitle = '用于快速查看沟通脉络，并帮助系统判断后续工作重点';
  html += '<div class="tl-card-hd"><span class="tl-type-badge">' + typeIcon + ' ' + typeLabel + '</span>' + (showDirection ? '<span class="tl-direction-badge ' + directionClass + '" title="' + directionTitle + '">' + escapeHtml(communicationDirectionLabel(item.direction)) + '</span>' : '') + weeklyStatus + '<span class="tl-date">' + formatDate(item.date) + '</span><div class="tl-card-actions">';
  var reportType = item.type === 'email' ? 'outreach' : 'follow';
  html += '<button class="tl-report-btn ' + reportClass + '" onclick="toggleReport(\'' + reportType + '\',' + item.id + ')" aria-label="' + reportTitle + '" aria-pressed="' + (item.is_reported ? 'true' : 'false') + '" title="' + reportTitle + '">' + reportIcon + '</button>';
  if (item.type === 'activity') html += '<button class="tl-action-btn" onclick="openFollowEditModal(' + item.id + ')" title="编辑记录">编辑</button><button class="tl-action-btn danger" onclick="deleteFollowLog(' + item.id + ')" title="删除记录">删除</button>';
  if (item.type === 'email') html += '<button class="btn btn-sm btn-danger" onclick="deleteOutreach(' + item.id + ')" style="font-size:0.68rem;padding:2px 6px;">删除</button>';
  var richAttrs = item.type === 'activity' ? ' data-rich-log-id="' + item.id + '" data-rich-field="' + (item.content ? 'content' : 'result') + '"' : '';
  var titleHtml = renderRichText(item.title);
  if (String(item.title || '').length > 280) {
    titleHtml = '<details class="timeline-long-content"><summary><span class="timeline-long-preview">' + titleHtml + '</span></summary></details>';
  }
  html += '</div></div><div class="tl-card-title"' + richAttrs + '>' + titleHtml + '</div>';
  if (item.meta_text) html += '<div class="tl-card-meta" data-rich-log-id="' + item.id + '" data-rich-field="result">' + renderRichText(item.meta_text) + '</div>';
  if (item.type === 'activity' && item.next_plan) html += '<div class="tl-card-plan">由此安排：<span data-rich-log-id="' + item.id + '" data-rich-field="next_plan">' + renderRichText(item.next_plan) + '</span></div>';
  if (item.type === 'email') html += '<div class="tl-card-meta">' + statusBadge(item.reply_status) + '</div>';
  html += '</div></div>';
  return html;
}

function renderCustomerTimelineEmpty(el) {
  var list = el && el.querySelector('.timeline');
  var activeRows = list && list.querySelectorAll('.tl-item:not([data-motion-leaving])');
  var rows = list && list.querySelectorAll('.tl-item');
  if (list && rows && rows.length && shouldAnimateLists()) {
    var token = String(Date.now()) + '-' + Math.random();
    el.dataset.timelineEmptyToken = token;
    if (activeRows && activeRows.length) reconcileKeyedElements(list, [], {
      selector: '.tl-item', leave: true,
      key: function(item) { return item.motionKey; },
      render: timelineItemHtml
    });
    window.setTimeout(function() {
      if (el.dataset.timelineEmptyToken !== token || list.querySelector('.tl-item')) return;
      delete el.dataset.timelineEmptyToken;
      el.textContent = '';
      el.innerHTML = '<div class="empty-state" style="padding:30px;"><p>暂无关系动态</p></div>';
    }, 330);
    renderCustomerTimelineMore(_customerDetailCache && _customerDetailCache.timeline_pagination);
    return;
  }
  delete el.dataset.timelineEmptyToken;
  el.textContent = '';
  el.innerHTML = '<div class="empty-state" style="padding:30px;"><p>暂无关系动态</p></div>';
}

// The timeline is reconciled by record identity instead of being replaced as a
// block: a saved record slides in where it belongs, an edited one keeps its
// place and is tinted once so the user can see which fact the confirmation
// refers to.  `changedKeys` names the records this render was triggered by.
function renderFollowTimeline(followLogs, outreachEmails, changedKeys) {
  var el = document.getElementById('outreachList');
  if (!el) return;
  var changed = {};
  (changedKeys || []).forEach(function(key) { if (key) changed[key] = true; });
  var items = [];
  _followTimelineCache = {};
  (followLogs || []).forEach(function(f) {
    _followTimelineCache[f.id] = f;
    var displayedDirection = f.direction || 'unknown';
    if (displayedDirection === 'unknown') displayedDirection = inferCommunicationDirectionFromText(f.content || f.result || '');
    items.push({
      motionKey: 'follow-' + f.id,
      type: 'activity', activity_type: f.activity_type || 'follow_up', direction: displayedDirection, id: f.id,
      date: f.follow_date || '', title: f.content || f.result || '沟通记录', content: f.content || '',
      result: f.result || '', next_plan: f.next_plan || '', is_reported: f.is_reported || false,
      meta_text: f.content && f.result ? f.result : ''
    });
  });
  (outreachEmails || []).forEach(function(o) {
    items.push({
      motionKey: 'outreach-' + o.id,
      type: 'email', id: o.id, date: o.sent_date || '', title: o.subject || '开发信',
      content: o.content || '', reply_status: o.reply_status || 'pending', is_reported: o.is_reported || false,
      meta_text: o.content ? o.content.substring(0, 100) + (o.content.length > 100 ? '...' : '') : ''
    });
  });
  items.sort(function(a, b) { return b.date.localeCompare(a.date); });

  if (items.length === 0) {
    renderCustomerTimelineEmpty(el);
    renderCustomerTimelineMore(_customerDetailCache && _customerDetailCache.timeline_pagination);
    return;
  }
  delete el.dataset.timelineEmptyToken;

  var list = el.querySelector('.timeline');
  if (!list) {
    el.textContent = '';
    list = document.createElement('div');
    list.className = 'timeline';
    el.appendChild(list);
  }
  reconcileKeyedElements(list, items, {
    selector: '.tl-item',
    leave: true,
    key: function(item) { return item.motionKey; },
    render: function(item) { return timelineItemHtml(item); },
    changed: function(item, node, isNew) {
      // A brand-new record already slides in; only the confirmation tint is
      // added on top so "this is the record I just saved" stays readable.
      if (isNew || changed[item.motionKey]) markElementConfirmed(node);
    }
  });
  renderCustomerTimelineMore(_customerDetailCache && _customerDetailCache.timeline_pagination);
}

// 把“本周工作”标记立即应用到当前工作区缓存与时间线；乐观更新与失败回滚共用。
function applyTimelineReportState(scope, recordType, id, isReported) {
  var cache = liveCustomerCache(scope);
  if (!cache) return false;
  (cache.timeline_items || []).forEach(function(item) {
    if (item.type === recordType && Number(item.id) === Number(id)) item.is_reported = isReported;
  });
  cache.follow_history = (cache.timeline_items || []).filter(function(item) { return item.type === 'follow'; });
  cache.outreach_emails = (cache.timeline_items || []).filter(function(item) { return item.type === 'outreach'; });
  renderFollowTimeline(cache.follow_history, cache.outreach_emails,
    [customerTimelineKey({ type: recordType, id: id })]);
  var workspace = cache.id && _customerWorkspaceCache[cache.id];
  if (workspace && workspace.timeline && Array.isArray(workspace.timeline.items)) {
    workspace.timeline.items.forEach(function(item) {
      if (item.type === recordType && Number(item.id) === Number(id)) item.is_reported = isReported;
    });
    workspace.savedAt = Date.now();
  }
  return true;
}

async function toggleReport(type, id) {
  var customerId = _customerDetailCache && _customerDetailCache.id;
  var scope = beginCustomerScope(customerId);
  var recordType = type === 'follow' ? 'follow' : 'outreach';
  var url = type === 'follow'
    ? '/api/follow-history/' + id + '/report'
    : '/api/outreach/' + id + '/report';
  var current = findCustomerTimelineEntry(recordType, id);
  var previous = current ? !!current.is_reported : null;
  var optimistic = previous === null ? true : !previous;
  // 立即反映用户意图：星标先翻转，网络失败再回滚；确认后以服务端权威结果覆盖，
  // 不再等时间线读取回来才动。
  if (previous !== null) applyTimelineReportState(scope, recordType, id, optimistic);
  var actionId = beginActionStatus('report-' + recordType + '-' + id,
    optimistic ? '正在加入本周工作…' : '正在移出本周工作…');
  try {
    var res = await api(url, { method: 'POST', silentError: true });
    var isReported = !!res.is_reported;
    applyTimelineReportState(scope, recordType, id, isReported);
    showToast(isReported ? '已加入本周工作' : '已从本周工作中移除', 'success');
    finishActionStatus(actionId, 'success', isReported ? '已加入本周工作' : '已从本周工作中移除');
    reconcileCustomerTimeline().catch(function() {});
  } catch(e) {
    if (previous !== null) applyTimelineReportState(scope, recordType, id, previous);
    showToast('本周工作状态未能保存，请重试', 'error');
    finishActionStatus(actionId, 'error', '本周工作状态未保存', function() { toggleReport(type, id); });
  }
}

function updateFollowHistorySaveLabel() {
  var task = document.getElementById('followHistoryNextTask').value.trim();
  var date = document.getElementById('followHistoryNext').value;
  var button = document.getElementById('followHistorySubmit');
  button.textContent = task && date ? '保存并安排下一步' : '只保存记录';
}

async function addFollowHistory() {
  var id = document.getElementById('editCustomerId').value;
  var button = document.getElementById('followHistorySubmit');
  // A record is one atomic write.  Ignore a second click while the first
  // request is in flight so users never see duplicate failures or create
  // duplicate timeline entries when a response is slow.
  if (!id || !button || button.dataset.submitting === 'true') return false;
  var content = richTextHtml(document.getElementById('followHistoryContent'));
  var direction = resolvedCommunicationDirection('history');
  if (!content) { showToast('请填写沟通内容', 'warning'); return false; }
  var nextTask = document.getElementById('followHistoryNextTask').value.trim();
  var nextDate = document.getElementById('followHistoryNext').value;
  if (nextTask && !nextDate) { showToast('安排下一步时需要选择日期', 'warning'); return false; }
  var data = {
    activity_type: (document.getElementById('followHistoryType').value || '其他跟进').trim(),
    direction: direction,
    activity_content: content,
    activity_result: richTextHtml(document.getElementById('followHistoryResult')),
    next_task: nextTask,
    next_follow_up: nextTask ? nextDate : '',
    is_reported: document.getElementById('followHistoryReport').checked
  };
  var workspaceModal = document.getElementById('customerEditModal');
  var workspaceOpen = !!(workspaceModal && workspaceModal.classList.contains('show'));
  var workspaceWasDirty = workspaceOpen && customerModalIsDirty('customerEditModal');
  var scope = beginCustomerScope(id);
  button.dataset.submitting = 'true';
  button.disabled = true;
  var originalLabel = button.textContent;
  button.textContent = '正在保存…';
  try {
    var saved = await api('/api/customers/' + id + '/follow_history', { method: 'POST', body: JSON.stringify(data) });
    showToast(nextTask ? '记录已保存，下一步已安排' : (attentionStateMessage(saved.attention) || '记录已保存'), 'success');
    var cache = liveCustomerCache(scope);
    if (cache) {
      document.getElementById('followHistoryContent').innerHTML = '';
      document.getElementById('followHistoryResult').innerHTML = '';
      document.getElementById('followHistoryNextTask').value = '';
      document.getElementById('followHistoryNext').value = '';
      document.getElementById('followHistoryReport').checked = false;
      document.getElementById('followHistoryDirectionOverride').value = 'auto';
      _communicationAnalyses.history = null;
      updateAutoDirectionPreview('history');
      var composer = document.getElementById('followCompose');
      if (composer) composer.open = false;
    }
    var activity = Object.assign({ type: 'follow' }, saved.activity || {
      id: saved.id, follow_date: saved.recent_contact_date || localDateString(),
      content: data.activity_content, result: data.activity_result, next_plan: data.next_task,
      activity_type: data.activity_type, direction: direction, is_reported: data.is_reported
    });
    if (cache) {
      var changedKey = upsertCustomerTimelineEntry(activity);
      cache.follow_history = cache.timeline_items.filter(function(item) { return item.type === 'follow'; });
      cache.outreach_emails = cache.timeline_items.filter(function(item) { return item.type === 'outreach'; });
      if (saved.completed_task) {
        cache.reminders = (cache.reminders || []).filter(function(task) { return Number(task.id) !== Number(saved.completed_task.id); });
      }
      if (saved.next_step) {
        cache.reminders = (cache.reminders || []).filter(function(task) { return Number(task.id) !== Number(saved.next_step.id); }).concat([saved.next_step]).sort(function(a, b) { return String(a.remind_date || '').localeCompare(String(b.remind_date || '')); });
      }
      // `current_next_step` drives the summary label, while `next_task` is its
      // fallback. Keep both views together so a completed task cannot leave its
      // old date visible beside “没有明确下一步”.
      cache.next_task = (cache.reminders || [])[0] || null;
      cache.last_contact = saved.recent_contact_date || cache.last_contact;
      cache.next_follow_up = saved.next_follow_up || '';
      cache.current_next_step = saved.next_step ? {
        label: saved.next_step.title || saved.next_step.content || '没有明确下一步',
        date: saved.next_step.remind_date || '', source: '待办记录'
      } : { label: '没有明确下一步', date: '', source: '系统事实' };
      document.getElementById('editNextFollowUp').value = cache.next_follow_up;
      renderCustomerNextTask(cache.reminders || []);
      syncCustomerWorkspaceAfterCommunication(id, activity, changedKey);
      // The composer reset and the programmatic 下次跟进 write above are saved
      // server facts, so they must not raise the unsaved-changes prompt.
      if (workspaceOpen && !workspaceWasDirty) markModalClean('customerEditModal');
    }
    // 客户详情可能是从 Today 打开的。后端已经完成了到期待办，
    // 这里刷新底层工作台，让关闭详情后不会留下旧的今日事项。
    if (currentPage === 'dashboard') loadDashboard();
    else if (currentPage === 'customers') loadCustomers({ preservePosition: true });
    reconcileCustomerTimeline({ includeSummary: true }).catch(function() {});
    return true;
  } catch(e) { return false; }
  finally {
    button.disabled = false;
    delete button.dataset.submitting;
    updateFollowHistorySaveLabel();
    // The label can be changed while the request is in flight by a pending
    // input event; keep the default usable even if that event did not fire.
    if (!button.textContent) button.textContent = originalLabel;
  }
}

async function saveCustomerWorkspaceAndExit() {
  var bulkInput = document.getElementById('bulkContactEmails');
  if (bulkInput && bulkInput.value.trim() && !(await addBulkContacts())) return false;
  var contactFields = ['contactName', 'contactTitle', 'contactEmail', 'contactPhone', 'contactWhatsapp', 'contactLinkedin'];
  var hasContactDraft = contactFields.some(function(id) {
    var field = document.getElementById(id);
    return field && field.value.trim();
  });
  if (hasContactDraft && !(await addContact())) return false;
  var followContent = document.getElementById('followHistoryContent');
  if (followContent && richTextPlain(followContent) && !(await addFollowHistory())) return false;
  var saved = await saveCustomer();
  // The button that reaches this handler is labelled 保存并退出, so a successful
  // save must actually leave the workspace instead of only clearing dirty state.
  if (saved) closeModal('customerEditModal', true);
  return saved;
}

async function addOutreach() {
  var id = document.getElementById('editCustomerId').value;
  parseOutreachPaste();
  var subject = document.getElementById('outreachSubject').value.trim();
  if (!subject) { showToast('请粘贴邮件或填写主题', 'warning'); return; }
  var data = { subject: subject, content: document.getElementById('outreachContent').value.trim(), sent_date: document.getElementById('outreachDate').value, reply_status: document.getElementById('outreachReply').value };
  var scope = beginCustomerScope(id);
  try {
    var saved = await api('/api/customers/' + id + '/outreach', { method: 'POST', body: JSON.stringify(data) });
    var cache = liveCustomerCache(scope);
    if (cache && saved && saved.outreach) {
      var changedKey = upsertCustomerTimelineEntry(Object.assign({ type: 'outreach' }, saved.outreach));
      syncCustomerWorkspaceAfterOutreach(id, saved.outreach, changedKey);
    }
    showToast('记录已添加', 'success');
    if (cache) {
      document.getElementById('outreachPaste').value = ''; document.getElementById('outreachSubject').value = ''; document.getElementById('outreachContent').value = '';
      document.getElementById('outreachDate').value = ''; document.getElementById('outreachReply').value = 'pending';
    }
    var composer = document.getElementById('followCompose');
    if (composer) composer.open = false;
    reconcileCustomerTimeline({ includeSummary: true }).catch(function() {});
  } catch(e) {}
}

async function deleteOutreach(outreachId) {
  if (!await showAppConfirm({ title: '删除记录', message: '确认删除这条记录？', submitLabel: '删除' })) return;
  var customerModal = document.getElementById('customerEditModal').classList.contains('show');
  var scope = beginCustomerScope();
  var removed = customerModal ? findCustomerTimelineEntry('outreach', outreachId) : null;
  // 立即从时间线移除；失败时把记录放回原位，避免“看似删掉其实还在”。
  if (customerModal && liveCustomerCache(scope)) removeCustomerTimelineEntry('outreach', outreachId);
  var actionId = beginActionStatus('outreach-delete-' + outreachId, '正在删除记录…');
  try {
    await api('/api/outreach/' + outreachId, { method: 'DELETE', silentError: true });
    showToast('记录已删除', 'success');
    finishActionStatus(actionId, 'success', '记录已删除');
    if (customerModal) reconcileCustomerTimeline({ includeSummary: true }).catch(function() {});
    else loadHistory();
  } catch(e) {
    if (customerModal && removed && liveCustomerCache(scope)) {
      var changedKey = upsertCustomerTimelineEntry(removed);
      syncCustomerWorkspaceAfterOutreach(scope.customerId, removed, changedKey);
    }
    showToast('记录未删除，请重试', 'error');
    finishActionStatus(actionId, 'error', '记录未删除', function() { deleteOutreach(outreachId); });
  }
}


// ========== COMPLETE REMINDER ==========
// 连续打开两次“记录跟进”时（比如快速点两条今日待办），先打开的那次读取返回
// 较慢会重新 fillCompleteModal，把用户已经在第二次弹窗里输入的内容整个清掉。
// fillToken 保证只有最后一次 openCompleteModal 的响应才能填充表单。
var _completeModalFillToken = 0;
function openCompleteModal(reminderId) {
  var fillToken = ++_completeModalFillToken;
  api('/api/reminders/today').then(function(reminders) {
    var r = (reminders || []).find(function(r) { return r.id === reminderId; });
    if (r) {
      if (fillToken === _completeModalFillToken) fillCompleteModal(r);
      return;
    }
    // also try upcoming
    api('/api/reminders/upcoming').then(function(upcoming) {
      r = (upcoming || []).find(function(r) { return r.id === reminderId; });
      if (!r || fillToken !== _completeModalFillToken) return;
      fillCompleteModal(r);
    });
  });
}

function fillCompleteModal(r) {
  document.getElementById('completeReminderId').value = r.id;
  document.getElementById('completeModal').dataset.customerId = r.customer_id || '';
  document.getElementById('completeCustomerName').textContent = r.customer_name || '';
  document.getElementById('completeContent').textContent = r.task_title || r.title || r.content || '';
  document.getElementById('completeActivityType').value = 'whatsapp';
  document.getElementById('completeResult').value = '';
  document.getElementById('completeDirectionOverride').value = 'auto';
  _communicationAnalyses.complete = null;
  updateAutoDirectionPreview('complete');
  var analysisPanel = document.getElementById('completeAnalysis');
  if (analysisPanel) { analysisPanel.hidden = true; analysisPanel.innerHTML = ''; }
  document.getElementById('completeOutcome').value = '';
  document.getElementById('completeNextTask').value = '';
  document.getElementById('completeHasNext').checked = false;
  document.getElementById('completeNextSection').hidden = true;
  var minDate = new Date();
  minDate.setHours(12, 0, 0, 0);
  minDate.setDate(minDate.getDate() + 1);
  document.getElementById('completeNextFollow').min = localDateString(minDate);
  document.getElementById('completeNextFollow').value = '';
  document.querySelectorAll('#completeDateChoices .date-choice').forEach(function(choice) { choice.classList.remove('active'); });
  document.getElementById('completeIsReported').checked = false;
  var moreOptions = document.querySelector('#completeModal .complete-more-options');
  if (moreOptions) moreOptions.open = false;
  document.getElementById('reportCatSection').style.display = 'none';
  document.querySelectorAll('#reportCatPills .cat-pill').forEach(function(p,i){
    p.style.background = i===0 ? 'var(--brand-500)' : '';
    p.style.color = i===0 ? '#fff' : 'var(--text-500)';
    p.style.border = i===0 ? 'none' : '1px solid var(--border-200)';
    p.classList.toggle('active', i===0);
  });
  updateCompleteSaveLabel();
  openModal('completeModal');
  markModalClean('completeModal');
}

function parseOutreachPaste() {
  var paste = document.getElementById('outreachPaste');
  if (!paste || !paste.value.trim()) return;
  var raw = paste.value.replace(/\r\n/g, '\n').trim();
  var subjectMatch = raw.match(/^(?:subject|主题)\s*[:：]\s*(.+)$/im);
  var dateMatch = raw.match(/^(?:date|sent|发送时间|发送日期|日期)\s*[:：]\s*(.+)$/im);
  var subject = subjectMatch ? subjectMatch[1].trim() : '';
  var lines = raw.split('\n');
  var bodyStart = lines.findIndex(function(line) { return !line.trim(); });
  var body = bodyStart >= 0 ? lines.slice(bodyStart + 1).join('\n').trim() : raw;
  if (!subject) {
    var firstContent = lines.find(function(line) { return line.trim() && !/^(?:to|from|cc|date|sent|主题|发送时间|发送日期|日期)\s*[:：]/i.test(line); });
    subject = (firstContent || '开发邮件').trim().substring(0, 160);
  }
  var date = dateMatch ? new Date(dateMatch[1]) : null;
  var dateInput = document.getElementById('outreachDate');
  if (date && !isNaN(date.getTime()) && dateInput) dateInput.value = localDateString(date);
  document.getElementById('outreachSubject').value = subject;
  document.getElementById('outreachContent').value = body || raw;
  var status = /(?:undeliverable|delivery[\s-]?failed|退信|投递失败)/i.test(raw) ? 'bounced'
    : /^(?:re|回复)\s*[:：]/im.test(raw) || /(?:客户回复|回复内容)/.test(raw) ? 'replied' : 'pending';
  document.getElementById('outreachReply').value = status;
}

function toggleCompleteNext() {
  var enabled = document.getElementById('completeHasNext').checked;
  document.getElementById('completeNextSection').hidden = !enabled;
  if (enabled && !document.getElementById('completeNextFollow').value) {
    var defaultChoices = document.querySelectorAll('#completeDateChoices .date-choice');
    if (defaultChoices.length >= 2) setCompleteNextDate(15, defaultChoices[1]);
    document.getElementById('completeNextTask').focus();
  }
  if (!enabled) {
    document.getElementById('completeNextTask').value = '';
    document.getElementById('completeNextFollow').value = '';
  }
  updateCompleteSaveLabel();
}

function setCompleteNextDate(days, button) {
  var date = new Date();
  date.setHours(12, 0, 0, 0);
  date.setDate(date.getDate() + Number(days || 0));
  document.getElementById('completeNextFollow').value = localDateString(date);
  document.querySelectorAll('#completeDateChoices .date-choice').forEach(function(choice) {
    choice.classList.toggle('active', choice === button);
  });
  updateCompleteSaveLabel();
}

function chooseCompleteCustomDate(button) {
  document.querySelectorAll('#completeDateChoices .date-choice').forEach(function(choice) {
    choice.classList.toggle('active', choice === button);
  });
  var input = document.getElementById('completeNextFollow');
  input.focus();
  if (typeof input.showPicker === 'function') {
    try { input.showPicker(); } catch(e) { input.click(); }
  } else {
    input.click();
  }
}

function updateCompleteSaveLabel() {
  var date = document.getElementById('completeNextFollow').value;
  var task = document.getElementById('completeNextTask').value.trim();
  var button = document.getElementById('completeSubmitBtn');
  var hasNext = document.getElementById('completeHasNext').checked;
  if (button) button.textContent = hasNext && task && date ? '完成并安排到 ' + formatChineseDate(date) : '完成任务';
}

// 周报复选框切换分类面板
document.addEventListener('DOMContentLoaded', function(){
  var cb = document.getElementById('completeIsReported');
  if(cb) cb.addEventListener('change', function(){
    document.getElementById('reportCatSection').style.display = this.checked ? 'block' : 'none';
  });
  // 分类标签点击
  var pills = document.getElementById('reportCatPills');
  if(pills){
    pills.addEventListener('click', function(e){
      var p = e.target.closest('.cat-pill');
      if(!p) return;
      pills.querySelectorAll('.cat-pill').forEach(function(el){
        el.classList.remove('active');
        el.style.background = ''; el.style.color = 'var(--text-500)';
        el.style.border = '1px solid var(--border-200)';
      });
      p.classList.add('active');
      p.style.background = 'var(--brand-500)'; p.style.color = '#fff';
      p.style.border = 'none';
    });
  }
});

async function submitComplete() {
  if (!beginWrite('complete')) return;
  try {
    await runSubmitComplete();
  } finally {
    endWrite('complete');
  }
}

async function runSubmitComplete() {
  var id = document.getElementById('completeReminderId').value;
  var content = document.getElementById('completeResult').value.trim();
  var direction = resolvedCommunicationDirection('complete');
  var hasNext = document.getElementById('completeHasNext').checked;
  var nextTask = hasNext ? document.getElementById('completeNextTask').value.trim() : '';
  var nextDate = hasNext ? document.getElementById('completeNextFollow').value.trim() : '';
  if (!content) { showToast('请简单记录这次发生了什么', 'warning'); document.getElementById('completeResult').focus(); return; }
  if (hasNext && (!nextTask || !nextDate)) { showToast('请填写下一步动作和日期', 'warning'); return; }
  var data = {
    activity_type: document.getElementById('completeActivityType').value,
    direction: direction,
    activity_content: content,
    activity_result: document.getElementById('completeOutcome').value.trim(),
    next_task: nextTask,
    next_follow_up: nextTask ? nextDate : '',
    is_reported: document.getElementById('completeIsReported').checked ? 1 : 0
  };
  try {
    var saved = await api('/api/reminders/' + id, { method: 'PUT', body: JSON.stringify(data) });
    showToast(nextTask ? '记录已保存，下一步已安排' : (attentionStateMessage(saved.attention) || '记录已保存，当前任务已完成'), 'success');
    closeModal('completeModal', true);
    if (currentPage === 'dashboard') loadDashboard();
    else if (currentPage === 'calendar') loadCalendar();
  } catch(e) {}
}

// ========== ADD CUSTOMER MODALS ==========
function draftContactHtml(prefix, index) {
  var primary = index === 0;
  return '<div class="draft-contact-card" data-index="' + index + '"><div class="draft-contact-title"><strong>' + (primary ? '主要联系人' : '联系人 ' + (index + 1)) + '</strong>' +
    (!primary ? '<button type="button" class="text-action danger" onclick="this.closest(\'.draft-contact-card\').remove()">移除</button>' : '<span>暂时不知道可以留空</span>') + '</div>' +
    '<div class="form-row"><div class="form-group"><label class="form-label">姓名</label><input class="form-control draft-contact-name" placeholder="联系人姓名"></div><div class="form-group"><label class="form-label">邮箱</label><input type="email" class="form-control draft-contact-email" placeholder="name@company.com" onblur="dedupeDraftContactCards(\'' + prefix + '\')"></div></div>' +
    '<div class="form-group"><label class="form-label">WhatsApp</label><input class="form-control draft-contact-whatsapp" placeholder="含国家区号"></div>' +
    '<details class="draft-contact-more"><summary>更多联系方式</summary><div class="form-row"><div class="form-group"><label class="form-label">职位</label><input class="form-control draft-contact-title-input" placeholder="采购、负责人、技术等"></div><div class="form-group"><label class="form-label">电话</label><input class="form-control draft-contact-phone" placeholder="含国家区号"></div></div>' +
    '<div class="form-group"><label class="form-label">LinkedIn</label><input class="form-control draft-contact-linkedin" placeholder="个人主页链接"></div>' +
    '<div class="form-group"><label class="form-label">首选联系方式</label><select class="form-control draft-contact-channel"><option value="">暂不确定</option><option value="email">邮件</option><option value="whatsapp">WhatsApp</option><option value="phone">电话</option><option value="linkedin">LinkedIn</option></select></div></details></div>';
}

function resetDraftContacts(prefix) {
  var container = document.getElementById(prefix + 'DraftContacts');
  if (container) container.innerHTML = draftContactHtml(prefix, 0);
}

function toggleAddCustomerAdvanced(prefix) {
  var modal = document.getElementById(prefix === 'new' ? 'addNewCustomerModal' : 'addCustomerModal');
  if (!modal) return;
  var showing = modal.classList.toggle('show-add-advanced');
  var button = modal.querySelector('.add-more-toggle');
  if (button) button.textContent = showing ? '收起更多资料' : '更多公司资料';
}

function normalizeEmailList(text) {
  return Array.from(new Set(String(text || '').split(/[\s,;]+/).map(function(value) {
    return value.trim().toLowerCase();
  }).filter(Boolean)));
}

var _emailImportTimers = {};
var _emailVerificationPollers = {};
var _smartFillTimer = null;
function scheduleSmartFillCustomer(type) {
  clearTimeout(_smartFillTimer);
  _smartFillTimer = setTimeout(function() { smartFillCustomer(type); }, 250);
}
function scheduleEmailImport(prefix) {
  clearTimeout(_emailImportTimers[prefix]);
  _emailImportTimers[prefix] = setTimeout(function() {
    if (prefix === 'bulk') addBulkContacts();
    else importDraftEmails(prefix);
  }, 350);
}

function renderEmailValidation(panelId, result) {
  var panel = document.getElementById(panelId);
  if (!panel) return;
  var counts = result.counts || {};
  var items = result.results || [];
  panel.hidden = false;
  panel.innerHTML = '<strong>邮箱可发送性检查：可尝试发送 ' + Number(counts.valid || 0) + ' · 需核对 ' + Number(counts.suspicious || 0) + ' · 无法发送 ' + Number(counts.invalid || 0) + ' · 已存在 ' + Number(counts.duplicate || 0) + '</strong>' +
    '<div class="email-validation-items">' + items.map(function(item) {
      var meta = [item.confidence === 'high' ? '高置信' : item.confidence === 'medium' ? '中等置信' : '', item.address_type === 'role_account' ? '部门邮箱' : item.address_type === 'free_provider' ? '免费邮箱' : ''].filter(Boolean).join(' · ');
      return '<div class="email-validation-item email-' + escapeHtml(item.status) + '"><span class="email-status">' + escapeHtml(item.category || item.status) + '</span><div><b>' + escapeHtml(item.normalized || item.email) + '</b><small>' + escapeHtml([meta, (item.reasons || [item.reason]).join('；')].filter(Boolean).join('：')) + '</small></div></div>';
    }).join('') + '</div>';
}

function removeDraftContactsByEmail(prefix, emails) {
  var blocked = new Set((emails || []).map(function(email) { return String(email || '').toLowerCase(); }));
  if (!blocked.size) return 0;
  var container = document.getElementById(prefix + 'DraftContacts');
  if (!container) return 0;
  var removed = 0;
  container.querySelectorAll('.draft-contact-card').forEach(function(card) {
    var input = card.querySelector('.draft-contact-email');
    if (input && blocked.has(String(input.value || '').trim().toLowerCase())) {
      card.remove();
      removed += 1;
    }
  });
  return removed;
}

function watchDraftEmailVerification(prefix, emails, attempts) {
  var pending = Array.from(new Set((emails || []).filter(Boolean)));
  if (!pending.length || attempts >= 12) return;
  clearTimeout(_emailVerificationPollers[prefix]);
  _emailVerificationPollers[prefix] = setTimeout(async function() {
    try {
      var query = pending.map(function(email) { return 'email=' + encodeURIComponent(email); }).join('&');
      var progress = await api('/api/emails/verification-jobs?' + query);
      var invalid = (progress.jobs || []).filter(function(job) {
        return ['invalid_address', 'invalid_domain', 'domain_does_not_accept_mail', 'invalid_mailbox'].indexOf(job.deliverability_status) >= 0;
      }).map(function(job) { return job.email; });
      var removed = removeDraftContactsByEmail(prefix, invalid);
      if (removed) showToast('已自动移除 ' + removed + ' 个无法发送的邮箱', 'warning');
      var stillPending = (progress.jobs || []).filter(function(job) {
        return job.job_status === 'queued' || job.job_status === 'running';
      }).map(function(job) { return job.email; });
      if (stillPending.length) watchDraftEmailVerification(prefix, stillPending, attempts + 1);
    } catch (e) {}
  }, 3000);
}

async function importDraftEmails(prefix) {
  var input = document.getElementById(prefix + 'DraftEmailImport');
  if (!input) return;
  var emails = normalizeEmailList(input.value);
  if (!emails.length) { showToast('没有找到有效邮箱', 'warning'); return; }
  var validationPanel = document.getElementById(prefix + 'EmailValidation');
  validationPanel.hidden = false;
  validationPanel.innerHTML = '<strong>正在检查邮箱可发送性…</strong><span>检查格式、邮件路由、一次性邮箱、免费邮箱和部门邮箱</span>';
  try {
    var result = await api('/api/emails/validate', { method: 'POST', body: JSON.stringify({ emails: emails }) });
    var accepted = (result.results || []).filter(function(item) {
      return item.status === 'valid' || item.status === 'suspicious';
    });
    var invalidEmails = (result.results || []).filter(function(item) {
      return item.status === 'invalid';
    }).map(function(item) { return item.normalized || item.email; });
    var existing = collectDraftContacts(prefix);
    var existingEmails = existing.map(function(contact) { return String(contact.email || '').toLowerCase(); });
    accepted.forEach(function(item) {
      var normalized = item.normalized || item.email;
      if (existingEmails.indexOf(normalized) >= 0) return;
      existing.push({ name: normalized.split('@')[0], email: normalized, preferred_channel: 'email', contact_type: 'person' });
      existingEmails.push(normalized);
    });
    applyDraftContacts(prefix, existing);
    var removed = removeDraftContactsByEmail(prefix, invalidEmails);
    input.value = '';
    renderEmailValidation(prefix + 'EmailValidation', result);
    var queuedCount = (result.results || []).filter(function(item) { return item.smtp_job_status === 'queued' || item.smtp_job_status === 'running'; }).length;
    var queuedEmails = (result.results || []).filter(function(item) { return item.smtp_job_status === 'queued' || item.smtp_job_status === 'running'; }).map(function(item) { return item.normalized || item.email; });
    if (queuedEmails.length) watchDraftEmailVerification(prefix, queuedEmails, 0);
    showToast((accepted.length ? ('已自动添加 ' + accepted.length + ' 个邮箱') : '未添加可用邮箱') + (removed ? ('；已移除 ' + removed + ' 个无效邮箱') : '') + (queuedCount ? ('；' + queuedCount + ' 个继续后台复核') : ''), accepted.length ? 'success' : 'warning');
  } catch (e) {
    validationPanel.innerHTML = '<strong>邮箱验证失败</strong><span>' + escapeHtml(e.message || '请稍后重试') + '</span>';
  }
}

function addDraftContact(prefix) {
  var container = document.getElementById(prefix + 'DraftContacts');
  if (!container) return;
  var index = container.querySelectorAll('.draft-contact-card').length;
  container.insertAdjacentHTML('beforeend', draftContactHtml(prefix, index));
}

function collectDraftContacts(prefix) {
  var container = document.getElementById(prefix + 'DraftContacts');
  if (!container) return [];
  var contacts = Array.from(container.querySelectorAll('.draft-contact-card')).map(function(card) {
    return {
      name: card.querySelector('.draft-contact-name').value.trim(),
      title: card.querySelector('.draft-contact-title-input').value.trim(),
      email: card.querySelector('.draft-contact-email').value.trim(),
      phone: card.querySelector('.draft-contact-phone').value.trim(),
      whatsapp: card.querySelector('.draft-contact-whatsapp').value.trim(),
      linkedin: card.querySelector('.draft-contact-linkedin').value.trim(),
      preferred_channel: card.querySelector('.draft-contact-channel').value,
      contact_type: 'person'
    };
  }).filter(function(contact) { return contact.name || contact.email || contact.phone || contact.whatsapp || contact.linkedin; });
  return mergeDraftContactsByEmail(contacts);
}

function mergeDraftContactsByEmail(contacts) {
  var merged = [];
  var byEmail = {};
  var genericNames = ['公司公共邮箱', '公共邮箱', '联系人', 'contact', 'info'];
  (contacts || []).forEach(function(contact) {
    var copy = Object.assign({}, contact);
    copy.email = String(copy.email || '').trim().toLowerCase();
    var key = copy.email;
    if (!key || !byEmail[key]) {
      merged.push(copy);
      if (key) byEmail[key] = copy;
      return;
    }
    var target = byEmail[key];
    ['name', 'title', 'phone', 'whatsapp', 'linkedin', 'preferred_channel', 'contact_type'].forEach(function(field) {
      if (!target[field] && copy[field]) target[field] = copy[field];
    });
    if (copy.name && genericNames.indexOf(String(target.name || '').toLowerCase()) >= 0 && genericNames.indexOf(copy.name.toLowerCase()) < 0) target.name = copy.name;
  });
  return merged;
}

function dedupeDraftContactCards(prefix) {
  var container = document.getElementById(prefix + 'DraftContacts');
  if (!container) return;
  var emails = Array.from(container.querySelectorAll('.draft-contact-email')).map(function(input) {
    return String(input.value || '').trim().toLowerCase();
  }).filter(Boolean);
  var hasDuplicate = (new Set(emails)).size < emails.length;
  if (!hasDuplicate) return;
  var contacts = collectDraftContacts(prefix);
  applyDraftContacts(prefix, contacts);
  showToast('相同邮箱已合并为一个联系人', 'success');
}

function validateDraftContacts(contacts) {
  var invalid = (contacts || []).find(function(contact) {
    return contact.email && !/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(contact.email);
  });
  if (invalid) {
    showToast('联系人邮箱格式不正确：' + invalid.email, 'warning');
    return false;
  }
  return true;
}

function applyDraftContacts(prefix, contacts) {
  if (!contacts || !contacts.length) return;
  var container = document.getElementById(prefix + 'DraftContacts');
  if (!container) return;
  contacts = mergeDraftContactsByEmail(contacts);
  resetDraftContacts(prefix);
  for (var i = 1; i < contacts.length; i++) addDraftContact(prefix);
  var cards = document.querySelectorAll('#' + prefix + 'DraftContacts .draft-contact-card');
  contacts.forEach(function(contact, index) {
    var card = cards[index];
    if (!card) return;
    card.querySelector('.draft-contact-name').value = contact.name || '';
    card.querySelector('.draft-contact-title-input').value = contact.title || '';
    card.querySelector('.draft-contact-email').value = contact.email || '';
    card.querySelector('.draft-contact-phone').value = contact.phone || '';
    card.querySelector('.draft-contact-whatsapp').value = contact.whatsapp || '';
    card.querySelector('.draft-contact-linkedin').value = contact.linkedin || '';
    card.querySelector('.draft-contact-channel').value = contact.preferred_channel || (contact.email ? 'email' : (contact.whatsapp ? 'whatsapp' : ''));
  });
  var manualEntry = container.closest('.manual-contact-entry');
  if (manualEntry) manualEntry.open = true;
}

function openAddCustomerModal() {
  document.getElementById('addCustomerModal').classList.remove('show-add-advanced');
  var advancedToggle = document.getElementById('addCustomerModal').querySelector('.add-more-toggle');
  if (advancedToggle) advancedToggle.textContent = '更多公司资料';
  document.getElementById('addExistName').value = '';
  document.getElementById('addExistWebsite').value = '';
  document.getElementById('addExistTags').value = '';
  document.getElementById('addExistLevel').value = 'C';
  document.getElementById('addExistType').value = '';
  document.getElementById('addExistField').value = '';
  document.getElementById('addExistBusinessStage').value = '';
  document.getElementById('addExistSource').value = '';
  document.getElementById('addExistSourceDetail').value = '';
  document.getElementById('addExistNextFollow').value = '';
  document.getElementById('addExistProfile').value = '';
  document.getElementById('addExistNotes').value = '';
  document.getElementById('addExistCountry').value = '';
  resetDraftContacts('exist');
  document.getElementById('existSmartFillReview').hidden = true;
  document.getElementById('existSmartFillReview').className = 'smart-fill-review';
  var recognitionSummary = document.getElementById('existRecognitionSummary');
  if (recognitionSummary) { recognitionSummary.hidden = true; recognitionSummary.innerHTML = ''; }
  _pendingSmartFill = null;
  // Invalidate any in-flight 自动识别 so its preview cannot pop up later over an
  // unrelated page (or a reopened modal for another customer).
  _smartFillRequestToken++;
  openModal('addCustomerModal');
  markModalClean('addCustomerModal');
}

var _pendingSmartFill = null;
var _smartFillRequestToken = 0;

function renderSmartFillPreview(type, result) {
  var statusOk = result.website_status === 'read' || result.website_status === 'search_only';
  var pageCount = (result.website_pages || []).length;
  var websiteStatus = result.website_status === 'read'
    ? '<div class="smart-preview-status success"><strong>官网读取成功' + (pageCount > 1 ? ' · 已读取首页及 ' + (pageCount - 1) + ' 个介绍页面' : '') + '</strong><span>' + escapeHtml(result.website || '') + '</span></div>'
    : result.website_status === 'search_only'
      ? '<div class="smart-preview-status success"><strong>官网未能直接读取，已使用 Exa 公开摘要</strong><span>' + escapeHtml(result.website_error || result.website || '') + '</span></div>'
      : '<div class="smart-preview-status error"><strong>' + (result.website_status === 'not_provided' ? '未提供官网' : '官网未能读取') + '</strong><span>' + escapeHtml(result.website_error || '可以继续手动填写公司资料') + '</span></div>';
  function previewField(label, value, key) {
    var source = (result.sources || {})[key] || '待确认';
    return '<div><span>' + label + '</span><strong>' + escapeHtml(value || '未识别') + '</strong><small>' + escapeHtml(source) + '</small></div>';
  }
  var contacts = (result.contacts || []).map(function(contact) {
    return '<li><strong>' + escapeHtml(contact.name || contact.email || '联系人') + '</strong><span>' + escapeHtml([contact.title, contact.email, contact.phone || contact.whatsapp, contact.linkedin].filter(Boolean).join(' · ')) + '</span><small>' + escapeHtml(contact.source || '官网事实，待确认') + '</small></li>';
  }).join('');
  var profile = result.profile || (statusOk ? '官网中没有提取到可核实的公司简介。' : '网站未成功读取，暂时无法生成公司简介。');
  var profileSource = (result.sources || {}).profile || '待确认';
  var facts = (result.website_facts || []).map(function(item) {
    return '<li><strong>' + escapeHtml(item.field || '事实') + '</strong><span>' + escapeHtml(item.value || '') + '</span><small>' + escapeHtml(item.source || '官网事实') + '</small></li>';
  }).join('');
  var sourceLinks = (result.source_links || []).slice(0, 5).map(function(item) {
    return '<li><a href="' + escapeHtml(item.url || '#') + '" target="_blank" rel="noopener noreferrer">' + escapeHtml(item.title || item.url || '来源页面') + '</a><span>' + escapeHtml(item.snippet || '') + '</span></li>';
  }).join('');
  var methodNote = result.website_read_method === 'web_fetch_exa'
    ? '网页正文由 Exa MCP 读取'
    : (result.browser_tools_used ? '动态页面由 browser-tools 读取' : (result.exa_used ? 'Exa MCP 已补充公开来源' : '当前未使用外部搜索摘要'));
  document.getElementById('smartFillPreviewContent').innerHTML = websiteStatus +
    '<div class="smart-preview-grid">' + previewField('公司', result.name, 'name') +
    previewField('国家 / 地区', result.country, 'country') +
    previewField('客户类型', result.type, 'type') +
    previewField('行业', result.field, 'field') + '</div>' +
    '<section class="smart-preview-profile"><span>' + (result.ai_used ? 'AI 整理的公司简介（待确认）' : '官网简介事实') + '</span><p>' + escapeHtml(profile) + '</p><small>' + escapeHtml(profileSource) + '</small></section>' +
    (facts ? '<section class="smart-preview-contacts smart-preview-facts"><span>官网直接提取的事实</span><ul>' + facts + '</ul></section>' : '') +
    (contacts ? '<section class="smart-preview-contacts"><span>识别到的联系方式</span><ul>' + contacts + '</ul></section>' : '') +
    (sourceLinks ? '<section class="smart-preview-contacts smart-preview-sources"><span>外部来源（只读）</span><ul>' + sourceLinks + '</ul></section>' : '') +
    '<p class="smart-preview-note">' + escapeHtml(methodNote) + '。结构化字段尚未写入客户表；点击“确认并应用”后才会填入表单，保存客户仍需再次点击保存。</p>';
  document.getElementById('smartFillApplyButton').textContent = statusOk ? '确认并应用' : '应用可用信息';
  openModal('smartFillPreviewModal');
}

async function smartFillCustomer(type) {
  var isNew = type === 'new';
  var company = document.getElementById(isNew ? 'newCustomerName' : 'addExistName').value.trim();
  var website = document.getElementById(isNew ? 'newCustomerWebsite' : 'addExistWebsite').value.trim();
  if (!company && !website) { showToast('请先输入公司名称或网站', 'warning'); return; }

  var prefix = isNew ? 'new' : 'exist';
  var button = document.getElementById(prefix + 'SmartFillButton');
  var review = document.getElementById(prefix + 'SmartFillReview');
  var progressStep = 0;
  var requestToken = ++_smartFillRequestToken;
  var progressMessages = ['正在连接网站…', '正在读取官网正文…', '正在整理可核实的官网事实…'];
  button.disabled = true;
  button.textContent = '正在识别…';
  review.hidden = false;
  review.className = 'smart-fill-review is-loading';
  review.innerHTML = '<strong>' + progressMessages[0] + '</strong><span>通常需要几秒，请不要关闭窗口。</span>';
  var progressTimer = setInterval(function() {
    progressStep = Math.min(progressStep + 1, progressMessages.length - 1);
    review.querySelector('strong').textContent = progressMessages[progressStep];
  }, 2500);

  try {
    var result = await api('/api/customers/smart-import', {
      method: 'POST', body: JSON.stringify({ company: company, website: website, use_ai: false })
    });
    if (requestToken !== _smartFillRequestToken) return;
    var hostModal = document.getElementById(isNew ? 'addNewCustomerModal' : 'addCustomerModal');
    if (!hostModal || !hostModal.classList.contains('show')) return;
    _pendingSmartFill = { type: type, result: result, originalCompany: company, originalWebsite: website };
    review.className = 'smart-fill-review ' + (result.website_status === 'error' ? 'has-error' : 'is-ready');
    review.innerHTML = result.website_status === 'error'
      ? '<strong>识别完成，可应用已有信息</strong><span>' + escapeHtml(result.website_error || '请检查网址后重试') + '；结果仍需你确认。</span>'
      : '<strong>识别完成，等待确认</strong><span>结果尚未填入表单，确认后再补充并保存。</span>';
    renderSmartFillPreview(type, result);
  } catch(e) {
    review.className = 'smart-fill-review has-error';
    review.innerHTML = '<strong>识别失败</strong><span>' + escapeHtml(e.message || '服务暂时不可用') + '</span>';
  } finally {
    clearInterval(progressTimer);
    button.disabled = false;
    button.innerHTML = uiIcon('sparkle') + '自动识别';
  }
}

function applySmartFillResult(pending) {
  if (!pending) return;
  var result = pending.result;
  var isNew = pending.type === 'new';
  var nameEl = document.getElementById(isNew ? 'newCustomerName' : 'addExistName');
  var websiteEl = document.getElementById(isNew ? 'newCustomerWebsite' : 'addExistWebsite');
  if (result.name && !pending.originalCompany) nameEl.value = result.name;
  if (result.website && !pending.originalWebsite) websiteEl.value = result.website;
  if (result.country) document.getElementById(isNew ? 'newCustomerCountry' : 'addExistCountry').value = result.country;
  var typeEl = document.getElementById(isNew ? 'newCustomerType' : 'addExistType');
  if (typeEl && result.type) typeEl.value = result.type;
  var fieldEl = document.getElementById(isNew ? 'newCustomerField' : 'addExistField');
  if (result.field && !fieldEl.value) fieldEl.value = result.field;
  var profileEl = document.getElementById(isNew ? 'newCustomerProfile' : 'addExistProfile');
  if (result.profile) profileEl.value = result.profile;
  if (result.contacts && result.contacts.length) applyDraftContacts(isNew ? 'new' : 'exist', result.contacts);
  var summary = document.getElementById(isNew ? 'newRecognitionSummary' : 'existRecognitionSummary');
  if (summary) {
    var facts = [['国家', result.country], ['类型', result.type], ['行业', result.field]].filter(function(item) { return item[1]; });
    summary.hidden = facts.length === 0;
    summary.innerHTML = facts.map(function(item) { return '<span><b>' + item[0] + '：</b>' + escapeHtml(item[1]) + '</span>'; }).join('');
  }
  _pendingSmartFill = null;
}

function confirmSmartFillResult() {
  if (!_pendingSmartFill) { closeModal('smartFillPreviewModal', true); return; }
  applySmartFillResult(_pendingSmartFill);
  closeModal('smartFillPreviewModal', true);
  showToast('识别结果已应用', 'success');
}

async function submitExistCustomer(copyEmails) {
  if (!beginWrite('existCustomer')) return;
  try {
    var name = document.getElementById('addExistName').value.trim();
    if (!name) { showToast('请填写公司名称', 'warning'); return; }

    var country = document.getElementById('addExistCountry').value.trim();

    var contacts = collectDraftContacts('exist');
    if (!validateDraftContacts(contacts)) return;
    var data = {
      name: name, company: name,
      country: country,
      level: document.getElementById('addExistLevel').value,
      business_role: document.getElementById('addExistType').value,
      field: document.getElementById('addExistField').value.trim(),
      business_stage: document.getElementById('addExistBusinessStage').value,
      source: document.getElementById('addExistSource').value,
      source_detail: document.getElementById('addExistSourceDetail').value.trim(),
      next_follow_up: document.getElementById('addExistNextFollow').value,
      website: document.getElementById('addExistWebsite').value.trim(),
      tags: document.getElementById('addExistTags').value.trim(),
      profile: document.getElementById('addExistProfile').value.trim(),
      notes: document.getElementById('addExistNotes').value.trim(),
      contacts: contacts
    };
    try {
      var saved = await api('/api/customers', { method: 'POST', body: JSON.stringify(data) });
      showToast(copyEmails ? '客户已保存' : '客户添加成功', 'success');
      markModalClean('addCustomerModal');
      closeModal('addCustomerModal', true);
      if (copyEmails) await copyEmailsToClipboard(contacts.map(function(contact) { return contact.email; }));
      if (currentPage === 'customers') loadCustomers(); else loadDashboard();
    } catch(e) {}
  } finally {
    endWrite('existCustomer');
  }
}

function openAddNewCustomerModal() {
  document.getElementById('addNewCustomerModal').classList.remove('show-add-advanced');
  var advancedToggle = document.getElementById('addNewCustomerModal').querySelector('.add-more-toggle');
  if (advancedToggle) advancedToggle.textContent = '更多公司资料';
  document.getElementById('newCustomerName').value = '';
  document.getElementById('newCustomerCountry').value = '';
  document.getElementById('newCustomerField').value = '';
  document.getElementById('newCustomerType').value = '';
  document.getElementById('newCustomerSource').value = '';
  document.getElementById('newCustomerSourceDetail').value = '';
  document.getElementById('newCustomerWebsite').value = '';
  document.getElementById('newCustomerProfile').value = '';
  document.getElementById('newCustomerNotes').value = '';
  resetDraftContacts('new');
  document.getElementById('newSmartFillReview').hidden = true;
  document.getElementById('newSmartFillReview').className = 'smart-fill-review';
  _pendingSmartFill = null;
  // See openAddCustomerModal: retire any in-flight recognition for a modal that
  // is no longer (or no longer the same) open.
  _smartFillRequestToken++;
  openModal('addNewCustomerModal');
  markModalClean('addNewCustomerModal');
}

async function submitNewCustomer() {
  if (!beginWrite('newCustomer')) return;
  try {
    var name = document.getElementById('newCustomerName').value.trim();
    if (!name) { showToast('请填写公司名称', 'warning'); return; }
    var contacts = collectDraftContacts('new');
    if (!validateDraftContacts(contacts)) return;
    var data = {
      name: name, company: name,
      country: document.getElementById('newCustomerCountry').value.trim(),
      type: document.getElementById('newCustomerType').value,
      field: document.getElementById('newCustomerField').value.trim(),
      source: document.getElementById('newCustomerSource').value,
      source_detail: document.getElementById('newCustomerSourceDetail').value.trim(),
      website: document.getElementById('newCustomerWebsite').value.trim(),
      profile: document.getElementById('newCustomerProfile').value.trim(),
      notes: document.getElementById('newCustomerNotes').value.trim(),
      contacts: contacts
    };
    try {
      await api('/api/customers', { method: 'POST', body: JSON.stringify(data) });
      showToast('新客户已添加', 'success');
      markModalClean('addNewCustomerModal');
      closeModal('addNewCustomerModal', true);
      loadDashboard();
    } catch(e) {}
  } finally {
    endWrite('newCustomer');
  }
}

function openBatchAddModal() {
  document.getElementById('batchAddText').value = '';
  openModal('batchAddModal');
}

async function submitBatchAdd() {
  if (!beginWrite('batchAdd')) return;
  try {
    var text = document.getElementById('batchAddText').value.trim();
    if (!text) { showToast('请输入客户数据', 'warning'); return; }
    var lines = text.split('\n').filter(function(l) { return l.trim(); });
    var count = 0;
    for (var i = 0; i < lines.length; i++) {
      var parts = lines[i].split(',').map(function(s) { return s.trim(); });
      if (parts[0]) {
        try {
          await api('/api/customers', { method: 'POST', body: JSON.stringify({ name: parts[0], company: parts[1] || '', country: parts[2] || '', field: parts[3] || '', notes: parts[4] || '' }) });
          count++;
        } catch(e) {}
      }
    }
    showToast('Added ' + count + ' new clients', 'success');
    closeModal('batchAddModal', true);
    loadDashboard();
  } finally {
    endWrite('batchAdd');
  }
}

// ========== CALENDAR ==========
async function loadCalendar() {
  _pageDataLoadedAt = Date.now();
  try {
    var results = await Promise.all([api('/api/reminders/today'), api('/api/reminders/upcoming')]);
    var reminders = results[0];
    var upcoming = results[1];
    calendarData = {};
    var all = reminders.concat(upcoming);
    all.forEach(function(r) {
      var d = r.remind_date ? r.remind_date.substring(0, 10) : '';
      if (!d) return;
      if (!calendarData[d]) calendarData[d] = [];
      calendarData[d].push(r);
    });
    renderCalendar();
  } catch(e) { calendarData = {}; renderCalendar(); }
}

function renderCalendar() {
  var grid = document.getElementById('calendarGrid');
  var title = document.getElementById('calendarTitle');
  title.textContent = calendarYear + '年' + (calendarMonth + 1) + '月';
  var dayNames = ['日','一','二','三','四','五','六'];
  var html = '';
  dayNames.forEach(function(d) { html += '<div class="calendar-day-header">' + d + '</div>'; });
  var firstDay = new Date(calendarYear, calendarMonth, 1).getDay();
  var daysInMonth = new Date(calendarYear, calendarMonth + 1, 0).getDate();
  var daysInPrevMonth = new Date(calendarYear, calendarMonth, 0).getDate();
  var today = new Date();
  var todayStr = today.getFullYear() + '-' + String(today.getMonth() + 1).padStart(2, '0') + '-' + String(today.getDate()).padStart(2, '0');
  for (var i = firstDay - 1; i >= 0; i--) {
    var d = daysInPrevMonth - i;
    html += '<div class="calendar-day other-month"><div class="day-num">' + d + '</div></div>';
  }
  for (var d = 1; d <= daysInMonth; d++) {
    var dateStr = calendarYear + '-' + String(calendarMonth + 1).padStart(2, '0') + '-' + String(d).padStart(2, '0');
    var isToday = dateStr === todayStr;
    var tasks = calendarData[dateStr] || [];
    var hasOverdue = tasks.some(function(t) { return t.remind_date < todayStr; });
    html += '<div class="calendar-day' + (isToday ? ' today' : '') + '" onclick="showCalendarDetail(\'' + dateStr + '\')">' +
      '<div class="day-num">' + d + '</div>' +
      (tasks.length > 0 ? '<div class="day-tasks' + (hasOverdue ? ' overdue' : '') + '">' + tasks.length + ' 项</div>' : '') + '</div>';
  }
  var totalCells = firstDay + daysInMonth;
  var remaining = (7 - (totalCells % 7)) % 7;
  for (var d = 1; d <= remaining; d++) {
    html += '<div class="calendar-day other-month"><div class="day-num">' + d + '</div></div>';
  }
  grid.innerHTML = html;
  document.getElementById('calendarDetail').innerHTML = '';
}

function showCalendarDetail(dateStr) {
  var tasks = calendarData[dateStr] || [];
  var el = document.getElementById('calendarDetail');
  if (tasks.length === 0) {
    el.innerHTML = '<div class="card" style="margin-top:16px;"><div class="card-body"><div class="empty-state" style="padding:30px;"><p>' + formatChineseDate(dateStr) + '没有安排</p></div></div></div>';
    return;
  }
  var html = '<div class="card" style="margin-top:16px;"><div class="card-header"><h3>' + formatChineseDate(dateStr) + ' · ' + tasks.length + ' 项</h3></div><div class="card-body">';
  tasks.forEach(function(r) {
    var overdue = r.remind_date < localDateString();
    var basicInfo = '<span style="color:var(--fg-secondary);font-size:0.85rem;font-weight:600;">' + (r.customer_company || r.customer_name || '客户') + '</span>';
    if (r.country) basicInfo += ' <span style="color:var(--fg-light);margin:0 4px;">·</span> <span style="color:var(--fg-secondary);font-size:0.82rem;">' + r.country + '</span>';
    if (r.business_role === '中间商' || r.business_role === '终端') basicInfo += ' <span class="badge" style="background:var(--bg-warm);color:var(--fg-muted);border-color:var(--border);padding:1px 6px;font-size:0.68rem;">' + r.business_role + '</span>';
    if (r.field) basicInfo += ' <span style="color:var(--fg-light);margin:0 4px;">·</span> <span style="color:var(--fg-muted);font-size:0.78rem;">' + r.field + '</span>';
    var profileHtml = r.profile ? '<div style="font-size:0.78rem;color:var(--fg-muted);margin-top:4px;line-height:1.5;">' + escapeHtml(r.profile) + '</div>' : '';
    var lastContactHtml = r.last_contact ? '<div style="font-size:0.75rem;color:var(--fg-light);margin-top:4px;">上次联系：<span style="color:var(--fg-muted);">' + formatDate(r.last_contact) + '</span></div>' : '';
    html += '<div class="reminder-item"><div class="reminder-info"><div>' + basicInfo + '</div>' + profileHtml + lastContactHtml + '<div style="margin-top:6px;"><span class="reminder-content">' + escapeHtml(r.content || '') + '</span></div></div>' +
      '<div style="display:flex;flex-direction:column;align-items:flex-end;gap:6px;flex-shrink:0;">' +
      (overdue ? '<span class="badge badge-overdue">逾期</span>' : '') + (r.level ? levelBadge(r.level) : '') + '<button class="btn btn-sm" onclick="openCompleteModal(' + r.id + ')">记录跟进</button></div></div>';
  });
  html += '</div></div>';
  el.innerHTML = html;
}

function changeMonth(delta) {
  calendarMonth += delta;
  if (calendarMonth > 11) { calendarMonth = 0; calendarYear++; }
  if (calendarMonth < 0) { calendarMonth = 11; calendarYear--; }
  loadCalendar();
}

function goToday() {
  var now = new Date(); calendarYear = now.getFullYear(); calendarMonth = now.getMonth(); loadCalendar();
}

function exportCalendarICS() {
  if (Object.keys(calendarData).length === 0) { showToast('没有可导出的任务', 'warning'); return; }
  var ics = 'BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//CRM Follow-up//EN\r\n';
  for (var date in calendarData) {
    var tasks = calendarData[date];
    tasks.forEach(function(t) {
      var d = date.replace(/-/g, '');
      ics += 'BEGIN:VEVENT\r\nDTSTART;VALUE=DATE:' + d + '\r\nDTEND;VALUE=DATE:' + d + '\r\nSUMMARY:' + (t.customer_name || 'Follow-up') + '\r\nDESCRIPTION:' + (t.content || '').replace(/[,;\\n]/g, ' ') + '\r\nEND:VEVENT\r\n';
    });
  }
  ics += 'END:VCALENDAR';
  var blob = new Blob([ics], { type: 'text/calendar;charset=utf-8' });
  var url = URL.createObjectURL(blob);
  var a = document.createElement('a'); a.href = url; a.download = 'followup_calendar.ics'; a.click();
  URL.revokeObjectURL(url);
  showToast('日历导出成功', 'success');
}

// ========== CALENDAR SUBSCRIPTION (iCal) ==========
function initIcalUrl() {
  var input = document.getElementById('icalUrlInput');
  var status = document.getElementById('icalSyncStatus');
  if (!input) return;
  input.value = '正在检测网络...';
  api('/api/network/ip').then(function(net) {
    input.value = net.subscribe_url || '';
    if (status) {
      var changed = net.last_changed_at && !net.last_changed_at.startsWith('2000-') ? net.last_changed_at : '暂无未来待办';
      status.textContent = '个人订阅源已更新：' + changed + ' · 当前 ' + (net.active_count || 0) + ' 项待办';
    }
  }).catch(function() {
    input.value = '获取个人订阅链接失败';
    if (status) status.textContent = '请确认服务正在运行后重试';
  });
}

function copyIcalUrl() {
  var input = document.getElementById('icalUrlInput');
  if (!input || !/^https?:\/\//.test(input.value)) { showToast('个人订阅链接尚未就绪', 'warning'); return; }
  input.select();
  try {
    document.execCommand('copy');
    showToast('订阅链接已复制', 'success');
  } catch(e) {
    showToast('复制失败，请手动复制', 'error');
  }
}

function showIcalHelp() {
  var helpHtml = '<div style="max-width:500px;">' +
    '<h3 style="font-weight:600;margin-bottom:14px;">在 iPhone 上订阅日历</h3>' +
    '<ol style="margin:0;padding-left:20px;line-height:1.8;font-size:0.85rem;color:var(--fg-secondary);">' +
    '<li>打开 iPhone <strong>设置</strong> App</li>' +
    '<li>依次选择 <strong>App</strong>、<strong>日历</strong></li>' +
    '<li>依次选择 <strong>日历账户</strong>、<strong>添加账户</strong></li>' +
    '<li>依次选择 <strong>其他</strong>、<strong>添加已订阅的日历</strong></li>' +
    '<li>粘贴上方链接，轻点 <strong>下一步</strong></li>' +
    '</ol>' +
    '<p style="margin-top:14px;font-size:0.82rem;color:var(--danger);">如果以前订阅过不带个人令牌的旧链接，请先在日历账户中取消旧订阅，再添加当前个人链接，避免手机继续显示旧缓存。</p>' +
    '<p style="margin-top:14px;font-size:0.82rem;color:var(--fg-muted);">这是当前账号的私有只读链接，请勿转发。完成、新建或改期待办后，Trade OS 会立即更新订阅源；iPhone 会在下一次获取时同步，也可以在日历中下拉刷新。</p>' +
    '<p style="font-size:0.82rem;color:var(--fg-muted);">如验证失败：先在 iPhone Safari 浏览器中打开链接测试能否访问，如无法访问请检查防火墙设置（需放行 TCP 8080 端口）。</p>' +
    '</div>';
  
  // 用已有的自定义 modal 展示
  showCustomModal('日历订阅说明', helpHtml);
}

function refreshCalendarFeed() {
  var status = document.getElementById('icalSyncStatus');
  showToast('正在检查个人订阅源...', 'info');
  api('/api/calendar/refresh', { method: 'POST' }).then(function(data){
    if (data.success) {
      var changed = data.last_changed_at && !data.last_changed_at.startsWith('2000-') ? data.last_changed_at : '暂无未来待办';
      if (status) status.textContent = '个人订阅源已更新：' + changed + ' · 当前 ' + (data.active_count || 0) + ' 项待办';
      showToast('订阅源已是最新，Apple 日历会在下次获取时同步', 'success');
    }
  }).catch(function() {
    showToast('检查失败，请确认服务正在运行', 'error');
  });
}

function showCustomModal(title, bodyHtml) {
  var overlay = document.createElement('div');
  overlay.className = 'modal-overlay show ephemeral-modal';
  overlay.innerHTML = '<div class="modal" style="max-width:560px;">' +
    '<div class="modal-header"><h3>' + title + '</h3><button class="modal-close" aria-label="关闭" onclick="closeEphemeralModal(this.closest(\'.modal-overlay\'))">' + uiIcon('close') + '</button></div>' +
    '<div class="modal-body">' + bodyHtml + '</div>' +
    '<div class="modal-footer"><button class="btn" onclick="closeEphemeralModal(this.closest(\'.modal-overlay\'))">关闭</button></div>' +
    '</div>';
  overlay.addEventListener('click', function(e) { if (e.target === this) closeEphemeralModal(this); });
  document.body.appendChild(overlay);
  syncModalBodyLock();
}

// ========== FOLLOW-UP HISTORY ==========
async function loadHistory() {
  _pageDataLoadedAt = Date.now();
  try {
    var history = await api('/api/follow-history');
    var el = document.getElementById('historyTimeline');
    if (!history || history.length === 0) { el.innerHTML = '<div class="empty-state"><div class="empty-icon">' + uiIcon('list') + '</div><p>暂无跟进历史</p></div>'; return; }
    _followTimelineCache = {};
    var html = '<div class="timeline">';
    history.forEach(function(h) {
      _followTimelineCache[h.id] = h;
      html += '<div class="timeline-item"><div class="timeline-date">' + (h.follow_date || '') + (h.created_at ? ' | ' + h.created_at.substring(11, 16) : '') + '</div><div class="timeline-title">' + escapeHtml(h.customer_name || 'Unknown') + '</div><div class="timeline-desc" data-rich-log-id="' + h.id + '" data-rich-field="content">' + renderRichText(h.content || '') + '</div>' +
        (h.result ? '<div class="timeline-desc" style="margin-top:2px;"><strong>Result:</strong> <span data-rich-log-id="' + h.id + '" data-rich-field="result">' + renderRichText(h.result) + '</span></div>' : '') +
        (h.next_plan ? '<div class="timeline-desc" style="margin-top:2px;"><strong>Next Plan:</strong> <span data-rich-log-id="' + h.id + '" data-rich-field="next_plan">' + renderRichText(h.next_plan) + '</span></div>' : '') +
        '<div style="margin-top:8px;display:flex;gap:6px;">' +
          '<button class="btn btn-sm" onclick="openFollowEditModal(' + h.id + ')">' + uiIcon('edit') + '编辑</button>' +
          '<button class="btn btn-sm btn-danger" onclick="deleteFollowLog(' + h.id + ')" style="font-size:0.72rem;">删除</button>' +
        '</div></div>';
    });
    html += '</div>';
    el.innerHTML = html;
  } catch(e) {}
}

// Follow-up history: edit & delete (stored in memory for modal use)
var _followCache = {};

async function openFollowEditModal(logId) {
  try {
    var log = _followTimelineCache[logId];
    if (!log) {
      var history = await api('/api/follow-history');
      log = history.find(function(h) { return h.id === logId; });
    }
    if (!log) { showToast('记录未找到', 'error'); return; }
    _followCache = log;
    document.getElementById('followEditId').value = log.id;
    document.getElementById('followEditCustomer').value = log.customer_name || document.getElementById('customerEditTitle').textContent || '客户';
    document.getElementById('followEditDate').value = (log.follow_date || '').substring(0, 10);
    document.getElementById('followEditType').value = communicationTypeLabel(log.activity_type);
    document.getElementById('followEditDirection').value = log.direction || 'unknown';
    setRichText(document.getElementById('followEditContent'), log.content || '', true);
    setRichText(document.getElementById('followEditResult'), log.result || '', true);
    setRichText(document.getElementById('followEditNextPlan'), log.next_plan || '', true);
    openModal('followEditModal');
    setTimeout(function() {
      var el = document.getElementById('followEditContent');
      el.focus();
      var r = document.createRange();
      r.selectNodeContents(el);
      r.collapse(false);
      var s = window.getSelection();
      s.removeAllRanges();
      s.addRange(r);
    }, 80);
  } catch(e) {}
}

// 高亮工具栏：对当前选中文本套 mark.hl-{color}
var _highlightRange = null;
var _highlightTarget = null;
function persistHighlightTarget(target) {
  if (target && target.logId) saveTimelineHighlight(target);
}

function _hl_apply(color) {
  var sel = window.getSelection();
  var range = _highlightRange || (sel && sel.rangeCount ? sel.getRangeAt(0) : null);
  if (!range || range.collapsed) {
    showToast('请先选中要高亮的文字', 'info');
    return;
  }
  _highlightRange = null;
  if (color === 'clear') {
    var clearedFragment = _hl_strip_marks_in_range(range);
    if (clearedFragment !== null) {
      range.insertNode(clearedFragment);
    }
    if (sel) sel.removeAllRanges();
    persistHighlightTarget(_highlightTarget);
    _highlightTarget = null;
    return;
  }
  var strippedFragment = _hl_strip_marks_in_range(range);
  if (strippedFragment !== null) {
    var markNode = document.createElement('mark');
    markNode.className = 'hl-' + color;
    markNode.appendChild(strippedFragment);
    range.insertNode(markNode);
    if (sel) sel.removeAllRanges();
    var r2 = document.createRange();
    r2.selectNodeContents(markNode);
    if (sel) { sel.removeAllRanges(); sel.addRange(r2); }
    persistHighlightTarget(_highlightTarget);
    _highlightTarget = null;
    return;
  }
  var simpleMark = document.createElement('mark');
  simpleMark.className = 'hl-' + color;
  try {
    range.surroundContents(simpleMark);
  } catch (e) {
    var contents = range.extractContents();
    simpleMark.appendChild(contents);
    range.insertNode(simpleMark);
  }
  if (sel) sel.removeAllRanges();
  persistHighlightTarget(_highlightTarget);
  _highlightTarget = null;
}

async function saveTimelineHighlight(target) {
  var log = _followTimelineCache[target.logId];
  if (!log || !target.field || !target.element) return;
  var scope = beginCustomerScope();
  log[target.field] = richTextHtml(target.element);
  try {
    await api('/api/follow-history/' + target.logId, { method: 'PUT', body: JSON.stringify({
      follow_date: log.follow_date || '', activity_type: log.activity_type || 'follow_up',
      direction: log.direction || 'unknown', content: log.content || '', result: log.result || '', next_plan: log.next_plan || ''
    }) });
    var cache = liveCustomerCache(scope);
    if (cache) {
      (cache.timeline_items || []).forEach(function(item) {
        if (item.type === 'follow' && Number(item.id) === Number(target.logId)) item[target.field] = log[target.field];
      });
      cache.follow_history = (cache.timeline_items || []).filter(function(item) { return item.type === 'follow'; });
      cache.outreach_emails = (cache.timeline_items || []).filter(function(item) { return item.type === 'outreach'; });
      renderFollowTimeline(cache.follow_history, cache.outreach_emails,
        [customerTimelineKey({ type: 'follow', id: target.logId })]);
      if (_customerWorkspaceCache[cache.id]) {
        _customerWorkspaceCache[cache.id].timeline = Object.assign({}, _customerWorkspaceCache[cache.id].timeline || {}, {
          items: cache.timeline_items
        });
        _customerWorkspaceCache[cache.id].savedAt = Date.now();
      }
    }
    showToast('高亮已保存', 'success');
    reconcileCustomerTimeline().catch(function() {});
  } catch (e) { showToast('高亮保存失败', 'error'); }
}

function _hl_strip_marks_in_range(range) {
  try {
    var fragment = range.extractContents();
    var marks = fragment.querySelectorAll('mark');
    marks.forEach(function(m) {
      var parent = m.parentNode;
      while (m.firstChild) parent.insertBefore(m.firstChild, m);
      parent.removeChild(m);
    });
    return fragment;
  } catch (e) {
    return null;
  }
}

function initHighlightInteractions() {
  var menu = document.getElementById('highlightContextMenu');
  if (!menu) return;
  function showForSelection() {
    var selection = window.getSelection();
    var range = selection && selection.rangeCount ? selection.getRangeAt(0) : null;
    var node = range && range.commonAncestorContainer;
    var container = node && (node.nodeType === Node.ELEMENT_NODE ? node : node.parentElement);
    var editor = container && container.closest('.rich-text-area[contenteditable="true"]');
    var savedField = container && container.closest('[data-rich-log-id][data-rich-field]');
    if (savedField && savedField.dataset.highlightReadonly === 'true') savedField = null;
    if ((!editor && !savedField) || !range || range.collapsed) { menu.hidden = true; return; }
    _highlightRange = range.cloneRange();
    _highlightTarget = savedField ? {
      logId: savedField.dataset.richLogId,
      field: savedField.dataset.richField,
      element: savedField
    } : { element: editor };
    var rect = range.getBoundingClientRect();
    menu.hidden = false;
    menu.style.left = Math.max(12, Math.min(rect.left, window.innerWidth - menu.offsetWidth - 12)) + 'px';
    menu.style.top = Math.max(12, Math.min(rect.bottom + 8, window.innerHeight - menu.offsetHeight - 12)) + 'px';
  }
  // 选区变化是浏览器最稳定的信号：拖选、双击和键盘选字都会触发。
  var selectionFrame = null;
  document.addEventListener('selectionchange', function() {
    if (selectionFrame) return;
    selectionFrame = window.requestAnimationFrame(function() {
      selectionFrame = null;
      showForSelection();
    });
  });
  // 鼠标松开作为补充，覆盖少数浏览器延后更新选区的情况。
  document.addEventListener('mouseup', function() { window.setTimeout(showForSelection, 0); });
  document.addEventListener('keyup', function(event) {
    if (event.shiftKey || (event.ctrlKey && /^Arrow/.test(event.key))) showForSelection();
  });
  menu.addEventListener('click', function(event) {
    var button = event.target.closest('button[data-color]');
    if (!button) return;
    _hl_apply(button.dataset.color);
    menu.hidden = true;
  });
  document.addEventListener('pointerdown', function(event) { if (!menu.contains(event.target)) menu.hidden = true; });
  document.querySelectorAll('.rich-text-area[contenteditable="true"]').forEach(function(editor) {
    editor.addEventListener('paste', function(event) {
      event.preventDefault();
      document.execCommand('insertText', false, (event.clipboardData || window.clipboardData).getData('text/plain'));
    });
    editor.addEventListener('keydown', function(event) {
      if (event.key === 'Enter') { event.preventDefault(); document.execCommand('insertHTML', false, '<br>'); }
    });
  });
}
if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', initHighlightInteractions);
else initHighlightInteractions();

async function saveFollowEdit() {
  var id = document.getElementById('followEditId').value;
  var data = {
    follow_date: document.getElementById('followEditDate').value,
    activity_type: document.getElementById('followEditType').value,
    direction: document.getElementById('followEditDirection').value,
    content: richTextHtml(document.getElementById('followEditContent')),
    result: richTextHtml(document.getElementById('followEditResult')),
    next_plan: richTextHtml(document.getElementById('followEditNextPlan'))
  };
  try {
    var saved = await api('/api/follow-history/' + id, { method: 'PUT', body: JSON.stringify(data) });
    var customerModal = document.getElementById('customerEditModal').classList.contains('show');
    // Ownership comes from the edited record itself. Reading the *currently*
    // open customer here made the guard compare B to B and let A's edit land
    // in B's timeline when the workspace was switched mid-request.
    var ownerId = (_followCache && _followCache.customer_id) || (_customerDetailCache && _customerDetailCache.id);
    var updated = Object.assign({ type: 'follow' }, _followCache || {}, data, saved || {}, { id: Number(id) });
    if (customerModal && liveCustomerCache(beginCustomerScope(ownerId))) {
      var changedKey = upsertCustomerTimelineEntry(updated);
      syncCustomerWorkspaceAfterCommunication(ownerId, updated, changedKey);
    } else {
      _followTimelineCache[id] = updated;
    }
    showToast('跟进记录已更新', 'success');
    closeModal('followEditModal', true);
    if (customerModal) reconcileCustomerTimeline({ includeSummary: true }).catch(function() {});
    else loadHistory();
  } catch(e) { showToast('更新失败', 'error'); }
}

async function deleteFollowLog(logId) {
  if (!await showAppConfirm({ title: '移除跟进记录', message: '确认移除这条跟进记录？移除后仍可撤销。', submitLabel: '移除' })) return;
  var customerModal = document.getElementById('customerEditModal').classList.contains('show');
  var scope = beginCustomerScope();
  var removed = customerModal ? findCustomerTimelineEntry('follow', logId) : null;
  // 立即从时间线移除；失败时放回，成功后再给出撤销入口。
  if (customerModal && liveCustomerCache(scope)) removeCustomerTimelineEntry('follow', logId);
  var actionId = beginActionStatus('follow-delete-' + logId, '正在移除记录…');
  try {
    await api('/api/follow-history/' + logId, { method: 'DELETE', silentError: true });
    finishActionStatus(actionId, 'success', '记录已移除');
    if (customerModal) {
      showFollowUndoToast(logId, removed);
      reconcileCustomerTimeline({ includeSummary: true }).catch(function() {});
    } else {
      showFollowUndoToast(logId, removed);
      loadHistory();
    }
  } catch(e) {
    if (customerModal && removed && liveCustomerCache(scope)) {
      var changedKey = upsertCustomerTimelineEntry(removed);
      syncCustomerWorkspaceAfterCommunication(removed.customer_id || scope.customerId, removed, changedKey);
    }
    showToast('记录未移除，请重试', 'error');
    finishActionStatus(actionId, 'error', '记录未移除', function() { deleteFollowLog(logId); });
  }
}

function showFollowUndoToast(logId, removed) {
  var container = document.getElementById('toastContainer');
  var toast = document.createElement('div');
  toast.className = 'toast success toast-with-action';
  toast.innerHTML = uiIcon('check') + '<span>记录已移除</span><button type="button">撤销</button>';
  toast.querySelector('button').onclick = async function() {
    var restoreScope = beginCustomerScope(removed && removed.customer_id);
    try {
      await api('/api/follow-history/' + logId + '/restore', { method: 'POST' });
      var customerModal = document.getElementById('customerEditModal').classList.contains('show');
      // Ownership comes from the removed record itself; comparing against the
      // *currently* open customer made the guard self-satisfying and restored
      // A's record into B's timeline.
      if (customerModal && removed && liveCustomerCache(restoreScope)) {
        var changedKey = upsertCustomerTimelineEntry(Object.assign({ type: 'follow' }, removed));
        syncCustomerWorkspaceAfterCommunication(removed.customer_id, removed, changedKey);
      }
      toast.remove();
      showToast('记录已恢复', 'success');
      if (customerModal) reconcileCustomerTimeline({ includeSummary: true }).catch(function() {});
      else loadHistory();
    } catch(e) {}
  };
  container.appendChild(toast);
  setTimeout(function() { if (toast.isConnected) toast.remove(); }, 10000);
}

// ========== ACTIVITY LOGS ==========
async function loadLogs(action) {
  try {
    var url = '/api/logs?limit=100';
    if (action && action !== 'all') url += '&action=' + encodeURIComponent(action);
    var logs = await api(url);
    var el = document.getElementById('logsList');
    
    // Always show filter buttons first
    var actions_list = ['all', 'CREATE', 'UPDATE', 'DELETE', 'COMPLETE', 'SYNC'];
    var labels = ['全部', '创建', '更新', '删除', '完成', '同步'];
    var html = '<div style="margin-bottom:12px;display:flex;gap:6px;flex-wrap:wrap;">';
    actions_list.forEach(function(a, i) {
      var isActive = a === (action || 'all');
      html += '<button class="btn btn-sm' + (isActive ? ' btn-primary' : '') + '" onclick="loadLogs(\'' + a + '\')" style="font-size:0.72rem;">' + labels[i] + '</button>';
    });
    html += '</div>';
    
    // Then show log entries (or empty state)
    if (!logs || logs.length === 0) {
      html += '<div class="empty-state"><div class="empty-icon">' + uiIcon('settings') + '</div><p>暂无操作日志</p></div>';
      el.innerHTML = html;
      return;
    }
    logs.forEach(function(l) {
      var badgeClass = '';
      if (l.action === 'create') badgeClass = 'badge-today';
      else if (l.action === 'update') badgeClass = 'badge-info';
      else if (l.action === 'delete') badgeClass = 'badge-overdue';
      else if (l.action === 'complete') badgeClass = '';
      html += '<div class="log-item"><span class="log-time">' + (l.created_at || '') + '</span><span class="badge' + (badgeClass ? ' ' + badgeClass : '') + '" style="font-size:0.68rem;">' + (l.action || '') + '</span><span class="log-detail">' + escapeHtml(l.details || '') + '</span></div>';
    });
    el.innerHTML = html;
  } catch(e) {}
}

// ========== SETTINGS ==========
function renderGmailIntegrationStatus(status) {
  var container = document.getElementById('gmailIntegrationStatus');
  if (!container) return;
  status = status || {};
  var state = status.status || (status.connected ? 'connected' : 'not_connected');
  var html = '';
  if (!status.configured) {
    html = '<div class="gmail-integration-state gmail-integration-unavailable"><strong>尚未配置</strong><span>' + escapeHtml(status.configuration_message || '请由维护者完成 Gmail OAuth 与令牌加密配置。') + '</span></div>';
  } else if (!status.connected) {
    html = '<div class="gmail-integration-state"><strong>尚未连接 Gmail</strong><span>首次连接默认同步最近 90 天；之后会按新邮件增量读取。</span><button class="btn btn-sm btn-primary" type="button" onclick="connectGmailIntegration()">连接 Gmail</button></div>';
  } else {
    var label = state === 'syncing' ? '正在同步…' : (state === 'needs_reconnect' ? '需要重新连接' : (state === 'error' ? '上次同步未完成' : '已连接'));
    var detail = status.email ? ('账号：' + status.email) : '已连接账号';
    if (status.last_success_at) detail += ' · 上次成功：' + formatDate(status.last_success_at);
    if (state === 'syncing') detail += ' · 邮件会在后台逐步写入时间线或 Inbox。';
    if (status.last_error) detail += ' · ' + status.last_error;
    var result = status.last_result || {};
    var counts = [];
    if (Number(result.matched || 0)) counts.push('已归档 ' + result.matched + ' 封');
    if (Number(result.unmatched || 0)) counts.push('待归属 ' + result.unmatched + ' 封');
    if (Number(result.ambiguous || 0)) counts.push('待确认 ' + result.ambiguous + ' 封');
    if (counts.length) detail += ' · ' + counts.join('，');
    var syncLabel = state === 'syncing' ? '正在同步' : (state === 'needs_reconnect' ? '重新连接 Gmail' : '现在同步');
    var action = state === 'needs_reconnect'
      ? '<button class="btn btn-sm btn-primary" type="button" onclick="connectGmailIntegration()">' + syncLabel + '</button>'
      : '<button class="btn btn-sm btn-primary" type="button" onclick="syncGmailIntegration()" ' + (state === 'syncing' ? 'disabled' : '') + '>' + syncLabel + '</button>';
    html = '<div class="gmail-integration-state gmail-integration-' + escapeHtml(state) + '"><strong>' + escapeHtml(label) + '</strong><span>' + escapeHtml(detail) + '</span><div class="gmail-integration-actions">' + action + '<button class="text-action" type="button" onclick="disconnectGmailIntegration()">停止同步</button></div></div>';
  }
  container.innerHTML = html;
  if (state === 'syncing') setTimeout(loadGmailIntegrationStatus, 1800);
}

async function loadGmailIntegrationStatus() {
  try {
    var status = await api('/api/integrations/gmail/status', { skipGlobalSync: true, silentError: true, retryAttempts: 1 });
    if (status) renderGmailIntegrationStatus(status);
  } catch (error) {
    var container = document.getElementById('gmailIntegrationStatus');
    if (container) container.innerHTML = '<div class="gmail-integration-state gmail-integration-error"><strong>无法读取 Gmail 状态</strong><button class="btn btn-sm" type="button" onclick="loadGmailIntegrationStatus()">重试</button></div>';
  }
}

function connectGmailIntegration() {
  window.location.assign('/api/integrations/gmail/authorize');
}

async function syncGmailIntegration() {
  try {
    var result = await api('/api/integrations/gmail/sync', { method: 'POST', body: JSON.stringify({}), skipGlobalSync: true });
    if (result && result.already_running) showToast('Gmail 正在同步，请稍候', 'info');
    else showToast('已开始在后台同步 Gmail', 'success');
    loadGmailIntegrationStatus();
  } catch (error) {
    showToast((error && error.message) || '无法开始 Gmail 同步', 'error');
  }
}

async function disconnectGmailIntegration() {
  if (!await showAppConfirm({ title: '停止 Gmail 同步', message: '将停止读取此账号的新邮件，并删除 Trosa 本地保存的授权令牌；已经归档的沟通记录会保留。', submitLabel: '停止同步', danger: true })) return;
  try {
    await api('/api/integrations/gmail', { method: 'DELETE', body: JSON.stringify({}), skipGlobalSync: true });
    showToast('已停止 Gmail 同步并删除本地授权', 'success');
    loadGmailIntegrationStatus();
  } catch (error) {
    showToast((error && error.message) || '无法停止 Gmail 同步', 'error');
  }
}

function showGmailOAuthNotice() {
  var params = new URLSearchParams(window.location.search);
  var state = params.get('gmail');
  if (!state) return;
  var message = {
    connected: 'Gmail 已连接，正在后台同步最近邮件。',
    denied: '未完成 Gmail 授权，尚未读取任何邮件。',
    invalid_state: 'Gmail 授权已过期或不属于当前登录会话，请重新连接。',
    failed: 'Gmail 连接未完成，请检查设置后重试。'
  }[state];
  if (message) showToast(message, state === 'connected' ? 'success' : 'warning');
  params.delete('gmail');
  var query = params.toString();
  window.history.replaceState({}, '', window.location.pathname + (query ? '?' + query : '') + window.location.hash);
}

async function loadSettings() {
  if (!userPreferences) await loadUserPreferences();
  renderPersonalSettings();
  var teamCard = document.getElementById('teamMembersSettingsCard');
  if (teamCard) teamCard.style.display = currentUser && currentUser.role === 'admin' ? '' : 'none';
  if (currentUser && currentUser.role === 'admin') loadTeamMembers();
  loadAiConfig();
  loadGmailIntegrationStatus();
  try {
    var sys = await api('/api/system');
    document.getElementById('systemInfo').innerHTML =
      '<div class="settings-row"><span class="label">数据库路径</span><span class="value" style="font-size:0.78rem;">' + (sys.db_path || '-') + '</span></div>' +
      '<div class="settings-row"><span class="label">客户总数（不含已删除）</span><span class="value">' + (sys.customer_count || 0) + '</span></div>' +
      '<div class="settings-row"><span class="label">调度器</span><span class="value" style="color:' + (sys.scheduler_running ? 'var(--success)' : 'var(--danger)') + ';">' + (sys.scheduler_running ? '运行中' : '未运行') + '</span></div>';
  } catch(e) {}
  document.getElementById('excelUploadResult').innerHTML = '';
}

async function loadTeamMembers() {
  var list = document.getElementById('teamMembersList');
  if (!list) return;
  list.textContent = '正在读取成员…';
  try {
    var data = await api('/api/team/members');
    list.innerHTML = '<div class="settings-row"><span class="label">成员</span><span class="value">状态</span></div>' +
      (data.members || []).map(function(member) {
        var active = Number(member.active) === 1 || member.active === true;
        return '<div class="settings-row"><span class="label">' + escapeHtml(member.name || member.username) + '</span><span class="value">' +
          (active ? '启用' : '已禁用') + (active && member.username !== currentUser.id ? ' <button class="btn btn-sm" type="button" onclick="disableTeamMember(\'' + escapeHtml(member.username) + '\')">禁用</button>' : '') + '</span></div>';
      }).join('');
    loadTeamInvitations();
  } catch (error) { list.textContent = error.message || '成员列表暂时无法加载'; }
}

async function loadTeamInvitations() {
  var list = document.getElementById('teamMembersList');
  if (!list) return;
  try {
    var data = await api('/api/team/invitations');
    var invitations = (data.invitations || []).filter(function(invitation) {
      return !invitation.accepted_at && !invitation.revoked_at && new Date(invitation.expires_at) > new Date();
    });
    if (!invitations.length) return;
    list.innerHTML += '<div class="settings-row"><span class="label">待接受邀请</span><span class="value">' + invitations.map(function(invitation) {
      return '有效至 ' + escapeHtml(new Date(invitation.expires_at).toLocaleDateString()) +
        ' <button class="btn btn-sm" type="button" onclick="revokeTeamInvitation(\'' + escapeHtml(invitation.id) + '\')">撤销</button>';
    }).join('<br>') + '</span></div>';
  } catch (error) {
    // The member list remains useful if invitations happen to fail to load.
  }
}

async function createTeamInvitation(event) {
  event.preventDefault();
  var form = document.getElementById('teamInvitationForm');
  var result = document.getElementById('teamInvitationResult');
  try {
    var data = await api('/api/team/invitations', { method: 'POST', body: JSON.stringify({}) });
    var url = data && data.invitation && data.invitation.url;
    if (!url) throw new Error('邀请链接暂时无法生成');
    result.hidden = false;
    result.innerHTML = '<label class="form-label" for="teamInvitationUrl">请复制并发送给成员（只在此显示一次）</label>' +
      '<div style="display:flex; gap:8px; flex-wrap:wrap;"><input class="form-control" id="teamInvitationUrl" readonly value="' + escapeHtml(url) + '" style="min-width:280px; flex:1;"><button class="btn btn-sm" type="button" onclick="copyTeamInvitationUrl()">复制链接</button></div>';
    copyTeamInvitationUrl();
    showToast('邀请链接已生成并复制', 'success');
    loadTeamMembers();
  } catch (error) { showToast(error.message || '生成邀请失败', 'error'); }
}

async function copyTeamInvitationUrl() {
  var input = document.getElementById('teamInvitationUrl');
  if (!input) return;
  input.select();
  try {
    if (navigator.clipboard && window.isSecureContext) await navigator.clipboard.writeText(input.value);
    else document.execCommand('copy');
  } catch (error) {
    // The selected, visible field is the intentional fallback for browsers
    // that deny clipboard access.
  }
}

async function revokeTeamInvitation(invitationId) {
  if (!await showAppConfirm({title: '撤销邀请', message: '撤销后，这个链接将不能再用于创建账号。', submitLabel: '确认撤销'})) return;
  try {
    await api('/api/team/invitations/' + encodeURIComponent(invitationId) + '/revoke', {method: 'POST'});
    showToast('邀请已撤销', 'success');
    loadTeamMembers();
  } catch (error) { showToast(error.message || '撤销邀请失败', 'error'); }
}

async function disableTeamMember(username) {
  if (!await showAppConfirm({title: '禁用成员账号', message: '禁用后该成员将无法登录，但历史工作记录会保留。', submitLabel: '确认禁用'})) return;
  try {
    await api('/api/team/members/' + encodeURIComponent(username) + '/disable', {method: 'POST'});
    showToast('成员账号已禁用', 'success');
    loadTeamMembers();
  } catch (error) { showToast(error.message || '禁用失败', 'error'); }
}

function uploadExcel() {
  var input = document.getElementById('excelFileInput');
  if (!input.files || !input.files[0]) { showToast('请选择文件', 'warning'); return; }
  var file = input.files[0];
  var formData = new FormData();
  formData.append('file', file);
  var resultEl = document.getElementById('excelUploadResult');
  resultEl.innerHTML = '正在上传导入，请稍候...';
  fetch('/api/excel/upload', { method: 'POST', body: formData, credentials: 'include' })
    .then(function(r){return r.json();})
    .then(function(data){
      if (data.success) {
        resultEl.innerHTML = '<span style="color:var(--success);">' + data.message + '</span>';
        showToast('导入成功: ' + data.message, 'success');
      } else {
        resultEl.innerHTML = '<span style="color:var(--error);">' + (data.error || '导入失败') + '</span>';
      }
    })
    .catch(function(e){
      resultEl.innerHTML = '<span style="color:var(--error);">上传失败: ' + e.message + '</span>';
    });
}

async function recoverExcelHistory() {
  var resultEl = document.getElementById('excelUploadResult');
  if (!await showAppConfirm({ title: '恢复历史沟通记录', message: '系统将从已保存的历史 Excel 恢复沟通记录，自动跳过重复内容，并在开始前创建安全备份。', submitLabel: '开始恢复' })) return;
  resultEl.innerHTML = '正在恢复历史沟通记录，请稍候...';
  try {
    var data = await api('/api/excel/recover-history', { method: 'POST' });
    if (!data.success) throw new Error(data.error || '恢复失败');
    var unmatched = data.unmatched_customers || 0;
    resultEl.innerHTML = '<span style="color:var(--success);">已恢复 ' + data.imported + ' 条活动，跳过 ' + data.skipped + ' 条重复记录。' +
      (unmatched ? ' 另有 ' + unmatched + ' 个未匹配客户已保留待审阅，未自动创建。' : '') + '</span>';
    showToast('历史沟通记录已恢复 ' + data.imported + ' 条', 'success');
  } catch (e) {
    resultEl.innerHTML = '<span style="color:var(--error);">恢复失败：' + escapeHtml(e.message || '未知错误') + '</span>';
    showToast('恢复历史记录失败', 'error');
  }
}

async function runHealthCheck() {
  var panel = document.getElementById('healthCheckPanel');
  panel.innerHTML = '<div class="empty-state"><p style="color:var(--fg-muted);">正在诊断系统健康状态...</p></div>';
  try {
    var health = await api('/api/health');
    var statusColors = { 'ok': 'var(--success)', 'warning': 'var(--warning)', 'error': 'var(--danger)', 'info': 'var(--fg-muted)' };
    var statusLabels = { 'ok': '正常', 'warning': '警告', 'error': '错误', 'info': '信息' };
    var overallLabel = health.overall === 'healthy' ? '系统健康' : '存在问题';
    var overallColor = health.overall === 'healthy' ? 'var(--success)' : 'var(--danger)';
    
    var html = '<div style="display:flex;align-items:center;gap:10px;margin-bottom:16px;padding:12px 16px;background:var(--bg);border-radius:var(--radius-sm);border-left:4px solid ' + overallColor + ';">' +
      '<span style="width:10px;height:10px;border-radius:50%;background:' + overallColor + ';flex-shrink:0;"></span>' +
      '<span style="font-weight:600;color:' + overallColor + ';font-size:0.9rem;">' + overallLabel + '</span>' +
      '<span style="color:var(--fg-muted);font-size:0.78rem;margin-left:auto;">' + (health.timestamp || '') + '</span></div>';
    
    health.checks.forEach(function(check) {
      var c = statusColors[check.status] || 'var(--fg-light)';
      var l = statusLabels[check.status] || check.status;
      html += '<div style="display:flex;align-items:flex-start;gap:10px;padding:10px 0;border-bottom:1px solid var(--border);">' +
        '<span style="width:8px;height:8px;border-radius:50%;background:' + c + ';flex-shrink:0;margin-top:4px;"></span>' +
        '<div style="flex:1;"><div style="display:flex;align-items:center;gap:6px;"><span style="font-weight:600;color:var(--fg-primary);font-size:0.85rem;">' + check.name + '</span><span style="font-size:0.7rem;color:' + c + ';font-weight:500;">[' + l + ']</span></div>' +
        '<div style="color:var(--fg-muted);font-size:0.78rem;margin-top:2px;">' + escapeHtml(check.detail || '') + '</div></div></div>';
    });
    
    html += '<div style="margin-top:12px;"><button class="btn btn-sm" onclick="runHealthCheck()" style="font-size:0.78rem;">重新检测</button></div>';
    panel.innerHTML = html;
  } catch(e) {
    panel.innerHTML = '<div class="empty-state"><p style="color:var(--danger);">健康检测失败: ' + escapeHtml(e.message || '网络错误') + '</p><button class="btn btn-sm" onclick="runHealthCheck()" style="margin-top:10px;">重新检测</button></div>';
  }
}

// ========== MODAL HELPERS ==========
var _modalCleanStates = {};
var _pendingCustomerModalClose = '';
var _modalCloseTimers = {};
var _modalReturnFocus = {};
var _unguardedModals = ['unsavedChangesModal', 'smartFillPreviewModal'];
var _modalSaveHandlers = {
  customerEditModal: saveCustomerWorkspaceAndExit,
  completeModal: submitComplete,
  batchCompleteModal: submitBatchComplete,
  addCustomerModal: submitExistCustomer,
  addNewCustomerModal: submitNewCustomer,
  batchAddModal: submitBatchAdd,
  customerTaskModal: createCustomerTask,
  inboxReplyModal: saveInboxReply,
  batchSetModal: submitBatchSet,
  todayQuickEditModal: submitTodayQuickEdit,
  followEditModal: saveFollowEdit
};

function customerModalState(id) {
  var modal = document.getElementById(id);
  if (!modal) return '';
  var fields = Array.from(modal.querySelectorAll('input, select, textarea, [contenteditable="true"]')).filter(function(field) {
    return field.type !== 'hidden' && field.type !== 'button' && field.type !== 'submit' && !field.disabled &&
      !(field.closest && field.closest('.contact-edit-form'));
  });
  return JSON.stringify(fields.map(function(field) {
    // Rich-text composers are contenteditable, not form controls. Without this
    // branch a draft typed only into 沟通内容 / 结果 was invisible to the
    // unsaved-changes guard and was discarded silently on close.
    var editable = field.isContentEditable || field.getAttribute('contenteditable') === 'true' || field.getAttribute('contenteditable') === '';
    var value = editable
      ? String(field.innerHTML || '')
      : (field.type === 'checkbox' || field.type === 'radio' ? field.checked : field.value);
    return [field.id || field.name || field.className, value, field.files && field.files[0] ? field.files[0].name : ''];
  }));
}

function markModalClean(id) { _modalCleanStates[id] = customerModalState(id); }
function customerModalIsDirty(id) { return _modalCleanStates[id] !== undefined && _modalCleanStates[id] !== customerModalState(id); }
function modalNeedsUnsavedGuard(id) {
  var modal = document.getElementById(id);
  return !!modal && _unguardedModals.indexOf(id) < 0 && !!modal.querySelector('input:not([type="hidden"]), select, textarea');
}
function syncModalBodyLock() { document.body.style.overflow = document.querySelector('.modal-overlay.show') ? 'hidden' : ''; }

function focusFirstModalControl(modal) {
  if (!modal || !modal.classList.contains('show')) return;
  var focusable = modal.querySelector('input:not([type="hidden"]):not([disabled]), textarea:not([disabled]), select:not([disabled]), button:not([disabled]), [tabindex]:not([tabindex="-1"])');
  if (focusable) focusable.focus({ preventScroll: true });
}

var _appDialogResolver = null;
var _appDialogSettling = false;
var _appDialogMode = 'prompt';
function showAppPrompt(options) {
  options = options || {};
  return new Promise(function(resolve) {
    var modal = document.getElementById('appDialogModal');
    var input = document.getElementById('appDialogInput');
    if (!modal || !input) { resolve(null); return; }
    // 对话框只有一个 resolver 槽位。新的对话框必须先用“取消”结算掉上一次
    // 请求，否则前一个 Promise 永远 pending，调用它的流程就此挂死。
    if (_appDialogResolver) finishAppDialog(_appDialogMode === 'confirm' ? false : null);
    _appDialogResolver = resolve;
    _appDialogMode = 'prompt';
    document.getElementById('appDialogTitle').textContent = options.title || '输入信息';
    document.getElementById('appDialogMessage').textContent = options.message || '';
    document.getElementById('appDialogLabel').textContent = options.label || '内容';
    document.getElementById('appDialogSubmit').textContent = options.submitLabel || '确定';
    document.getElementById('appDialogInputGroup').style.display = '';
    input.type = options.type === 'date' ? 'date' : 'text';
    input.value = options.value == null ? '' : String(options.value);
    input.readOnly = !!options.readonly;
    input.onkeydown = function(event) {
      if (event.key === 'Enter' && !options.readonly) {
        event.preventDefault();
        submitAppDialog();
      }
    };
    modal.querySelector('.modal').classList.toggle('is-readonly', !!options.readonly);
    openModal('appDialogModal');
    setTimeout(function() {
      input.focus({ preventScroll: true });
      if (options.readonly) input.select();
    }, 0);
  });
}

function showAppConfirm(options) {
  options = options || {};
  return new Promise(function(resolve) {
    var modal = document.getElementById('appDialogModal');
    if (!modal) { resolve(false); return; }
    // 同 showAppPrompt：先用取消结算掉被覆盖的上一个对话框请求。
    if (_appDialogResolver) finishAppDialog(_appDialogMode === 'confirm' ? false : null);
    _appDialogResolver = resolve;
    _appDialogMode = 'confirm';
    document.getElementById('appDialogTitle').textContent = options.title || '确认操作';
    document.getElementById('appDialogMessage').textContent = options.message || '';
    document.getElementById('appDialogSubmit').textContent = options.submitLabel || '确认';
    document.getElementById('appDialogInputGroup').style.display = 'none';
    modal.querySelector('.modal').classList.remove('is-readonly');
    openModal('appDialogModal');
  });
}

function finishAppDialog(value) {
  var resolve = _appDialogResolver;
  _appDialogResolver = null;
  _appDialogSettling = true;
  closeModal('appDialogModal', true);
  _appDialogSettling = false;
  if (resolve) resolve(value);
}

function submitAppDialog() {
  var input = document.getElementById('appDialogInput');
  finishAppDialog(_appDialogMode === 'confirm' ? true : (input ? input.value : ''));
}

function dismissAppDialog() { finishAppDialog(_appDialogMode === 'confirm' ? false : null); }

// Every `.modal-overlay` covers the whole viewport and shares one z-index, so the
// browser paints and hit-tests open overlays in DOM order: whatever sits last in
// the body receives the click. A modal opened on top of an already-open dialog
// (the unsaved-changes prompt over 安排下一步 / 记录沟通 / 批量操作, an in-app
// confirm over a composer) must therefore move to the end of the body, otherwise
// it is painted behind the dialog it belongs to and cannot be used at all.
function raiseModalAboveOpenDialogs(modal) {
  if (!modal || !modal.parentNode) return;
  var openOverlays = Array.prototype.slice.call(document.querySelectorAll('.modal-overlay.show'));
  var topOverlay = openOverlays[openOverlays.length - 1];
  if (topOverlay && topOverlay !== modal) document.body.appendChild(modal);
}

function openModal(id) {
  var modal = document.getElementById(id);
  if (!modal) return;
  var active = document.activeElement;
  if (active && active !== document.body && !modal.contains(active)) _modalReturnFocus[id] = active;
  if (_modalCloseTimers[id]) {
    clearTimeout(_modalCloseTimers[id]);
    delete _modalCloseTimers[id];
  }
  raiseModalAboveOpenDialogs(modal);
  modal.classList.remove('is-closing');
  modal.classList.add('show');
  if (modalNeedsUnsavedGuard(id)) markModalClean(id);
  syncModalBodyLock();
  requestAnimationFrame(function() { focusFirstModalControl(modal); });
}

function closeModal(id, force) {
  if (id === 'appDialogModal' && !force && !_appDialogSettling) {
    dismissAppDialog();
    return true;
  }
  var modal = document.getElementById(id);
  if (!modal || !modal.classList.contains('show') || modal.classList.contains('is-closing')) return true;
  // While a customer record is still loading there are no user edits to
  // protect.  Do not let the unsaved-changes layer consume the first close
  // tap on a slow phone or LAN connection.
  var customerLoading = id === 'customerEditModal' &&
    (modal.classList.contains('is-loading') || modal.classList.contains('is-error') || modal.getAttribute('aria-busy') === 'true');
  if (!force && id === 'customerEditModal' && contactEditIsDirty()) {
    var editingContactId = _editingContactId;
    showAppConfirm({
      title: '联系人修改未保存',
      message: '联系人资料有修改，关闭会放弃这些修改。',
      submitLabel: '放弃修改'
    }).then(function(confirmed) {
      if (!confirmed || Number(_editingContactId) !== Number(editingContactId)) return;
      cancelContactEdit(editingContactId);
      closeModal(id);
    });
    return false;
  }
  if (!force && !customerLoading && modalNeedsUnsavedGuard(id) && customerModalIsDirty(id)) {
    _pendingCustomerModalClose = id;
    var subtitle = document.getElementById('unsavedChangesSubtitle');
    var title = document.querySelector('#' + id + ' .modal-header h3');
    if (subtitle) subtitle.textContent = (title ? '“' + title.textContent.trim() + '”中的' : '你刚才填写的') + '内容还没有保存。';
    var saveButton = document.getElementById('unsavedSaveButton');
    if (saveButton) saveButton.style.display = _modalSaveHandlers[id] ? '' : 'none';
    // Show the prompt through openModal so it is raised above the modal it
    // protects and receives focus; otherwise it is unreachable and the user is
    // trapped in the composer with no way back to 继续编辑 / 放弃修改.
    openModal('unsavedChangesModal');
    return false;
  }
  if (id === 'customerEditModal') {
    _customerDetailLoadToken++;
    _customerDetailLoadingId = null;
    // Closing the workspace ends every in-flight echo that captured its scope.
    _customerScopeGeneration++;
    if (_customerDetailController) { _customerDetailController.abort(); _customerDetailController = null; }
    var customerLoadingModal = document.getElementById('customerEditModal');
    if (customerLoadingModal) {
      customerLoadingModal.classList.remove('is-loading', 'is-error');
      customerLoadingModal.setAttribute('aria-busy', 'false');
    }
  }
  modal.classList.remove('show');
  if (!_motionReduced) modal.classList.add('is-closing');
  delete _modalCleanStates[id];
  syncModalBodyLock();
  var finishClose = function() {
    modal.classList.remove('is-closing');
    delete _modalCloseTimers[id];
    var returnFocus = _modalReturnFocus[id];
    delete _modalReturnFocus[id];
    if (!document.querySelector('.modal-overlay.show') && returnFocus && returnFocus.isConnected && !returnFocus.disabled) {
      returnFocus.focus({ preventScroll: true });
    }
    if (_pendingVersionRefresh && !document.querySelector('.modal-overlay.show')) {
      _pendingVersionRefresh = false;
      setTimeout(function() { location.reload(); }, 30);
    }
  };
  if (_motionReduced) finishClose();
  else _modalCloseTimers[id] = setTimeout(finishClose, 170);
  return true;
}

function closeEphemeralModal(element) {
  if (!element || !element.isConnected) return;
  element.classList.remove('show');
  if (_motionReduced) {
    element.remove();
    syncModalBodyLock();
    return;
  }
  element.classList.add('is-closing');
  setTimeout(function() {
    element.remove();
    syncModalBodyLock();
  }, 170);
}

function continueEditingCustomerForm() {
  closeModal('unsavedChangesModal', true);
  _pendingCustomerModalClose = '';
}

function discardCustomerFormChanges() {
  var target = _pendingCustomerModalClose;
  closeModal('unsavedChangesModal', true);
  _pendingCustomerModalClose = '';
  if (target === 'addCustomerModal' || target === 'addNewCustomerModal') {
    _smartFillRequestToken++;
    _pendingSmartFill = null;
  }
  if (target) closeModal(target, true);
}

function savePendingCustomerForm() {
  var target = _pendingCustomerModalClose;
  closeModal('unsavedChangesModal', true);
  _pendingCustomerModalClose = '';
  var handler = _modalSaveHandlers[target];
  if (handler) handler();
}

document.querySelectorAll('.modal-overlay').forEach(function(overlay) {
  overlay.addEventListener('click', function(e) {
    if (e.target !== this || this.id === 'unsavedChangesModal') return;
    closeModal(this.id);
  });
});
document.addEventListener('keydown', function(e) {
  if (e.key !== 'Escape') return;
  // Open modals are kept in DOM order by openModal, so the last `.show` overlay
  // is the topmost one. Escape must dismiss what the user actually sees: the
  // unsaved-changes prompt goes back to editing, every other modal is closed
  // normally (which re-raises the prompt when there are unsaved edits).
  var openModals = Array.from(document.querySelectorAll('.modal-overlay.show'));
  var top = openModals[openModals.length - 1];
  if (top) {
    if (top.id === 'unsavedChangesModal') continueEditingCustomerForm();
    else if (top.classList.contains('ephemeral-modal')) closeEphemeralModal(top);
    else closeModal(top.id);
    return;
  }
  var sidebar = document.getElementById('sidebar');
  if (sidebar && sidebar.classList.contains('open')) {
    closeSidebar();
    var toggle = document.getElementById('sidebarToggle');
    if (toggle) toggle.focus({ preventScroll: true });
  }
});
window.addEventListener('beforeunload', function(e) {
  var hasUnsaved = Array.from(document.querySelectorAll('.modal-overlay.show')).some(function(modal) {
    return modalNeedsUnsavedGuard(modal.id) && customerModalIsDirty(modal.id);
  });
  if (!hasUnsaved) return;
  e.preventDefault();
  e.returnValue = '';
});

// ========== TAB HELPER ==========
var _customerTabTransitionToken = 0;
function switchTab(btn, tabId) {
  var parent = btn.closest('.modal-body');
  var nextPanel = document.getElementById(tabId);
  var currentPanel = parent && parent.querySelector('.tab-content.active');
  var isCustomerWorkspace = !!btn.closest('#customerEditModal');
  if (!parent || !nextPanel || currentPanel === nextPanel) return;
  if (isCustomerWorkspace) loadCustomerSection(tabId);
  parent.querySelectorAll('.tab-btn').forEach(function(t) { t.classList.remove('active'); });
  btn.classList.add('active');
  var saveButton = document.getElementById('saveCustomerFooterBtn');
  if (saveButton && isCustomerWorkspace) saveButton.style.display = tabId === 'editTabBasic' ? '' : 'none';
  if (!currentPanel || !isCustomerWorkspace || _motionReduced) {
    parent.querySelectorAll('.tab-content').forEach(function(t) { t.classList.remove('active'); });
    nextPanel.classList.add('active');
    return;
  }

  var token = ++_customerTabTransitionToken;
  var workspaceMain = currentPanel.closest('.customer-workspace-main');
  if (workspaceMain) workspaceMain.style.minHeight = currentPanel.offsetHeight + 'px';
  currentPanel.classList.add('customer-tab-exit');
  currentPanel.setAttribute('aria-hidden', 'true');
  window.setTimeout(function() {
    if (token !== _customerTabTransitionToken) return;
    currentPanel.classList.remove('active', 'customer-tab-exit');
    nextPanel.classList.add('active', 'customer-tab-enter');
    nextPanel.setAttribute('aria-hidden', 'false');
    window.setTimeout(function() {
      if (token === _customerTabTransitionToken) nextPanel.classList.remove('customer-tab-enter');
    }, 190);
    window.setTimeout(function() {
      if (token === _customerTabTransitionToken && workspaceMain) workspaceMain.style.minHeight = '';
    }, 220);
  }, 120);
}

function setCustomerSectionLoading(section, message) {
  var targets = {
    editTabContacts: 'contactsList', editTabFiles: 'customerFilesList',
    editTabTasks: 'customerTasksList'
  };
  var targetId = targets[section];
  var target = targetId && document.getElementById(targetId);
  if (target && !target.dataset.loaded) target.innerHTML = '<div class="workspace-loading"><p>' + escapeHtml(message || '正在加载…') + '</p></div>';
}

async function loadCustomerSection(tabId) {
  var customer = _customerDetailCache;
  var customerId = customer && customer.id;
  if (!customerId || _customerSectionLoads[tabId]) return;
  if (tabId === 'editTabContacts' && Array.isArray(customer.contacts)) return;
  if (tabId === 'editTabFiles' && Array.isArray(customer.files)) return;
  if (tabId === 'editTabTasks' && Array.isArray(customer.tasks)) return;

  var scope = beginCustomerScope(customerId);
  _customerSectionLoads[tabId] = true;
  setCustomerSectionLoading(tabId, tabId === 'editTabContacts' ? '正在读取联系人…' : tabId === 'editTabFiles' ? '正在读取文件清单…' : '正在读取待办…');
  try {
    if (tabId === 'editTabContacts') {
      var contacts = await api('/api/customers/' + customerId + '/contacts');
      var contactsCache = liveCustomerCache(scope);
      if (contactsCache) {
        contactsCache.contacts = (contacts && (contacts.contacts || contacts)) || [];
        contactsCache.contact_count = contactsCache.contacts.length;
        contactsCache.primary_contact = contactsCache.contacts[0] || null;
        renderContacts(contactsCache.contacts);
        renderCustomerFactsBrief(contactsCache);
      }
    } else if (tabId === 'editTabFiles') {
      var files = await api('/api/customers/' + customerId + '/files');
      var filesCache = liveCustomerCache(scope);
      if (filesCache) {
        filesCache.files = (files && (files.files || files)) || [];
        renderCustomerFiles(filesCache.files);
      }
    } else if (tabId === 'editTabTasks') {
      var tasks = await api('/api/customers/' + customerId + '/tasks');
      var tasksCache = liveCustomerCache(scope);
      if (tasksCache) {
        tasksCache.tasks = (tasks && tasks.tasks) || [];
        tasksCache.reminders = tasksCache.tasks;
        renderCustomerTasks(tasksCache.tasks);
        renderCustomerNextTask(tasksCache.tasks);
        renderCustomerFactsBrief(tasksCache);
      }
    }
  } catch (e) {
    if (!liveCustomerCache(scope)) return;
    var targets = { editTabContacts: 'contactsList', editTabFiles: 'customerFilesList', editTabTasks: 'customerTasksList' };
    var target = document.getElementById(targets[tabId]);
    if (target) target.innerHTML = '<div class="workspace-loading"><p>这部分内容暂时无法加载。</p><button class="btn btn-sm" type="button" onclick="loadCustomerSection(\'' + tabId + '\')">重新加载</button></div>';
  } finally {
    _customerSectionLoads[tabId] = false;
  }
}

// ========== AUTH ==========
function invitationTokenFromLocation() {
  var match = /^\/invite\/([^/]+)\/?$/.exec(window.location.pathname || '');
  return match ? decodeURIComponent(match[1]) : '';
}

async function checkLogin() {
  var invitationToken = invitationTokenFromLocation();
  if (invitationToken) {
    showInvitationAcceptance(invitationToken);
    return;
  }
  try {
    var response = await fetch('/api/auth/me', { credentials: 'include' });
    var data = await response.json();
    if (data.weekly_viewer || data.internal_viewer) {
      enterOverview();
      return;
    }
    if (data.logged_in && data.user) {
      currentUser = data.user;
      showApp();
      return;
    }
  } catch(e) {}
  showLogin();
}

async function showInvitationAcceptance(token) {
  var overlay = document.getElementById('invitationOverlay');
  var form = document.getElementById('invitationForm');
  var desc = document.getElementById('invitationDesc');
  var errorEl = document.getElementById('invitationError');
  var loginOverlay = document.getElementById('loginOverlay');
  if (loginOverlay) loginOverlay.style.display = 'none';
  if (overlay) { overlay.hidden = false; overlay.style.display = 'flex'; }
  if (form) form.hidden = true;
  if (errorEl) errorEl.textContent = '';
  if (desc) desc.textContent = '正在验证邀请链接…';
  try {
    var response = await fetch('/api/invitations/' + encodeURIComponent(token), {cache: 'no-store'});
    var data = await response.json();
    if (!response.ok || !data.valid) throw new Error((data && data.error) || '邀请链接无效');
    if (desc) desc.textContent = '请设置你的姓名和密码。姓名就是登录账号。';
    if (form) {
      form.hidden = false;
      form.dataset.token = token;
      var nameInput = document.getElementById('invitationName');
      if (nameInput) nameInput.focus();
    }
  } catch (error) {
    if (desc) desc.textContent = '这个邀请链接无法使用';
    if (errorEl) errorEl.textContent = error.message || '邀请链接无效、已过期或已被使用';
  }
}

function returnToStandardLogin() {
  // An invite can be opened accidentally in an already-used browser.  It must
  // never become a dead end for an existing member or administrator.
  window.history.replaceState({}, '', '/');
  var overlay = document.getElementById('invitationOverlay');
  if (overlay) { overlay.hidden = true; overlay.style.display = 'none'; }
  checkLogin();
}

document.getElementById('invitationName').addEventListener('input', function(event) {
  var hint = document.getElementById('invitationAccountHint');
  var name = event.target.value.trim();
  if (hint) hint.textContent = name ? '登录账号：' + name : '账号将使用你填写的姓名';
});

document.getElementById('invitationForm').addEventListener('submit', async function(event) {
  event.preventDefault();
  var form = event.currentTarget;
  var errorEl = document.getElementById('invitationError');
  var name = document.getElementById('invitationName').value.trim();
  var password = document.getElementById('invitationPassword').value;
  var passwordConfirm = document.getElementById('invitationPasswordConfirm').value;
  if (password !== passwordConfirm) {
    if (errorEl) errorEl.textContent = '两次输入的密码不一致';
    return;
  }
  if (!/^\d{6}$/.test(password)) {
    if (errorEl) errorEl.textContent = '请输入 6 位数字密码';
    return;
  }
  try {
    var data = await api('/api/invitations/' + encodeURIComponent(form.dataset.token || '') + '/accept', {
      method: 'POST', body: JSON.stringify({name: name, password: password})
    });
    if (!data || !data.success) throw new Error('账号创建失败');
    currentUser = data.user;
    window.history.replaceState({}, '', '/');
    document.getElementById('invitationOverlay').style.display = 'none';
    showApp();
  } catch (error) {
    if (errorEl) errorEl.textContent = error.message || '暂时无法创建账号，请稍后重试';
  }
});

function switchCustomerCompose(mode) {
  var composer = document.getElementById('followCompose');
  if (!composer) return;
  composer.querySelectorAll('[data-compose-mode]').forEach(function(button) {
    var active = button.dataset.composeMode === mode;
    button.classList.toggle('active', active);
    button.setAttribute('aria-selected', active ? 'true' : 'false');
  });
  composer.querySelectorAll('[data-compose-panel]').forEach(function(panel) {
    panel.hidden = panel.dataset.composePanel !== mode;
  });
}

function showLogin() {
  // Account-specific preferences must not affect the shared weekly overview
  // available from the account-selection screen.
  var loginViewToken = ++_loginViewToken;
  if (_loginUsersController) _loginUsersController.abort();
  _loginUsersController = typeof AbortController === 'function' ? new AbortController() : null;
  currentUser = null;
  resetActionStatus();
  var invitationOverlay = document.getElementById('invitationOverlay');
  if (invitationOverlay) { invitationOverlay.hidden = true; invitationOverlay.style.display = 'none'; }
  stopInboxAutoRefresh();
  userPreferences = null;
  document.getElementById('loginOverlay').style.display = 'flex';
  document.getElementById('appLayout').style.display = 'none';
  var container = document.getElementById('loginUsers');
  container.innerHTML = '';
  var pinInput = document.getElementById('loginPin');
  var pinForm = document.getElementById('loginPinForm');
  var errorEl = document.getElementById('loginError');
  var description = document.getElementById('loginDesc');
  window._selectedLoginUser = null;
  if (pinInput) pinInput.value = '';
  if (pinForm) pinForm.hidden = true;
  if (pinForm) delete pinForm.dataset.mode;
  var pinLabel = document.getElementById('loginPinLabel');
  var pinSubmit = document.getElementById('loginPinSubmit');
  if (pinLabel) pinLabel.textContent = '个人访问码';
  if (pinSubmit) pinSubmit.textContent = '进入';
  if (errorEl) errorEl.textContent = '';
  
  // Show network access URL
  var netEl = document.getElementById('loginNetwork');
  if (netEl) {
    netEl.innerHTML = '<div class="login-network-label">访问地址</div><div class="login-network-url">正在检测...</div>';
    fetch('/api/network/ip').then(function(r) {
      if (!r.ok) throw new Error('无法读取访问地址');
      return r.json();
    }).then(function(net) {
      if (!net || !net.local_ip || !net.port) throw new Error('访问地址不完整');
      netEl.innerHTML = '<div class="login-network-label">其他设备访问地址</div><div class="login-network-url">http://' + net.local_ip + ':' + net.port + '</div>';
    }).catch(function(){
      netEl.innerHTML = '<div class="login-network-label">访问地址</div><div class="login-network-url">http://' + window.location.host + '</div>';
    });
  }
  
  fetch('/api/auth/users', {
    credentials: 'include',
    cache: 'no-store',
    signal: _loginUsersController ? _loginUsersController.signal : undefined
  }).then(function(r) {
    if (!r.ok) throw new Error('无法读取账号列表');
    return r.json();
  }).then(function(data) {
    // Several expired requests can arrive together. Only the latest account
    // screen is allowed to render, otherwise delayed responses append duplicate
    // weekly-report and account cards after the user has already navigated away.
    if (loginViewToken !== _loginViewToken || document.getElementById('loginOverlay').style.display === 'none') return;
    var requiresPin = Boolean(data.requires_pin);
    if (description) description.textContent = requiresPin ? '选择账号后继续' : '选择账号进入';
    var users = Array.isArray(data.users) ? data.users.slice() : [];
    users.sort(function(a, b) { return a.name.localeCompare(b.name); });
    container.innerHTML = '<button type="button" class="login-user-btn" data-login-overview="true"><span class="login-user-avatar" style="background:#8B7355">' + uiIcon('star') + '</span><span class="login-user-name">本周工作</span></button>' +
      users.map(function(u) {
        return '<button type="button" class="login-user-btn" data-user-id="' + escapeHtml(u.id) + '"><span class="login-user-avatar" style="background:' + escapeHtml(u.color) + '">' + escapeHtml(u.name.charAt(0).toUpperCase()) + '</span><span class="login-user-name">' + escapeHtml(u.name) + '</span></button>';
      }).join('');
    container.querySelector('[data-login-overview]').onclick = enterOverview;
    users.forEach(function(u) {
      var button = container.querySelector('[data-user-id="' + String(u.id).replace(/"/g, '\\"') + '"]');
      if (button) button.onclick = function() { selectLoginUser(u, requiresPin); };
    });
    // The shared weekly board is a LAN-only read-only entry. Hide its button
    // when this client is not an internal viewer, so it cannot fail with a
    // misleading login-expired message from outside the office network.
    fetch('/api/auth/me', { credentials: 'include', cache: 'no-store' }).then(function(r) {
      return r.ok ? r.json() : null;
    }).then(function(me) {
      if (loginViewToken !== _loginViewToken || document.getElementById('loginOverlay').style.display === 'none') return;
      if (!me || !me.internal_viewer) {
        var overviewButton = container.querySelector('[data-login-overview]');
        if (overviewButton) overviewButton.style.display = 'none';
      }
    }).catch(function() {});
  }).catch(function(error) {
    if (error && error.name === 'AbortError') return;
    if (loginViewToken !== _loginViewToken) return;
    if (errorEl) errorEl.textContent = '账号列表暂时无法加载，请刷新后重试';
  });
}

function selectLoginUser(user, requiresPin) {
  if (!requiresPin) {
    loginUser(user.id, '');
    return;
  }
  window._selectedLoginUser = user;
  document.querySelectorAll('.login-user-btn').forEach(function(button) {
    button.classList.toggle('is-selected', button.dataset.userId === user.id);
  });
  var form = document.getElementById('loginPinForm');
  var pinInput = document.getElementById('loginPin');
  var errorEl = document.getElementById('loginError');
  var pinLabel = document.getElementById('loginPinLabel');
  var submit = document.getElementById('loginPinSubmit');
  var isSetup = Boolean(user.pin_setup_required);
  var isPassword = Boolean(user.password_login);
  if (form) form.hidden = false;
  if (form) form.dataset.mode = isSetup ? 'setup' : (isPassword ? 'password' : 'login');
  if (pinLabel) pinLabel.textContent = isSetup ? '创建 6 位个人访问码' : (isPassword ? '密码' : '个人访问码');
  if (submit) submit.textContent = isSetup ? '创建并进入' : '进入';
  if (pinInput) pinInput.placeholder = isSetup ? '设置你的 6 位数字' : (isPassword ? '输入密码' : '输入 6 位数字');
  if (pinInput) { pinInput.value = ''; pinInput.focus(); }
  if (errorEl) errorEl.textContent = '';
}

async function loginUser(userId, pin) {
  var errorEl = document.getElementById('loginError');
  try {
    var data = await api('/api/auth/login', { method: 'POST', body: JSON.stringify({ user: userId, pin: pin || '', password: pin || '' }) });
    if (data && data.success) {
      currentUser = data.user;
      showApp();
    }
  } catch (error) {
    if (errorEl) errorEl.textContent = error.message || '无法登录，请稍后重试';
  }
}

async function setupUserPin(userId, pin) {
  var errorEl = document.getElementById('loginError');
  try {
    var data = await api('/api/auth/setup-pin', { method: 'POST', body: JSON.stringify({ user: userId, pin: pin }) });
    if (data && data.success) {
      currentUser = data.user;
      showApp();
    }
  } catch (error) {
    if (errorEl) errorEl.textContent = error.message || '暂时无法创建访问码，请稍后重试';
    if (error && error.pin_setup_required === false) showLogin();
  }
}

document.getElementById('loginPinForm').addEventListener('submit', function(event) {
  event.preventDefault();
  var selected = window._selectedLoginUser;
  var pin = document.getElementById('loginPin').value.trim();
  var errorEl = document.getElementById('loginError');
  if (!selected) return;
  var passwordMode = form && form.dataset.mode === 'password';
  if ((!passwordMode && !/^\d{6}$/.test(pin)) || (passwordMode && pin.length < 1)) {
    if (errorEl) errorEl.textContent = '请输入 6 位数字访问码';
    return;
  }
  var form = document.getElementById('loginPinForm');
  if (form && form.dataset.mode === 'setup') setupUserPin(selected.id, pin);
  else loginUser(selected.id, pin);
});

function enterOverview() {
  _loginViewToken++;
  if (_loginUsersController) _loginUsersController.abort();
  document.getElementById('loginOverlay').style.display = 'none';
  document.getElementById('appLayout').style.display = 'flex';
  // 显示总览导航，隐藏个人导航
  var p = document.querySelector('.nav-personal');
  var o = document.querySelector('.nav-overview');
  if(p) p.style.display = 'none';
  if(o) o.style.display = '';
  currentUser = null;
  userPreferences = null;
  updateSidebarIdentity();
  switchPage('overview');
}

async function showApp() {
  _loginViewToken++;
  if (_loginUsersController) _loginUsersController.abort();
  document.getElementById('loginOverlay').style.display = 'none';
  document.getElementById('appLayout').style.display = 'flex';
  // 显示个人导航，隐藏总览导航
  var p = document.querySelector('.nav-personal');
  var o = document.querySelector('.nav-overview');
  if(p) p.style.display = '';
  if(o) o.style.display = 'none';
  updateSidebarIdentity();
  // 先显示默认页面，让手机用户可以立即开始工作；偏好设置在后台补齐。
  var now = new Date();
  calendarYear = now.getFullYear();
  calendarMonth = now.getMonth();
  document.getElementById('dashDate').textContent = formatChineseToday(now);
  switchPage('dashboard');
  showGmailOAuthNotice();
  loadUserPreferences().then(function(preferences) {
    if (preferences && preferences.default_page && preferences.default_page !== 'dashboard' && currentPage === 'dashboard') switchPage(preferences.default_page);
  }).catch(function(error) { console.warn('偏好设置稍后重试', error); });
  refreshInboxBadge();
  startInboxAutoRefresh();
}

// ========== OVERVIEW（全新设计：周报为主体 + 可展开客户表）==========
var OV = {
  colors: { hamid: '#8B9DAF', amy: '#C4877A', kelley: '#8BA88A' },
  labels: { hamid: 'Hamid', amy: 'Amy', kelley: 'Kelley' },
  _custOpen: false,
  _allCusts: [],
  _weeklyLoadToken: 0,
  _detailToken: 0,
  _detailController: null,
  _weeklyMembers: {},
  _weeklyFilter: 'all'
};

function getWeekStart(offset) {
  var d = new Date();
  d.setDate(d.getDate() + offset * 7);
  var day = d.getDay();
  var diff = d.getDate() - day + (day === 0 ? -6 : 1);
  d.setDate(diff);
  return d.toISOString().split('T')[0];
}

function formatWeekLabel(ws) {
  var d = new Date(ws), e = new Date(d);
  e.setDate(e.getDate() + 6);
  var m=['1月','2月','3月','4月','5月','6月','7月','8月','9月','10月','11月','12月'];
  return m[d.getMonth()]+d.getDate()+'日—'+m[e.getMonth()]+e.getDate()+'日';
}

function weeklyMemberShell(uid) {
  var color = OV.colors[uid];
  return '<section class="weekly-person is-loading" data-weekly-member="' + uid + '" style="--person-color:' + color + '">' +
    '<header class="weekly-person-header"><div class="weekly-person-avatar" style="background:' + color + '">' + OV.labels[uid][0] + '</div><div><h2>' + OV.labels[uid] + '</h2></div></header>' +
    '<div class="weekly-member-state" role="status"><span class="loading-spinner" aria-hidden="true"></span><span>正在读取已选择内容</span></div></section>';
}

function weeklyMemberIds() {
  return Object.keys(OV.labels);
}

function defaultWeeklyMember() {
  var memberIds = weeklyMemberIds();
  if (typeof currentUser !== 'undefined' && currentUser && OV.labels[currentUser]) return currentUser;
  return memberIds[0] || '';
}

function renderWeeklyMemberNavigator() {
  var root = document.getElementById('weeklyTeamOverview');
  if (!root) return;
  var memberIds = weeklyMemberIds();
  if (!OV.labels[OV._weeklyFilter]) OV._weeklyFilter = defaultWeeklyMember();
  var activeFilter = OV._weeklyFilter;
  var filters = '';
  memberIds.forEach(function(uid) {
    var isActive = activeFilter === uid;
    var filterValue = escapeHtml(JSON.stringify(String(uid)));
    var color = OV.colors[uid] || '#8B7355';
    filters += '<button type="button" class="weekly-member-filter' + (isActive ? ' is-active' : '') + '" data-weekly-filter="' + escapeHtml(uid) + '" aria-pressed="' + isActive + '" style="--member-color:' + color + '" onclick="setWeeklyMemberFilter(' + filterValue + ')"><i aria-hidden="true">' + escapeHtml((OV.labels[uid] || uid).slice(0, 1)) + '</i><span>' + escapeHtml(OV.labels[uid]) + '</span></button>';
  });
  root.innerHTML = '<div class="weekly-team-summary" aria-live="polite"><span>本周沟通</span><strong>' + escapeHtml(OV.labels[activeFilter] || '客户沟通') + ' 的客户记录</strong></div><div class="weekly-team-index" aria-label="选择负责人的周报">' + filters + '</div>';
}

function setWeeklyMemberFilter(uid) {
  if (!OV.labels[uid]) return;
  OV._weeklyFilter = uid;
  var board = document.getElementById('weeklyMemberContent');
  if (board) board.innerHTML = weeklyMemberShell(uid);
  renderWeeklyMemberNavigator();
  Array.from(document.querySelectorAll('.weekly-member-filter')).some(function(button) {
    if (button.dataset.weeklyFilter !== OV._weeklyFilter) return false;
    button.focus({ preventScroll: true });
    return true;
  });
  loadWeeklyMember(uid);
}

function weeklyLocalCacheKey(uid, weekStart) {
  return 'tradeos.weekly-summary.v3:' + weekStart + ':' + uid;
}

function readWeeklyLocalCache(uid, weekStart) {
  try {
    var raw = localStorage.getItem(weeklyLocalCacheKey(uid, weekStart));
    if (!raw) return null;
    var cached = JSON.parse(raw);
    return cached && cached.data ? cached.data : null;
  } catch (e) {
    return null;
  }
}

function writeWeeklyLocalCache(uid, weekStart, data) {
  try { localStorage.setItem(weeklyLocalCacheKey(uid, weekStart), JSON.stringify({ savedAt: Date.now(), data: data })); } catch (e) {}
}

function weeklyCustomerText(customer, field) {
  return customer && customer[field] || '';
}

function weeklyRichTextBlock(source, className, label) {
  if (!source) return '';
  return '<div class="weekly-work-block ' + (className || '') + '"><span>' + label + '</span><p>' + renderRichText(source) + '</p></div>';
}

function renderWeeklyMember(uid, data, error, options) {
  options = options || {};
  var section = document.querySelector('[data-weekly-member="' + uid + '"]');
  if (!section) return;
  var color = OV.colors[uid];
  if (error || !data || data.error) {
    if (options.staleData) {
      renderWeeklyMember(uid, data, null, { staleError: true });
      return;
    }
    section.classList.remove('is-loading');
    section.innerHTML = '<header class="weekly-person-header"><div class="weekly-person-avatar" style="background:' + color + '">' + OV.labels[uid][0] + '</div><div><h2>' + OV.labels[uid] + '</h2></div></header><div class="weekly-member-error" role="alert"><strong>这位成员的周报加载失败</strong><button type="button" onclick="loadWeeklyMember(\'' + uid + '\')">重试</button></div>';
    renderWeeklyMemberNavigator();
    return;
  }
  var reps = data.reported_customers || [];
  var reportPagination = data.reported_customer_pagination || {};
  var statusText = options.staleError ? '更新失败' : (options.fromCache ? '更新中' : '');
  var html = '<header class="weekly-person-header"><div class="weekly-person-avatar" style="background:' + color + '">' + OV.labels[uid][0] + '</div><div><h2>' + OV.labels[uid] + '</h2></div>' + (statusText ? '<div class="weekly-person-header-meta"><em>' + statusText + '</em></div>' : '') + '</header>';
  if (options.staleError) html += '<div class="weekly-member-refresh-error" role="alert">更新失败，已保留最近一次内容。<button type="button" onclick="loadWeeklyMember(\'' + uid + '\')">重试</button></div>';
  if (!reps.length) html += '<div class="weekly-person-empty"><strong>本周还没有确认的沟通</strong><span>确认后的客户进展会按负责人汇总在这里。</span></div>';
  reps.forEach(function(r) {
    var nm = r.customer_company || r.customer_name || '客户', canOpen = !!r.customer_id;
    html += '<article class="weekly-work-card">';
    html += '<div class="weekly-work-top"><div class="weekly-work-title"><h3>' + escapeHtml(nm) + '</h3></div><time>' + escapeHtml(formatDate(r.date || '')) + '</time></div>';
    var meta = [r.customer_name && r.customer_name !== nm ? r.customer_name : '', r.customer_country].filter(Boolean);
    if (meta.length) html += '<div class="weekly-customer-meta">' + meta.map(function(item) { return '<span>' + escapeHtml(item) + '</span>'; }).join('') + '</div>';
    var count = Number(r.activity_count || 0);
    html += '<div class="weekly-entry-count">本周 ' + count + ' 次活动</div>';
    var actualWork = weeklyCustomerText(r, 'actual_work');
    var result = weeklyCustomerText(r, 'result');
    var nextStep = r.next_step || '';
    html += weeklyRichTextBlock(actualWork, '', '实际工作');
    html += weeklyRichTextBlock(result, 'result', '结果');
    html += weeklyRichTextBlock(nextStep, 'next', '下一步');
    if (canOpen) html += '<button type="button" class="weekly-card-link" onclick="overviewShowCustDetail(' + Number(r.customer_id) + ',\'' + uid + '\')">查看详情 ' + uiIcon('right') + '</button>';
    html += '</article>';
  });
  if (reportPagination.has_next) html += '<button class="btn btn-sm weekly-load-more" type="button" onclick="loadMoreWeeklyMembers(\'' + uid + '\')">显示更多客户</button>';
  section.classList.remove('is-loading'); section.innerHTML = html;
  renderWeeklyMemberNavigator();
}

async function loadWeeklyMember(uid, loadToken) {
  var ws = getWeekStart(overviewWeekOffset);
  loadToken = loadToken || OV._weeklyLoadToken;
  var cached = readWeeklyLocalCache(uid, ws);
  if (cached && loadToken === OV._weeklyLoadToken) {
    OV._weeklyMembers[uid] = cached;
    renderWeeklyMember(uid, cached, null, { fromCache: true });
  }
  try {
    var data = await api('/api/weekly-summary/' + encodeURIComponent(uid) + '?week_start=' + encodeURIComponent(ws) + '&limit=10&offset=0');
    if (loadToken !== OV._weeklyLoadToken || ws !== getWeekStart(overviewWeekOffset)) return;
    writeWeeklyLocalCache(uid, ws, data);
    OV._weeklyMembers[uid] = data;
    renderWeeklyMember(uid, data, null);
  } catch (e) {
    if (loadToken !== OV._weeklyLoadToken) return;
    if (cached) renderWeeklyMember(uid, cached, e, { staleData: true });
    else renderWeeklyMember(uid, null, e);
  }
}

async function loadMoreWeeklyMembers(uid) {
  var current = OV._weeklyMembers[uid];
  if (!current || !current.reported_customer_pagination || !current.reported_customer_pagination.has_next) return;
  var ws = getWeekStart(overviewWeekOffset);
  var offset = (current.reported_customers || []).length;
  try {
    var next = await api('/api/weekly-summary/' + encodeURIComponent(uid) + '?week_start=' + encodeURIComponent(ws) + '&limit=10&offset=' + offset);
    if (ws !== getWeekStart(overviewWeekOffset) || !next) return;
    current.reported_customers = (current.reported_customers || []).concat(next.reported_customers || []);
    current.reported_customer_count = next.reported_customer_count;
    current.reported_customer_pagination = next.reported_customer_pagination || {};
    current.cache_status = next.cache_status || current.cache_status;
    OV._weeklyMembers[uid] = current;
    renderWeeklyMember(uid, current, null);
  } catch (e) { showToast('更多周报客户暂时无法加载', 'error'); }
}

async function loadOverview() {
  _pageDataLoadedAt = Date.now();
  var loadToken = ++OV._weeklyLoadToken;
  OV._weeklyMembers = {};
  OV._weeklyFilter = '';
  var ws = getWeekStart(overviewWeekOffset);
  document.getElementById('overviewDateLabel').textContent = formatWeekLabel(ws);
  try {
    var userData = await api('/api/auth/users');
    var activeUsers = (userData.users || []).filter(function(user) { return user.id; });
    if (loadToken !== OV._weeklyLoadToken) return;
    if (activeUsers.length) {
      OV.labels = {};
      OV.colors = {};
      activeUsers.forEach(function(user) {
        OV.labels[user.id] = user.name || user.id;
        OV.colors[user.id] = user.color || '#8B7355';
      });
    }
  } catch (error) { /* keep the current three-member fallback when auth is unavailable */ }
  if (loadToken !== OV._weeklyLoadToken) return;
  OV._weeklyFilter = defaultWeeklyMember();
  document.getElementById('ovReports').innerHTML = '<div class="weekly-team-overview" id="weeklyTeamOverview"></div><div class="weekly-board" id="weeklyMemberContent">' + (OV._weeklyFilter ? weeklyMemberShell(OV._weeklyFilter) : '') + '</div>';
  renderWeeklyMemberNavigator();
  if (OV._weeklyFilter) loadWeeklyMember(OV._weeklyFilter, loadToken);
}

function showOverviewCustomerLoading(owner) {
  var existing = document.getElementById('ovDetailModal');
  if (existing) existing.remove();
  var div = document.createElement('div');
  div.innerHTML = '<div class="modal-overlay show" id="ovDetailModal" role="dialog" aria-modal="true" aria-labelledby="ovDetailTitle" onclick="if(event.target===this)overviewCloseCustDetail()"><div class="modal ov-customer-workspace"><div class="modal-header ov-customer-header"><div><div class="workspace-kicker">客户工作区 · 只读</div><h3 id="ovDetailTitle">正在加载客户详情…</h3><div class="workspace-meta"><span>' + escapeHtml(OV.labels[owner] || owner || '') + '</span></div></div><button type="button" class="modal-close" aria-label="返回本周工作" onclick="event.preventDefault();event.stopPropagation();overviewCloseCustDetail()">' + uiIcon('close') + '</button></div><div class="modal-body ov-customer-body"><div class="ov-customer-loading" role="status"><span class="loading-spinner" aria-hidden="true"></span><p>正在读取本周进展和最近沟通…</p></div></div></div></div>';
  document.body.appendChild(div.firstElementChild);
  var close = document.getElementById('ovDetailModal').querySelector('.modal-close');
  if (close) close.focus({preventScroll:true});
}

function showOverviewCustomerError(message, custId, owner, timelinePage) {
  var modal = document.getElementById('ovDetailModal');
  if (!modal) return;
  var body = modal.querySelector('.ov-customer-body');
  if (!body) return;
  body.innerHTML = '<div class="ov-customer-loading ov-customer-loading-error" role="alert"><strong>客户详情加载失败</strong><p>' + escapeHtml(message || '请检查连接后重试。') + '</p><button class="btn btn-sm btn-primary" type="button" onclick="overviewShowCustDetail(' + Number(custId) + ',\'' + escapeHtml(owner) + '\',' + Number(timelinePage || 1) + ')">重试</button></div>';
}

async function overviewShowCustDetail(custId, owner, timelinePage) {
  custId = Number(custId);
  timelinePage = Math.max(1, Number(timelinePage) || 1);
  if (!custId || !owner) {
    showToast('无法识别该客户', 'warning');
    return;
  }
  var detailToken = ++OV._detailToken;
  if (OV._detailController) OV._detailController.abort();
  OV._detailController = typeof AbortController === 'function' ? new AbortController() : null;
  showOverviewCustomerLoading(owner);
  // Weekly reports already carry the owner and customer ID. Loading details
  // directly keeps the card usable even when the overview customer index is
  // incomplete or has not refreshed yet.
  var c;
  try {
    c = await api('/api/overview/customers/' + encodeURIComponent(owner) + '/' + Number(custId) + '?week_start=' + encodeURIComponent(getWeekStart(overviewWeekOffset)) + '&timeline_page=' + timelinePage, {
      signal: OV._detailController ? OV._detailController.signal : undefined,
      silentError: true
    });
  } catch (e) {
    if (detailToken === OV._detailToken) showOverviewCustomerError('请检查连接后重试。', custId, owner, timelinePage);
    return;
  }
  if (detailToken !== OV._detailToken) return;
  
  var customer = c.customer || {};
  var website = (customer.website || '').trim();
  var websiteUrl = website && !/^https?:\/\//i.test(website) ? 'https://' + website : website;
  var facts = [
    ['归属成员', customer.owner_label || customer.owner], ['国家 / 地区', customer.country], ['行业 / 领域', customer.industry || customer.field],
    ['业务角色', customer.business_role || '未标记'], ['业务阶段', customer.business_stage || '未标记'], ['来源', customer.source || customer.import_source], ['建立日期', customer.created_at],
    ['最近实际联系', customer.last_actual_contact]
  ].filter(function(item) { return item[1]; });
  var h = '<div class="modal-overlay show" id="ovDetailModal" role="dialog" aria-modal="true" aria-labelledby="ovDetailTitle" onclick="if(event.target===this)overviewCloseCustDetail()"><div class="modal ov-customer-workspace"><div class="modal-header ov-customer-header"><div><div class="workspace-kicker">客户工作区 · 只读</div><h3 id="ovDetailTitle">' + escapeHtml(customer.company || customer.name || '客户详情') + '</h3><div class="workspace-meta">' +
    (customer.name && customer.company && customer.name !== customer.company ? '<span>' + escapeHtml(customer.name) + '</span>' : '') +
    (websiteUrl ? '<a href="' + escapeHtml(websiteUrl) + '" target="_blank" rel="noopener">访问官网 ↗</a>' : '') +
    '</div></div><button type="button" class="modal-close" aria-label="返回本周工作" onclick="event.preventDefault();event.stopPropagation();overviewCloseCustDetail()">' + uiIcon('close') + '</button></div><div class="modal-body ov-customer-body">';
  h += '<section class="ov-customer-facts">' + facts.map(function(item) { return '<div><span>' + item[0] + '</span><strong>' + escapeHtml(String(item[1])) + '</strong></div>'; }).join('') + '</section>';
  h += '<div class="ov-customer-columns"><div class="ov-customer-main">';
  h += '<section class="ov-customer-section"><h4>本周发生了什么</h4><div class="ov-customer-timeline">' + ((c.week_activity || []).length ? c.week_activity.map(function(item) { return '<article><time>' + escapeHtml(formatDate(item.date || '')) + '</time><div><span>' + escapeHtml(item.type === 'outreach' ? '开发邮件' : communicationTypeLabel(item.activity_type)) + '</span><p>' + renderRichText(item.result || item.content || '已记录工作') + '</p>' + (item.next_plan ? '<small>已记录下一步：' + renderRichText(item.next_plan) + '</small>' : '') + '</div></article>'; }).join('') : '<p class="ov-customer-empty">本周没有更多已上报记录。</p>') + '</div></section>';
  var pagination = c.timeline_pagination || {};
  h += '<section class="ov-customer-section"><h4>此前发生过什么</h4><div class="ov-customer-timeline">' + ((c.recent_timeline || []).length ? c.recent_timeline.map(function(item) { return '<article><time>' + escapeHtml(formatDate(item.date || '')) + '</time><div><span>' + escapeHtml(item.type === 'outreach' ? '开发邮件' : communicationTypeLabel(item.activity_type)) + '</span><p>' + renderRichText(item.result || item.content || '已记录工作') + '</p>' + (item.next_plan ? '<small>下一步：' + renderRichText(item.next_plan) + '</small>' : '') + '</div></article>'; }).join('') : '<p class="ov-customer-empty">暂无可展示的历史记录。</p>') + '</div>' + (pagination.has_next ? '<button class="ov-timeline-more" type="button" onclick="overviewShowCustDetail(' + custId + ',\'' + escapeHtml(owner) + '\',' + (timelinePage + 1) + ')">查看更早记录</button>' : '') + '</section></div><aside class="ov-customer-side"><section class="ov-customer-section"><h4>当前状态</h4><div class="ov-customer-copy"><span>当前等待</span><p>' + escapeHtml(customer.current_waiting || '当前未记录明确等待事项') + '</p></div><div class="ov-customer-copy"><span>已确认下一步</span><p>' + escapeHtml(customer.next_confirmed_action || '本周未记录明确下一步') + '</p></div></section>';
  h += '<section class="ov-customer-section"><h4>未完成待办</h4>' + ((c.open_tasks || []).length ? c.open_tasks.map(function(item) { return '<div class="ov-customer-task"><time>' + escapeHtml(formatDate(item.remind_date || '')) + '</time><strong>' + escapeHtml(item.title || item.content || '待办') + '</strong>' + (item.reason ? '<p>' + escapeHtml(item.reason) + '</p>' : '') + '</div>'; }).join('') : '<p class="ov-customer-empty">暂无未完成待办。</p>') + '</section></aside></div></div></div></div>';
  
  var div = document.createElement('div');
  div.innerHTML = h;
  document.body.appendChild(div.firstElementChild);
  document.getElementById('ovDetailModal').querySelector('.modal-close').focus({preventScroll:true});
}

function overviewCloseCustDetail() {
  OV._detailToken++;
  if (OV._detailController) { OV._detailController.abort(); OV._detailController = null; }
  var modal = document.getElementById('ovDetailModal');
  if (modal) {
    modal.remove();
    syncModalBodyLock();
  }
}

function overviewPrevWeek() { overviewWeekOffset--; loadOverview(); }
function overviewNextWeek() { overviewWeekOffset++; loadOverview(); }
