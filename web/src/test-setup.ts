// Node >= 24 在 globalThis 上预置实验性 localStorage（无 --localstorage-file 时为
// undefined），vitest 的 populateGlobal 因此跳过 jsdom 的 localStorage，jsdom 环境
// 里 window.localStorage 变成 undefined。这里用内存实现补上，只影响测试。

if (typeof window !== "undefined" && window.localStorage === undefined) {
  const backing = new Map<string, string>();
  const storage: Storage = {
    get length() {
      return backing.size;
    },
    key(index: number) {
      return Array.from(backing.keys())[index] ?? null;
    },
    getItem(key: string) {
      return backing.has(key) ? (backing.get(key) as string) : null;
    },
    setItem(key: string, value: string) {
      backing.set(String(key), String(value));
    },
    removeItem(key: string) {
      backing.delete(key);
    },
    clear() {
      backing.clear();
    },
  };
  Object.defineProperty(window, "localStorage", { value: storage, configurable: true });
}
