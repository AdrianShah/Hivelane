import { useEffect, type ReactNode } from 'react';
import { Link, NavLink, useLocation } from 'react-router-dom';
import { Mark } from '../brand/BrandArt';
import ThemeToggle from '../components/ThemeToggle';
import { DOCS_URL, REPO_URL } from '../links';
import './site.css';

const DOCS_LINKS = [
  ['Getting started', 'quick-start/'],
  ['Core concepts', 'architecture/'],
  ['Integrations', 'adapters/'],
  ['Self-hosting', 'self-hosting/'],
  ['Security', 'security/'],
] as const;

/** Nav and footer shared by the public pages (landing, about, updates, legal). */
export default function SiteLayout({ children, title }: { children: ReactNode; title?: string }) {
  const { pathname, hash } = useLocation();

  useEffect(() => {
    document.title = title ? `${title} · Hivelane` : 'Hivelane · Live coordination for parallel AI coding agents';
  }, [title]);

  // Router navigation keeps the scroll position; a new page starts at the top
  // unless the link points at a section.
  useEffect(() => {
    if (hash) document.getElementById(hash.slice(1))?.scrollIntoView();
    else window.scrollTo(0, 0);
  }, [pathname, hash]);

  return (
    <div className="site">
      <header className="site__nav">
        <div className="site__bar">
          <Link to="/" className="site__brand" aria-label="Hivelane home">
            <Mark size={26} />
            Hivelane
          </Link>
          <nav className="site__links" aria-label="Site">
            <a href={DOCS_URL}>Docs</a>
            <NavLink to="/updates">Updates</NavLink>
            <NavLink to="/about">About</NavLink>
            <a href={REPO_URL}>GitHub</a>
          </nav>
          <div className="site__end">
            <ThemeToggle />
            <Link to="/start" className="site-btn site-btn--primary site-btn--sm">
              Open dashboard
            </Link>
          </div>
        </div>
      </header>

      <main className="site__main">{children}</main>

      <footer className="site__footer">
        <div className="site__footer-grid">
          <div className="site__footer-brand">
            <Link to="/" className="site__brand">
              <Mark size={24} />
              Hivelane
            </Link>
            <p>Live coordination for parallel AI coding agents. Open source under Apache-2.0.</p>
          </div>
          <div>
            <h3>Product</h3>
            <Link to="/start">Open dashboard</Link>
            <Link to="/#how-it-works">How it works</Link>
            <Link to="/updates">Updates</Link>
            <a href={REPO_URL}>GitHub</a>
          </div>
          <div>
            <h3>Docs</h3>
            {DOCS_LINKS.map(([label, path]) => (
              <a key={path} href={`${DOCS_URL}${path}`}>
                {label}
              </a>
            ))}
          </div>
          <div>
            <h3>Project</h3>
            <Link to="/about">About</Link>
            <Link to="/about#contact">Contact</Link>
            <Link to="/privacy">Privacy</Link>
            <Link to="/terms">Terms</Link>
            <Link to="/security">Security</Link>
          </div>
        </div>
        <div className="site__footer-base">
          <span>© {new Date().getFullYear()} Hivelane contributors.</span>
          <span>Self-hostable. Your code never has to leave your network.</span>
        </div>
      </footer>
    </div>
  );
}
