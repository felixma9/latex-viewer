# Mobile PWA shell

**Status:** approved design, not yet implemented
**Date:** 2026-09-15

## Goal

Make the LaTeX workspace usable from an iPhone: open the URL, add it to the
home screen, and get a fullscreen app that shows the current PDF and recompiles
on demand.

## Constraints

1. **The desktop UI does not change.** `INDEX_HTML` stays byte-for-byte
   identical and `/` keeps serving it. The mobile shell is a separate document
   at its own route.
2. **No offline support.** No service worker, no cache logic. The tailnet must
   be reachable for the app to work.
3. **No networking changes.** The app is served over plain HTTP on 8585 exactly
   as it is today. Nothing is added to the host's Tailscale config.

Constraints 2 and 3 are linked: a service worker only registers in a secure
context, so offline support would require HTTPS. Offline was judged not worth
that, and both were dropped together.

## Why the PDF can't stay in an iframe

The desktop viewer renders the PDF into an `<iframe>` (`server.py:604`). iOS
Safari will not scroll a PDF in an iframe — it paints page 1 as an inert
thumbnail and stops. Standalone home-screen mode does not change this. The
mobile shell therefore renders pages as images instead. This is the reason the
work is more than a manifest file.

## Routes

All new. Nothing existing is modified except `/mtime/`, which gains one field.

| Route | Returns |
|---|---|
| `GET /m` | `MOBILE_HTML`, the mobile shell |
| `GET /manifest.webmanifest` | manifest, `start_url: "/m"`, `display: "standalone"` |
| `GET /icon-180.png` | apple-touch-icon |
| `GET /icon-512.png` | manifest icon |
| `GET /page/<project>/<n>.png` | page `n` of `main.pdf`, rasterized |

`/projects`, `/log/`, `/compile/` are reused unchanged. `/mtime/` gains a
`pages` field (integer, `0` when unknown) so the shell knows how many page
images to request.

### Page rasterization

`poppler-utils` is added to the Dockerfile. `/page/` shells out to:

```
pdftoppm -png -r 150 -f <n> -l <n> main.pdf <out-prefix>
```

150dpi puts an A4 page at ~1240px wide, which matches a 390pt iPhone at 3x.
The DPI is a module constant, not hardcoded at the call site.

Rasterization is **lazy**: it happens on request, never as part of a compile. A
session that only uses the desktop viewer does no rasterizing at all.

Output is cached at `.build/pages/<pdf-mtime>-<n>.png`. A request whose PDF
mtime differs from the cached prefix renders fresh and deletes the stale files
for that project. Rendering holds a per-project lock so two concurrent requests
for the same page cannot race the same output file.

Page count comes from `parse_log()`'s `output.pages` (`server.py:301`), which
reads the `Output written on …` line. When the log is absent or lacks that line
— a PDF compiled outside the container, for instance — fall back to `pdfinfo`.
If both fail, `pages` is `0` and the shell shows the "no PDF yet" state.

### Safety

`/page/` resolves the project through the existing `_project_dir()`, so it
refuses paths outside `/documents` the same way `/pdf/` does. The page number
must parse as an integer and fall within `1..pages`; anything else is a 404,
never a subprocess call.

Two existing behaviours touch `.build/` and were checked against this cache:

- After a failed first `pdflatex` run, `_compile_once()` clears the build
  directory before retrying (`server.py:76-78`). It unlinks *files* only, so a
  `pages/` subdirectory survives. The cache must therefore be a subdirectory,
  not a flat `pages-<n>.png` naming scheme inside `.build/` — the latter would
  be silently deleted on every retry.
- `_drop_privileges()` chowns build output at startup (`server.py:1137-1141`).
  Page images are written after privileges are dropped, so they are already
  owned correctly and need no handling there.

## Mobile shell

A separate `MOBILE_HTML` constant. Same visual language as the desktop viewer
(same CSS custom properties, same Catppuccin palette) but laid out for touch.

**Head:** `viewport-fit=cover` for the notch, `apple-mobile-web-app-capable`,
`apple-mobile-web-app-status-bar-style`, `theme-color`, and the manifest link.
The viewport sets no `maximum-scale` — pinch-zoom on a CV page is the whole
point and must not be locked.

**Header:** hamburger, project name, status dot. Reuses the desktop status
vocabulary (compiling / ready / updated / failed).

**Pages:** a vertical scroll of full-width `<img>`, one per page, `loading="lazy"`,
each `src` carrying the PDF mtime as a cache-buster. Page height is reserved
from the known page count before images load, so the scroll position does not
jump as they arrive.

**Drawer:** projects list slides in from the left over a backdrop, tap outside
to dismiss. The Compile button lives here.

**Error strip:** fixed to the bottom, padded for the home indicator, showing
`▲ 2 errors · 5 warnings`. Tapping expands a sheet listing the parsed problems
from `/log/` — severity dot, message, source line. No tabs, no filter chips, no
raw log viewer; those stay desktop-only.

**Pull-to-refresh** at the top of the page list forces a recompile, the same
call the Compile button makes.

### Polling

The shell polls `/mtime/` like the desktop viewer, with two changes for
battery:

- Polling stops on `document.hidden` and resumes with an immediate poll on
  `visibilitychange`. Without this the app runs a 2s request loop in a pocket
  all day.
- On failure the interval backs off 2s → 5s → 15s → 30s and resets to 2s on the
  first success.

When polling fails the header shows a muted "can't reach server" state and the
last-rendered pages stay on screen, matching the desktop behaviour of keeping
the last good PDF visible.

### Icons

Generated once and embedded in `server.py` as base64 constants, keeping the
repo's single-file-server property. iOS uses `apple-touch-icon` for the home
screen and ignores manifest icons, so the 180px PNG is the one that matters;
512px is there for correctness.

## Testing

The repo has no test suite. This adds `tests/` with pytest:

- `/` returns `INDEX_HTML` byte-for-byte unchanged — the regression guard for
  constraint 1.
- Page count parsing: from the log, from the `pdfinfo` fallback, and the
  both-unavailable case.
- Page cache: keyed on mtime, stale files swept, lock prevents concurrent
  render of the same page.
- `/page/` rejects traversal attempts, non-integer page numbers, and page
  numbers outside `1..pages`, without invoking `pdftoppm`.
- `/manifest.webmanifest` and the icon routes return the right content types.
- `/mtime/` still returns its existing four fields alongside the new `pages`.

`pdftoppm` is stubbed in tests; no test shells out to poppler.

Manual verification on the actual iPhone is required before this is called
done: install to the home screen, confirm it opens fullscreen with no Safari
chrome, confirm multi-page scrolling and pinch-zoom, confirm a save on the host
shows up within a few seconds, and confirm a deliberate LaTeX error surfaces in
the bottom strip.

## Out of scope

- Offline support, service worker, HTTPS (see Constraints).
- Editing `.tex` from the phone. This is a viewer.
- Any change to the desktop viewer's markup, styles or behaviour.
- Android install prompts (`beforeinstallprompt` needs HTTPS and a service
  worker; manual add-to-home-screen still works).
