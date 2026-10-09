import { Navigate, Outlet, Route, Routes } from 'react-router-dom';
import type { Role } from './api/types';
import { dashboardPath, useAuth } from './auth/AuthProvider';
import { Layout } from './components/Layout';
import { Landing } from './pages/Landing';
import { AuthPage } from './pages/AuthPage';
import { Account, Dashboard } from './pages/Dashboard';
import { Link } from 'react-router-dom';

function Protected({ role }: { role?: Role }) {
  const { user } = useAuth();
  if (!user) return <Navigate to="/login" replace />;
  if (role && user.role !== role) return <Navigate to={dashboardPath(user)} replace />;
  return <Outlet />;
}

export function App() {
  return <Routes><Route element={<Layout />}>
    <Route index element={<Landing />} />
    <Route path="login" element={<AuthPage key="login" mode="login" />} />
    <Route path="register" element={<AuthPage key="register" mode="register" />} />
    <Route element={<Protected role="passenger" />}><Route path="passenger" element={<Dashboard key="passenger" />} /></Route>
    <Route element={<Protected role="driver" />}><Route path="driver" element={<Dashboard key="driver" />} /></Route>
    <Route element={<Protected />}><Route path="account" element={<Account />} /></Route>
    <Route path="*" element={<section className="container not-found"><span className="eyebrow">A SMALL DETOUR</span><h1>This road ends here.</h1><p>We couldn’t find that page. Let’s get you back on track.</p><Link className="button" to="/">Back to home</Link></section>} />
  </Route></Routes>;
}
