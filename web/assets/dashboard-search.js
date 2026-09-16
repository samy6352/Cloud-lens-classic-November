/** Fast, local navigation over the views the current account can actually open. */
import { PRIMARY_SECTIONS, sectionFor, sectionViews } from "./dashboard-navigation.js";
import { railIcon } from "./icons.js";

const LIMIT = 6;
const normalize = value => value.trim().toLowerCase().replace(/\s+/g, " ");
const ALIASES = {
  overview: "home dashboard total spend",
  "ai-billing": "artificial intelligence models tokens consumption utilization openai foundry inference",
  uptime: "hour hourly runtime cost by hour",
  tags: "application allocation showback chargeback",
  savings: "waste orphan idle rightsizing shutdown advisor optimization",
  commitments: "reservation reserved instance savings plan discounts",
  governance: "compliance policy",
  esu: "end of support extended security updates lifecycle",
  health: "retirements retirement lifecycle",
  "azure-updates": "announcements azure status service health incidents",
  report: "download excel csv powerpoint",
  settings: "scheduled exports configuration",
};

export function searchableTabs(views) {
  const labels = new Map(PRIMARY_SECTIONS.flatMap(section =>
    sectionViews(section.id, views).map(view => [view.id, view.label])));
  return views.flatMap(view => {
    const section = sectionFor(view.id, views);
    if (!section) return [];
    const label = labels.get(view.id) || view.label;
    return [{ id: view.id, label, section: section.label,
      keywords: normalize(`${label} ${view.label} ${view.id} ${section.label} ${ALIASES[view.id] || ""}`) }];
  });
}

export function matchingTabs(tabs, query) {
  const text = normalize(query);
  if (!text) return tabs;
  const words = text.split(" ");
  const rank = tab => normalize(tab.label) === text || tab.id === text ? 0
    : normalize(tab.label).startsWith(text) ? 1 : normalize(tab.label).includes(text) ? 2 : 3;
  return tabs.filter(tab => words.every(word => tab.keywords.includes(word)))
    .sort((a, b) => rank(a) - rank(b));
}

function validHistory(raw) {
  if (!Array.isArray(raw)) return [];
  return raw.filter(item => item && typeof item.id === "string" && item.id.length <= 100
    && typeof item.query === "string" && item.query.trim() && item.query.length <= 80).slice(0, LIMIT);
}

export function recentTabSearches(raw, tabs) {
  const available = new Map(tabs.map(tab => [tab.id, tab]));
  return validHistory(raw).flatMap(item => available.has(item.id)
    ? [{ ...item, tab: available.get(item.id) }] : []);
}

export function rememberTabSearch(raw, query, id) {
  const text = query.trim().slice(0, 80);
  if (!text) return validHistory(raw);
  return [{ query: text, id }, ...validHistory(raw).filter(item =>
    item.id !== id || normalize(item.query) !== normalize(text))].slice(0, LIMIT);
}

export function tabSearchHistoryKey(user) {
  const identity = user?.email || user?.name;
  return typeof identity === "string" && identity.trim()
    ? `cloudlens.tabSearch:${encodeURIComponent(user.source || "local")}:${encodeURIComponent(normalize(identity))}`
    : null;
}

