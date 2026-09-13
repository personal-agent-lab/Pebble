import type { Badge } from "../status";

const TONE_CLASS: Record<Badge["tone"], string> = {
  neutral: "",
  wait: " wait",
  run: " run",
  ok: " ok",
  err: " err",
  unk: " unk",
};

/** 状态徽标：等宽字体承载状态，颜色只区分类别，不叠加多个强调色。 */
export default function StatusBadge({ badge }: { badge: Badge }) {
  return (
    <span className={`badge${TONE_CLASS[badge.tone]}`}>
      <span className="dot" />
      {badge.label}
    </span>
  );
}
