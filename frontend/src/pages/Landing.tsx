import { useEffect } from 'react';
import { Link, useLocation } from 'react-router-dom';
import { dashboardPath, useAuth } from '../auth/AuthProvider';
import { Icon } from '../components/Icon';

export function Landing() {
  const { user } = useAuth();
  const { hash } = useLocation();
  useEffect(() => { if (hash === '#how-it-works') document.getElementById('how-it-works')?.scrollIntoView(); }, [hash]);
  return <>
    <section className="hero container">
      <div className="hero-copy"><span className="eyebrow"><span className="little-dot" /> THE ROAD IS BETTER TOGETHER</span>
        <h1>Your next journey.<br /><span>Better shared.</span></h1>
        <p className="hero-description">From familiar commutes to a change of scenery. Connect with people heading your way and make the miles feel a little shorter.</p>
        <div className="hero-actions"><Link className="button" to={user ? dashboardPath(user) : '/register'}>{user ? 'Go to my dashboard' : 'Start your journey'}<Icon name="arrow" size={19} /></Link><a className="text-link" href="#how-it-works">See how it works <span>↗</span></a></div>
        <div className="hero-note"><span className="note-icon"><Icon name="check" size={16} /></span>For passengers. For drivers. For the road ahead.</div>
      </div>
      <div className="hero-art"><img src="/journey.svg" alt="Illustration of a car following a winding road through blue mountains" fetchPriority="high" /><div className="art-label"><span className="icon-tile"><Icon name="road" /></span><div><strong>A new perspective</strong><span>One shared journey at a time</span></div></div><span className="art-caption">LESS ROUTINE. MORE JOURNEY.</span></div>
    </section>
    <section className="journey-strip"><div className="container strip-inner"><span>GOING THE SAME WAY?</span><p>A seat to share.<span />A reason to connect.<span />A better way to travel.</p></div></section>
    <section id="how-it-works" className="container steps-section"><div className="section-heading"><div><span className="eyebrow">A SIMPLE START</span><h2>Good journeys start here.</h2></div><p>A few simple steps.<br />A whole new way to share the road.</p></div>
      <div className="steps-grid">{[
        { icon: 'user' as const, title: 'Make yourself at home', text: 'Create your account as a passenger or driver. One place to keep your journeys together.' },
        { icon: 'road' as const, title: 'Go your own way, together', text: 'Passengers request a place. Drivers decide who joins their ride. A shared journey starts with a connection.' },
        { icon: 'bag' as const, title: 'Keep your plans in view', text: 'Your dashboard brings your rides or booking history into one simple, personal space.' },
      ].map((item, index) => <article className="step-card" key={item.title}><div className="step-top"><span className="icon-tile"><Icon name={item.icon} size={24} /></span><span className="step-number">0{index + 1}</span></div><h3>{item.title}</h3><p>{item.text}</p></article>)}</div>
    </section>
    <section className="container"><div className="join-banner"><div><span className="eyebrow">YOUR NEXT CHAPTER</span><h2>There’s a journey<br />with your name on it.</h2><p>Start with an account. See where the road takes you.</p></div><Link className="button button-white" to={user ? dashboardPath(user) : '/register'}>{user ? 'Open dashboard' : 'Join RideShare'}<Icon name="arrow" size={19} /></Link><div className="banner-orbit" aria-hidden="true" /></div></section>
  </>;
}