export function wireTabSearch({ button, dialog, esc, getCurrent, navigate, readStore, writeStore, toast }) {
  const input = dialog.querySelector("#tabSearchInput");
  const results = dialog.querySelector("#tabSearchResults");
  const status = dialog.querySelector("#tabSearchStatus");
  const clear = dialog.querySelector("#tabSearchClear");
  let tabs = [], history = [], historyKey = null, rows = [], active = 0;

  const select = (index, scroll = false) => {
    active = index;
    const options = [...results.querySelectorAll("[data-search-index]")];
    for (const [i, option] of options.entries()) option.setAttribute("aria-selected", String(i === active));
    const option = options[active];
    if (option) {
      input.setAttribute("aria-activedescendant", option.id);
      if (scroll) option.scrollIntoView({ block: "nearest" });
    } else input.removeAttribute("aria-activedescendant");
  };

  const render = () => {
    const query = input.value.trim();
    const matches = matchingTabs(tabs, query);
    const recent = query ? [] : recentTabSearches(history, tabs);
    rows = [];
    const option = (tab, search = "") => {
      const index = rows.push({ tab, query: search || query || tab.label }) - 1;
      return `<button type="button" role="option" tabindex="-1" class="tab-search-option"
        id="tab-search-option-${index}" data-search-index="${index}" aria-selected="false">
        ${railIcon(tab.id)}
        <span class="tab-search-option-text"><span>${esc(search || tab.label)}</span>
          <small>${esc(search ? `${tab.section} / ${tab.label}` : tab.section)}</small></span>
        ${tab.id === getCurrent() ? '<span class="tab-search-current">Current</span>' : ""}
      </button>`;
    };
    results.innerHTML = `${recent.length ? `<div role="group" aria-labelledby="tabSearchRecentTitle">
      <div class="tab-search-group" id="tabSearchRecentTitle">Recent searches</div>
      ${recent.map(item => option(item.tab, item.query)).join("")}</div>` : ""}
      <div role="group" aria-labelledby="tabSearchAllTitle">
        <div class="tab-search-group" id="tabSearchAllTitle">${query ? "Matching tabs" : "All tabs"}</div>
        ${matches.map(tab => option(tab)).join("")}
      </div>`;
    status.textContent = matches.length ? `${matches.length} tab${matches.length === 1 ? "" : "s"} available`
      : "No matching tabs. Try another name.";
    clear.hidden = !recentTabSearches(history, tabs).length;
    select(0);
    results.scrollTop = 0;
  };

  const open = () => {
    if (button.disabled) return;
    input.value = "";
    render();
    if (!dialog.open) dialog.showModal();
    button.setAttribute("aria-expanded", "true");
    input.setAttribute("aria-expanded", "true");
    input.focus();
  };

  const activate = index => {
    const row = rows[index];
    if (!row || !tabs.some(tab => tab.id === row.tab.id)) {
      toast("This tab is no longer available for the current account or scope.", "err");
      render();
      return;
    }
    history = rememberTabSearch(history, row.query, row.tab.id);
    if (historyKey) writeStore(historyKey, history);
    dialog.close();
    navigate(row.tab.id).catch(error => toast(error?.message || "Could not open this tab.", "err"));
  };

  button.onclick = open;
  dialog.querySelector("#tabSearchClose").onclick = () => dialog.close();
  dialog.onclose = () => {
    button.setAttribute("aria-expanded", "false");
    input.setAttribute("aria-expanded", "false");
    input.removeAttribute("aria-activedescendant");
  };
  dialog.onclick = event => {
    if (event.target !== dialog) return;
    const box = dialog.getBoundingClientRect();
    if (event.clientX < box.left || event.clientX > box.right || event.clientY < box.top || event.clientY > box.bottom) dialog.close();
  };
  input.oninput = render;
  input.onkeydown = event => {
    if (event.isComposing) return;
    if (event.key === "ArrowDown" || event.key === "ArrowUp") {
      event.preventDefault();
      if (rows.length) select((active + (event.key === "ArrowDown" ? 1 : -1) + rows.length) % rows.length, true);
    } else if (event.key === "Enter") {
      event.preventDefault();
      if (rows.length) activate(active);
    }
  };
  results.onclick = event => {
    const option = event.target.closest("[data-search-index]");
    if (option) activate(Number(option.dataset.searchIndex));
  };
  results.onpointermove = event => {
    const option = event.target.closest("[data-search-index]");
    if (option) select(Number(option.dataset.searchIndex));
  };
  clear.onclick = () => {
    history = [];
    if (historyKey) writeStore(historyKey, history);
    render();
    input.focus();
  };
  return {
    update(views, user) {
      tabs = searchableTabs(views);
      const nextKey = tabSearchHistoryKey(user);
      if (nextKey !== historyKey) {
        historyKey = nextKey;
        history = historyKey ? validHistory(readStore(historyKey, [])) : [];
      }
      button.disabled = tabs.length === 0;
      if (dialog.open) render();
    },
  };
}
