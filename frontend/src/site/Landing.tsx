import { useEffect, useRef, useState } from 'react';
import { Link } from 'react-router-dom';
import type { AvatarState } from '../brand/mascot';
import { copyText } from '../clipboard';
import Bee from '../components/Bee';
import { DOCS_URL, REPO_URL } from '../links';
import ArchitectureDiagram from './ArchitectureDiagram';
import SiteLayout from './SiteLayout';
import './landing.css';

const INSTALL = 'pipx install git+https://github.com/AdrianShah/Hivelane && hivelane up';

const AGENTS = ['Claude Code', 'Codex', 'Cursor', 'Gemini CLI', 'VS Code', 'Windsurf', 'Any CLI via hivelane run'];

const FILES = ['src/api/invoice.ts', 'src/billing/totals.ts', 'server/routes.py'];

const REASONS = [
  {
    title: 'Checked before every write.',
    body: 'Agent hooks ask the relay before each edit. The answer is allow, warn or block, down to the function or line range a teammate is in.',
  },
  {
    title: 'Every lane, live.',
    body: "Each agent gets a lane with its intent, prompts, activity captions and diffs, so you can read what your teammates' agents are doing without asking.",
  },
  {
    title: 'Fails open, never in the way.',
    body: 'If the daemon or the relay is unreachable, agents keep working. Hivelane only ever adds a warning; it never stalls a session.',
  },
  {
    title: 'Private by design.',
    body: 'No accounts. Rooms expire after seven days, secrets are redacted on your machine, and end-to-end encryption keeps diffs unreadable to the relay.',
  },
];

const PILLARS = [
  {
    kicker: 'See',
    title: 'One board for every agent',
    body: 'Who is running what, on which branch, in which files. A heat map and timeline show where the team is concentrated.',
  },
  {
    kicker: 'Prevent',
    title: 'Overlap caught at edit time',
    body: 'Two agents reaching for the same function get warned (or blocked) before the write, not at pull-request time.',
  },
  {
    kicker: 'Coordinate',
    title: 'Plans that reach each other',
    body: "Teammates' intents and plans are fed back to your agent, so it can pick other work or wait instead of colliding.",
  },
];

const STEPS = [
  {
    title: 'Install the CLI',
    body: 'Once per machine. pipx, uv, Homebrew, Scoop or an install script.',
  },
  {
    title: 'Run hivelane up in your repo',
    body: 'Installs the agent hooks, creates a room, starts a small background daemon and commits the room to .hivelane.toml.',
  },
  {
    title: 'Your team runs the same command',
    body: 'Their agents join the room from the committed config and start checking with each other before every edit.',
  },
];

/** The bee's stripe takes the state's colour, like the desktop bee's body. */
const TONE_COLOR = { clear: '#2FA36B', warn: '#F5A524', block: '#E5484D' } as const;

/** The notch cycles through what a room looks like over a few seconds. */
const NOTCH_STATES: { bee: AvatarState; chip: string; tone: 'clear' | 'warn' | 'block'; line: string }[] = [
  { bee: 'editing', chip: 'all clear', tone: 'clear', line: 'Four agents, four lanes. Nobody is in anybody’s way.' },
  { bee: 'conflict', chip: '1 overlap', tone: 'warn', line: 'Maya and Tomás both reached for src/api/invoice.ts, lines 40–48.' },
  { bee: 'blocked', chip: '1 held', tone: 'block', line: 'Codex waits for routes.py until Maya’s edit lands. No lost work.' },
];

const LANES: { name: string; app: string; file: string; state: AvatarState; accent: string; status: string; tone: string }[] = [
  { name: 'Maya R.', app: 'Claude Code', file: 'src/billing/totals.ts', state: 'editing', accent: '#7C5CFF', status: 'Editing', tone: 'ok' },
  { name: 'Tomás O.', app: 'Cursor', file: 'src/api/invoice.ts', state: 'conflict', accent: '#14B8A6', status: 'Overlap', tone: 'warn' },
  { name: 'Codex', app: 'Codex CLI', file: 'server/routes.py', state: 'blocked', accent: '#F472B6', status: 'Held', tone: 'block' },
  { name: 'Gemini', app: 'Gemini CLI', file: '—', state: 'idle', accent: '#9CA3AF', status: 'Idle', tone: 'idle' },
];

