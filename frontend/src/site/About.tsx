import { Link } from 'react-router-dom';
import { Mascot } from '../brand/BrandArt';
import { DOCS_URL, REPO_URL } from '../links';
import SiteLayout from './SiteLayout';

export default function About() {
  return (
    <SiteLayout title="About">
      <section className="site__wrap site-page site-prose">
        <header className="site-page__head">
          <Mascot pose="clear" size={64} />
          <span className="site-kicker">About</span>
          <h1 className="site-h1 site-h1--sm">Built for teams that code with agents.</h1>
        </header>

        <p>
          One developer running one AI agent is easy to reason about. A team of five, each running two or three
          agents in the same repository, is not. The agents cannot see each other, so they quietly rewrite the same
          functions, and the collision only shows up as a merge conflict hours later.
        </p>
        <p>
          Hivelane gives every agent the missing piece: a live picture of what the rest of the team is touching. It
          sits in the agent&apos;s own hooks, asks before every write, and answers allow, warn or block. People see
          the same picture on the dashboard.
        </p>

        <h2>What we believe</h2>
        <ul>
          <li>
            <strong>Coordination, not control.</strong> Hivelane warns by default and fails open. It should never be
            the reason an agent stalls.
          </li>
          <li>
            <strong>Your code stays yours.</strong> No accounts, short-lived rooms, redaction on your machine,
            optional end-to-end encryption and a relay you can host yourself.
          </li>
          <li>
            <strong>Open by default.</strong> The CLI, the relay, the dashboard and the editor extension are all
            Apache-2.0.
          </li>
        </ul>

        <h2>Where we are</h2>
        <p>
          Hivelane is in alpha, used by a small group of teams. It started life as Synergo and was renamed in October
          2026. Follow along on the <Link to="/updates">updates</Link> page.
        </p>

        <h2 id="contact">Contact</h2>
        <p>
          Questions, bugs and ideas go to{' '}
          <a href={`${REPO_URL}/issues`}>GitHub issues</a>. Security reports should stay private: see the{' '}
          <Link to="/security">security page</Link> for how to send one. For anything else, the maintainer&apos;s
          address is on their <a href="https://github.com/AdrianShah">GitHub profile</a>.
        </p>
        <p>
          Want to contribute? Start with the <a href={`${REPO_URL}/blob/main/CONTRIBUTING.md`}>contributing guide</a>{' '}
          and the <a href={`${DOCS_URL}architecture/`}>architecture overview</a>.
        </p>
      </section>
    </SiteLayout>
  );
}
