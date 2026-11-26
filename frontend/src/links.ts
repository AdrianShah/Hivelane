import {
  INSTALL_COMMAND,
  RELAY_PARAM,
  basePath,
  invitePath,
  needsInsecure,
  normalizeRoomCode,
  relayIsThisOrigin,
  relayWsBase,
} from './relayUrl.ts';

/** Public documentation site (built from docs-site/). */
export const DOCS_URL = 'https://adrianshah.github.io/Hivelane/docs/';

/** The relay's HTTP and WebSocket API, documented on the docs site. The relay's own /docs is off in production. */
export const API_REFERENCE_URL = `${DOCS_URL}protocol/`;

/** Source repository; also where issues and contact go until there is a mailbox. */
export const REPO_URL = 'https://github.com/AdrianShah/Hivelane';

/** What `hivelane up` connects to with no --relay (hivelane/config.py DEFAULT_RELAY). */
const CLI_DEFAULT_RELAY = 'ws://localhost:8000/ws';

/** Where this page is served and which relay it talks to: all a share link depends on. */
export interface ShareContext {
  /** window.location.origin */
  origin: string;
  /** basePath(): '' or '/Hivelane' */
  base: string;
  /** relayWsBase() */
  relayWs: string;
  /** The relay is the server this page came from, so the link needs no ?relay=. */
  relayIsOrigin: boolean;
}

export function currentShareContext(): ShareContext {
  return {
    origin: typeof window === 'undefined' ? '' : window.location.origin,
    base: basePath(),
    relayWs: relayWsBase(),
    relayIsOrigin: relayIsThisOrigin(),
  };
}

/**
 * The link teammates get to join a room: the /j/<CODE> invite page, with
 * ?relay= unless this page's own server is the relay. Every share surface
 * (the start page, the invite sheet, the setup guide, the agent prompt)
 * builds it here and only here, mirroring hivelane/links.py share_link, so
 * what a share link carries can change in one place. Never carries an e2e
 * room key: that goes to teammates separately.
 */
export function shareLinkFor(roomId: string, ctx: ShareContext): string {
  const link = `${ctx.origin}${ctx.base}${invitePath(roomId)}`;
  if (ctx.relayIsOrigin) return link;
  return `${link}?${RELAY_PARAM}=${encodeURIComponent(ctx.relayWs)}`;
}

export function shareLink(roomId: string): string {
  return shareLinkFor(roomId, currentShareContext());
}

/**
 * The sentence a person pastes into any coding agent (Claude Code, Cursor,
 * Codex, Copilot, Gemini…) to bring the repo it works in into the room.
 * Mirrors hivelane/links.py agent_join_prompt, which `hivelane up` prints.
 * The link is quoted: `?` is a glob character in zsh.
 */
export function agentPromptFor(roomId: string, link: string, relayWs: string): string {
  const insecure = needsInsecure(relayWs) ? ' --insecure' : '';
  return (
    `Join Hivelane room ${normalizeRoomCode(roomId)} in this repo: install hivelane if needed ` +
    `(\`${INSTALL_COMMAND}\`), then run \`hivelane up --room "${link}" -y${insecure}\`.`
  );
}

export function agentPrompt(roomId: string): string {
  const ctx = currentShareContext();
  return agentPromptFor(roomId, shareLinkFor(roomId, ctx), ctx.relayWs);
}

/**
 * `.hivelane.toml` for the repo: committed, every clone joins this room with
 * a plain `hivelane up` (and the agent-instructions section `up` adds tells
 * agents to run it). The relay is spelled out unless it is the CLI default.
 */
export function projectFileFor(roomId: string, relayWs: string): string {
  const lines = [`room = "${normalizeRoomCode(roomId)}"`];
  if (relayWs !== CLI_DEFAULT_RELAY) lines.push(`relay = "${relayWs}"`);
  if (needsInsecure(relayWs)) lines.push('insecure = true');
  return `${lines.join('\n')}\n`;
}

export function projectFile(roomId: string): string {
  return projectFileFor(roomId, relayWsBase());
}