/** Reveal [data-reveal] elements once as they scroll in (no-op under reduced motion). */
function useReveal() {
  const root = useRef<HTMLDivElement>(null);
  useEffect(() => {
    const el = root.current;
    if (!el) return;
    const items = Array.from(el.querySelectorAll<HTMLElement>('[data-reveal]'));
    if (typeof IntersectionObserver === 'undefined' || window.matchMedia('(prefers-reduced-motion: reduce)').matches) {
      items.forEach((item) => item.classList.add('is-in'));
      return;
    }
    const seen = new IntersectionObserver(
      (entries) => {
        for (const entry of entries) {
          if (entry.isIntersecting) {
            entry.target.classList.add('is-in');
            seen.unobserve(entry.target);
          }
        }
      },
      { rootMargin: '0px 0px -8% 0px', threshold: 0.12 },
    );
    items.forEach((item) => seen.observe(item));
    return () => seen.disconnect();
  }, []);
  return root;
}

function useCycle(length: number, ms: number) {
  const [index, setIndex] = useState(0);
  useEffect(() => {
    if (window.matchMedia('(prefers-reduced-motion: reduce)').matches) return;
    const timer = window.setInterval(() => setIndex((i) => (i + 1) % length), ms);
    return () => window.clearInterval(timer);
  }, [length, ms]);
  return index;
}

function HeroNotch() {
  const state = NOTCH_STATES[useCycle(NOTCH_STATES.length, 3800)];
  return (
    <div className="lp2-notch" tabIndex={0} aria-label={`Example room: ${state.chip}. ${state.line}`}>
      <div className="lp2-notch__bar">
        <Bee state={state.bee} accent={TONE_COLOR[state.tone]} size={28} />
        <span className="lp2-notch__room">billing-api</span>
        <span className="lp2-notch__live">4 live</span>
        <span className={`lp2-notch__chip is-${state.tone}`}>
          <span className="lp2-notch__dot" />
          {state.chip}
        </span>
      </div>
      <p className="lp2-notch__more" aria-hidden="true">
        {state.line}
      </p>
    </div>
  );
}

function FileTicker() {
  const index = useCycle(FILES.length, 2600);
  return (
    <span className="lp2-ticker" style={{ width: `${FILES[index].length}ch` }} aria-label={FILES.join(', ')}>
      <span className="lp2-ticker__track" style={{ transform: `translateY(${-index * 1.15}em)` }} aria-hidden="true">
        {FILES.map((file) => (
          <span key={file}>{file}</span>
        ))}
      </span>
    </span>
  );
}

