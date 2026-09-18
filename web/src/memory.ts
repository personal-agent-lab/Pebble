/**
 * 后台记忆回顾提示的识别：时间线里的整理提示带“查看记忆”入口。
 */
const MEMORY_NOTICE_PREFIXES = ["已整理记忆"];

export const isMemoryNotice = (text: string) =>
  MEMORY_NOTICE_PREFIXES.some((prefix) => text.startsWith(prefix));
