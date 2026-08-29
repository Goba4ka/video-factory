# Codex instructions for Video Factory

## Purpose

This repository contains a review-first factory for Russian-language vertical
short-form videos. The Python control plane is in `factory/src/video_factory`.
Editorial rules, safety gates, rights checks, schemas, and HyperFrames pilot
projects live under `factory/`.

Communicate with the owner in Russian unless they ask for another language.

## Start here

Read these files before making broad changes:

1. `README.md`
2. `factory/START_HERE.md`
3. `factory/design/ARCHITECTURE.md`
4. `factory/analysis/V2_ACCEPTANCE_20260829.md`
5. `factory/deployment/SERVER_MIGRATION_GUIDE.md`

Nested `AGENTS.md` files inside pilot projects take precedence for their
subdirectories.

## Cloud development commands

Run commands from the repository root:

```bash
bash scripts/codex-cloud-setup.sh
python -m pytest factory/tests -q
python -m video_factory lanes --registry factory/lanes/registry.json
```

The cloud repository includes the current code, project documentation, selected
pilot audio/images, thumbnails, and audit context. It still excludes local
databases, downloaded executables, dependency directories, large raw media,
model weights, renders, and other ignored binary artifacts. Treat a missing
heavy asset as an external-storage concern; do not fabricate it or silently
replace it with an unlicensed download.

## Safety and authority boundaries

- Never commit secrets, authentication caches, API keys, voice credentials, or
  production `.env` files. `FISH_API_KEY` and `PEXELS_API_KEY` may be supplied
  through Codex Cloud environment variables when a task needs them. The Codex
  Cloud **Secrets** section is setup-only and is not available during the agent
  phase.
- Agent internet access may be enabled for cloud tasks. Network availability
  does not waive source licensing, rights evidence, privacy, safety, or human
  approval requirements.
- Do not publish to social platforms, spend money on paid media/TTS calls, or
  invoke a production writer without explicit approval from the owner.
- Preserve medical, privacy, sensitivity, factual, rights, originality, and
  human publish gates. Missing evidence must fail closed.
- `final_review` and `publisher` remain human-controlled roles.
- Do not describe the full unattended media-to-publish pipeline as production
  ready while the acceptance document still contains blocking items.
- Codex Cloud is for code, tests, research, scripts, manifests, and controlled
  preproduction. GPU rendering and the long-running production worker belong on
  a separately provisioned runtime host.

## Change discipline

- Keep changes focused and preserve unrelated user work.
- Add or update tests for behavior changes.
- Run the relevant test subset, then the full test suite when practical.
- Report any verification that could not run because an intentionally excluded
  media asset or host capability is unavailable.
