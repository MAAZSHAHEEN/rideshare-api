import { useEffect } from 'react';
import { useSearchParams } from 'react-router-dom';
import { useAuth } from '../../auth/AuthProvider';
import { Dashboard, DashboardShell } from '../Dashboard';
import { MyBookings } from './MyBookings';
import { RideDetails } from './RideDetails';
import { SearchRides } from './SearchRides';

/**
 * Passenger journey views live under the exact /passenger path, selected by
 * query parameters (?view=search, ?ride=<id>, ?view=bookings). The backend's
 * SPA fallback (frontend_serving.CLIENT_PATHS) only serves exact paths, so
 * nested routes such as /passenger/rides/7 would 404 on reload or deep link.
 * See handover/tasks/AST-023-passenger.md (API/serving gap G1).
 */
export function PassengerArea() {
  const [params] = useSearchParams();
  const { selectMode } = useAuth();
  useEffect(() => { selectMode('passenger'); }, [selectMode]);
  const ride = params.get('ride');
  const view = params.get('view');
  if (ride !== null) {
    const id = /^[1-9]\d{0,9}$/.test(ride) ? Number(ride) : NaN;
    return <DashboardShell mode="passenger" active="search"><RideDetails key={ride} rideId={id} /></DashboardShell>;
  }
  if (view === 'search') return <DashboardShell mode="passenger" active="search"><SearchRides /></DashboardShell>;
  if (view === 'bookings') return <DashboardShell mode="passenger" active="bookings"><MyBookings /></DashboardShell>;
  return <Dashboard key="passenger" mode="passenger" />;
}

/** Sets the document title and moves focus to the view heading when a passenger view opens. */
export function usePassengerView(title: string, headingId: string) {
  useEffect(() => {
    // After the layout's own route effect, which runs later in the same commit.
    const timer = setTimeout(() => { document.title = `${title} | RideShare`; }, 0);
    document.getElementById(headingId)?.focus({ preventScroll: true });
    return () => clearTimeout(timer);
  }, [title, headingId]);
}
