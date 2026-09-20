import { useCallback, useEffect, useRef, useState } from "react";
import SkillPicker from "../features/skills/SkillPicker";
import { emptySelection } from "../features/skills/api";
import { Link, useNavigate } from "react-router-dom";

import {
  ApiError, cachedCatalog, cachedModels, listModels, type ModelCatalog, type ModelEntry,
} from "../api";
import AppShell, { TaskLinks } from "../components/AppShell";
import Composer from "../components/Composer";
import Notice from "../components/Notice";
import { startTask, subscribe, takeDraft, type UnsentDraft } from "../pendingTasks";
import { useTasks } from "../tasks";

const STALE_RETRY_MS = 5000;

/** 默认型号在目录里就选它，否则留空，由页面提示默认模型不可用。 */
const initialModel = (catalog: ModelCatalog | null) =>
  catalog?.models.some((entry) => entry.id === catalog.default_model) ? catalog.default_model : "";

/**
 * 新任务入口。任务索引在侧栏，这里只有一件事：写下目标。
 * 自动触发的任务与手动发起的任务都会出现在任务列表，无需手动刷新。
 */
export default function TaskListPage() {
  const navigate = useNavigate();
  const [selection, setSelection] = useState(emptySelection);
  const { error, reload } = useTasks();
  const [models, setModels] = useState<ModelEntry[]>(() => cachedModels() ?? []);
  const [model, setModel] = useState(() => initialModel(cachedCatalog()));
  const [catalogStale, setCatalogStale] = useState(false);
  const [catalogLoading, setCatalogLoading] = useState(false);
  const [catalogError, setCatalogError] = useState<ApiError | null>(null);
  // 没发出的消息退回来的文字与附件。放在 effect 里取：离开任务页时的退回发生在
  // 旧页面卸载时，晚于新页面首次渲染；输入框以 key 重建来接住它。
  // 停留期间才退回的（后台创建失败）不直接覆盖输入框，免得冲掉正在写的内容，由用户决定放回。
  const [draft, setDraft] = useState<UnsentDraft | null>(null);
  const [late, setLate] = useState<UnsentDraft | null>(null);
  const [draftKey, setDraftKey] = useState(0);
  useEffect(() => {
    const returned = takeDraft();
    if (returned !== null) { setDraft(returned); setDraftKey((key) => key + 1); }
    return subscribe(() => {
      const later = takeDraft();
      if (later !== null) setLate(later);
    });
  }, []);
  const applyLate = () => {
    setDraft(late);
    setLate(null);
    setDraftKey((key) => key + 1);
  };
  const retryTimer = useRef<number | null>(null);

  // 先用缓存的目录立即可选，再请求最新目录；读不到时沿用旧目录并提示，只有从未读到过才报错。
  const loadModels = useCallback(async (autoRetry: boolean) => {
    setCatalogLoading(true);
    try {
      const catalog = await listModels();
      setModels(catalog.models);
      setModel((current) => catalog.models.some((entry) => entry.id === current)
        ? current : initialModel(catalog));
      setCatalogStale(catalog.stale);
      setCatalogError(catalog.models.some((entry) => entry.id === catalog.default_model)
        ? null
        : new ApiError("invalid_model", `默认模型当前不可用：${catalog.default_model}，请另选模型`, 422));
      // 服务端在后台刷新目录：稍后再取一次，刷新成功时提示自动消失。
      if (catalog.stale && autoRetry) {
        retryTimer.current = window.setTimeout(() => void loadModels(false), STALE_RETRY_MS);
      }
    } catch (failure) {
      if (cachedModels() !== null) setCatalogStale(true);
      else setCatalogError(failure instanceof ApiError ? failure : new ApiError("offline", String(failure), 0));
    } finally {
      setCatalogLoading(false);
    }
  }, []);

  useEffect(() => {
    void loadModels(true);
    return () => { if (retryTimer.current !== null) window.clearTimeout(retryTimer.current); };
  }, [loadModels]);

  // 不等服务端：先进入任务页显示这条消息，创建结果与失败处理都在任务页。
  const start = async (message: string, files: File[]) => {
    navigate(`/tasks/${startTask(message, model, files, selection)}`);
    return null;
  };

  return (
    <AppShell serviceError={error}>
      <div className="hero">
        <div className="hero-inner">
          <h1 className="hero-title">今天要做什么？</h1>

          <SkillPicker value={selection} onChange={setSelection} />
          <Composer key={draftKey} placeholder="随心输入" sending={false}
            model={model} models={models}
            initialMessage={draft?.message} initialFiles={draft?.files}
            onModelChange={setModel} onSubmit={start}
            catalogNotice={catalogStale ? {
              message: "模型目录可能不是最新",
              retrying: catalogLoading,
              onRetry: () => void loadModels(false),
            } : null} />

          {error !== null && (
            <Notice
              tone="danger"
              title="无法连接服务"
              actions={
                <button type="button" className="btn-secondary" onClick={() => void reload()}>
                  重试连接
                </button>
              }
            >
              {error.message}
              <br />
              确认 Pebble 服务已启动后重试。已保存的草稿与待确认内容不会丢失。
            </Notice>
          )}

          {catalogError !== null && (
            <Notice
              tone="danger"
              title="模型目录不可用"
              actions={
                <button type="button" className="btn-secondary" disabled={catalogLoading}
                  onClick={() => void loadModels(false)}>
                  {catalogLoading ? "读取中…" : "重新读取"}
                </button>
              }
            >
              {catalogError.message}
            </Notice>
          )}

          {late !== null && (
            <Notice tone="danger" title="上一条消息没有发出"
              actions={<button type="button" className="btn-secondary" onClick={applyLate}>放回输入框</button>}>
              {late.reason ?? "发送失败"}
            </Notice>
          )}
          {late === null && draft !== null && draft.reason !== null && (
            <Notice tone="danger" title="上一条消息没有发出">
              {draft.reason}。内容已放回输入框，可以修改后重新发送。
            </Notice>
          )}
          {draft !== null && draft.lostFiles > 0 && (
            <Notice tone="muted" title="附件需要重新添加">
              页面刷新前这条消息没有发出，文字已放回输入框，{draft.lostFiles} 个附件未能保留。
            </Notice>
          )}

          {/* 手机没有侧栏：任务列表改挂在这里，两端都能从任务页进入已有任务。 */}
          <div className="narrow-only task-nav">
            <div className="task-nav-head">
              <span>任务列表</span>
              <Link to="/search" className="task-nav-search">搜索对话</Link>
            </div>
            <TaskLinks />
          </div>
        </div>
      </div>
    </AppShell>
  );
}
