import { SCOPE_LABEL, type ScopeQuality } from '../detection';

/** Per-scope useful vs noise votes seen by this dashboard this session. */
export default function DetectionQuality({ rows }: { rows: ScopeQuality[] }) {
  const totalVotes = rows.reduce((sum, r) => sum + r.useful + r.noise, 0);
  return (
    <section className="lane-shell rounded-2xl p-5 mb-6" aria-labelledby="dq-title">
      <h2 id="dq-title" className="section-label m-0">
        Detection quality
      </h2>
      <p className="text-[12px] text-on-muted mt-1 mb-4">
        Useful vs noise votes on warn/block decisions this session, by scope.
        {totalVotes === 0 ? ' No votes yet — rate decisions in the agent lanes.' : ''}
      </p>
      <table className="w-full text-left border-collapse text-[12.5px]">
        <thead>
          <tr className="text-[10px] uppercase tracking-[0.12em] text-on-dim">
            <th className="py-1.5 font-bold">Scope</th>
            <th className="py-1.5 font-bold text-right">Decisions</th>
            <th className="py-1.5 font-bold text-right">Useful</th>
            <th className="py-1.5 font-bold text-right">Noise</th>
            <th className="py-1.5 font-bold pl-4 w-[40%]">Share useful</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => {
            const votes = row.useful + row.noise;
            const pct = votes ? Math.round((row.useful / votes) * 100) : null;
            return (
              <tr key={row.scope} className="border-t border-outline-soft/50">
                <td className="py-2 text-on-surface">{SCOPE_LABEL[row.scope]}</td>
                <td className="py-2 text-right font-mono tabular-nums text-on-muted">
                  {row.decisions}
                </td>
                <td className="py-2 text-right font-mono tabular-nums text-emerald">
                  {row.useful}
                </td>
                <td className="py-2 text-right font-mono tabular-nums text-error">
                  {row.noise}
                </td>
                <td className="py-2 pl-4">
                  {pct == null ? (
                    <span className="text-on-dim">—</span>
                  ) : (
                    <span className="flex items-center gap-2">
                      <span
                        className="quality-bar"
                        role="img"
                        aria-label={`${pct}% useful`}
                      >
                        <span style={{ width: `${pct}%` }} />
                      </span>
                      <span className="font-mono tabular-nums text-on-muted">{pct}%</span>
                    </span>
                  )}
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </section>
  );
}
