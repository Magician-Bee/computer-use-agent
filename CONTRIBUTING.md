# Contributing

ComputerUSE welcomes focused improvements to browser execution, model compatibility, privacy, evaluation and reproducible setup.

## Development

Follow the setup and validation commands in [README.md](README.md). Keep dependency changes explicit and update the corresponding lockfile. Do not introduce a second authoritative task store or silently enable the experimental desktop driver. Architecture changes should include an ADR and tests.

## Pull requests

Explain the problem, scope, tested platforms, actual test commands and known limitations. Add a focused regression test for a bug fix. Separate mocks, local browser fixtures, real-model tasks and native-desktop checks. Preserve failed baseline evidence; do not modify a task oracle or hard-code a site's coordinates merely to obtain a passing score.

## Public data

Use synthetic names, example domains and portable paths. Never commit credentials, private account data, cookies, browser profiles, raw model conversations, session databases or screenshots of a personal desktop. Run `python scripts/check_public_privacy.py` and review the staged diff before pushing. Keep local outputs in ignored directories.

Use your GitHub-provided noreply address when committing. Do not merge a pre-cleanup local history back into the public repository. Start from a fresh clone and bring across only reviewed changes.

## Reports and conduct

Public issues should contain minimal, de-identified reproductions. Do not post a working credential or third-party personal information. Follow [SECURITY.md](SECURITY.md) for sensitive reports. Communicate respectfully and keep discussion specific to the project.

By contributing original work, you agree to release that contribution under the project's MIT license. Preserve upstream notices for third-party code and disclose its provenance.
