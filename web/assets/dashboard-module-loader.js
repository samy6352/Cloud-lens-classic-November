/** Share module-only preloads with navigation; no feature instances or API reads are started. */
export function createModuleLoader(loaders, report = error => console.warn("Tab preload failed:", error)) {
  const pending = new Map();
  const load = family => {
    if (!Object.hasOwn(loaders, family)) return Promise.reject(new Error(`Unknown tab family: ${family}`));
    if (!pending.has(family)) {
      const promise = Promise.resolve().then(() => loaders[family]()).catch(error => {
        pending.delete(family);
        throw error;
      });
      pending.set(family, promise);
    }
    return pending.get(family);
  };
  return { load, warm: family => load(family).catch(report) };
}
