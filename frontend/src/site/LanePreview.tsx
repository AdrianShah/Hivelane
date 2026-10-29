import { BeeAvatar } from '../brand/BrandArt';
import { personColor } from '../dashboard/people';
import '../pages/Home.css';

/** Illustrative preview of the dashboard: two lanes colliding on one file. */
export default function LanePreview() {
  return (
    <figure className="home__preview" aria-label="Example: two agents editing the same file">
      <div className="home__lane">
        <div className="home__lane-head">
          <BeeAvatar className="home__avatar" state="editing" accent={personColor('Maya R.')} />
          <div>
            <strong>Maya R.</strong>
            <span>Claude Code · feat/billing</span>
          </div>
          <span className="home__live">live</span>
        </div>
        <p className="home__intent">Moving invoice totals into a shared helper</p>
        <ul className="home__files">
          <li>
            src/billing/totals.ts <b>+42 −8</b>
          </li>
          <li className="is-hot">
            src/api/invoice.ts <b>+6 −3</b>
          </li>
        </ul>
      </div>
      <div className="home__lane home__lane--offset">
        <div className="home__lane-head">
          <BeeAvatar className="home__avatar" state="conflict" accent={personColor('Tomás O.')} />
          <div>
            <strong>Tomás O.</strong>
            <span>Codex · fix/rounding</span>
          </div>
          <span className="home__live">live</span>
        </div>
        <p className="home__intent">Rounding tax per line instead of per invoice</p>
        <ul className="home__files">
          <li className="is-hot">
            src/api/invoice.ts <b>+11 −4</b>
          </li>
        </ul>
      </div>
      <figcaption className="home__alert">
        Both agents are editing <code>src/api/invoice.ts</code>
      </figcaption>
    </figure>
  );
}
