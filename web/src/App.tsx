import { Navigate, Route, Routes } from "react-router-dom";

import ConfirmPage from "./pages/ConfirmPage";
import TaskListPage from "./pages/TaskListPage";
import TaskPage from "./pages/TaskPage";

/** PC 与手机共用同一套页面组件与路由，按屏幕尺寸调整布局。 */
export default function App() {
  return (
    <Routes>
      <Route path="/" element={<Navigate to="/tasks" replace />} />
      <Route path="/tasks" element={<TaskListPage />} />
      <Route path="/tasks/:taskId" element={<TaskPage />} />
      <Route path="/tasks/:taskId/confirm" element={<ConfirmPage />} />
      <Route path="*" element={<Navigate to="/tasks" replace />} />
    </Routes>
  );
}