/** A platform-neutral browser frame around a miniature of the real dashboard. */
function ProductFrame() {
  return (
    <figure className="lp2-frame" data-reveal aria-label="The Hivelane dashboard: four agents, one overlap">
      <div className="lp2-frame__bar">
        <span className="lp2-frame__brand">
          <Bee state="idle" accent="#0062D1" size={20} ring={false} />
          Hivelane
        </span>
        <span className="lp2-frame__address">
          <svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" aria-hidden="true">
            <rect x="5" y="11" width="14" height="10" rx="2" />
            <path d="M8 11V8a4 4 0 0 1 8 0v3" />
          </svg>
          hivelane.app/r/roomcode
        </span>
        <span className="lp2-frame__meta">4 live</span>
      </div>
      <div className="lp2-frame__body">
        <nav className="lp2-frame__side" aria-hidden="true">
          {['Dashboard', 'Hivelane feed', 'Tasks', 'Timeline', 'Heat map', 'Conflicts'].map((item, i) => (
            <span key={item} className={i === 0 ? 'is-on' : undefined}>
              {item}
              {item === 'Conflicts' ? <b>1</b> : null}
            </span>
          ))}
        </nav>
        <div className="lp2-frame__main">
          <p className="lp2-frame__headline">
            Four agents are working. <span>One overlap in</span> <code>invoice.ts</code> <span>needs a look.</span>
          </p>
          <div className="lp2-kpis">
            {[
              ['Active agents', '4'],
              ['Total diffs', '27'],
              ['Active conflicts', '1'],
              ['Prevented', '3'],
            ].map(([label, value]) => (
              <div key={label}>
                <span>{label}</span>
                <strong>{value}</strong>
              </div>
            ))}
          </div>
          <div className="lp2-lanes">
            {LANES.map((lane, i) => (
              <div key={lane.name} className={`lp2-lane is-${lane.tone}`} style={{ animationDelay: `${0.35 + i * 0.08}s` }}>
                <Bee state={lane.state} accent={lane.accent} size={34} />
                <span className="lp2-lane__who">
                  <strong>{lane.name}</strong>
                  <span>{lane.app}</span>
                </span>
                <code>{lane.file}</code>
                <span className="lp2-lane__status">
                  <i />
                  {lane.status}
                </span>
              </div>
            ))}
          </div>
          <div className="lp2-overlap">
            <div className="lp2-overlap__head">
              Overlap in <code>src/api/invoice.ts</code>
            </div>
            <div className="lp2-overlap__track" aria-hidden="true">
              <span className="a" />
              <span className="b" />
              <span className="x" />
            </div>
            <div className="lp2-overlap__legend">
              <span className="a">Maya 12–48</span>
              <span className="x">shared 40–48</span>
              <span className="b">Tomás 40–88</span>
            </div>
          </div>
        </div>
      </div>
    </figure>
  );
}

