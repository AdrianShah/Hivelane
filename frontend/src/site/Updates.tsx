import { useSearchParams } from 'react-router-dom';
import { REPO_URL } from '../links';
import SiteLayout from './SiteLayout';
import { UPDATES, type UpdateKind } from './updates';

const FILTERS: { value: UpdateKind | 'all'; label: string }[] = [
  { value: 'all', label: 'All' },
  { value: 'changelog', label: 'Changelog' },
  { value: 'post', label: 'Posts' },
];

function formatDate(date: string): string {
  return new Date(`${date}T12:00:00Z`).toLocaleDateString('en-US', {
    year: 'numeric',
    month: 'long',
    day: 'numeric',
    timeZone: 'UTC',
  });
}

/** The blog and the changelog in one feed. */
export default function Updates() {
  const [params, setParams] = useSearchParams();
  const raw = params.get('type');
  const filter = FILTERS.some((f) => f.value === raw) ? (raw as UpdateKind | 'all') : 'all';
  const shown = UPDATES.filter((update) => filter === 'all' || update.kind === filter);

  return (
    <SiteLayout title="Updates">
      <section className="site__wrap site-page">
        <header className="site-page__head">
          <span className="site-kicker">Updates</span>
          <h1 className="site-h1 site-h1--sm">What&apos;s new in Hivelane</h1>
          <p className="site-lead">Release notes and the occasional longer post, in one place.</p>
          <div className="site-tabs" role="tablist" aria-label="Filter updates">
            {FILTERS.map((f) => (
              <button
                key={f.value}
                type="button"
                role="tab"
                aria-selected={filter === f.value}
                onClick={() => setParams(f.value === 'all' ? {} : { type: f.value }, { replace: true })}
              >
                {f.label}
              </button>
            ))}
          </div>
        </header>

        <ol className="upd-list">
          {shown.map((update) => (
            <li key={update.slug} id={update.slug} className="upd">
              <div className="upd__meta">
                <time dateTime={update.date}>{formatDate(update.date)}</time>
                <span className={`upd__tag upd__tag--${update.kind}`}>
                  {update.kind === 'post' ? 'Post' : 'Changelog'}
                </span>
              </div>
              <div className="upd__body">
                <h2>
                  <a href={`#${update.slug}`}>{update.title}</a>
                </h2>
                <p>{update.summary}</p>
                {update.items ? (
                  <ul>
                    {update.items.map((item) => (
                      <li key={item}>{item}</li>
                    ))}
                  </ul>
                ) : null}
                {update.prs?.length ? (
                  <p className="upd__prs">
                    {update.prs.map((pr) => (
                      <a key={pr} href={`${REPO_URL}/pull/${pr}`}>
                        #{pr}
                      </a>
                    ))}
                  </p>
                ) : null}
              </div>
            </li>
          ))}
        </ol>
      </section>
    </SiteLayout>
  );
}
