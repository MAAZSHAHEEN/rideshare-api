import { useQuery } from '@tanstack/react-query';
import type { RideBookingCounts } from '../api/types';
import { useAuth } from '../auth/AuthProvider';

/**
 * Request counts for a page of the driver's own rides (D2): one
 * GET /rides/me/booking-counts call per page (the client chunks above 100 ids).
 * Keyed under 'booking-counts' so accept/reject/cancel refreshes can invalidate it.
 * A failure only hides the counts; the ride list itself still renders.
 */
export function useRideBookingCounts(rideIds: number[]) {
  const { api, user } = useAuth();
  const ids = [...new Set(rideIds)].sort((a, b) => a - b);
  return useQuery({
    queryKey: ['booking-counts', user!.id, ids],
    enabled: ids.length > 0,
    queryFn: async ({ signal }) => new Map<number, RideBookingCounts>((await api.rideBookingCounts(ids, signal)).map(row => [row.ride_id, row])),
  });
}
