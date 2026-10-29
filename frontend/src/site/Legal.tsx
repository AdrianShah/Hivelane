import type { ReactNode } from 'react';
import { DOCS_URL, REPO_URL } from '../links';
import SiteLayout from './SiteLayout';

const UPDATED = 'October 29, 2026';

function LegalPage({ title, children }: { title: string; children: ReactNode }) {
  return (
    <SiteLayout title={title}>
      <section className="site__wrap site-page site-prose">
        <header className="site-page__head">
          <span className="site-kicker">Legal</span>
          <h1 className="site-h1 site-h1--sm">{title}</h1>
          <p className="site-muted">Last updated {UPDATED}</p>
        </header>
        {children}
      </section>
    </SiteLayout>
  );
}

export function Privacy() {
  return (
    <LegalPage title="Privacy">
      <p>
        Hivelane is built to know as little about you as possible. This page covers the hosted dashboard and the
        alpha relay run by the project. A relay you host yourself is covered by your own policies, not this one.
      </p>
      <h2>What we collect</h2>
      <ul>
        <li>
          <strong>No accounts.</strong> There is no sign-up, so we hold no names, emails or passwords.
        </li>
        <li>
          <strong>Room data.</strong> While a room is active the relay relays what members&apos; daemons send: agent
          intents, file paths, prompts, captions and diffs. In an end-to-end encrypted room it sees only hashes and
          ciphertext.
        </li>
        <li>
          <strong>Room records.</strong> A room&apos;s code, creation time, settings and any webhooks its owner adds.
          These are removed when the room expires, seven days after it is created.
        </li>
        <li>
          <strong>Operational logs.</strong> Standard request logs and aggregate metrics, used to keep the service
          running and to stop abuse.
        </li>
      </ul>
      <h2>What we don&apos;t do</h2>
      <ul>
        <li>We don&apos;t sell or share data with advertisers.</li>
        <li>We don&apos;t use your code or prompts to train models.</li>
        <li>The dashboard uses no analytics or tracking cookies; it keeps preferences in your browser only.</li>
      </ul>
      <h2>AI features</h2>
      <p>
        Captions and conflict classification are optional. When the relay operator turns them on, diffs from rooms
        without end-to-end encryption are sent to the configured model provider to produce them.
      </p>
      <p>
        The full list of what the relay stores and for how long is in the{' '}
        <a href={`${DOCS_URL}security/`}>security docs</a>. Questions go to{' '}
        <a href={`${REPO_URL}/issues`}>GitHub issues</a>.
      </p>
    </LegalPage>
  );
}

export function Terms() {
  return (
    <LegalPage title="Terms">
      <p>
        Hivelane is open-source software licensed under the{' '}
        <a href={`${REPO_URL}/blob/main/LICENSE`}>Apache License 2.0</a>. These terms cover using the hosted
        dashboard and the project&apos;s alpha relay.
      </p>
      <h2>Alpha service</h2>
      <p>
        The hosted service is an early preview, provided as is, without warranties of any kind. It can change, go
        down or lose room data at any time. Don&apos;t depend on it for anything you can&apos;t afford to lose; host
        your own relay if you need guarantees.
      </p>
      <h2>Acceptable use</h2>
      <ul>
        <li>Don&apos;t send content you don&apos;t have the right to share with your room.</li>
        <li>Don&apos;t try to break into rooms that aren&apos;t yours, or overload the relay.</li>
        <li>Don&apos;t use Hivelane to break the law or the terms of the AI tools it connects to.</li>
      </ul>
      <p>We may remove rooms or block clients that break these rules.</p>
      <h2>Liability</h2>
      <p>
        To the extent the law allows, the Hivelane contributors are not liable for any damages arising from using
        the software or the hosted service.
      </p>
    </LegalPage>
  );
}

export function Security() {
  return (
    <LegalPage title="Security">
      <p>
        Hivelane sits in the path of every edit an AI coding agent makes, so we treat security reports as the
        highest-priority work in the project.
      </p>
      <h2>How Hivelane protects your code</h2>
      <ul>
        <li>Secrets and ignored files are redacted by the daemon before anything leaves your machine.</li>
        <li>Optional end-to-end encryption: the relay routes ciphertext it cannot read.</li>
        <li>Owner tokens, join approval and per-room quotas guard who can see a room.</li>
        <li>The relay can run entirely inside your own network.</li>
      </ul>
      <p>
        The full threat model is in the <a href={`${DOCS_URL}security/`}>security docs</a>.
      </p>
      <h2>Reporting a vulnerability</h2>
      <p>
        <strong>Please don&apos;t open a public issue.</strong> Email the maintainer at the address on their{' '}
        <a href="https://github.com/AdrianShah">GitHub profile</a> with &ldquo;Hivelane security&rdquo; in the
        subject. Include the component, version and steps to reproduce. Once the repository is public you can also
        use GitHub&apos;s <a href={`${REPO_URL}/security/advisories/new`}>private vulnerability reporting</a>.
      </p>
      <p>
        Full policy: <a href={`${REPO_URL}/blob/main/SECURITY.md`}>SECURITY.md</a>.
      </p>
    </LegalPage>
  );
}
