# Homebrew tap

Hivelane ships through its own tap rather than homebrew-core (which wants a
track record and notable usage first). Users install with:

```bash
brew install adrianshah/hivelane/hivelane
# equivalent to: brew tap adrianshah/hivelane && brew install hivelane
```

`hivelane.rb` in this directory is the formula **template**. The live formula
lives in the tap repository and is bumped by `.github/workflows/release.yml`.

## One-time setup

1. **Create the tap repository** `AdrianShah/homebrew-hivelane` on GitHub. It
   must be public and its name must start with `homebrew-`. Give it at least
   one commit (a README is enough) so the workflow can check it out. A
   `Formula/` directory is optional: the first release copies the template in.
2. **Create a token** that can push to the tap and nothing else: a fine-grained
   personal access token scoped to `AdrianShah/homebrew-hivelane` only, with
   *Contents: Read and write*. Give it an expiry and a calendar reminder.
3. **Add it to the Hivelane repository** as an Actions *repository* secret named
   `HOMEBREW_TAP_TOKEN` (Settings → Secrets and variables → Actions).

Without the secret the `bump Homebrew tap` job logs a notice and skips; nothing
else in the release depends on it.

## What a release does

On every stable `v*` tag (prereleases such as `v0.2.0rc1` are skipped), after
the PyPI upload succeeds, the `homebrew` job on a macOS runner:

1. waits for PyPI's JSON API to list the new sdist and reads its URL and sha256;
2. copies `packaging/homebrew/hivelane.rb` into the tap if `Formula/hivelane.rb`
   does not exist yet, then rewrites the formula's top-level `url`/`sha256`;
3. runs `brew update-python-resources` to regenerate every `resource` block;
4. runs `brew install --build-from-source` and `brew test` — a formula that does
   not install is never pushed;
5. commits `hivelane X.Y.Z` to the tap and pushes.

## Bumping by hand

If the job is disabled or fails part-way:

```bash
brew tap adrianshah/hivelane
cd "$(brew --repository adrianshah/hivelane)"
# edit Formula/hivelane.rb: url + sha256 from https://pypi.org/project/hivelane/#files
brew update-python-resources adrianshah/hivelane/hivelane
brew install --build-from-source adrianshah/hivelane/hivelane
brew test adrianshah/hivelane/hivelane
brew audit --strict adrianshah/hivelane/hivelane
git commit -am "hivelane X.Y.Z" && git push
```

## Notes

- `depends_on "rust" => :build` is there for `pydantic-core`: Homebrew builds
  every resource from its sdist. If a future dependency needs another build
  tool, the install step in the workflow fails and says which.
- The `test` block runs `hivelane --version`, so the formula test fails if the
  CLI's version and the sdist's version ever drift.
