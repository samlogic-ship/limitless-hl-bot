---
name: limitless-sourcey-site-validator
description: Validate the Sourcey maintainer reference for Limitless HL Bot.
source:
  type: cli-tool
  command: node
  args: [run.mjs]
  timeout_seconds: 30
  sandbox:
    profile: readonly
    cwd_policy: skill-directory
inputs:
  site_dir: { type: string, required: true }
  target_commit: { type: string, required: true }
output:
  packet: sourcey.limitless_site_validation.v1
  schema:
    validation_report: { type: object }
runx:
  category: validation
  keywords: [sourcey, documentation, validation]
---

# Limitless Sourcey Site Validator

Checks the generated page set, pinned commit, and source-link coverage.
