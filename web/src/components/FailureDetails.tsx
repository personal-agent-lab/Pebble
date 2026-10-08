import type { Failure } from "../api";

const SOURCES: Record<string, string> = {
  model: "模型服务", sdk: "模型运行时", gateway: "运行调度", api: "接口",
  tool: "工具", storage: "存储", execution: "外部执行", background: "后台处理",
};
const RECOVERY: Record<string, string> = {
  check_configuration: "检查服务配置", correct_input: "修改输入",
  read_current: "重新读取当前内容", wait: "稍后再尝试",
  verify_result: "核实执行结果", new_session: "尝试切换模型或新建对话",
};

/** 简短原因常驻；技术字段按需展开，不提供未经程序授权的恢复按钮。 */
export default function FailureDetails({ failure }: { failure: Failure }) {
  return <details className="failure-details">
    <summary>查看错误详情</summary>
    <dl>
      <dt>错误类型</dt><dd>{failure.code}</dd>
      <dt>来源</dt><dd>{SOURCES[failure.source] ?? failure.source}</dd>
      <dt>阶段</dt><dd>{failure.stage}</dd>
      {RECOVERY[failure.recovery] && <div><dt>处理建议</dt><dd>{RECOVERY[failure.recovery]}</dd></div>}
      <dt>诊断编号</dt><dd>{failure.diagnostic_id}</dd>
      {Object.entries(failure.details).filter(([, value]) =>
        typeof value === "string" || typeof value === "number" || typeof value === "boolean",
      ).map(([key, value]) => <div key={key}><dt>{key}</dt><dd>{String(value)}</dd></div>)}
    </dl>
  </details>;
}
