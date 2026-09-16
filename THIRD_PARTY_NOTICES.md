# Third-party notices

The repository LICENSE covers original ComputerUSE code and documentation. It does not replace licenses, copyrights, service terms or model-use conditions belonging to third parties.

## Dependency manifests

Python dependencies and resolved versions are listed in `pyproject.toml` and `uv.lock`; frontend dependencies are listed in `frontend/package.json` and `frontend/package-lock.json`. Optional perception dependencies are in `requirements-perception.txt`. Consult each installed distribution's license and notices before redistributing a combined application. This inventory is not a claim that all dependencies share the MIT license.

## External integrations

- Hermes Agent: https://github.com/NousResearch/hermes-agent. The candidate integration uses an explicitly pinned tracked-source export. Hermes is a separate upstream project with its own MIT license and notices; its personal configuration, credentials and runtime are not bundled here. Preserve upstream notices if exporting or redistributing it.
- Cua: https://github.com/trycua/cua. Candidate desktop integration is separate from certified desktop support. Consult upstream component licenses; not every optional component is covered by the same terms.
- Ultralytics / YOLO-World: https://github.com/ultralytics/ultralytics and https://docs.ultralytics.com/help/FAQ/. This optional dependency has AGPL-3.0 and commercial licensing considerations. Installing or distributing it can impose obligations on a combined application. This repository's MIT license does not waive them; do not describe a bundled distribution as MIT-only.
- Ollama, OCR models, planning models and downloaded weights are not included in the public source package. Review the license and use restrictions of each exact model/version independently.

API providers and operating-system vendors retain their trademarks and service terms. This project is not an official OpenAI, Anthropic, Google, Nous Research, Cua, Microsoft or Apple product and does not imply endorsement.
