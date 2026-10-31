import { Navigate, Outlet, Route, Routes, useLocation } from 'react-router-dom';
import type { AccountMode } from './api/types';
import { dashboardPath, useAuth } from './auth/AuthProvider';
import { Layout } from './components/Layout';
import { Landing } from './pages/Landing';
import { AuthPage } from './pages/AuthPage';
import { Account, Dashboard } from './pages/Dashboard';
import { PassengerArea } from './pages/passenger/PassengerArea';
import { DriverArea } from './pages/driver/DriverArea';
import { SessionRestoring } from './components/SessionRestoring';
import { Link } from 'react-router-dom';

function Protected({ role }: { role?: AccountMode }) {
  const { user, mode, restoring, loggedOut } = useAuth();
  const location = useLocation();
  if (restoring) return <SessionRestoring />;
  // Keep where the visitor was going (path + query) so sign-in can return there,
  // unless they just logged out on purpose.
  if (!user) return <Navigate to="/login" replace state={loggedOut ? undefined : { from: `${location.pathname}${location.search}` }} />;
  if (role && (user.role === 'admin' || !user.allowed_modes.includes(role))) return <Navigate to={dashboardPath(user, mode)} replace />;
  return <Outlet />;
}

export function App() {
  return <Routes><Route element={<Layout />}>
    <Route index element={<Landing />} />
    <Route path="login" element={<AuthPage key="login" mode="login" />} />
    <Route path="register" element={<AuthPage key="register" mode="register" />} />
    <Route element={<Protected role="passenger" />}><Route path="passenger" element={<PassengerArea />} /></Route>
    <Route element={<Protected role="driver" />}>
      <Route path="driver" element={<Dashboard key="driver" mode="driver" />} />
      <Route path="driver/rides" element={<DriverArea view="rides" />} />
      <Route path="driver/rides/new" element={<DriverArea view="new" />} />
      <Route path="driver/rides/:rideId" element={<DriverArea view="ride" />} />
    </Route>
    <Route element={<Protected />}><Route path="account" element={<Account />} /></Route>
    <Route path="*" element={<section className="container not-found"><span className="eyebrow">A SMALL DETOUR</span><h1>This road ends here.</h1><p>We couldn’t find that page. Let’s get you back on track.</p><Link className="button" to="/">Back to home</Link></section>} />
  </Route></Routes>;
}
