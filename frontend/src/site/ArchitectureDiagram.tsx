/**
 * How one pre-edit check travels: each laptop's agent hook asks its daemon,
 * the daemon asks the relay, the relay answers and fans the event out to the
 * dashboard. Plain HTML/CSS so it reflows to a vertical stack on phones.
 */
const LAPTOPS = [
  { owner: "Maya's laptop", agent: 'Claude Code' },
  { owner: "Tomás's laptop", agent: 'Codex' },
];

export default function ArchitectureDiagram() {
  return (
    <figure className="arch" aria-label="Agent hooks talk to a daemon on each laptop, daemons talk to the relay, the relay feeds the dashboard">
      <div className="arch__col arch__laptops">
        {LAPTOPS.map((laptop) => (
          <div key={laptop.owner} className="arch__box arch__laptop">
            <span className="arch__label">{laptop.owner}</span>
            <div className="arch__chain">
              <span className="arch__chip">{laptop.agent}</span>
              <span className="arch__mini" aria-hidden="true">→</span>
              <span className="arch__chip">hook</span>
              <span className="arch__mini" aria-hidden="true">→</span>
              <span className="arch__chip arch__chip--strong">daemon</span>
            </div>
          </div>
        ))}
      </div>

      <div className="arch__link" aria-hidden="true">
        <span className="arch__tag">edit_check</span>
        <span className="arch__wire">
          <i className="arch__pulse" />
          <i className="arch__pulse arch__pulse--back" />
        </span>
        <span className="arch__tag arch__tag--answer">allow · warn · block</span>
      </div>

      <div className="arch__col">
        <div className="arch__box arch__relay">
          <span className="arch__label">Relay</span>
          <strong>Room</strong>
          <ul>
            <li>who is editing what</li>
            <li>overlap check</li>
            <li>optional e2e</li>
          </ul>
        </div>
      </div>

      <div className="arch__link" aria-hidden="true">
        <span className="arch__tag">live feed</span>
        <span className="arch__wire">
          <i className="arch__pulse arch__pulse--slow" />
        </span>
        <span className="arch__tag arch__tag--ghost">WebSocket</span>
      </div>

      <div className="arch__col">
        <div className="arch__box arch__dash">
          <span className="arch__label">Dashboard</span>
          <div className="arch__lanes">
            <span />
            <span className="is-hot" />
            <span />
          </div>
          <small>one lane per agent</small>
        </div>
      </div>
    </figure>
  );
}
