/** Search, ordering and row identity for the shared dashboard tables. No DOM or API calls. */
import { stableKey } from "./dashboard-state.js";

const collator = new Intl.Collator(undefined, { numeric: true, sensitivity: "base" });
const entities = { amp: "&", lt: "<", gt: ">", quot: '"', apos: "'", nbsp: " " };
const identityFields = [
  "resource_id", "resourceId", "id", "name", "resource", "label", "date", "day", "month",
  "resource_group", "group", "grp", "subscription_id", "subscriptionId", "subscription",
  "region", "location", "type", "category", "key", "currency",
];

export function cellText(markup) {
  return String(markup).replace(/<[^>]*>/g, "").replace(
    /&(#(?:x[0-9a-f]+|[0-9]+)|amp|lt|gt|quot|apos|nbsp);/gi,
    (match, entity) => {
      if (!entity.startsWith("#")) return entities[entity.toLowerCase()];
      const hex = entity[1]?.toLowerCase() === "x";
      const code = Number.parseInt(entity.slice(hex ? 2 : 1), hex ? 16 : 10);
      return code > 0 && code <= 0x10ffff && !(code >= 0xd800 && code <= 0xdfff)
        ? String.fromCodePoint(code) : "\ufffd";
    },
  );
}

function compareValues(left, right, direction) {
  const missing = value => value == null || (typeof value === "number" && !Number.isFinite(value));
  if (missing(left) || missing(right)) return Number(missing(left)) - Number(missing(right));
  const order = typeof left === "number" && typeof right === "number"
    ? left - right : collator.compare(String(left), String(right));
  return order * direction;
}

export function tableRows(state) {
  const cacheKey = JSON.stringify([state.query, state.sort, state.dir]);
  if (state.viewCache?.key === cacheKey) return state.viewCache.rows;
  let rows = state.rows;
  if (state.query) {
    const query = state.query.toLowerCase();
    state.searchRows ??= rows.map(row => state.columns
      .map(column => cellText(column.cell(row))).join("\u0000").toLowerCase());
    rows = rows.filter((_, index) => state.searchRows[index].includes(query));
  }
  if (state.sort != null) {
    const column = state.columns[state.sort];
    if (!column) throw new RangeError(`Unknown table sort column: ${state.sort}`);
    const value = column.value || (row => cellText(column.cell(row)));
    rows = rows.map(row => ({ row, value: value(row) }))
      .sort((left, right) => compareValues(left.value, right.value, state.dir))
      .map(item => item.row);
  }
  state.viewCache = { key: cacheKey, rows };
  return rows;
}

export function tableRowKeys(rows) {
  const keys = new Map(), counts = new Map(), duplicates = new Map();
  for (const row of rows) {
    const identity = identityFields.filter(field => row[field] != null).map(field => [field, row[field]]);
    const key = stableKey(JSON.stringify(identity.length ? identity : row));
    keys.set(row, key);
    counts.set(key, (counts.get(key) || 0) + 1);
  }
  for (const row of rows) {
    const key = keys.get(row);
    if (counts.get(key) < 2) continue;
    // Rows without a unique semantic identity must not share an expanded-detail panel.
    const full = `${key}-${stableKey(JSON.stringify(row))}`;
    const ordinal = duplicates.get(full) || 0;
    duplicates.set(full, ordinal + 1);
    keys.set(row, `${full}-${ordinal}`);
  }
  return keys;
}

export function outlierThreshold(rows, columns) {
  const column = columns.find(item => item.num && typeof item.value === "function");
  if (!column || rows.length < 5) return null;
  const values = rows.map(row => column.value(row)).filter(value => Number.isFinite(value) && value > 0);
  if (values.length < 5) return null;
  const sorted = [...values].sort((left, right) => left - right);
  const median = sorted[Math.floor(sorted.length / 2)];
  if (!median) return null;
  const cut = median * 3;
  const flagged = values.filter(value => value >= cut).length;
  return flagged && flagged <= Math.max(1, values.length * 0.2) ? { cut, col: column } : null;
}
