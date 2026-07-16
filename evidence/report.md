# Limitless HL Bot Sourcey Maintainer Reference Report

- Bounty: Frantic #33, `Publish Sourcey docs for a maintained OSS library`.
- Target: `samlogic-ship/limitless-hl-bot`, pinned at commit `d5e965d3d5ff5f929d5bbe6cc34839d5fa8123db`.
- License: MIT, added at the pinned commit.
- Activity: the target's preceding production commit is dated 2026-06-14 and the repository contains more than 30 Python implementation modules plus tests.
- Public docs URL: `https://alpha.samlogic.org/`.
- Maintainer domain: `samlogic.org`; `alpha.samlogic.org` is a dedicated maintainer-controlled documentation surface, and the target repository README links to that exact docs home.
- Sourcey command: `sourcey build --config docs-source/sourcey.config.ts -o public-docs`.
- Sourcey adapter: Markdown.
- Generated pages: overview, market discovery, scoring/signals, execution/orders, risk/exits, and operations/learning.
- Coverage: 56 pinned source links covering more than 20 public APIs and operational concepts.
- Maintainer-facing gap: the original README explained setup and PM2 posture but did not map the system's execution boundaries, risk layers, learning loop, or source entry points.
- User-facing gap: operators could not quickly trace a candidate from venue discovery through scoring, intent construction, exit policy, and post-trade attribution.
- Verification: `runx:receipt:sha256:212ef3157eefb551c4b211fd492543eb99ea5164ddf1107a7c22b8e8f9ea5886` from runx-cli 0.7.1; local verification reports `valid: true`.

The result is a real maintainer reference rather than a placeholder. It is
generated from checked-in Sourcey inputs, linked from the target repository,
served on the maintainer's own domain, and recomputable from the pinned source.
