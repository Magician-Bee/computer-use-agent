# Public-repository privacy

The public environment report uses example home-directory names. It is a sanitized description of the recorded test environment, not a machine-specific configuration to execute unchanged. Use your own home directory when following documented commands.

Do not commit credentials, browser profiles, session databases, raw request dumps, private conversations, or screenshots containing personal information. Keep local environment details in `environment-report.local.json` (ignored). Publish only reviewed, de-identified evidence; the existing `artifacts/` exclusion remains in place.

Before pushing, run `python scripts/check_public_privacy.py` and review `git diff --cached`. This generic heuristic does not certify that every secret or all personal information has been found. Intentional test fixtures may require manual review; do not solve findings by globally disabling checks.

Use the GitHub-provided noreply address for future commits. Changes to local Git configuration do not rewrite existing commits. Replacing a public branch's history does not erase existing clones, caches, retained commit objects, or provider logs. Do not merge an old local history back into a sanitized public history. Preserve local-only work separately and copy reviewed changes onto a fresh clone.

This cleanup does not change feature status, benchmark outcomes, or the project's licensing. Existing benchmark statements remain maintainer-recorded results, not newly executed tests.
