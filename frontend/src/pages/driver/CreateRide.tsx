import { useRef, useState } from 'react';
import type { ChangeEvent, FormEvent } from 'react';
import { useMutation, useQueryClient } from '@tanstack/react-query';
import { Link, useNavigate } from 'react-router-dom';
import { ApiError } from '../../api/client';
import { useAuth } from '../../auth/AuthProvider';
import { Icon } from '../../components/Icon';
import { driverErrorMessage, rideFieldErrors } from '../../driver/errors';
import type { RideFormErrors, RideFormValues } from '../../driver/rideForm';
import { FARE_MAX, PLACE_MAX, SEATS_MAX, SEATS_MIN, SUGGESTED_CITIES, karachiLocalToIso, karachiNowLocal, toRideInput, validateRide } from '../../driver/rideForm';
import { formatDeparture } from '../../passenger/time';
import { useDriverView } from '../../driver/useDriverView';

const EMPTY: RideFormValues = { origin: '', destination: '', departure: '', seats: '3', fare: '' };

export function CreateRide() {
  const { api } = useAuth();
  const queryClient = useQueryClient();
  const navigate = useNavigate();
  const [values, setValues] = useState<RideFormValues>(EMPTY);
  const [errors, setErrors] = useState<RideFormErrors>({});
  const [summary, setSummary] = useState('');
  const summaryRef = useRef<HTMLDivElement>(null);
  useDriverView('Publish a ride', 'publish-heading');

  const create = useMutation({
    mutationFn: (input: ReturnType<typeof toRideInput>) => api.createRide(input),
    onSuccess: ride => {
      void queryClient.invalidateQueries({ queryKey: ['my-rides'] });
      navigate(`/driver/rides/${ride.id}`, { state: { published: true } });
    },
    onError: reason => {
      if (reason instanceof ApiError && reason.status === 401) return; // the guard sends the driver to sign in
      setErrors(rideFieldErrors(reason));
      show(driverErrorMessage(reason, 'create'));
    },
  });

  function show(message: string) {
    setSummary(message);
    requestAnimationFrame(() => summaryRef.current?.focus());
  }
  const change = (field: keyof RideFormValues) => (event: ChangeEvent<HTMLInputElement>) => {
    const next = { ...values, [field]: event.target.value };
    setValues(next);
    if (errors[field]) setErrors(current => { const copy = { ...current }; delete copy[field]; return copy; });
  };

  function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (create.isPending) return;
    const invalid = validateRide(values, Date.now());
    setErrors(invalid);
    if (Object.keys(invalid).length) { show('Please fix the highlighted ride details.'); return; }
    setSummary('');
    create.mutate(toRideInput(values));
  }

  const iso = karachiLocalToIso(values.departure);
  const described = (name: keyof RideFormValues, help?: string) => [help, errors[name] ? `ride-${name}-error` : undefined].filter(Boolean).join(' ') || undefined;
  const field = (name: keyof RideFormValues) => ({ id: `ride-${name}`, name, value: values[name], onChange: change(name), 'aria-invalid': Boolean(errors[name]) });
  return <div className="passenger-view driver-view">
    <Link className="back-link" to="/driver/rides">← My rides</Link>
    <div className="view-heading"><span className="eyebrow">PUBLISH A RIDE</span><h1 id="publish-heading" tabIndex={-1}>Offer seats on a scheduled ride</h1><p>Intercity rides are planned ahead. Times are Pakistan time (PKT, UTC+05:00), whatever your device’s time zone.</p></div>
    <form className="search-form ride-form" aria-label="Publish a ride" onSubmit={submit} noValidate>
      {summary && <div className="notice error" role="alert" tabIndex={-1} ref={summaryRef}>{summary}</div>}
      <div className="ride-form-grid">
        <div className="field"><label htmlFor="ride-origin">From</label><input {...field('origin')} list="ride-city-suggestions" maxLength={PLACE_MAX} autoComplete="off" placeholder="e.g. Lahore" aria-describedby={described('origin')} required /><span className="field-error" id="ride-origin-error">{errors.origin}</span></div>
        <div className="field"><label htmlFor="ride-destination">To</label><input {...field('destination')} list="ride-city-suggestions" maxLength={PLACE_MAX} autoComplete="off" placeholder="e.g. Islamabad" aria-describedby={described('destination')} required /><span className="field-error" id="ride-destination-error">{errors.destination}</span></div>
        <div className="field"><label htmlFor="ride-departure">Departure (PKT)</label><input {...field('departure')} type="datetime-local" min={karachiNowLocal()} step={60} aria-describedby={described('departure', 'ride-departure-help')} required /><span className="field-help" id="ride-departure-help">{iso && !errors.departure ? `Departs ${formatDeparture(iso)}.` : 'Date and time in Pakistan.'}</span><span className="field-error" id="ride-departure-error">{errors.departure}</span></div>
        <div className="field"><label htmlFor="ride-seats">Seats offered</label><input {...field('seats')} type="number" inputMode="numeric" min={SEATS_MIN} max={SEATS_MAX} step={1} aria-describedby={described('seats', 'ride-seats-help')} required /><span className="field-help" id="ride-seats-help">{SEATS_MIN} to {SEATS_MAX}. Each accepted request uses one seat.</span><span className="field-error" id="ride-seats-error">{errors.seats}</span></div>
        <div className="field"><label htmlFor="ride-fare">Fare per seat (PKR)</label><input {...field('fare')} type="number" inputMode="numeric" min={0} max={FARE_MAX} step={1} placeholder="e.g. 2500" aria-describedby={described('fare', 'ride-fare-help')} required /><span className="field-help" id="ride-fare-help">Whole rupees, shown to passengers as the price per seat.</span><span className="field-error" id="ride-fare-error">{errors.fare}</span></div>
      </div>
      <datalist id="ride-city-suggestions">{SUGGESTED_CITIES.map(city => <option key={city} value={city} />)}</datalist>
      <p className="field-help">Passengers search by exact place name, so use the common spelling, for example “Lahore”.</p>
      <div className="search-actions"><button className="button" type="submit" disabled={create.isPending}>{create.isPending ? <><span className="spinner" aria-hidden="true" />Publishing…</> : <>Publish ride <Icon name="arrow" size={17} /></>}</button><Link className="button button-outline" to="/driver/rides">Cancel</Link></div>
    </form>
  </div>;
}
