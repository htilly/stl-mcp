# Backlog: ready for a small public test (POC) and performance

Based on a code read of `server.py`, `store.py`, `render.py` and the deploy files. Nothing here
has been measured yet; phase 0 includes load testing to confirm the order of the performance items.

Size: S = under 1 hour, M = a few hours, L = a day or more.

## Phase 0: unblock

- [ ] **Resolve the merge conflicts** in `server.py`, `web/common.js` and `web/style.css` on
  `feature/design-system`, run the test suite and commit. (S)
- [x] **Access model decided: real users with roles** (admin, member), per-user ownership of
  models. Built in the "Users, roles and usage stats" phase below, which comes before the
  resource limits in phase 1 because those limits are per user.
- [ ] **Baseline measurements.** Log build and render time per `kind` (`build_seconds` already
  exists in the versions). Write a small load script: 5-10 concurrent `create_model` and
  `render_model` calls. Record p50/p95 and CPU. (M)

## Phase 0b: users, roles and usage stats

Today `auth.json` holds passkeys, OAuth clients and tokens with no owner, and everything is
guarded by one shared `MCP_TOKEN`. This phase adds real users.

### Users and roles
- [x] **User model** (done: Postgres, PR #11) in `auth.py`/`auth.json`: `id`, `name`, `email` (optional), `role`, `created`,
  `last_login`, `disabled`. Passkeys and OAuth tokens get a `user_id`. (M)
- [x] **Roles** (admin/member done, PRs #11-#12; visibility/public models still open below):
  - `admin`: manage users, see all models and all stats, change settings (`/admin`, print-check
    settings), delete any model.
  - `member`: create, edit, comment on and delete their own models; see their own usage.
  - Visibility (decided): models are **private by default**; the owner can make a model **public**
    (and back to private). Public models are readable by every logged-in user and can be
    **copied** into their own library. See "Public models and copying" below.
  - `MCP_TOKEN` stays as a bootstrap/break-glass admin credential, not a normal login.
- [x] ~~Onboarding by invite~~ **Replaced by open signup** (PRs #13-#14): passkey or Google/GitHub/
  Apple sign-in, Cloudflare Turnstile, a per-IP limit, behind `SIGNUP=1`. (M)
- [x] **Migration** (done, PRs #11-#12): existing models and passkeys are assigned to the first admin on first start. (S)
- [x] **Ownership and enforcement** (done, PR #12; `tests/test_isolation.py`): `owner` on each model; filter `list_models`, `/api/models`,
  `/files/...`, `/preview.png`, comments and events by owner and role. MCP tool calls resolve
  the user from the OAuth token, so a member's Claude only sees that member's models. Test with
  two users that neither can read, change or delete the other's models, files or previews. (L)
- [x] **Public models and copying** (M-L; done except the report link and stats below)
  - `visibility` on each model: `private` (default) or `public`. The owner toggles it in the web
    UI and through an MCP tool (for example `set_visibility`).
  - Readable by others when public: the model, its versions, STL files, previews and renders. Not
    readable: comments, and anything about who else uses it. Editing, deleting and commenting
    stay owner-only (admins can always act).
  - A **public gallery** page listing public models with owner name and copy count.
  - **Copy** action (web UI button and MCP tool, building on `duplicate_model`): creates a new
    private model owned by the copier from the source's current recipe, with a single version
    history starting at the copy (not the original's history). Record `copied_from` (model id
    and owner) for attribution. Copying counts against the copier's model limit and quota.
  - If the owner later makes the model private or deletes it, existing copies are unaffected.
  - Anonymous (not logged in) access to public models: off by default; decide whether
    `FILES_PUBLIC`/`UI_PUBLIC` should cover public models or stay separate. (decision)
  - Tests: private model returns 404 (not 403) to non-owners on every route; public model is
    readable but not writable by others; copy produces an independent model.
  - Abuse: let admins unpublish any model, and add a report link or at least an admin list of
    public models. Count publishes and copies in the stats below.
- [x] **Admin UI** (done, PR #15): `/admin/users` lists users with models, storage and ways in;
  change role, disable, log out everywhere, delete. (M)
- [x] **Per-user limits**, partly (PR #13): max models and disk quota per user. Rate limits on
  heavy endpoints are still open in phase 1. (M)

### Usage stats and logging
- [ ] **Event log** (append-only JSONL or SQLite in `/data`): `login` (success and failure), `logout`,
  `model_created`, `model_updated`, `model_deleted`, `render`, `upload`, with `user_id`, time
  and country. (M)
- [ ] **Counters per user**, shown to admins: number of logins, last login, number of models
  (private and public), versions, disk used, renders and build seconds, public models and how
  many times they were copied. Also totals over time (per day). (M)
- [x] **Country of origin per login** (done: GEOIP_DB or CF_COUNTRY, kept LOGIN_HISTORY_DAYS):
  - Resolve from the client IP with an offline GeoIP database (DB-IP lite or MaxMind GeoLite2;
    check the license/account terms) or a CDN header such as `CF-IPCountry` if one is added.
  - Caddy must pass the real client IP (`X-Forwarded-For`); read it only from the trusted proxy,
    otherwise it can be spoofed.
  - Store the country code only, not the raw IP, or at most a truncated/hashed IP. (S-M)
- [ ] **Stats view in `/admin`**: table of users with logins, last seen, country, models; simple
  charts for logins per day and models per user. (M)
- [x] **Privacy note**, partly (PR #13: `/privacy` and account deletion; the retention period
  waits for the event log) (GDPR, users are likely in the EU): tell testers what is logged and why,
  set a retention period for the event log (for example 90 days), and let an admin delete a
  user together with their data. (S)

## Phase 1: safe to expose

- [ ] **Keep `ALLOW_SCRIPT=0`** in production. `_build_script` runs arbitrary Python in a subprocess
  without a real sandbox. Note this in `deploy/README.md`. (S)
- [ ] **Resource limits in the app** (M)
  - Max number of models and a disk quota, per user (from phase 0b) and in total for `/data`.
  - Lower `MAX_UPLOAD_MB` to about 20 for the test.
  - Return a clear error to the MCP client when a limit is hit.
- [ ] **Rate limiting** on the heavy endpoints (render, create/update, upload, `/preview.png`): per
  token or IP, either in Starlette middleware or in Caddy. (M)
- [ ] **Container hardening** in `Dockerfile` and `docker-compose.yml` (S)
  - `mem_limit` and `cpus`.
  - `healthcheck` against `/health`.
  - Run as a non-root `USER`.
- [ ] **Backup of `/data`** (daily copy off the host) and a tested restore. (S)
- [ ] **HTTPS and token hygiene.** Use the Caddy profile, a strong `MCP_TOKEN`, `UI_PUBLIC=0`, and
  decide on `FILES_PUBLIC` (STL links are shareable when 1). (S)

## Phase 2: performance

In this order, re-measuring against the phase 0 baseline after each step.

- [ ] **Limit concurrent heavy jobs.** `_run` uses anyio's default thread pool (about 40 threads), so
  many simultaneous builds and renders fight over the CPU and the GIL. Add an
  `asyncio.Semaphore(2-3)` around build and render, then consider a `ProcessPoolExecutor` for
  CPU-bound work so the web server stays responsive. (M)
- [ ] **Do not hold the store lock during builds.** `Store.create` and `Store.update` keep the
  global `RLock` for the whole `_new_version`, so every write and comment waits for the CSG
  build. Build first, then take the lock only to number the version, write `model.json` and bump
  the revision. Watch for two concurrent updates of the same model (re-check the base version
  under the lock). (M)
- [ ] **Render cost** (`render.py`) (M-L)
  - Cache renders per (model, version, views, size), not only `preview.png`. Check whether
    `render_model` is cached today.
  - Cap the triangle count before rendering (decimate to about 20-30k).
  - Render the four views in parallel in the process pool.
  - Consider lowering the supersampling (`ss=2`) for the web previews.
- [ ] **Cache model metadata in memory.** `_read` parses `model.json` on every call and
  `list_models` reads every model. Keep a dict, invalidate in `_write`. (S-M)
- [ ] **Move `recheck` and `check_print` over all models to a background job** with progress, instead
  of holding the HTTP request. (M)
- [ ] **Replace the SSE 0.5 s polling loop** with an `asyncio.Condition`/`Event` that `_write` and
  `_bump` trigger. Immediate updates and no idle wakeups. (S)
- [ ] **Verify static and STL caching through Caddy.** STL files already send
  `immutable, max-age`; confirm Caddy passes the headers and gzips what it should. (S)

## Phase 3: operate the test

- [ ] **Extend the stats view** from phase 0b with request counts, build/render times, errors and
  total disk usage. (M)
- [ ] **Cleanup policy**: what happens to old models and versions on the host (pruning exists for
  STLs beyond `MAX_VERSIONS`, not for whole models). (S)
- [ ] **Tester onboarding**: a short guide on connecting claude.ai or Claude Code and what the
  limits are. (S)
- [ ] **Re-run the load test** and write the results next to this file. (S)

## More tools for the AI

Goal: let Claude/ChatGPT make more and better models. In three PRs; the first is done.

- [x] **OpenSCAD libraries and reference material** (PR #56): NopSCADlib, Round-Anything,
  gridfinity-rebuilt-openscad and MCAD next to BOSL2; the file-reader check per library;
  `lookup_part` and `print_guide` (`reference/`). (M)
- [ ] **Newer OpenSCAD with the Manifold backend.** Debian ships 2021.01 (CGAL), where booleans
  are slow: `pcb(RPI4)` alone takes about 15-20 s. Install a pinned 2025 snapshot (AppImage or
  tarball, checked against a sha256) in the `Dockerfile` and pass `--backend=manifold` in
  `openscad.build` when the binary supports it (check `--help` once). Re-run the OpenSCAD tests
  and the libraries against it. Blocked in the cloud session: files.openscad.org was not
  reachable there. (S-M)
- [ ] **More parts in `reference/parts.json`**: common displays (SSD1306 0.96" OLED, 1602 LCD),
  USB-C/micro-USB breakout boards, buck converters, relay modules, fans (30/40/50 mm),
  servos (SG90, MG996R), NEMA 17. Check every value against a datasheet or NopSCADlib. (S)

### build123d and a real sandbox (PR 2) (L)
- [ ] **Sandbox for Python builds**: a separate `sandbox` service in `docker-compose.yml` with
  its own image, no internet (`network_mode: none`, only an internal socket/port to the server),
  read-only rootfs, tmpfs `/tmp`, non-root, `cap_drop: ALL`, `no-new-privileges`, a seccomp
  profile, mem/cpu/pids limits, and `runtime: runsc` (gVisor) where the host has it. Per job a
  fresh process with the rlimit/timeout/process-group pattern from `buildworker.py`; meshes come
  back as .npy like there.
- [ ] **`build123d` kind** (OpenCascade B-rep): real fillets/chamfers on any edge, sketches,
  shell, loft/sweep. New MCP tool `create_from_build123d` (the code sets `result` or
  `parts={...}`), a new branch in `Store.build`, STEP export per version next to the STL
  (download in the UI).
- [ ] **Move the `script` kind into the same sandbox**, so `ALLOW_SCRIPT` can be on for everyone
  (then drop the phase 1 item "Keep `ALLOW_SCRIPT=0`").
- [ ] **STEP import** (`import_file` with `.step/.stp`) through the sandbox.
- [ ] Tests that try to read files, reach the network, fork-bomb, eat memory and print the
  environment from inside the sandbox: all must fail or come back empty.

### SDF shapes and import (PR 3) (M-L)
- [ ] **SDF kind as a data recipe** (not code, so safe like the CSG spec): `sphere/box/capsule/
  torus/...`, `smooth_union/smooth_difference(k)`, `shell`, `twist`, `round`, `offset`; evaluated
  with numpy and marching cubes (scikit-image) in `buildworker` (add it to `KINDS`). Reuse the
  expression engine and limits from `fnshapes.py`/`csg.py`. For grips, figures and soft
  transitions that CSG cannot do.
- [ ] **SVG/DXF → extrusion**: a CSG node `outline` (svgpathtools + ezdxf + shapely → polygon),
  files brought in with `import_file`. For logos and laser-cut outlines.
- [ ] **Image → lithophane/relief**: a CSG node `image_relief` on top of the existing heightmap
  (pillow is already there).
- [ ] Pin the new packages in `requirements.txt`; a test that a `smooth_union` is watertight.

### Ideas, not planned yet
- [ ] **Better "eyes" for the AI**: a `measure` tool (distances between faces/parts, smallest wall
  thickness, collisions such as lid vs box), section views as images (trimesh `section`), and a
  headless slicer (PrusaSlicer/OrcaSlicer CLI) for print time, filament and where support is
  needed. (M-L)

## Done when

- A test group can use the service for a week without anyone able to fill the disk or starve the CPU.
- p95 for `create_model` and `render_model` is known, and the web UI stays responsive while a
  render runs.
- Backups exist and a restore has been tried once.
- Every tester has their own account; one member cannot see or change another's models, and an
  admin can see per-user logins, models and country of origin.

## Notes

- Tokens are not a hosting cost: the server never calls an AI API. Costs are CPU, disk, bandwidth
  and domain.
