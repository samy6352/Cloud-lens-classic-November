/** Ephemeral interaction state. Identity is semantic; array positions are never resource IDs. */
const KEY_ENCODER = new TextEncoder();
const KEY_HEX = Array.from({ length: 256 }, (_, value) => value.toString(16).padStart(2, "0"));

export function stableKey(value) {
  // Preserve the complete UTF-8 key, without per-byte callbacks and temporary arrays.
  const text = String(value);
  let result = "";
  if (!/[^\x00-\x7f]/.test(text)) {
    for (let index = 0; index < text.length; index++) result += KEY_HEX[text.charCodeAt(index)];
  } else {
    for (const byte of KEY_ENCODER.encode(text)) result += KEY_HEX[byte];
  }
  return result;
}

function identity(el, root) {
  if (el === root) return "$root";
  if (el.id) return `#${el.id}`;
  const data = el.dataset || {};
  const own = data.stateKey || data.table || data.rid
    || (data.rg && `${data.rg}|${data.day || ""}`);
  if (own) return `${el.tagName}:${own}`;
  const parent = el.parentElement;
  const anchor = parent?.closest("[id],[data-state-key],[data-table],[data-rid],[data-rg]");
  const prefix = anchor && root.contains(anchor) ? identity(anchor, root) : "";
  const choice = el.matches('input[type="radio"],input[type="checkbox"]')
    ? `${data.group || el.name || "choice"}:${el.getAttribute("value") || "on"}` : "";
  const label = data.tsearch || data.col || data.page || data.cat || choice || el.name
    || el.getAttribute("aria-label") || el.getAttribute("placeholder")
    || (anchor?.hasAttribute("data-state-key") && el.className ? el.className : "")
    || (el.tagName === "DETAILS" ? el.querySelector("summary")?.textContent : "")
    || (el.matches("button,a,summary") ? el.textContent?.trim() : "")
    || el.className;
  return label ? `${prefix}/${el.tagName}:${label}` : null;
}

function index(root, selector) {
  const map = new Map();
  for (const el of root.querySelectorAll(selector)) {
    const key = identity(el, root);
    // Ambiguous controls are deliberately not matched to a different row.
    if (key) map.set(key, map.has(key) ? null : el);
  }
  return map;
}

const CONTROLS = "input:not([type=password]):not([type=file]):not([type=hidden]),textarea,select";
const EXPANDED = "details,[aria-expanded]";
const SCROLL = ".table-wrap,[data-preserve-scroll]";
const LIVE = ".wf-drill,.rc-drill,[data-preserve-live]";
const scrollCommits = new WeakMap();

function settleScroll(root, positions) {
  const anchor = root.firstChild;
  for (const [node, left, top] of positions) {
    if ((!left && !top) || scrollCommits.get(node)?.valid()) continue;
    const commit = { valid: () => root.isConnected && root.firstChild === anchor };
    scrollCommits.set(node, commit);
    // Synchronous subpanel construction can briefly shorten the view and clamp its scroll.
    queueMicrotask(() => {
      if (scrollCommits.get(node) !== commit) return;
      scrollCommits.delete(node);
      if (commit.valid()) { node.scrollLeft = left; node.scrollTop = top; }
    });
  }
}

export function captureInteractions(root) {
  const controls = new Map();
  for (const [key, el] of index(root, CONTROLS)) {
    if (!el) continue;
    controls.set(key, {
      value: el.value, checked: el.checked,
      selected: el.multiple ? [...el.selectedOptions].map(o => o.value) : null,
      start: el.selectionStart, end: el.selectionEnd, direction: el.selectionDirection,
    });
  }
  const expanded = new Map();
  for (const [key, el] of index(root, EXPANDED)) {
    if (el) expanded.set(key, el.tagName === "DETAILS" ? el.open : el.getAttribute("aria-expanded"));
  }
  const scroll = new Map();
  for (const [key, el] of index(root, SCROLL)) {
    if (el) scroll.set(key, [el.scrollLeft, el.scrollTop]);
  }
  const active = root.contains(document.activeElement) ? document.activeElement : null;
  return { controls, expanded, scroll, top: root.scrollTop, left: root.scrollLeft,
    focus: active ? identity(active, root) : null };
}

