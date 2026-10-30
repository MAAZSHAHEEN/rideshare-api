import { useRef, useState } from 'react';
import type { FormEvent } from 'react';
import { keepPreviousData, useQuery } from '@tanstack/react-query';
import { Link, useLocation, useSearchParams } from 'react-router-dom';
import { useAuth } from '../../auth/AuthProvider';
import { Icon } from '../../components/Icon';
import type { Ride } from '../../api/types';
import { passengerErrorMessage } from '../../passenger/errors';
import { formatDeparture, formatFare, isCalendarDate, karachiDayWindow, karachiToday, seatsLabel } from '../../passenger/time';
import { usePassengerView } from './PassengerArea';

export const PAGE_SIZE = 20;
const MAX_PLACE = 100;
// Suggestions only: the API matches origin and destination exactly as typed.
const CITIES = ['Lahore', 'Islamabad', 'Rawalpindi', 'Faisalabad', 'Gujranwala', 'Multan', 'Peshawar', 'Sialkot'];

export interface SearchFilters { from: string; to: string; date: string }
type FieldErrors = Partial<Record<keyof SearchFilters, string>>;

/** Client-side checks mirroring what the API would reject or never return. */
export function validateFilters(filters: SearchFilters, today = karachiToday()): FieldErrors {
  const errors: FieldErrors = {};
  if (filters.from.length > MAX_PLACE) errors.from = `Use ${MAX_PLACE} characters or fewer.`;
  if (filters.to.length > MAX_PLACE) errors.to = `Use ${MAX_PLACE} characters or fewer.`;
  if (filters.from && filters.to && filters.from.toLocaleLowerCase() === filters.to.toLocaleLowerCase()) errors.to = 'Choose a destination different from where you’re leaving.';
  if (filters.date) {
    if (!isCalendarDate(filters.date)) errors.date = 'Enter a valid date.';
    else if (filters.date < today) errors.date = 'Choose today or a later date. Only upcoming rides can be booked.';
  }
  return errors;
}

function readFilters(params: URLSearchParams): SearchFilters {
  return { from: (params.get('from') ?? '').trim(), to: (params.get('to') ?? '').trim(), date: (params.get('date') ?? '').trim() };
}

