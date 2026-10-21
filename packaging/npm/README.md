# hivelane-cli

A zero-dependency npm launcher for [Hivelane](https://github.com/AdrianShah/Hivelane),
the live coordination layer for parallel AI coding agents. Hivelane itself is a
Python package; this shim lets you reach it from `npx`.

```bash
npx hivelane-cli --version
npx hivelane-cli up
```

## How it works

`bin/hivelane.js` runs the first of these that is installed, passing your
arguments, stdio and exit code straight through:

1. `uvx --from hivelane hivelane …`
2. `uv tool run --from hivelane hivelane …`
3. `pipx run --spec hivelane hivelane …`

If none is available it prints how to install uv and exits 1. Set
`HIVELANE_VERSION=1.2.3` to pin the Python package version.

## For everyday use, install Hivelane itself

Agent hooks call `hivelane` directly from your PATH, and a runner like `uvx`
does not put it there. Use this package to try Hivelane or run one-off commands;
for a repository you work in every day, install it persistently:

```bash
uv tool install hivelane     # or: pipx install hivelane
```

See [docs/install.md](https://github.com/AdrianShah/Hivelane/blob/main/docs/install.md)
for every install channel.

## Publishing (maintainers)

The launcher always runs the latest PyPI release, so it only needs republishing
when `bin/hivelane.js` changes:

```bash
cd packaging/npm
npm publish --dry-run   # check the file list: bin/hivelane.js, README.md, package.json
npm publish
```