export function restoreInteractions(root, state, { focus = true } = {}) {
  if (!state) return;
  for (const [key, el] of index(root, CONTROLS)) {
    const saved = state.controls.get(key);
    if (!el || !saved) continue;
    if (el.tagName === "SELECT") {
      if (saved.selected) {
        for (const option of el.options) option.selected = saved.selected.includes(option.value);
      } else if ([...el.options].some(o => o.value === saved.value)) el.value = saved.value;
    } else el.value = saved.value;
    if ("checked" in el) el.checked = saved.checked;
    if (saved.start != null && el.setSelectionRange) {
      try { el.setSelectionRange(saved.start, saved.end, saved.direction); } catch { /* non-text input */ }
    }
  }
  for (const [key, el] of index(root, EXPANDED)) {
    if (!el || !state.expanded.has(key)) continue;
    const value = state.expanded.get(key);
    if (el.tagName === "DETAILS") el.open = value;
    else {
      el.setAttribute("aria-expanded", value);
      const panel = el.getAttribute("aria-controls")
        ? document.getElementById(el.getAttribute("aria-controls"))
        : el.parentElement?.querySelector(".sv-body");
      if (panel) panel.hidden = value !== "true";
    }
  }
  if (focus && state.focus) {
    const target = index(root, `${CONTROLS},button,a,summary,[tabindex]`).get(state.focus);
    target?.focus({ preventScroll: true });
  }
  for (const [key, el] of index(root, SCROLL)) {
    const position = state.scroll.get(key);
    if (el && position) [el.scrollLeft, el.scrollTop] = position;
  }
  root.scrollTop = state.top;
  root.scrollLeft = state.left;
}

/** Capture at commit time, not request time: edits made while Azure is running must win. */
export function replaceContent(root, html, { preserve = true } = {}) {
  if (!root) return;
  const ancestors = [];
  if (preserve) for (let node = root.parentElement; node; node = node.parentElement) {
    ancestors.push([node, node.scrollLeft, node.scrollTop]);
  }
  const state = preserve ? captureInteractions(root) : null;
  const live = preserve ? index(root, LIVE) : new Map();
  const template = document.createElement("template");
  template.innerHTML = html;
  if (preserve) {
    for (const [key, el] of index(template.content, LIVE)) {
      const previous = live.get(key);
      if (el && previous) el.replaceWith(previous);
    }
  }
  root.replaceChildren(template.content);
  restoreInteractions(root, state);
  for (const [node, left, top] of ancestors) { node.scrollLeft = left; node.scrollTop = top; }
  if (state) settleScroll(root, [[root, state.left, state.top], ...ancestors]);
}

/** Bounded page-lifetime snapshots: retain real nodes and handlers until explicitly cleared. */
export function createViewCache(limit = 8, ttl = Infinity) {
  const entries = new Map();
  const remove = key => {
    entries.get(key)?.fragment.remove();
    entries.delete(key);
  };
  return {
    save(key, root, extra = {}) {
      if (!key || !root.childElementCount || root.querySelector(".sk-wrap")) return;
      const state = captureInteractions(root);
      // Connected, inert storage keeps Chart.js from destroying a retained canvas on the next
      // chart draw. Hidden content is neither tabbable nor exposed to the accessibility tree.
      const fragment = document.createElement("div");
      fragment.hidden = true;
      fragment.inert = true;
      fragment.setAttribute("aria-hidden", "true");
      fragment.dataset.dashboardCache = key;
      document.body.appendChild(fragment);
      fragment.append(...root.childNodes);
      remove(key);
      entries.set(key, { fragment, state, at: Date.now(), onclick: root.onclick, ...extra });
      while (entries.size > limit) remove(entries.keys().next().value);
    },
    take(key, root) {
      const entry = entries.get(key);
      if (!entry) return null;
      entries.delete(key);
      if (Date.now() - entry.at >= ttl) {
        entry.fragment.remove();
        return null;
      }
      root.replaceChildren(...entry.fragment.childNodes);
      entry.fragment.remove();
      root.onclick = entry.onclick;
      restoreInteractions(root, entry.state, { focus: false });
      return entry;
    },
    clear() { for (const key of entries.keys()) remove(key); },
  };
}
