(function () {
  const clean = (value) => String(value || '').replace(/\u00a0/g, ' ').replace(/[ \t]+/g, ' ').trim();
  const normalizeEmail = (value) => clean(value).toLowerCase();
  const normalizePhone = (value) => {
    const raw = clean(value); if (!raw) return '';
    const digits = raw.replace(/\D/g, '');
    return (raw.startsWith('+') ? '+' : '') + digits;
  };
  const fingerprint = (message) => {
    const stable = ['message_id', 'time', 'direction', 'sender', 'text', 'type']
      .map((key) => clean(message[key])).join('|');
    let hash = 2166136261;
    for (let i = 0; i < stable.length; i += 1) hash = Math.imul(hash ^ stable.charCodeAt(i), 16777619);
    return `msg-${(hash >>> 0).toString(16)}-${stable.length}`;
  };
  const scopeRoots = (root) => {
    const scopes = []; const seen = new Set();
    const visit = (scope) => {
      if (!scope || seen.has(scope)) return;
      seen.add(scope); scopes.push(scope);
      (scope.querySelectorAll?.('*') || []).forEach((node) => { if (node.shadowRoot) visit(node.shadowRoot); });
    };
    visit(root); return scopes;
  };
  const queryAllDeep = (root, selector) => {
    const nodes = []; const seen = new Set();
    scopeRoots(root).forEach((scope) => (scope.querySelectorAll?.(selector) || []).forEach((node) => {
      if (!seen.has(node)) { seen.add(node); nodes.push(node); }
    }));
    return nodes;
  };
  const queryOneDeep = (root, selector) => queryAllDeep(root, selector)[0] || null;
  const accessibleDocuments = (root = document) => {
    const documents = []; const seen = new Set();
    const visit = (doc) => {
      if (!doc || seen.has(doc)) return;
      seen.add(doc); documents.push(doc);
      queryAllDeep(doc, 'iframe').forEach((frame) => { try { visit(frame.contentDocument); } catch (_) { /* inaccessible frame */ } });
    };
    visit(root); return documents;
  };
  window.TradeOSAdapterCommon = { clean, normalizeEmail, normalizePhone, fingerprint, queryAllDeep, queryOneDeep, accessibleDocuments };
})();
