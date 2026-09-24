import type { TimelineItem } from "../api";

type ToolItem = Extract<TimelineItem, { kind: "tool" }>;

type Display = { label: string; target?: string; known: boolean };

function value(args: Record<string, unknown>, key: string): string | undefined {
  const found = args[key];
  return typeof found === "string" && found.trim() ? found.trim() : undefined;
}

function dateTime(raw: string | undefined): string | undefined {
  if (!raw) return undefined;
  const match = /^(\d{4})-(\d{2})-(\d{2})(?:T(\d{2}):(\d{2}))?/.exec(raw);
  if (!match) return undefined;
  const [, year, month, day, hour, minute] = match;
  return `${year}/${Number(month)}/${Number(day)}${hour ? ` ${hour}:${minute}` : ""}`;
}

function period(args: Record<string, unknown>, first: string, last: string): string | undefined {
  const start = dateTime(value(args, first));
  const end = dateTime(value(args, last));
  return start && end ? `${start} 至 ${end}` : undefined;
}

function document(args: Record<string, unknown>): string | undefined {
  const ref = args.ref;
  const path = value(args, "path") ??
    (ref && typeof ref === "object" && !Array.isArray(ref)
      ? value(ref as Record<string, unknown>, "path") : undefined);
  return path?.replace(/^kb\//, "").replace(/\.md$/i, "");
}

function webHost(raw: string | undefined): string | undefined {
  if (!raw) return undefined;
  try { return new URL(raw).host || undefined; } catch { return undefined; }
}

function basename(raw: string | undefined): string | undefined {
  return raw?.split(/[\\/]/).filter(Boolean).at(-1);
}

function skillName(item: ToolItem): string | undefined {
  if (item.status !== "ok" || !item.result || item.result.length > 100_000) return undefined;
  try {
    const data: unknown = JSON.parse(item.result);
    if (data && typeof data === "object" && !Array.isArray(data)) {
      return value(data as Record<string, unknown>, "name");
    }
  } catch { /* 历史记录或 SDK 返回可不是 JSON。 */ }
  return undefined;
}

function action(item: ToolItem, verb: string, target?: string): Display {
  const label = item.status === "running" ? `正在${verb}`
    : item.status === "error" ? `${verb}失败` : `已${verb}`;
  return { label, target, known: true };
}

/** 收起行只描述事实；完整的工具名、参数与返回留在展开面板。 */
export function toolCallDisplay(item: ToolItem): Display {
  const args = item.arguments;
  const query = value(args, "query");
  switch (item.name) {
    case "skill_list": return action(item, "查看技能目录");
    case "skill_view": return action(item,
      value(args, "file_path") ? "读取技能附件" : "读取技能",
      value(args, "file_path") ? basename(value(args, "file_path")) : skillName(item) ?? value(args, "skill_id"));
    case "skill_manage": return action(item, "处理技能变更", value(args, "skill_id") ??
      (args.payload && typeof args.payload === "object" && !Array.isArray(args.payload)
        ? value(args.payload as Record<string, unknown>, "skill_id") : undefined));
    case "calendar_list_events": return action(item, "查看日历", period(args, "time_min", "time_max"));
    case "calendar_get_event": return action(item, "读取日程");
    case "calendar_check_conflicts": return action(item, "检查日程冲突", period(args, "start", "end"));
    // 成功返回也可能表示有冲突、未创建；不能把工具成功写成日程已创建。
    case "calendar_create_event": return action(item, "尝试创建日程", value(args, "summary"));
    case "history_search": return action(item, "搜索历史消息", query);
    case "history_read": return action(item, "读取历史消息");
    case "memory_edit": return action(item, "整理长期记忆");
    case "gmail_search": return action(item, "搜索邮件", query);
    case "gmail_get_thread": return action(item, "读取邮件往来");
    case "gmail_get_message": return action(item, "读取邮件");
    case "gmail_get_attachment": return action(item, "读取邮件附件");
    case "gmail_prepare_reply": return action(item, "准备回复草稿", value(args, "subject"));
    case "gmail_prepare_email": return action(item, "准备邮件草稿", value(args, "subject"));
    case "gmail_read_draft": return action(item, "读取邮件草稿");
    case "gmail_update_draft": return action(item, "修改邮件草稿", value(args, "subject"));
    case "kb_save": return action(item, "保存资料", value(args, "title"));
    case "kb_search": return action(item, "搜索资料", query);
    case "kb_list": return action(item, args.deleted ? "查看已删除资料" : "查看资料列表", value(args, "directory"));
    case "kb_read": return action(item, "读取资料", document(args));
    case "kb_update": return action(item, "修改资料", document(args) ?? value(args, "title"));
    case "kb_history": return action(item, "查看资料历史版本", document(args));
    case "kb_archive": return action(item, "归档任务", value(args, "title"));
    case "kb_delete": return action(item, "删除资料", document(args));
    case "kb_move": return action(item, "移动资料", document(args) ?? value(args, "new_path"));
    case "kb_restore": return action(item, "恢复资料", document(args));
    case "WebSearch": return action(item, "联网搜索", query);
    case "WebFetch": return action(item, "读取网页", webHost(value(args, "url")));
    case "Read": return action(item, "读取附件", basename(value(args, "file_path") ?? value(args, "path")));
    default: return { label: item.name, known: false };
  }
}
