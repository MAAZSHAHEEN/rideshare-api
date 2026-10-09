import { useEffect, useState } from 'react';
import { Link, NavLink, Outlet, useLocation } from 'react-router-dom';
import { dashboardPath, useAuth } from '../auth/AuthProvider';
import { Icon } from './Icon';

export function Brand() {
  return <Link className="brand" to="/" aria-label="RideShare home"><span className="brand-mark"><Icon name="road" size={25} /></span>Ride<span className="brand-blue">Share</span><span className="brand-dot">.</span></Link>;
}

export function Layout() {
  const { user, logout } = useAuth();
  const [menu, setMenu] = useState(false);
  const location = useLocation();
  useEffect(() => {
    setMenu(false);
    if (!location.hash) window.scrollTo(0, 0);
    document.title = `${({ '/': 'A better way together', '/login': 'Log in', '/register': 'Create account', '/passenger': 'Passenger dashboard', '/driver': 'Driver dashboard', '/account': 'My account' } as Record<string, string>)[location.pathname] || 'Page not found'} | RideShare`;
    document.getElementById('main-content')?.focus({ preventScroll: true });
  }, [location.pathname, location.hash]);
  return <>
    <a className="skip-link" href="#main-content">Skip to content</a>
    <header className="site-header"><div className="container header-inner">
      <Brand />
      <button className="menu-button" aria-label={menu ? 'Close navigation' : 'Open navigation'} aria-expanded={menu} aria-controls="main-navigation" onClick={() => setMenu(!menu)}><Icon name={menu ? 'close' : 'menu'} /></button>
      <nav id="main-navigation" className={`main-nav ${menu ? 'is-open' : ''}`} aria-label="Main navigation">
        <NavLink to="/" end>Home</NavLink><Link to="/#how-it-works" onClick={() => setMenu(false)}>How it works</Link>
        {user ? <><NavLink to={dashboardPath(user)}>My dashboard</NavLink><button className="button button-outline small" onClick={logout}>Log out</button></> : <><NavLink to="/login">Log in</NavLink><Link className="button small" to="/register">Get started <Icon name="arrow" size={17} /></Link></>}
      </nav>
    </div></header>
    <main id="main-content" tabIndex={-1}><Outlet /></main>
    <footer className="site-footer"><div className="container footer-top"><div><Brand /><p>A little company. A better journey.</p></div><div className="footer-links"><Link to="/">Home</Link><Link to="/#how-it-works">How it works</Link><Link to={user ? dashboardPath(user) : '/register'}>{user ? 'My dashboard' : 'Join RideShare'}</Link></div></div><div className="container footer-bottom"><span>© {new Date().getFullYear()} RideShare</span><span>Made for the journeys between.</span></div></footer>
  </>;
}
