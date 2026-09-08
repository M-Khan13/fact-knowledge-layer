import { Navigate, Route, Routes } from "react-router-dom";
import Landing from "./components/Landing.jsx";
import Workspace from "./components/Workspace.jsx";
import About from "./components/About.jsx";

export default function App() {
  return (
    <Routes>
      <Route path="/" element={<Landing />} />
      <Route path="/about" element={<About />} />
      <Route path="/c/:collectionId" element={<Workspace />} />
      <Route path="*" element={<Navigate to="/" replace />} />
    </Routes>
  );
}
