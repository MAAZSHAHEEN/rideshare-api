import { useEffect, useRef, useState } from 'react';
import type { FormEvent } from 'react';
import { Link, Navigate, useLocation, useNavigate } from 'react-router-dom';
import { ApiError } from '../api/client';
import { dashboardPath, useAuth } from '../auth/AuthProvider';
import { passwordError } from '../auth/validation';
import { Icon } from '../components/Icon';

export function AuthPage({ mode }: { mode: 'login' | 'register' }) {
  const registering = mode === 'register';
  const { api, user, login, notice } = useAuth();
  const navigate = useNavigate();
  const location = useLocation();
  const [role, setRole] = useState<'passenger' | 'driver'>('passenger');
  const [busy, setBusy] = useState(false);
  const [showPassword, setShowPassword] = useState(false);
  const [error, setError] = useState('');
  const [fields, setFields] = useState<Record<string, string>>({});
  const errorRef = useRef<HTMLDivElement>(null);
  const registrationRequest = useRef<AbortController | null>(null);
  useEffect(() => () => registrationRequest.current?.abort(), []);
  useEffect(() => { if (error) errorRef.current?.focus(); }, [error]);
  if (user) return <Navigate to={dashboardPath(user)} replace />;

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (busy) return;
    const data = new FormData(event.currentTarget);
    const value = (name: string) => String(data.get(name) ?? '');
    const password = value('password');
    const invalid = passwordError(password, registering);
    if (invalid) { setFields({ password: invalid }); setError('Please check your password.'); return; }
    setError(''); setFields({}); setBusy(true);
    try {
      const email = value('email');
      if (registering) {
        const controller = new AbortController();
        registrationRequest.current = controller;
        await api.register({ email, password, name: value('name'), cnic: value('cnic'), phone_number: value('phone_number'), role }, controller.signal);
        navigate('/login', { replace: true, state: { registered: true } });
      } else {
        const identity = await login({ email, password });
        navigate(dashboardPath(identity), { replace: true });
      }
    } catch (reason) {
      if (reason instanceof DOMException && reason.name === 'AbortError') return;
      if (reason instanceof ApiError) {
        setFields(reason.fields);
        setError(registering && (reason.status === 0 || reason.status >= 500)
          ? 'We could not confirm registration. Try signing in before submitting again.' : reason.message);
      } else setError(reason instanceof Error ? reason.message : 'Something went wrong. Please try again.');
    } finally { setBusy(false); }
  }
  function field(name: string, label: string, type = 'text', autoComplete?: string, placeholder?: string) {
    return <div className="field"><label htmlFor={name}>{label}</label><input id={name} name={name} type={type} autoComplete={autoComplete} placeholder={placeholder} required aria-invalid={Boolean(fields[name])} aria-describedby={fields[name] ? `${name}-error` : undefined} /><span className="field-error" id={`${name}-error`}>{fields[name]}</span></div>;
  }
  return <section className={`container auth-page ${registering ? 'registration' : ''}`}>
    <aside className="auth-story"><span className="eyebrow">A BETTER WAY TOGETHER</span><h1>{registering ? <>Every great journey<br />starts with a hello.</> : <>Familiar roads.<br />New possibilities.</>}</h1><p>{registering ? 'Find your place in a community of passengers and drivers sharing the road.' : 'Your next journey is waiting. Let’s pick up where you left off.'}</p><img src="/journey.svg" alt="" /><div className="story-bottom"><Icon name="road" /><span>A little company goes a long way.</span></div></aside>
    <div className="auth-form-wrap"><Link className="back-link" to="/">← Back to home</Link><span className="eyebrow">{registering ? 'JOIN THE JOURNEY' : 'GOOD TO SEE YOU'}</span><h2>{registering ? 'Create your account' : 'Welcome back.'}</h2><p className="form-intro">{registering ? 'A few details, then you’re ready to get started.' : 'Log in to your own little corner of the road.'}</p>
      {!registering && location.state?.registered && <div className="notice success" role="status">Your account is ready. Log in to get started.</div>}
      {!registering && notice && <div className="notice" role="status">{notice}</div>}
      {error && <div className="notice error" role="alert" tabIndex={-1} ref={errorRef}>{error}</div>}
      <form onSubmit={submit} aria-label={registering ? 'Create account' : 'Log in'}>
        <fieldset disabled={busy} className="form-fields">
          {registering && <fieldset className="role-fieldset"><legend>I’m joining as a</legend><div className="role-options">{(['passenger', 'driver'] as const).map(option => <label className={`role-option ${role === option ? 'selected' : ''}`} key={option}><input type="radio" name="role" value={option} checked={role === option} onChange={() => setRole(option)} /><Icon name={option === 'driver' ? 'car' : 'user'} size={20} /><span>{option === 'driver' ? 'Driver' : 'Passenger'}</span><span className="radio-dot" /></label>)}</div></fieldset>}
          {registering && field('name', 'Full name', 'text', 'name', 'Your full name')}
          {field('email', 'Email address', 'email', 'email', 'you@example.com')}
          {registering && <div className="form-row">{field('cnic', 'CNIC', 'text', 'off', 'Your CNIC number')}{field('phone_number', 'Phone number', 'tel', 'tel', 'Your phone number')}</div>}
          <div className="field"><label htmlFor="password">Password</label><div className="password-field"><input id="password" name="password" type={showPassword ? 'text' : 'password'} autoComplete={registering ? 'new-password' : 'current-password'} required aria-invalid={Boolean(fields.password)} aria-describedby="password-help password-error" /><button type="button" onClick={() => setShowPassword(!showPassword)} aria-label={showPassword ? 'Hide password' : 'Show password'} aria-pressed={showPassword}>{showPassword ? 'Hide' : 'Show'}</button></div><span className="field-help" id="password-help">{registering ? 'At least 12 characters. Maximum 72 UTF-8 bytes.' : 'Enter your existing password.'}</span><span className="field-error" id="password-error">{fields.password}</span></div>
          <button className="button full-width" type="submit" disabled={busy}>{busy ? <><span className="spinner" />{registering ? 'Creating account…' : 'Signing in…'}</> : <>{registering ? 'Create account' : 'Log in'}<Icon name="arrow" size={18} /></>}</button>
        </fieldset>
      </form>
      <p className="auth-switch">{registering ? 'Already part of the journey?' : 'New to RideShare?'} <Link to={registering ? '/login' : '/register'}>{registering ? 'Log in' : 'Create an account'}</Link></p>
      {!registering && <p className="session-note"><Icon name="check" size={14} />Your session stays in this tab’s memory. Reloading signs you out.</p>}
    </div>
  </section>;
}
