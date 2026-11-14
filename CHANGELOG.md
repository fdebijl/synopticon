# Changelog

All notable changes to Synopticon are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project
adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html). While the version is below
1.0, a minor bump may include breaking changes.

Dependency bumps are left out unless they change behaviour.

## [Unreleased]

### Fixed

- **Detect faces** keeps the originals cache within `originals_cache_gb` *during* a run. Before
  this, `sync --hash`, `extract` and `regen-crops` only evicted once the command had finished, so
  the cache could grow to the size of the whole library mid-run. (#144)
- Photos whose file can't be decoded as an image are remembered in a new `extract_skips` table
  and left out of later runs until the file or the pipeline version changes. Before this, every
  run downloaded them in full and failed on them again. Failures a retry could fix (NAS, disk,
  memory, unexpected errors) are still retried. `extract --retry-skipped`, also on the Detect
  faces card, tries them again. (#144)
- SCRFD no longer skips a photo with a `KeyError` on every 101st distinct input shape. The
  anchor-centre cache now evicts old entries before storing a new one, not after. (#143)

### Changed

- The static project site has real content now. (#142)

## [0.24.0] - 2026-10-26

### Added

- A **Stats** page with lifetime figures (runs, NAS writes, review decisions) and library figures
  (photos, faces, how faces are spread across named people, the latest grouping run). (#121)
- A lifetime run ledger (`command_runs`): every command records its status and result, whether
  it ran in a terminal, from the GUI or on a schedule. Past GUI jobs are imported once from their
  job logs. (#121)
- `GET /api/stats/lifetime` and `GET /api/stats/library` endpoints. (#121)

## [0.23.0] - 2026-10-20

### Added

- `[clustering] proposal_kinds` sets which kinds of suggestion Group faces adds to the review
  queue (assign, low confidence, reassign, merge, named merge, new person). It shows as a set of
  checkboxes in Settings.

### Changed

- Detailed documentation moved out of the README into `docs/`.

## [0.22.0] - 2026-10-20

### Added

- Faces Synology tagged as the wrong person, or as one of its unlisted persons, are now proposed
  as a reassign to their nearest listed person. (#107)
- An evaluation for corrupted tags, which measures how well reassigns are found. (#108)

### Fixed

- A cluster is never mapped to a person Synology doesn't list. (#107)

## [0.21.1] - 2026-10-20

### Changed

- The Maintenance page responds faster.

## [0.21.0] - 2026-10-20

### Fixed

- Faces that only YOLO detected are no longer scored by, or used as references for, nearest-person
  attribution. Unconfirmed detections (strict profiles, the back of a head, pets, motion blur)
  matched each other rather than a real identity, so one person could collect a large share of
  bogus suggestions. (#106)

## [0.20.0] - 2026-10-19

### Added

- Untagged faces that no cluster claims are attributed to the nearest listed person when the
  match is strong enough. (#105)

## [0.19.0] - 2026-09-27

### Fixed

- Photos that sync has marked as deleted are no longer processed. Review items about them are
  hidden from lists and counts and skipped by `apply`. If the photo comes back, so do they.

## [0.18.1] - 2026-09-25

No functional changes.

## [0.18.0] - 2026-09-25

### Added

- Pipeline jobs show when they last ran.
- Review cards link the person's name and the photo.
- A static project site and the workflow that deploys it.

### Changed

- A quieter sidebar.
- The Tag button stands out more on the Inspect page.

### Fixed

- Live photos are included when detecting faces.

### Security

- GitHub Actions are pinned by commit SHA.

## [0.17.0] - 2026-08-29

### Added

- A `[security]` config section for who can reach the GUI and how hard it is to guess a password.
- Running behind a reverse proxy: the client address is read from `X-Forwarded-For` only when
  the request comes from `trusted_proxies`. There is also an address allowlist
  (`allow_from`, `allow_private_networks`).
- Sign-in throttling, both per address and per address and username pair.
- Two-step sign-in (TOTP) with recovery codes.
- A sign-in log.
- Session pinning, which ties a session to the client that created it.
- Security settings, two-step sign-in setup and the sign-in log in the GUI.
- CLI-only recovery commands for a locked-out operator: `disable-2fa`, `web-access` and
  `session-pin`.

### Changed

- Database backups no longer include two-step sign-in secrets.

### Fixed

- Each SQLite migration is applied atomically.

## [0.16.0] - 2026-08-24

### Added

- Inspect: tag or reassign a face by hand.
- Inspect: zoom and pan the photo.

### Changed

- Stricter detection defaults that cut false positives: one detection scale (`[1.0]`), and both
  the SCRFD and YOLO score floors raised to 0.45.
- A new person needs at least 5 faces across at least 8 photos (`new_person_min_faces`,
  `new_person_min_photos`).

### Fixed

- Face boxes on Inspect no longer scale with the photo.

## [0.15.0] - 2026-08-24

### Added

- An **Inspect** page for debugging one photo: our detections and Synology's boxes on the same
  image.

## [0.14.0] - 2026-08-19

### Added

- Pending review items can be reassigned to another person, merged into another person or
  hidden. None of these touch the NAS.
- `prune-queue` removes review items whose faces disappeared after faces were detected again.
  `regen-crops` repairs faces that are missing crops.

## [0.13.0] - 2026-08-19

### Added

- Download a backup of the database and the settings from the GUI.
- A Maintenance button, and the `clear-applies` CLI command, that clear applied items from the
  review queue.

## [0.12.4] - 2026-08-18

### Fixed

- PostgreSQL: recovers from a lost connection instead of failing the command.

## [0.12.3] - 2026-08-18

### Fixed

- PostgreSQL: null-safe comparisons are translated correctly.

## [0.12.2] - 2026-08-18

### Fixed

- PostgreSQL: integer overflow when migrating a large SQLite database.

## [0.12.1] - 2026-08-18

### Fixed

- The Docker images now include the PostgreSQL dependencies.

### Changed

- A shorter README with screenshots.

## [0.12.0] - 2026-08-16

### Added

- An optional PostgreSQL backend (`[database] backend = "postgres"`), and `db-migrate` to copy
  an existing SQLite database into it.

### Changed

- The interface says "Detect faces" and "Group faces" instead of "extract" and "cluster".

### Fixed

- Detecting faces logs why a photo was skipped.

## [0.11.0] - 2026-08-10

### Added

- A job scheduler that runs commands on a cron schedule, inside the web process.

## [0.10.0] - 2026-08-09

### Changed

- The Pipeline view and QuickMerger are easier to use.

## [0.9.0] - 2026-08-09

### Added

- A **Utilities** page with QuickMerger, which merges two Synology people directly. It refuses to
  merge two named people.

## [0.8.1] - 2026-08-03

### Changed

- Clearer setting descriptions.

## [0.8.0] - 2026-08-02

### Fixed

- The container healthcheck no longer times out while a CPU-heavy job runs. Core counts now
  respect container CPU limits.

## [0.7.1] - 2026-07-31

### Changed

- Jobs started from the GUI run at a lower priority (`job_nice`) with a cap on their threads
  (`job_threads`), so they don't slow the GUI down.

## [0.7.0] - 2026-07-31

### Added

- Better job progress: phases, rate and ETA.

## [0.6.0] - 2026-07-30

### Changed

- The GUI stays responsive while jobs run.

## [0.5.0] - 2026-07-28

No functional changes.

## [0.4.0] - 2026-07-28

### Changed

- Every web endpoint is async, with blocking work moved off the event loop.

## [0.3.0] - 2026-07-27

### Added

- A `reset-password` CLI command.

### Changed

- Polished setting descriptions.

## [0.2.0] - 2026-07-27

### Changed

- The GUI loads faster.

## [0.1.0] - 2026-07-27

First tagged release.

### Added

- Sync of a photo library from Synology Photos, personal and shared spaces both, with SHA-256 and
  perceptual hashing of photos.
- A face pipeline that combines two detectors (SCRFD and YOLO) and three embedding models
  (ArcFace, AdaFace, MagFace). Runs on CPU, or on an optional CUDA GPU.
- HEIF/HEIC support.
- Face clustering, checked against the people Synology already knows to propose assigns,
  reassigns, merges and new people.
- A review queue, and `apply` to write approved changes back to the NAS. `apply` is a dry run by
  default and needs a flag for each danger tier. Merging two named people has its own extra gate.
- `dedupe`, which finds duplicate photos.
- `benchmark`, plus `reset`, `clear-queue` and commands to restore or delete crops.
- A web GUI (Vue SPA) for every CLI command, with a focus view for the review queue, links to
  people and photos in Synology, and a description for every setting.
- Docker images on Docker Hub.
- CI that runs the test suite on every push.

### Fixed

- A dry run no longer used up approved items.
- Writeback no longer mixed up the two people in a merge.
- Running out of video memory in the CUDA pipeline.

[Unreleased]: https://github.com/fdebijl/synopticon/compare/v0.24.0...HEAD
[0.24.0]: https://github.com/fdebijl/synopticon/compare/v0.23.0...v0.24.0
[0.23.0]: https://github.com/fdebijl/synopticon/compare/v0.22.0...v0.23.0
[0.22.0]: https://github.com/fdebijl/synopticon/compare/v0.21.1...v0.22.0
[0.21.1]: https://github.com/fdebijl/synopticon/compare/v0.21.0...v0.21.1
[0.21.0]: https://github.com/fdebijl/synopticon/compare/v0.20.0...v0.21.0
[0.20.0]: https://github.com/fdebijl/synopticon/compare/v0.19.0...v0.20.0
[0.19.0]: https://github.com/fdebijl/synopticon/compare/v0.18.1...v0.19.0
[0.18.1]: https://github.com/fdebijl/synopticon/compare/v0.18.0...v0.18.1
[0.18.0]: https://github.com/fdebijl/synopticon/compare/v0.17.0...v0.18.0
[0.17.0]: https://github.com/fdebijl/synopticon/compare/v0.16.0...v0.17.0
[0.16.0]: https://github.com/fdebijl/synopticon/compare/v0.15.0...v0.16.0
[0.15.0]: https://github.com/fdebijl/synopticon/compare/v0.14.0...v0.15.0
[0.14.0]: https://github.com/fdebijl/synopticon/compare/v0.13.0...v0.14.0
[0.13.0]: https://github.com/fdebijl/synopticon/compare/v0.12.4...v0.13.0
[0.12.4]: https://github.com/fdebijl/synopticon/compare/v0.12.3...v0.12.4
[0.12.3]: https://github.com/fdebijl/synopticon/compare/v0.12.2...v0.12.3
[0.12.2]: https://github.com/fdebijl/synopticon/compare/v0.12.1...v0.12.2
[0.12.1]: https://github.com/fdebijl/synopticon/compare/v0.12.0...v0.12.1
[0.12.0]: https://github.com/fdebijl/synopticon/compare/v0.11.0...v0.12.0
[0.11.0]: https://github.com/fdebijl/synopticon/compare/v0.10.0...v0.11.0
[0.10.0]: https://github.com/fdebijl/synopticon/compare/v0.9.0...v0.10.0
[0.9.0]: https://github.com/fdebijl/synopticon/compare/v0.8.1...v0.9.0
[0.8.1]: https://github.com/fdebijl/synopticon/compare/v0.8.0...v0.8.1
[0.8.0]: https://github.com/fdebijl/synopticon/compare/v0.7.1...v0.8.0
[0.7.1]: https://github.com/fdebijl/synopticon/compare/v0.7.0...v0.7.1
[0.7.0]: https://github.com/fdebijl/synopticon/compare/v0.6.0...v0.7.0
[0.6.0]: https://github.com/fdebijl/synopticon/compare/v0.5.0...v0.6.0
[0.5.0]: https://github.com/fdebijl/synopticon/compare/v0.4.0...v0.5.0
[0.4.0]: https://github.com/fdebijl/synopticon/compare/v0.3.0...v0.4.0
[0.3.0]: https://github.com/fdebijl/synopticon/compare/v0.2.0...v0.3.0
[0.2.0]: https://github.com/fdebijl/synopticon/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/fdebijl/synopticon/releases/tag/v0.1.0
