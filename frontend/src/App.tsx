import { Routes, Route, Navigate } from 'react-router-dom';
import { ProtectedRoute } from './auth/ProtectedRoute';
import { useAuth } from './auth/AuthContext';
import { useBeacon } from './lib/beacon';
import { Login } from './pages/Login';
import { Terminal } from './pages/Terminal';
import { Watchlist } from './pages/Watchlist';
import { Starred } from './pages/Starred';
import { Surprises } from './pages/Surprises';
import { Transcripts } from './pages/Transcripts';
import { Research } from './pages/Research';
import { PaperTrading } from './pages/PaperTrading';
import { Investors } from './pages/Investors';
import { Universe } from './pages/Universe';
import { AdminPage } from './pages/admin/AdminPage';

/** Page-view + heartbeat beacon; active only for signed-in users. */
function Beacon() {
  const { user } = useAuth();
  useBeacon(!!user);
  return null;
}

export default function App() {
  return (
    <>
      <Beacon />
      <Routes>
        <Route path="/login" element={<Login />} />
        <Route element={<ProtectedRoute />}>
          <Route path="/terminal" element={<Terminal />} />
          <Route path="/watchlist" element={<Watchlist />} />
          <Route path="/starred" element={<Starred />} />
          <Route path="/surprises" element={<Surprises />} />
          <Route path="/transcripts" element={<Transcripts />} />
          <Route path="/research" element={<Research />} />
          <Route path="/papertrading" element={<PaperTrading />} />
          <Route path="/investors" element={<Investors />} />
          <Route path="/universe" element={<Universe />} />
          <Route path="/admin" element={<AdminPage />} />
          <Route path="/" element={<Navigate to="/terminal" replace />} />
        </Route>
        <Route path="*" element={<Navigate to="/terminal" replace />} />
      </Routes>
    </>
  );
}
