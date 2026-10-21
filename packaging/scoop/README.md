# Scoop bucket

`synergo.json` is the manifest template for a Scoop bucket. Users install with:

```powershell
scoop bucket add synergo https://github.com/AdrianShah/scoop-synergo
scoop install synergo/synergo
```

## How the manifest works

- `url` is the wheel on PyPI, so Scoop checks its sha256 before anything runs.
- `installer.script` runs `uv tool install` on that wheel (`depends: uv` pulls uv
  from the main bucket), falling back to `pipx install` if uv is missing — which
  only happens with `scoop install --independent`.
- The tool environment and the `synergo.exe` launcher are both placed under the
  app directory (`UV_TOOL_DIR`/`UV_TOOL_BIN_DIR`, or `PIPX_HOME`/`PIPX_BIN_DIR`),
  and `bin` shims that launcher onto PATH. Nothing is left in uv's or pipx's
  global registries, so `scoop uninstall synergo` removes everything.
- `checkver` reads the latest version from PyPI's JSON API; `autoupdate` rewrites
  `url` and pulls the matching wheel's sha256 from the same API.

## One-time setup

1. Create `AdrianShah/scoop-synergo` from the
   [ScoopInstaller/BucketTemplate](https://github.com/ScoopInstaller/BucketTemplate)
   template. Its bundled Excavator workflow runs `checkver` on a schedule and
   commits version bumps, so the bucket follows PyPI with no release-side job.
2. Copy `synergo.json` to `bucket/synergo.json` there **after the first PyPI
   release**, then fill in the real hash:

   ```powershell
   .\bin\checkver.ps1 synergo -Update   # from the bucket repo root
   ```

   The committed template's hash is a placeholder; installing it unchanged fails
   Scoop's hash check by design.
