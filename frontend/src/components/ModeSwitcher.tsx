import { NavLink } from 'react-router-dom';
import { useAuth } from '../auth/AuthProvider';
import { Icon } from './Icon';

export function ModeSwitcher() {
  const { user, mode } = useAuth();
  if (!user || user.role === 'admin' || user.allowed_modes.length === 0) return null;
  return <div className="mode-bar"><div className="container mode-bar-inner">
    <div><strong>Your travel mode</strong><p>One account. Both sides of the journey.</p></div>
    <nav className="mode-switcher" aria-label="Travel mode">
      {user.allowed_modes.map(option => <NavLink key={option} to={`/${option}`}
        className={mode === option ? 'selected' : ''}>
        <Icon name={option === 'driver' ? 'car' : 'bag'} size={18} />
        {option === 'driver' ? 'Driver Mode' : 'Passenger Mode'}
      </NavLink>)}
    </nav>
  </div></div>;
}
