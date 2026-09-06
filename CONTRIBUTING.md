# Contributing to MiniSky

We welcome contributions to MiniSky! Whether you're fixing bugs, adding new features, or improving documentation, your help is appreciated.

## Getting Started

1. Fork the repository
2. Clone your fork: `git clone https://github.com/berketpbs/MiniSky.git`
3. Set up the development environment using `uv`:

```bash
cd MiniSky
uv venv
uv pip sync pyproject.toml
```

## Development Workflow

1. Create a feature branch: `git checkout -b feature/my-new-feature`
2. Make your changes
3. Run formatting and linting:
   ```bash
   uv run black minisky tests
   uv run ruff check minisky tests
   ```
4. Run tests to ensure nothing is broken:
   ```bash
   uv run pytest tests/
   ```

   `tests/e2e/` runs MiniSky against a container running real sshd, covering
   the parts that only exist over SSH (key auth, remote exit codes, log
   capture, workdir sync, detached runs). It needs Docker and skips cleanly
   without it:
   ```bash
   uv run pytest tests/e2e        # just the end-to-end suite
   uv run pytest tests -m "not e2e"  # everything else
   ```
5. Commit your changes and push to your fork
6. Open a Pull Request

## Adding a New Provider

To add a new cloud provider to MiniSky:
1. Create a new file in `minisky/providers/`
2. Inherit from `BaseProvider`
3. Implement the required methods (`launch`, `status`, `terminate`, etc.)
4. Register your provider in `minisky/api/core.py` under `ProviderRegistry`
5. Add tests in `tests/test_providers.py`

## Architecture

Please review `plans/minisky-architecture.md` to understand the internal structure (State Manager, Task Parser, Provisioner, Executor) before making large architectural changes.
