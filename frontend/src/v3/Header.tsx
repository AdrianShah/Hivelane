import { useEffect, useRef, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { Mark } from '../brand/BrandArt';
import Bee from '../components/Bee';
import Icon from '../components/Icon';
import { ownedRooms } from '../roomAccess';
import Notch from './Notch';
import type { V3Model } from './model';

const AVATARS_SHOWN = 4;

/** The room code with a menu: switch to another room you own, copy, new, leave. */
function RoomMenu({ model }: { model: V3Model }) {
  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLDivElement>(null);
  const [rooms, setRooms] = useState<string[]>([]);
  const { actions } = model;

  useEffect(() => {
    if (!open) return;
    const away = (event: PointerEvent) => {
      if (ref.current && !ref.current.contains(event.target as Node)) setOpen(false);
    };
    const esc = (event: KeyboardEvent) => {
      if (event.key === 'Escape') setOpen(false);
    };
    window.addEventListener('pointerdown', away);
    window.addEventListener('keydown', esc);
    return () => {
      window.removeEventListener('pointerdown', away);
      window.removeEventListener('keydown', esc);
    };
  }, [open]);

  const close = (run: () => void) => () => {
    setOpen(false);
    run();
  };
  const others = rooms.filter((code) => code !== model.roomId);

  return (
    <div ref={ref} className="relative">
      <button
        type="button"
        className="v3-room-btn"
        aria-haspopup="menu"
        aria-expanded={open}
        onClick={() => {
          setRooms(ownedRooms());
          setOpen((o) => !o);
        }}
      >
        {model.settings?.e2e ? <Icon name="lock" size={13} label="End-to-end encrypted" /> : null}
        {model.roomId}
        <Icon name="chevron-down" size={13} />
      </button>
      {open ? (
        <div className="v3-menu" role="menu" style={{ top: 36, left: 0 }}>
          <p className="v3-menu__label">This room{model.isOwner ? ' · you own it' : ''}</p>
          <button type="button" role="menuitem" className="v3-menu__item" onClick={close(actions.copyLink)}>
            <Icon name="link" size={15} /> Copy room link
          </button>
          <button type="button" role="menuitem" className="v3-menu__item" onClick={close(actions.openInvite)}>
            <Icon name="invite" size={15} /> Invite teammates
          </button>
          {others.length ? (
            <>
              <div className="v3-menu__sep" />
              <p className="v3-menu__label">Your other rooms</p>
              {others.map((code) => (
                <button
                  key={code}
                  type="button"
                  role="menuitem"
                  className="v3-menu__item v3-mono"
                  onClick={close(() => actions.switchRoom(code))}
                >
                  <span className="v3-dot is-idle" aria-hidden="true" />
                  {code}
                </button>
              ))}
            </>
          ) : null}
          <div className="v3-menu__sep" />
          <button type="button" role="menuitem" className="v3-menu__item" onClick={close(actions.newRoom)}>
            <Icon name="plus" size={15} /> New room
          </button>
          <button type="button" role="menuitem" className="v3-menu__item" onClick={close(actions.leaveRoom)}>
            <Icon name="logout" size={15} /> Leave room
          </button>
        </div>
      ) : null}
    </div>
  );
}

export default function Header({ model }: { model: V3Model }) {
  const navigate = useNavigate();
  const online = model.lanes.filter((lane) => lane.offlineSince == null);
  const shown = online.slice(0, AVATARS_SHOWN);
  const isMac = typeof navigator !== 'undefined' && /mac|iphone|ipad/i.test(navigator.platform || navigator.userAgent);

  return (
    <header className="v3-header">
      <div className="v3-crumb">
        <button
          type="button"
          className="v3-icon-btn"
          style={{ width: 34, height: 34 }}
          onClick={() => navigate('/')}
          title="Hivelane home"
          aria-label="Hivelane home"
        >
          <Mark size={26} />
        </button>
        <span className="v3-crumb__slash" aria-hidden="true">
          /
        </span>
        <RoomMenu model={model} />
      </div>

      <Notch model={model} />

      <div className="v3-header__right">
        <button type="button" className="v3-search-btn" onClick={model.actions.openCommand} aria-label="Open the command menu">
          <Icon name="search" size={15} />
          <span className="v3-search-btn__text">Search or jump to</span>
          <span className="v3-kbd" style={{ marginLeft: 'auto' }}>
            {isMac ? '⌘K' : 'Ctrl K'}
          </span>
        </button>
        {online.length ? (
          <button
            type="button"
            className="v3-avatars"
            onClick={() => model.actions.navigate('agents')}
            aria-label={`${online.length} agents online: ${online.map((l) => l.id).join(', ')}. Open the feed`}
            title={online.map((l) => l.id).join(', ')}
          >
            {shown.map((lane) => (
              <Bee key={lane.id} state={lane.state} accent={lane.color} size={28} ring={false} />
            ))}
            {online.length > AVATARS_SHOWN ? (
              <span className="v3-avatars__more">+{online.length - AVATARS_SHOWN}</span>
            ) : null}
          </button>
        ) : null}
        <button type="button" className="v3-btn v3-btn--primary" onClick={model.actions.openInvite}>
          <Icon name="invite" size={15} />
          <span className="hidden sm:inline">Invite</span>
        </button>
      </div>
    </header>
  );
}
