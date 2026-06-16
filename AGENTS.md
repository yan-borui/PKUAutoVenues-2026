# Repository Guidelines

## Project Structure & Module Organization

This is a Python 3.12 CLI for PKU venue reservation automation. `main.py` coordinates login, captcha recognition, reservation, payment, logging, and notifications. Shared modules live in `utils/`, including `client.py`, `config.py`, `orders.py`, `recognize.py`, `notify.py`, and `time.py`. Tests live in `tests/` and use `unittest`. Static screenshots and sample output are in `assets/`. Runtime logs go to `logs/` and should not be committed.

## Build, Test, and Development Commands

- `uv sync`: install dependencies from `pyproject.toml` and `uv.lock`.
- `cp config.sample.ini config.ini`: create local configuration before running the tool.
- `uv run main.py -h`: show supported CLI options.
- `uv run main.py --venue qdb --date 2026-04-30 --times 15:00`: run a reservation attempt.
- `uv run python -m unittest discover -s tests`: run the test suite.

## Remote Server Synchronization

The project is also deployed on a small Ubuntu server: `ssh ubuntu@10.129.245.50`. Treat it as 1C1G hardware. Before updating, confirm the remote project path and run `git status`. Prefer `git pull` plus targeted validation. Avoid parallel jobs, load tests, repeated dependency reinstalls, or commands that spawn many Python processes. Never overwrite remote-only state such as `config.ini`, `logs/`, cron entries, or scheduled `at` jobs without owner approval.

## Coding Style & Naming Conventions

Follow the existing style: 4-space indentation, type hints for public helpers, descriptive snake_case names, and lowercase module names. Prefer explicit exceptions with useful messages when parsing remote responses or config. Use comments sparingly, mainly for reservation workflow steps or non-obvious external API behavior.

## Testing Guidelines

Add tests under `tests/` using `test_*.py` filenames and `unittest.TestCase` classes. Keep network behavior behind fake clients or fixtures; tests must not call PKU, captcha, payment, or notification services. Cover parsing and decision logic, especially order matching, date/time handling, and ambiguous-response fallbacks.

## Commit & Pull Request Guidelines

History uses Conventional Commit-style messages such as `feat: support specifying date by weekday`, `fix: recover matching unpaid reservation orders`, and `docs: update README.md`. Keep commits focused. Pull requests should describe behavior changes, list tests run, mention config changes, and include screenshots only for asset or README visual updates.

## Security & Configuration Tips

Never commit `config.ini`, credentials, captcha service passwords, notification keys, or generated logs. Update `config.sample.ini` when adding new configuration fields. Treat external service responses as untrusted: validate JSON shapes before accessing nested values and avoid logging secrets or tokens.
