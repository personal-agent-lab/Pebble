import SkillsPage from "./features/skills/SkillsPage";
import { Navigate, Route, Routes } from "react-router-dom";

import TaskListPage from "./pages/TaskListPage";
import TaskPage from "./pages/TaskPage";
import { SeenProvider } from "./seen";
import { TaskListProvider } from "./tasks";

/** PC 与手机共用同一套页面组件与路由，按屏幕尺寸调整布局。 */
export default function App() {
  return (
    <TaskListProvider>
      <SeenProvider>
        <Routes>
          <Route path="/" element={<Navigate to="/tasks" replace />} />
          <Route path="/skills" element={<SkillsPage />} />
          <Route path="/tasks" element={<TaskListPage />} />
          <Route path="/tasks/:taskId" element={<TaskPage />} />
          <Route path="*" element={<Navigate to="/tasks" replace />} />
        </Routes>
      </SeenProvider>
    </TaskListProvider>
  );
}
