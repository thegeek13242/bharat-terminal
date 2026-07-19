import { Routes, Route, Navigate } from 'react-router-dom';
import { ProtectedRoute } from './auth/ProtectedRoute';
import { Login } from './pages/Login';
import { Terminal } from './pages/Terminal';
import { Watchlist } from './pages/Watchlist';
import { Starred } from './pages/Starred';
import { Surprises } from './pages/Surprises';
import { Transcripts } from './pages/Transcripts';
import { Research } from './pages/Research';
import { Investors } from './pages/Investors';
import { Universe } from './pages/Universe';

export default function App() {
  return (
    <Routes>
      <Route path="/login" element={<Login />} />
      <Route element={<ProtectedRoute />}>
        <Route path="/terminal" element={<Terminal />} />
        <Route path="/watchlist" element={<Watchlist />} />
        <Route path="/starred" element={<Starred />} />
        <Route path="/surprises" element={<Surprises />} />
        <Route path="/transcripts" element={<Transcripts />} />
        <Route path="/research" element={<Research />} />
        <Route path="/investors" element={<Investors />} />
        <Route path="/universe" element={<Universe />} />
        <Route path="/" element={<Navigate to="/terminal" replace />} />
      </Route>
      <Route path="*" element={<Navigate to="/terminal" replace />} />
    </Routes>
  );
}
