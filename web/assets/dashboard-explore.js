/** Pure contextual navigation and safe, bounded row-detail rendering. */
const PORTAL = "https://portal.azure.com/#resource";
const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
const RESOURCE_KEYS = ["resource_id", "resourceId", "ResourceId", "id"];
const SUBSCRIPTION_KEYS = ["subscription_id", "subscriptionId", "subscription"];
const GROUP_KEYS = ["resource_group", "group", "grp"];
const LIMITS = { depth: 8, entries: 250, nodes: 2000, text: 20000, characters: 200000 };

function escapeHtml(value) {
  return String(value).replace(/[&<>"']/g, c => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  })[c]);
}

function ownValue(object, key) {
  if (!object || typeof object !== "object") return undefined;
  return Object.getOwnPropertyDescriptor(object, key)?.value;
}

function encodeSegment(value) {
  return encodeURIComponent(value).replace(/[!'()*]/g, c =>
    `%${c.charCodeAt(0).toString(16).toUpperCase()}`);
}

function validSegment(value) {
  if (!value || value === "." || value === ".." || /[\u0000-\u001f\u007f\\]/.test(value)) return false;
  try { encodeSegment(value); return true; } catch (error) {
    if (!(error instanceof URIError)) throw error;
    return false;
  }
}

function parseArm(value) {
  if (typeof value !== "string" || !value.startsWith("/")) return null;
  const parts = value.slice(1).split("/");
  if (parts.some(p => !validSegment(p)) || parts[0].toLowerCase() !== "subscriptions"
    || !UUID.test(parts[1] || "")) return null;
  parts[0] = "subscriptions";
  const subscription = parts[1];
  let index = 2, group;
  if (parts[index]?.toLowerCase() === "resourcegroups") {
    if (!parts[index + 1]) return null;
    parts[index] = "resourceGroups";
    group = parts[index + 1];
    index += 2;
  }
  if (index === parts.length) {
    return { kind: group ? "group" : "subscription", subscription, group, parts };
  }
  // Providers introduce namespace/type/name triples; children retain type/name pairs.
  if (parts[index]?.toLowerCase() !== "providers") return null;
  while (index < parts.length) {
    if (parts[index].toLowerCase() === "providers") {
      if (!/^[a-z0-9][a-z0-9.-]*\.[a-z0-9.-]+$/i.test(parts[index + 1] || "")
        || !parts[index + 2] || !parts[index + 3]) return null;
      parts[index] = "providers";
      index += 2;
    }
    if (!/^[a-z][a-z0-9._-]*$/i.test(parts[index] || "") || !parts[index + 1]) return null;
    index += 2;
  }
  return { kind: "resource", subscription, group, parts };
}

function armUrl(parts) {
  return `${PORTAL}/${parts.map(encodeSegment).join("/")}/overview`;
}

function rowArm(row) {
  const paths = RESOURCE_KEYS.map(key => parseArm(ownValue(row, key))).filter(Boolean);
  return paths.find(path => path.kind === "resource")
    || paths.find(path => path.kind === "group") || paths[0];
}

function subscriptionId(value) {
  return typeof value === "string" && UUID.test(value) ? value : parseArm(value)?.subscription;
}

function rowSubscription(row, scopeIds) {
  const explicit = SUBSCRIPTION_KEYS.map(key => subscriptionId(ownValue(row, key))).filter(Boolean);
  const unique = [...new Set(explicit.map(id => id.toLowerCase()))];
  if (unique.length > 1) return null;
  if (unique.length === 1) return explicit[0];
  return Array.isArray(scopeIds) && scopeIds.length === 1
    && typeof scopeIds[0] === "string" && UUID.test(scopeIds[0]) ? scopeIds[0] : null;
}

function rowGroup(row) {
  for (const key of GROUP_KEYS) {
    const value = ownValue(row, key);
    if (typeof value !== "string" || !value.trim()) continue;
    const path = parseArm(value);
    if (path?.kind === "group") return path;
    if (value.startsWith("/") || value.includes("/") || /^[a-z][a-z0-9+.-]*:/i.test(value)
      || !validSegment(value)) continue;
    return { group: value };
  }
  return null;
}

/** Most-specific target first, followed by known ancestors; never invent a resource ID. */
export function portalTargets(row, scopeIds = []) {
  const path = rowArm(row);
  const group = path?.group ? path : path?.kind !== "resource" ? rowGroup(row) : null;
  const subscription = path?.subscription || group?.subscription || rowSubscription(row, scopeIds);
  const targets = [];
  if (path?.kind === "resource") targets.push({ label: "Resource", url: armUrl(path.parts) });
  if (subscription && group?.group) {
    targets.push({ label: "Resource group", url: armUrl([
      "subscriptions", subscription, "resourceGroups", group.group,
    ]) });
  }
  if (subscription) {
    targets.push({ label: "Subscription", url: armUrl(["subscriptions", subscription]) });
  }
  return targets;
}

/** A resource name must not secretly link to a broader aggregate or selected subscription. */
export function portalLinkMarkup(row, scopeIds = [], label) {
  const text = label ?? ownValue(row, "name") ?? ownValue(row, "resource_name") ?? "Resource";
  const path = rowArm(row);
  if (path?.kind !== "resource") return escapeHtml(text);
  return `<a href="${escapeHtml(armUrl(path.parts))}" target="_blank" rel="noopener noreferrer">${escapeHtml(text)}</a>`;
}

function words(key) {
  return String(key).replace(/([a-z0-9])([A-Z])/g, "$1 $2")
    .replace(/([A-Z])([A-Z][a-z])/g, "$1 $2").replace(/[_\s.-]+/g, " ").trim();
}

function sensitiveKey(key) {
  const normalized = words(key).toLowerCase().replace(/[^a-z0-9]/g, "");
  return /(?:password|passwd|credential|secret|connectionstring|authorization|authentication|cookie)/.test(normalized)
    || /(?:token|apikey|accesskey|privatekey|signingkey|encryptionkey|sessionkey|sessionid|accountkey|storagekey|sharedkey|sharedaccesssignature|clientassertion)(?:s|value|\d+)?$/.test(normalized)
    || /^(?:auth|tokens|pwd|session|sessions|sas|bearer|jwt|keys)$/.test(normalized);
}

function sensitiveRecord(value) {
  if (!value || typeof value !== "object" || Array.isArray(value)) return false;
  return ["key", "name"].some(key => {
    const label = ownValue(value, key);
    return typeof label === "string" && sensitiveKey(label)
      && ["value", "values", "content"].some(field => Object.hasOwn(value, field));
  });
}

function displayable(value) {
  return value !== undefined && !["function", "symbol"].includes(typeof value) && !sensitiveRecord(value);
}

function fields(value) {
  return Object.keys(value).filter(key => !key.startsWith("__")
    && key !== "constructor" && key !== "prototype" && !sensitiveKey(key)
    && displayable(ownValue(value, key)));
}

function fieldLabel(key) {
  const known = {
    grp: "Resource group", rg: "Resource group", resourceid: "Resource ID",
    subscriptionid: "Subscription ID", resourcegroup: "Resource group",
    pct: "Percent", sku: "SKU", id: "ID", cpu: "CPU", ram: "RAM",
    changepct: "Change", sharepct: "Share of total", previous: "Previous period",
  };
  const label = words(key);
  return known[label.toLowerCase().replaceAll(" ", "")]
    || (label.charAt(0).toUpperCase() + label.slice(1))
      .replace(/\s+pct$/i, " %")
      .replace(/\b(id|ids|cpu|ram|sku|vm|vms|url|uri|ip|os|gb|mb|usd)\b/gi, word => word.toUpperCase());
}

function numericText(value, key, currency) {
  if (!Number.isFinite(value)) return String(value);
  const label = words(key).toLowerCase();
  if (/(?:^|\s)(?:pct|percent)$/.test(label)) {
    return `${new Intl.NumberFormat("en-US", { maximumSignificantDigits: 15 }).format(value)}%`;
  }
  // `previous` and `delta` sit beside `cost` in spend rows and are the same money; printed
  // bare they read as a different, unlabelled quantity.
  const money = (/(?:cost|spend|saving|price|charge|amount|budget|balance)/.test(label)
      || /^(?:previous|delta|baseline|impact)$/.test(label))
    && !/(?:percent|pct|ratio|count|hours|days)/.test(label);
  if (currency && money) {
    try {
      return new Intl.NumberFormat("en-US", {
        style: "currency", currency, maximumSignificantDigits: 15,
      }).format(value);
    } catch (error) {
      if (!(error instanceof RangeError)) throw error;
    }
  }
  return new Intl.NumberFormat("en-US", { maximumSignificantDigits: 15 }).format(value);
}

/** Escaped definition lists with native expandable details; no DOM or network dependencies. */
export function rowDetailsMarkup(row, { currency, title, stateKey } = {}) {
  const state = { nodes: 0, characters: 0, ancestors: new Set() };
  const safeCurrency = typeof currency === "string" && /^[a-z]{3}$/i.test(currency)
    ? currency.toUpperCase() : null;
  const notice = text => `<p class="explore-limit">${escapeHtml(text)}</p>`;
  const limitedText = value => {
    const raw = String(value);
    const allowed = Math.max(0, Math.min(LIMITS.text, LIMITS.characters - state.characters));
    const shown = raw.slice(0, allowed);
    state.characters += shown.length;
    return escapeHtml(shown) + (shown.length < raw.length
      ? ` <span class="explore-limit">${raw.length - shown.length} more characters not shown (display limit).</span>` : "");
  };
  const exhausted = () => state.nodes >= LIMITS.nodes || state.characters >= LIMITS.characters;

  function definitionList(value, depth, path = "") {
    const keys = fields(value);
    if (!keys.length) return notice("No displayable fields.");
    let markup = '<dl class="explore-fields">', index = 0;
    for (; index < Math.min(keys.length, LIMITS.entries) && !exhausted(); index++) {
      const key = keys[index];
      markup += `<div><dt>${limitedText(fieldLabel(key))}</dt><dd>${render(ownValue(value, key), key, depth, `${path}/field:${JSON.stringify(key)}`)}</dd></div>`;
    }
    markup += "</dl>";
    if (index < keys.length) markup += notice(`${keys.length - index} more fields not shown (display limit).`);
    return markup;
  }

  function render(value, key, depth, path = "") {
    if (exhausted()) return notice("Additional value not shown (display limit).");
    state.nodes++;
    if (value === null) return '<span class="explore-value">Not provided (null)</span>';
    if (typeof value !== "object") {
      const text = typeof value === "number" ? numericText(value, key, safeCurrency)
        : value === true ? "True" : value === false ? "False" : value === "" ? "(empty string)" : value;
      return `<span class="explore-value">${limitedText(text)}</span>`;
    }
    if (state.ancestors.has(value)) return notice("Circular reference not expanded.");
    const array = Array.isArray(value);
    const count = array ? value.length : fields(value).length;
    const unit = array ? "items" : "fields";
    if (!count) return notice(array ? "No items." : "No displayable fields.");
    if (depth >= LIMITS.depth) return notice(`${count} ${unit} not shown (depth limit).`);
    state.ancestors.add(value);
    const identity = stateKey ? ` data-state-key="${escapeHtml(`${stateKey}${path}`)}"` : "";
    let markup = `<details class="explore-nested"${identity}><summary>${count} ${unit}</summary>`;
    if (array) {
      markup += "<ol>";
      let index = 0;
      for (; index < Math.min(value.length, LIMITS.entries) && !exhausted(); index++) {
        const item = ownValue(value, String(index));
        if (displayable(item)) markup += `<li value="${index + 1}">${render(item, key, depth + 1, `${path}/${index}`)}</li>`;
      }
      markup += "</ol>";
      if (index < value.length) markup += notice(`${value.length - index} more items not shown (display limit).`);
    } else {
      markup += definitionList(value, depth + 1, path);
    }
    state.ancestors.delete(value);
    return markup + "</details>";
  }

  let markup = title == null ? "" : `<h3>${limitedText(title)}</h3>`;
  if (!displayable(row)) return markup + notice("No displayable details.");
  if (row && typeof row === "object" && !Array.isArray(row)) {
    state.ancestors.add(row);
    markup += definitionList(row, 0);
  } else {
    markup += `<dl class="explore-fields"><div><dt>Value</dt><dd>${render(row, "value", 0)}</dd></div></dl>`;
  }
  return markup;
}
