import { Routes, Route, Navigate } from 'react-router-dom';
import { ProtectedRoute } from './auth/ProtectedRoute';
import { Login } from './pages/Login';
import { Terminal } from './pages/Terminal';
import { Watchlist } from './pages/Watchlist';
import { Starred } from './pages/Starred';
import { Universe } from './pages/Universe';

export default function App() {
  return (
    <Routes>
      <Route path="/login" element={<Login />} />
      <Route element={<ProtectedRoute />}>
        <Route path="/terminal" element={<Terminal />} />
        <Route path="/watchlist" element={<Watchlist />} />
        <Route path="/starred" element={<Starred />} />
        <Route path="/universe" element={<Universe />} />
        <Route path="/" element={<Navigate to="/terminal" replace />} />
      </Route>
      <Route path="*" element={<Navigate to="/terminal" replace />} />
    </Routes>
  );
}
