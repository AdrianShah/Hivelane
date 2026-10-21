# Alpha testing (Adrian and Hossein)

Hivelane is not on PyPI yet, so each tester installs it straight from GitHub.
Everything runs against the hosted relay on Render and the dashboard on GitHub
Pages:

- Dashboard: <https://adrianshah.github.io/Hivelane/>
- Relay: `wss://hivelane.onrender.com/ws`. Its health check is
  <https://hivelane.onrender.com/readyz>.

## 1. Install (each person, once)

You need Python 3.12+ and [pipx](https://pipx.pypa.io/).

```bash
pipx install git+https://github.com/AdrianShah/Hivelane
hivelane --version
```

To update later, run `pipx install --force git+https://github.com/AdrianShah/Hivelane`.

## 2. Pick a repo to test in

Both of you need a clone of the **same** git repository with at least one
coding agent installed: Claude Code, Codex, Cursor or Gemini CLI. A small
throwaway repo is best for the first run.

## 3. One person creates the room

In the test repo:

```bash
hivelane up --relay wss://hivelane.onrender.com/ws
```

This installs the agent hooks, creates a room, starts the background daemon
and prints the room's dashboard link. It also writes `.hivelane.toml` with
the room code and relay. Commit and push that file.

## 4. The other person joins

Pull the repo so `.hivelane.toml` is there, then run:

```bash
hivelane up
```

It reads the room and relay from `.hivelane.toml`. Without pulling, use
`hivelane up --room CODE --relay wss://hivelane.onrender.com/ws`
instead.

## 5. Try a conflict

1. Both of you open the dashboard link (`hivelane open`) and check that
   both lanes show up.
2. Ask your agents to edit the **same file** within a few seconds of each
   other. The second agent should be told someone else is editing it, and
   held for up to about 20 seconds. The dashboard shows the conflict.
3. Edit **different files**. Nothing should be blocked.

## When something is off

- `hivelane doctor` explains what is wrong with the local setup.
- `hivelane status` shows the daemon and whether the repo is connected.
- `hivelane down` stops the daemon. `hivelane up` starts it again.
- **Relay waking up:** the relay runs on Render's free plan, which sleeps
  after about 15 minutes with no traffic. The first connection after that
  takes up to a minute while it wakes; the daemon keeps retrying on its own.
  A restart also clears every room, so run `hivelane up` again to create a
  fresh one.
- **Reporting a problem:** open an issue on the repo with the output of
  `hivelane doctor` and what the agents were doing.
