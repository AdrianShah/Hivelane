# Hivelane brand assets

The mascot is the **guard bee**. Real hives post guard bees at the entrance to check every bee coming in; Hivelane checks every edit before it lands. The bee itself is the logo mark, the favicon and every avatar.

| Pose | When it is used |
|---|---|
| `clear` | All clear, and empty states such as "no agents yet" |
| `warn` | Someone else is near the file |
| `blocked` | An edit was held (holding the stop paddle) |
| `waiting` | You're in line for a file (holding a queue ticket) |

The body is ink and paper with a single brand-blue stripe. Amber and red stay reserved for the warn and block states, so the bee never looks like an alert by accident.

## Files

- `mascot-<pose>.svg` and `mascot-<pose>-dark.svg`: the 160 × 160 mascot for light and dark backgrounds.
- `mark.svg` and `mark-dark.svg`: the logo mark (the happy bee). `node frontend/scripts/build-brand.mjs` also writes both favicons (`frontend/public`, `docs-site/public`, switching with the system theme) and the docs-site logos.

All of these are generated. Edit the drawing in `frontend/src/brand/mascot.ts`, then run:

```bash
node frontend/scripts/build-brand.mjs
```

The dashboard renders the same drawing through `frontend/src/brand/BrandArt.tsx`, themed with the app's CSS variables.