export default function Landing() {
  const [copied, setCopied] = useState(false);
  const root = useReveal();

  const copyInstall = async () => {
    if (await copyText(INSTALL)) {
      setCopied(true);
      window.setTimeout(() => setCopied(false), 1600);
    }
  };

  return (
    <SiteLayout>
      <div ref={root} className="lp2">
        <section className="lp2-hero site__wrap">
          <HeroNotch />
          <h1 className="lp2-h1 lp2-rise">
            Parallel agents,
            <br />
            <span className="lp2-muted">zero collisions.</span>
          </h1>
          <p className="lp2-lead lp2-rise" style={{ animationDelay: '0.08s' }}>
            Hivelane lets a team run AI coding agents side by side in one repository. Before an agent writes{' '}
            <FileTicker />, it checks whether a teammate&apos;s agent is already in it.
          </p>
          <div className="lp2-actions lp2-rise" style={{ animationDelay: '0.16s' }}>
            <Link to="/start" className="lp2-btn lp2-btn--primary">
              Open the dashboard <span aria-hidden="true">→</span>
            </Link>
            <a href={`${DOCS_URL}quick-start/`} className="lp2-btn">
              Read the quick start
            </a>
          </div>
          <button type="button" className="lp2-install lp2-rise" style={{ animationDelay: '0.24s' }} onClick={() => void copyInstall()}>
            <span aria-hidden="true">$</span>
            <code>{INSTALL}</code>
            <span className="lp2-install__state">{copied ? 'Copied' : 'Copy'}</span>
          </button>
        </section>

        <section className="site__wrap lp2-stage">
          <ProductFrame />
        </section>

        <section className="lp2-agents site__wrap" aria-label="Supported agents" data-reveal>
          <p>Works with the agents your team already uses</p>
          <ul>
            {AGENTS.map((agent) => (
              <li key={agent}>{agent}</li>
            ))}
          </ul>
        </section>

        <section className="site__wrap lp2-section">
          <h2 className="lp2-h2" data-reveal>
            Give every agent
            <br />
            <span className="lp2-muted">the context of the whole team.</span>
          </h2>
          <div className="lp2-reasons">
            {REASONS.map((reason, i) => (
              <p key={reason.title} data-reveal style={{ transitionDelay: `${i * 70}ms` }}>
                <strong>{reason.title}</strong> {reason.body}
              </p>
            ))}
          </div>
        </section>

        <section className="site__wrap lp2-section">
          <div className="lp2-pillars">
            {PILLARS.map((pillar, i) => (
              <article key={pillar.kicker} data-reveal style={{ transitionDelay: `${i * 90}ms` }}>
                <span className="lp2-kicker">
                  {String(i + 1).padStart(2, '0')} / {pillar.kicker}
                </span>
                <h3>{pillar.title}</h3>
                <p>{pillar.body}</p>
              </article>
            ))}
          </div>
        </section>

        <section className="site__wrap lp2-section">
          <h2 className="lp2-h2" data-reveal>
            The bee rides along.
            <br />
            <span className="lp2-muted">No dashboard required.</span>
          </h2>
          <div className="lp2-along">
            <figure className="lp2-term" data-reveal aria-label="A terminal agent with Hivelane in its status line">
              <div className="lp2-term__body">
                <div>
                  <span className="dim">›</span> move invoice totals into a shared helper
                </div>
                <div className="dim">● Update(src/api/invoice.ts)</div>
                <div className="lp2-term__card">
                  <strong>Hivelane · overlap</strong>
                  <span>Tomás (Cursor) is in lines 40–88. Starting with totals.ts instead.</span>
                </div>
              </div>
              <div className="lp2-term__status">
                <span>opus · billing-api</span>
                <span className="warn">
                  <i />⬡ billing-api · 4 agents · 1 overlap
                </span>
              </div>
              <figcaption>Terminal agents: Hivelane in the status line, a short card when something needs you.</figcaption>
            </figure>
            <figure className="lp2-desk" data-reveal style={{ transitionDelay: '90ms' }} aria-label="The bee in the system bar, with its menu open">
              <div className="lp2-desk__screen">
                <div className="lp2-desk__bar" aria-hidden="true">
                  <span className="lp2-desk__glyph" />
                  <span className="lp2-desk__glyph" />
                  <span className="lp2-desk__glyph is-round" />
                  <span className="lp2-desk__bee">
                    <Bee state="blocked" accent="#E5484D" size={20} ring={false} />
                    1 held
                  </span>
                  <span className="lp2-desk__clock">9:41</span>
                </div>
                <div className="lp2-desk__menu" aria-hidden="true">
                  <strong>Hivelane · billing-api · 4 agents</strong>
                  <span>An edit is held on server/routes.py (Maya).</span>
                  <hr />
                  <span className="is-action">Open the dashboard</span>
                  <span className="is-action">Hide the bee</span>
                </div>
                <span className="lp2-desk__edge" aria-hidden="true" />
              </div>
              <figcaption>
                Everywhere else: the bee sits in your menu bar or system tray beside your other background apps, or as a
                colour tab on the edge of the screen. Green, amber, red.
              </figcaption>
            </figure>
          </div>
        </section>

        <section id="how-it-works" className="site__wrap lp2-section">
          <h2 className="lp2-h2" data-reveal>
            Two minutes to set up.
            <br />
            <span className="lp2-muted">Nothing to learn after that.</span>
          </h2>
          <ol className="lp2-steps">
            {STEPS.map((step, index) => (
              <li key={step.title} data-reveal style={{ transitionDelay: `${index * 90}ms` }}>
                <span className="lp2-steps__n">{String(index + 1).padStart(2, '0')}</span>
                <h3>{step.title}</h3>
                <p>{step.body}</p>
              </li>
            ))}
          </ol>
          <div data-reveal>
            <ArchitectureDiagram />
          </div>
          <p className="lp2-more">
            <a href={`${DOCS_URL}architecture/`}>Read the architecture →</a>
          </p>
        </section>

        <section className="site__wrap lp2-cta" data-reveal>
          <Bee state="editing" accent="#0062D1" size={72} ring={false} />
          <h2 className="lp2-h2">Coordinate your team&apos;s agents today.</h2>
          <p className="lp2-lead">Free and open source. Create a room, run one command, invite the team.</p>
          <div className="lp2-actions">
            <Link to="/start" className="lp2-btn lp2-btn--primary">
              Open the dashboard <span aria-hidden="true">→</span>
            </Link>
            <a href={REPO_URL} className="lp2-btn">
              Star on GitHub
            </a>
          </div>
        </section>
      </div>
    </SiteLayout>
  );
}
