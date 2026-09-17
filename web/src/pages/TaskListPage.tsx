import { useCallback, useEffect, useRef, useState } from "react";
import { Link, useNavigate } from "react-router-dom";

import {
  ApiError, cachedCatalog, cachedModels, createTask, listModels, type ModelCatalog, type ModelEntry,
} from "../api";
import AppShell, { TaskLinks } from "../components/AppShell";
import Composer from "../components/Composer";
import Notice from "../components/Notice";
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
  const { error, reload } = useTasks();
  const [models, setModels] = useState<ModelEntry[]>(() => cachedModels() ?? []);
  const [model, setModel] = useState(() => initialModel(cachedCatalog()));
  const [catalogStale, setCatalogStale] = useState(false);
  const [catalogLoading, setCatalogLoading] = useState(false);
  const [catalogError, setCatalogError] = useState<ApiError | null>(null);
  const [starting, setStarting] = useState(false);
  const [startError, setStartError] = useState<ApiError | null>(null);
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

  const start = async (message: string, files: File[]) => {
    if (starting) return new ApiError("busy", "任务正在创建", 409);
    setStarting(true);
    setStartError(null);
    try {
      const created = await createTask(message, model, files);
      void reload();
      navigate(`/tasks/${created.task.task_id}`);
      return null;
    } catch (failure) {
      const error = failure instanceof ApiError ? failure : new ApiError("offline", String(failure), 0);
      setStartError(error);
      return error;
    } finally {
      setStarting(false);
    }
  };

  return (
    <AppShell serviceError={error}>
      <div className="hero">
        <div className="hero-inner">
          <h1 className="hero-title">今天要做什么？</h1>

          <Composer placeholder="随心输入" sending={starting} model={model} models={models}
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

          {startError !== null && (
            <Notice
              tone={startError.unavailable ? "muted" : "danger"}
              title={startError.unavailable ? "Agent 暂未开放" : "发起失败"}
            >
              {startError.message}
              {startError.unavailable && "。任务已创建，开放后可继续。"}
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
