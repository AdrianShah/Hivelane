#!/usr/bin/env node
// npm launcher for the Hivelane CLI. Hivelane itself is a Python package on PyPI;
// this hands off to `uvx` (preferred: a cached, isolated environment), then
// `uv tool run`, then `pipx run`, forwarding arguments, stdio and the exit code.
// Zero dependencies, so `npx hivelane-cli ...` costs one tiny download.
//
// HIVELANE_VERSION=1.2.3 pins the Python package version; unset runs the latest.
"use strict";

const { spawn } = require("node:child_process");

const pin = (process.env.HIVELANE_VERSION || "").trim();
const spec = pin ? `hivelane==${pin}` : "hivelane";
const args = process.argv.slice(2);

// Tried in order; a runner that is not installed (ENOENT) falls through to the
// next. uv and pipx ship as real executables on every OS, so no shell is needed
// and arguments are passed through without any re-quoting.
const RUNNERS = [
  ["uvx", ["--from", spec, "hivelane", ...args]],
  ["uv", ["tool", "run", "--from", spec, "hivelane", ...args]],
  ["pipx", ["run", "--spec", spec, "hivelane", ...args]],
];

const INSTALL_HELP = `hivelane: this launcher needs uv or pipx to run the Hivelane CLI (a Python package).

Install uv (recommended), then run your command again:
  macOS / Linux:  curl -LsSf https://astral.sh/uv/install.sh | sh
  Windows:        powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"

Or install Hivelane itself, which agent hooks need on your PATH anyway:
  uv tool install hivelane        (or: pipx install hivelane)

More: https://github.com/AdrianShah/Hivelane/blob/main/docs/install.md
`;

let current = null;

// Ctrl+C reaches the child directly (same terminal / process group); the
// launcher only has to outlive it so it can report the child's exit status.
process.on("SIGINT", () => {});
if (process.platform !== "win32") {
  // Signals aimed at the launcher alone (a supervisor, `kill`) go to the child.
  for (const signal of ["SIGTERM", "SIGHUP"]) {
    process.on(signal, () => {
      if (current) current.kill(signal);
    });
  }
}

function run(index) {
  if (index >= RUNNERS.length) {
    process.stderr.write(INSTALL_HELP);
    process.exit(1);
  }
  const [command, argv] = RUNNERS[index];
  let started = false;
  const child = spawn(command, argv, { stdio: "inherit" });
  current = child;

  child.on("spawn", () => {
    started = true;
  });
  child.on("error", (err) => {
    if (!started && err.code === "ENOENT") {
      run(index + 1);
      return;
    }
    process.stderr.write(`hivelane: could not run ${command}: ${err.message}\n`);
    process.exit(1);
  });
  child.on("exit", (code, signal) => {
    // 'exit' can follow a spawn failure; that case is handled by 'error'.
    if (!started) return;
    if (signal) {
      // Die the same way the child did, so callers see the real cause.
      process.removeAllListeners(signal);
      process.kill(process.pid, signal);
      return;
    }
    process.exit(code === null ? 1 : code);
  });
}

run(0);
