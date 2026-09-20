type MdNode = { type: string; value?: string; children?: MdNode[] };

/**
 * 收紧 remark-math 的行内公式：按 Pandoc 的规则，`$` 后紧跟空白、收尾 `$` 前是空白、
 * 或收尾 `$` 后紧跟数字的都不算公式，还原成字面文字。
 *
 * remark-math 见到成对的 `$` 就当公式，“$5 到 $10”会把中间一段排成公式；
 * 资料与消息里金额很常见，不能这样误伤。块级 `$$…$$` 不受影响。
 * remark-math 会去掉两侧各一个对称的空格（`$ x $` 读成 `x`），这种写法仍算公式。
 */
export function remarkStrictMath() {
  return (tree: MdNode) => { revert(tree); };
}

function revert(node: MdNode) {
  const children = node.children;
  if (!children) return;
  children.forEach((child, index) => {
    if (child.type !== "inlineMath") {
      revert(child);
      return;
    }
    const value = child.value ?? "";
    const next = children[index + 1];
    const loose = value === "" || /^\s|\s$/.test(value);
    const numeric = next?.type === "text" && /^\d/.test(next.value ?? "");
    if (loose || numeric) children[index] = { type: "text", value: `$${value}$` };
  });
}
