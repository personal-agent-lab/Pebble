/**
 * 长期记忆提示的识别：时间线里由程序生成的记忆变更提示带“查看记忆”入口。
 *
 * 前缀与服务端 `server/memory/notices.py` 的文案一一对应；追问（“想确认：”）不算变更，不带入口。
 */
const MEMORY_NOTICE_PREFIXES = [
  "已更新记忆",
  "已整理记忆",
  // 以下为旧版逐条提示，历史时间线里仍保留。
  "已记住：",
  "已修改：",
  "已删除这条记忆：",
  "已移到",
  "这条内容已经在记忆里",
  "记忆保存失败：",
  "整理记忆：",
];

export const isMemoryNotice = (text: string) =>
  MEMORY_NOTICE_PREFIXES.some((prefix) => text.startsWith(prefix));
