import { useState } from 'react';
import { Link } from 'react-router-dom';
import { Mascot } from '../brand/BrandArt';
import { copyText } from '../clipboard';
import { DOCS_URL, REPO_URL } from '../links';
import LanePreview from './LanePreview';
import SiteLayout from './SiteLayout';

const INSTALL = 'pipx install git+https://github.com/AdrianShah/Hivelane && hivelane up';

const AGENTS = ['Claude Code', 'Codex', 'Cursor', 'Gemini CLI', 'VS Code', 'Windsurf', 'Any CLI via hivelane run'];

const REASONS = [
  {
    title: 'Checked before every write.',
    body: 'Agent hooks ask the relay before each edit. The answer is allow, warn or block, down to the function or line range a teammate is in.',
  },
  {
    title: 'Every lane, live.',
    body: "Each agent gets a lane with its intent, prompts, AI captions and diffs, so you can read what your teammates' agents are doing without asking.",
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

export default function Landing() {
  const [copied, setCopied] = useState(false);

  const copyInstall = async () => {
    if (await copyText(INSTALL)) {
      setCopied(true);
      window.setTimeout(() => setCopied(false), 1600);
    }
  };

  return (
    <SiteLayout>
      <section className="lp-hero site__wrap">
        <div className="lp-hero__copy">
          <span className="site-pill">Open source · Free during alpha</span>
          <h1 className="site-h1">
            Parallel agents,
            <br />
            <em>zero collisions.</em>
          </h1>
          <p className="site-lead">
            Hivelane lets a team run AI coding agents side by side in one repository. Before any agent writes a
            file, it checks whether a teammate&apos;s agent is already in it.
          </p>
          <div className="site-actions">
            <Link to="/start" className="site-btn site-btn--primary">
              Open the dashboard <span aria-hidden="true">→</span>
            </Link>
            <a href={`${DOCS_URL}quick-start/`} className="site-btn">
              Read the quick start
            </a>
          </div>
          <button type="button" className="lp-install" onClick={() => void copyInstall()} title="Copy">
            <span aria-hidden="true">$</span>
            <code>{INSTALL}</code>
            <span className="lp-install__state">{copied ? 'Copied' : 'Copy'}</span>
          </button>
        </div>
        <LanePreview />
      </section>

      <section className="lp-agents site__wrap" aria-label="Supported agents">
        <p>Works with the agents your team already uses</p>
        <ul>
          {AGENTS.map((agent) => (
            <li key={agent}>{agent}</li>
          ))}
        </ul>
      </section>

      <section className="site__wrap lp-section">
        <h2 className="site-h2">
          Give every agent
          <br />
          <span className="site-muted">the context of the whole team.</span>
        </h2>
        <div className="lp-reasons">
          {REASONS.map((reason) => (
            <p key={reason.title}>
              <strong>{reason.title}</strong> {reason.body}
            </p>
          ))}
        </div>
      </section>

      <section className="site__wrap lp-section">
        <div className="lp-pillars">
          {PILLARS.map((pillar) => (
            <article key={pillar.kicker} className="site-card">
              <span className="site-kicker">{pillar.kicker}</span>
              <h3>{pillar.title}</h3>
              <p>{pillar.body}</p>
            </article>
          ))}
        </div>
      </section>

      <section id="how-it-works" className="site__wrap lp-section">
        <h2 className="site-h2">
          Two minutes to set up.
          <br />
          <span className="site-muted">Nothing to learn after that.</span>
        </h2>
        <ol className="lp-steps">
          {STEPS.map((step, index) => (
            <li key={step.title}>
              <span className="lp-steps__n">{String(index + 1).padStart(2, '0')}</span>
              <h3>{step.title}</h3>
              <p>{step.body}</p>
            </li>
          ))}
        </ol>
        <div className="lp-flow" aria-label="How a check travels">
          <div>
            <strong>Agent hook</strong>
            <span>before each edit</span>
          </div>
          <i aria-hidden="true" />
          <div>
            <strong>Daemon</strong>
            <span>one per laptop</span>
          </div>
          <i aria-hidden="true" />
          <div>
            <strong>Relay</strong>
            <span>allow · warn · block</span>
          </div>
          <i aria-hidden="true" />
          <div>
            <strong>Dashboard</strong>
            <span>live lanes</span>
          </div>
        </div>
        <p className="lp-flow__more">
          <a href={`${DOCS_URL}architecture/`}>Read the architecture →</a>
        </p>
      </section>

      <section className="site__wrap lp-section">
        <div className="lp-split site-card">
          <div>
            <span className="site-kicker">Yours to run</span>
            <h2 className="site-h2 site-h2--sm">Host the relay yourself, or don&apos;t.</h2>
            <p className="site-lead site-lead--sm">
              The relay is one small process in a container, with ready-made Docker Compose, Fly.io, Render and
              Helm setups. Run it inside your network and nothing about your code leaves it.
            </p>
            <div className="site-actions">
              <a href={`${DOCS_URL}self-hosting/`} className="site-btn">
                Self-hosting guide
              </a>
              <a href={`${DOCS_URL}security/`} className="site-btn site-btn--ghost">
                Security model →
              </a>
            </div>
          </div>
          <ul className="lp-checks">
            <li>No sign-up, no accounts</li>
            <li>Optional end-to-end encryption per room</li>
            <li>Secrets and ignored files redacted on your machine</li>
            <li>Room data removed after seven days</li>
            <li>Apache-2.0, every line on GitHub</li>
          </ul>
        </div>
      </section>

      <section className="site__wrap lp-cta">
        <Mascot pose="clear" size={72} />
        <h2 className="site-h2">Coordinate your team&apos;s agents today.</h2>
        <p className="site-lead">Free and open source. Create a room, run one command, invite the team.</p>
        <div className="site-actions site-actions--center">
          <Link to="/start" className="site-btn site-btn--primary">
            Open the dashboard <span aria-hidden="true">→</span>
          </Link>
          <a href={REPO_URL} className="site-btn">
            Star on GitHub
          </a>
        </div>
      </section>
    </SiteLayout>
  );
}
