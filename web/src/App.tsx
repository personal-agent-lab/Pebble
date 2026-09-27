import { lazy, Suspense } from "react";
import { Navigate, Route, Routes } from "react-router-dom";

import AppShell from "./components/AppShell";
import KbPage from "./pages/KbPage";
import SearchPage from "./pages/SearchPage";
import TaskListPage from "./pages/TaskListPage";
import TaskPage from "./pages/TaskPage";
import { SeenProvider } from "./seen";
import { TaskListProvider } from "./tasks";

// 资料与记忆共用的编辑器体积较大，只在打开对应页面时再加载，任务页不为它付下载成本。
const KbDocumentPage = lazy(() => import("./pages/KbDocumentPage"));
const MemoryPage = lazy(() => import("./pages/MemoryPage"));
const SkillsPage = lazy(() => import("./pages/SkillsPage"));
const loading = <div className="loading">读取中…</div>;
// 懒加载页面尚未下载完时也保留侧栏与底部导航，避免整页短暂消失。
const loadingPage = <AppShell>{loading}</AppShell>;

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
          <Route path="/memory" element={<Suspense fallback={loadingPage}><MemoryPage /></Suspense>} />
          <Route path="/skills" element={<Suspense fallback={loadingPage}><SkillsPage /></Suspense>} />
          <Route path="/kb/doc" element={<Suspense fallback={loadingPage}><KbDocumentPage /></Suspense>} />
          <Route path="/kb/new" element={<Suspense fallback={loadingPage}><KbDocumentPage creating /></Suspense>} />
          <Route path="*" element={<Navigate to="/tasks" replace />} />
        </Routes>
      </SeenProvider>
    </TaskListProvider>
  );
}
