export function Icon({ name, size = 22 }: { name: 'arrow' | 'road' | 'user' | 'car' | 'check' | 'bag' | 'grid' | 'logout' | 'clock' | 'menu' | 'close'; size?: number }) {
  const paths = {
    arrow: <><path d="M4 12h15m-6-6 6 6-6 6" /></>,
    road: <><path d="m8 3-4 18M16 3l4 18M12 3v3m0 4v4m0 4v3" /></>,
    user: <><circle cx="12" cy="8" r="4" /><path d="M4 21v-2a8 8 0 0 1 16 0v2" /></>,
    car: <><path d="m4 10 2-6h12l2 6M3 10h18v9H3zM6 19v2m12-2v2M6 14h2m8 0h2" /></>,
    check: <path d="m5 12 4 4L19 6" />,
    bag: <><rect x="4" y="7" width="16" height="14" rx="2" /><path d="M9 7V3h6v4M9 11v6m6-6v6" /></>,
    grid: <><rect x="3" y="3" width="7" height="7" rx="1" /><rect x="14" y="3" width="7" height="7" rx="1" /><rect x="3" y="14" width="7" height="7" rx="1" /><rect x="14" y="14" width="7" height="7" rx="1" /></>,
    logout: <><path d="M9 3H4v18h5m5-15 6 6-6 6m-5-6h11" /></>,
    clock: <><circle cx="12" cy="12" r="9" /><path d="M12 7v5l3 2" /></>,
    menu: <path d="M4 6h16M4 12h16M4 18h16" />,
    close: <path d="m6 6 12 12M6 18 18 6" />,
  };
  return <svg width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">{paths[name]}</svg>;
}