export function SearchRides() {
  const { api, user } = useAuth();
  const [params, setParams] = useSearchParams();
  const location = useLocation();
  const filters = readFilters(params);
  const page = Math.max(0, Math.min(10_000, Number.parseInt(params.get('page') ?? '0', 10) || 0));
  const urlErrors = validateFilters(filters);
  const [formErrors, setFormErrors] = useState<FieldErrors | null>(null);
  const errors = formErrors ?? urlErrors;
  const hasErrors = Object.keys(errors).length > 0;
  const summaryRef = useRef<HTMLDivElement>(null);
  usePassengerView('Find a ride', 'search-heading');

  const query = useQuery({
    queryKey: ['ride-search', user!.id, filters.from, filters.to, filters.date, page],
    enabled: Object.keys(urlErrors).length === 0,
    placeholderData: keepPreviousData,
    queryFn: ({ signal }) => {
      const range = filters.date ? karachiDayWindow(filters.date) : undefined;
      return api.searchRides({
        origin: filters.from || undefined, destination: filters.to || undefined,
        departureFrom: range?.from, departureBefore: range?.before, limit: PAGE_SIZE, offset: page * PAGE_SIZE,
      }, signal);
    },
  });

  function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const data = new FormData(event.currentTarget);
    const next: SearchFilters = { from: String(data.get('from') ?? '').trim(), to: String(data.get('to') ?? '').trim(), date: String(data.get('date') ?? '').trim() };
    const invalid = validateFilters(next);
    if (Object.keys(invalid).length) {
      setFormErrors(invalid);
      requestAnimationFrame(() => summaryRef.current?.focus());
      return;
    }
    setFormErrors(null);
    const nextParams = new URLSearchParams({ view: 'search' });
    if (next.from) nextParams.set('from', next.from);
    if (next.to) nextParams.set('to', next.to);
    if (next.date) nextParams.set('date', next.date);
    setParams(nextParams);
  }
  function clear() {
    setFormErrors(null);
    setParams(new URLSearchParams({ view: 'search' }));
  }
  function goToPage(next: number) {
    const nextParams = new URLSearchParams(params);
    if (next > 0) nextParams.set('page', String(next)); else nextParams.delete('page');
    setParams(nextParams);
  }

  const rides = query.data ?? [];
  const described = (name: keyof SearchFilters, help?: string) => [help, errors[name] ? `${name}-error` : undefined].filter(Boolean).join(' ') || undefined;
  return <div className="passenger-view">
    <div className="view-heading"><span className="eyebrow">FIND A RIDE</span><h1 id="search-heading" tabIndex={-1}>Where are you heading?</h1><p>Upcoming rides only. Times are shown in Pakistan time (PKT).</p></div>
    <form className="search-form" role="search" aria-label="Search rides" onSubmit={submit} key={params.toString()} noValidate>
      {hasErrors && formErrors && <div className="notice error" role="alert" tabIndex={-1} ref={summaryRef}>Please check the highlighted search filters.</div>}
      <div className="search-fields">
        <div className="field"><label htmlFor="search-from">From</label><input id="search-from" name="from" list="city-suggestions" defaultValue={filters.from} maxLength={MAX_PLACE} autoComplete="off" placeholder="e.g. Lahore" aria-invalid={Boolean(errors.from)} aria-describedby={described('from')} /><span className="field-error" id="from-error">{errors.from}</span></div>
        <div className="field"><label htmlFor="search-to">To</label><input id="search-to" name="to" list="city-suggestions" defaultValue={filters.to} maxLength={MAX_PLACE} autoComplete="off" placeholder="e.g. Islamabad" aria-invalid={Boolean(errors.to)} aria-describedby={described('to')} /><span className="field-error" id="to-error">{errors.to}</span></div>
        <div className="field"><label htmlFor="search-date">Date (PKT)</label><input id="search-date" name="date" type="date" defaultValue={filters.date} min={karachiToday()} aria-invalid={Boolean(errors.date)} aria-describedby={described('date', 'date-help')} /><span className="field-help" id="date-help">Optional. Leave empty for all upcoming dates.</span><span className="field-error" id="date-error">{errors.date}</span></div>
      </div>
      <datalist id="city-suggestions">{CITIES.map(city => <option key={city} value={city} />)}</datalist>
      <p className="field-help">Place names must match the ride exactly, for example “Lahore”.</p>
      <div className="search-actions"><button className="button" type="submit">Search rides <Icon name="arrow" size={17} /></button><button className="button button-outline" type="button" onClick={clear}>Clear filters</button></div>
    </form>

    <section className="results-panel" aria-labelledby="results-heading" aria-busy={query.isFetching}>
      <div className="history-heading"><div><h2 id="results-heading">Available rides</h2><p role="status" aria-live="polite">{resultSummary(query.isPending && query.fetchStatus !== 'idle', query.isError, rides.length, page)}</p></div>
        <button className="button button-outline small" onClick={() => void query.refetch()} disabled={query.isFetching || Object.keys(urlErrors).length > 0}>{query.isFetching ? 'Refreshing…' : 'Refresh'}</button></div>
      {Object.keys(urlErrors).length > 0 ? <div className="state-panel"><h3>Fix the filters to search.</h3><p>One of the search filters isn’t valid.</p></div>
        : query.isPending ? <div className="state-panel"><span className="spinner blue" aria-hidden="true" /><p>Looking for rides…</p></div>
        : query.isError ? <div className="state-panel"><h3>We couldn’t load rides.</h3><p role="alert">{passengerErrorMessage(query.error, 'search')}</p><button className="button small" onClick={() => void query.refetch()}>Try again</button></div>
        : rides.length === 0 ? <div className="state-panel"><span className="empty-illustration"><Icon name="car" size={34} /></span><h3>{page ? 'No more rides.' : 'No rides match yet.'}</h3><p>{page ? 'Go back to see earlier results.' : 'Try another date, or clear a filter to see every upcoming ride.'}</p></div>
        : <ul className="ride-list">{rides.map(ride => <li key={ride.id}><RideCard ride={ride} back={location.search} /></li>)}</ul>}
      <div className="pagination"><span>Page {page + 1}</span><div><button disabled={page === 0 || query.isFetching} onClick={() => goToPage(page - 1)}>← Previous</button><button disabled={rides.length < PAGE_SIZE || query.isFetching || query.isError} onClick={() => goToPage(page + 1)}>Next →</button></div></div>
    </section>
  </div>;
}

function resultSummary(loading: boolean, failed: boolean, count: number, page: number) {
  if (loading) return 'Searching…';
  if (failed) return 'Search failed.';
  if (count === 0) return page ? 'No more rides on this page.' : 'No rides found.';
  return `${count} ride${count === 1 ? '' : 's'} found${page ? ` on page ${page + 1}` : ''}.`;
}

export function RideCard({ ride, back }: { ride: Ride; back?: string }) {
  return <article className="ride-card" aria-labelledby={`ride-${ride.id}-title`}>
    <div className="ride-card-main">
      <h3 id={`ride-${ride.id}-title`}>{ride.origin} <span aria-label="to">→</span> {ride.destination}</h3>
      <p className="ride-time"><Icon name="clock" size={14} /><time dateTime={ride.departure_time}>{formatDeparture(ride.departure_time)}</time></p>
      <dl className="ride-facts"><div><dt>Fare</dt><dd>{formatFare(ride.fare_per_seat)} <span>per seat</span></dd></div><div><dt>Seats</dt><dd>{seatsLabel(ride.available_seats)}</dd></div><div><dt>Status</dt><dd><span className={`status-pill ${ride.status}`}>{ride.status}</span></dd></div></dl>
    </div>
    <Link className="button button-outline small" to={`/passenger?ride=${ride.id}`} state={{ back }} aria-label={`View details: ${ride.origin} to ${ride.destination}, ${formatDeparture(ride.departure_time)}`}>View details</Link>
  </article>;
}
