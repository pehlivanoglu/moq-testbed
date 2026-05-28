# TODO - moqx Root

Root-level follow-ups and repository-wide notes for AI agents.

## Documentation

- [ ] Add component-level `AGENTS.md` and `TODO.md` files as components are
  touched in future prompts.
- [ ] Keep root known limitations in `AGENTS.md` aligned with implementation
  and public docs.
- [ ] Review `docs/config.md` against current code for draft/version wording,
  especially draft 15 support and released-vs-current config fields.

## Project Hygiene

- [ ] Decide whether staged root documentation files should be committed with
  the existing staged `.gitmodules`, `deps/moxygen`, and draft text changes.

## Notes

- 2026-05-24: Replaced the initial moqlab-oriented root `AGENTS.md` draft with
  moqx-specific guidance and added the component documentation workflow.
- 2026-05-25: Added relay object-arrival logging in `src/relay/TopNFilter.cpp`
  so relay flow can be observed from object ingress logs.
