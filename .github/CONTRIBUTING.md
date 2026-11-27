# Contributing to `oa-git`

Thank you for your interest in improving `oa-git`! Bug reports, real-world Virtuoso/PDK feedback, documentation improvements, and pull requests are all welcome.

## Development Setup

`oa-git` requires **Python >= 3.11**.

```bash
git clone https://github.com/wsunskyr/oa-git.git
cd oa-git
python3 -m venv .venv
.venv/bin/pip install -e ".[dev]"
.venv/bin/pytest
```

All offline tests run without a Cadence Virtuoso installation.

## Core Guidelines

1. **Official SKILL APIs Only**: Any change under `src/oa_git/skill/` must use exclusively documented Cadence Virtuoso SKILL APIs (`db*`, `dd*`, `sch*`, `cdf*`, `le*`, `rod*`, `tech*`). Private/GUI APIs (`schHi*`, `leHi*`, `geHi*`, `cdfGetParam`, `dbReplaceProp`) and raw `.oa` binary parsing are strictly prohibited and enforced by CI.
2. **Fail-Closed Safety**: Never weaken live lock checks (`*.cdslck*` / `*.lck`), `allow_libraries` boundaries, or post-restore readback verification.
3. **Zero Proprietary Data**: Never commit real `.oa` binaries, foundry PDK paths, internal hostnames, or proprietary customer/lab circuits. Use synthetic fixtures in `tests/`.
4. **Tests Required**: Include offline `pytest` regression tests for bug fixes and new behavior. If you tested against a live Virtuoso environment, summarize the Virtuoso version and observed result in your PR description (without leaking private paths).

## Pull Request Process

1. Fork the repository and create a focused branch from `main`.
2. Ensure `.venv/bin/pytest` passes locally.
3. Open a Pull Request describing the problem, root cause, fix, and verification steps.
