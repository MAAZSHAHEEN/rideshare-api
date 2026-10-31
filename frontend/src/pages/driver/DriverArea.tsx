import { useEffect } from 'react';
import { useParams } from 'react-router-dom';
import { useAuth } from '../../auth/AuthProvider';
import { DashboardShell } from '../Dashboard';
import { CreateRide } from './CreateRide';
import { DriverRide } from './DriverRide';
import { MyRides } from './MyRides';

/**
 * Driver journey views (M3) under /driver/rides, /driver/rides/new and
 * /driver/rides/:id. Reloads and shared links work because the backend serves
 * index.html for /driver/* (G1, frontend_serving.CLIENT_AREAS). The route guard
 * only shapes the UI; the API enforces driver capability and ride ownership.
 */
export function DriverArea({ view }: { view: 'rides' | 'new' | 'ride' }) {
  const { selectMode } = useAuth();
  const { rideId } = useParams();
  useEffect(() => { selectMode('driver'); }, [selectMode]);
  if (view === 'new') return <DashboardShell mode="driver" active="publish"><CreateRide /></DashboardShell>;
  if (view === 'rides') return <DashboardShell mode="driver" active="rides"><MyRides /></DashboardShell>;
  const id = rideId && /^[1-9]\d{0,9}$/.test(rideId) ? Number(rideId) : NaN;
  return <DashboardShell mode="driver" active="rides"><DriverRide key={rideId} rideId={id} /></DashboardShell>;
}
