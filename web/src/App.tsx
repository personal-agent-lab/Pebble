import { lazy, Suspense } from "react";
import { Navigate, Route, Routes } from "react-router-dom";

import KbPage from "./pages/KbPage";
import SearchPage from "./pages/SearchPage";
import TaskListPage from "./pages/TaskListPage";
import TaskPage from "./pages/TaskPage";
import { SeenProvider } from "./seen";
import { TaskListProvider } from "./tasks";

// 资料与记忆共用的编辑器体积较大，只在打开对应页面时再加载，任务页不为它付下载成本。
const KbDocumentPage = lazy(() => import("./pages/KbDocumentPage"));
const MemoryPage = lazy(() => import("./pages/MemoryPage"));
const loading = <div className="loading">读取中…</div>;

/** PC 与手机共用同一套页面组件与路由，按屏幕尺寸调整布局。 */
export default function App() {
  return (
    <TaskListProvider>
      <SeenProvider>
        <Routes>
          <Route path="/" element={<Navigate to="/tasks" replace />} />
          <Route path="/tasks" element={<TaskListPage />} />
          <Route path="/tasks/:taskId" element={<TaskPage />} />
          <Route path="/search" element={<SearchPage />} />
          <Route path="/kb" element={<KbPage />} />
          <Route path="/memory" element={<Suspense fallback={loading}><MemoryPage /></Suspense>} />
          <Route path="/kb/doc" element={<Suspense fallback={loading}><KbDocumentPage /></Suspense>} />
          <Route path="/kb/new" element={<Suspense fallback={loading}><KbDocumentPage creating /></Suspense>} />
          <Route path="*" element={<Navigate to="/tasks" replace />} />
        </Routes>
      </SeenProvider>
    </TaskListProvider>
  );
}
