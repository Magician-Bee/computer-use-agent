# Security

## Scope

This repository is an early public source preview. It is not a hardened multi-user service, OS sandbox, security product or guarantee of safe autonomous operation. Experimental native desktop execution remains disabled.

Keep the service on loopback. Use disposable browser sessions, synthetic data and explicit per-step approval. Provider keys belong in runtime settings, never in source control. Text-only planning still sends task and observation text to the selected endpoint. See [Privacy](docs/PRIVACY.md).

## Reporting

Use GitHub's private vulnerability reporting option on the repository Security tab when it is available. Availability depends on repository configuration; this document does not claim the feature is enabled. If it is not offered, open a minimal issue asking for a private reporting channel, without credentials, exploit instructions, affected private records or other sensitive details. Do not publish a secret in a public issue or pull request.

Include affected version, component, security impact, a non-sensitive description and suggested remediation. Rotate any exposed credential with its provider; deleting a file does not revoke it.

## Verification boundary

Source privacy checks are heuristics. Unit/UI tests do not prove prompt-injection resistance, complete data redaction, desktop input isolation or suitability for untrusted multi-user hosting. No security certification is asserted.
