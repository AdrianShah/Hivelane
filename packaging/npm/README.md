# synergo-cli

A zero-dependency npm launcher for [Synergo](https://github.com/AdrianShah/Synergo),
the live coordination layer for parallel AI coding agents. Synergo itself is a
Python package; this shim lets you reach it from `npx`.

```bash
npx synergo-cli --version
npx synergo-cli up
```

## How it works

`bin/synergo.js` runs the first of these that is installed, passing your
arguments, stdio and exit code straight through:

1. `uvx --from synergo synergo …`
2. `uv tool run --from synergo synergo …`
3. `pipx run --spec synergo synergo …`

If none is available it prints how to install uv and exits 1. Set
`SYNERGO_VERSION=1.2.3` to pin the Python package version.

## For everyday use, install Synergo itself

Agent hooks call `synergo` directly from your PATH, and a runner like `uvx`
does not put it there. Use this package to try Synergo or run one-off commands;
for a repository you work in every day, install it persistently:

```bash
uv tool install synergo     # or: pipx install synergo
```

See [docs/install.md](https://github.com/AdrianShah/Synergo/blob/main/docs/install.md)
for every install channel.

## Publishing (maintainers)

The launcher always runs the latest PyPI release, so it only needs republishing
when `bin/synergo.js` changes:

```bash
cd packaging/npm
npm publish --dry-run   # check the file list: bin/synergo.js, README.md, package.json
npm publish
```
