# Contributing

Thank you for your interest in contributing to the El Paso seismic processing pipeline. This document covers the conventions and process for making changes.

---

## Code Style

### Python

- **Version**: Python 3.10+ (see `environment.yml` for the supported range)
- **Formatting**: Follow PEP 8. Use 4-space indentation, no tabs.
- **Line length**: 100 characters maximum (soft limit; allow slightly longer lines for URLs or string literals)
- **Imports**: Group in order: standard library, third-party, local. Separate groups with a blank line.
- **Type hints**: Use where they improve clarity, especially for function signatures in `lib/` and `dashboard/`. Not required for script-level code in pipeline stages.
- **Docstrings**: Use triple-quoted docstrings for public functions and classes. Follow NumPy/SciPy docstring conventions for parameter documentation.
- **Naming**: `snake_case` for functions and variables, `PascalCase` for classes, `UPPER_CASE` for module-level constants.

### YAML Configuration

- Use 2-space indentation
- Include section headers as comments
- Document non-obvious parameters with inline comments

### HTML/JavaScript (Dashboard)

- The dashboard frontend is a single HTML file with inline CSS and vanilla JavaScript. No build tools.
- Keep dependencies minimal (only Leaflet from CDN).
- Prefer `const` and `let` over `var`.

---

## Branch Naming

Use the following prefixes:

| Prefix | Use case | Example |
|--------|----------|---------|
| `feature/` | New functionality | `feature/add-md-magnitude` |
| `fix/` | Bug fixes | `fix/date-boundary-off-by-one` |
| `docs/` | Documentation changes | `docs/update-operator-guide` |
| `refactor/` | Code restructuring (no behavior change) | `refactor/extract-ml-module` |
| `ci/` | CI/CD pipeline changes | `ci/add-pytest-workflow` |
| `config/` | Configuration changes | `config/tune-gamma-params` |

Branch names should be lowercase with hyphens separating words.

---

## Commit Message Format

Follow the [Conventional Commits](https://www.conventionalcommits.org/) pattern:

```
<type>(<scope>): <short description>

<optional body>

<optional footer>
```

### Types

| Type | Description |
|------|-------------|
| `feat` | New feature |
| `fix` | Bug fix |
| `docs` | Documentation only |
| `refactor` | Code restructuring |
| `test` | Adding or updating tests |
| `ci` | CI/CD changes |
| `chore` | Maintenance tasks |
| `perf` | Performance improvement |

### Scopes

Use the pipeline stage number or component name: `ingest`, `process`, `detect`, `associate`, `catalog`, `pipeline`, `dashboard`, `lib`, `docs`.

### Examples

```
feat(detect): add EQTransformer as alternative phase picker
fix(ingest): handle FDSNWS 503 responses with retry
docs(operator): add GPU troubleshooting section
refactor(lib): extract magnitude computation into separate module
test(associate): add unit tests for station count filter
```

---

## Pull Request Process

1. **Create a branch** from `main` using the naming convention above.
2. **Make your changes** with clear, focused commits.
3. **Run tests** locally before pushing (see Testing below).
4. **Open a PR** against `main` with:
   - A descriptive title following the commit message format
   - A summary of what changed and why
   - Any relevant issue numbers
5. **Address review feedback** with additional commits (do not force-push during review).
6. **Squash or rebase** before merge if the commit history is noisy.

### PR Checklist

- [ ] Code follows the style guidelines above
- [ ] New/changed functionality is documented (docstrings, config comments, relevant docs)
- [ ] Tests pass locally
- [ ] No secrets or credentials in the diff
- [ ] CHANGELOG.md updated (if user-facing change)

---

## Testing

### Running Tests

```bash
conda activate elpaso-quake
pytest tests/ -v
```

### Test Structure

Tests live in the `tests/` directory, mirroring the project structure:

```
tests/
  test_ingest.py
  test_process.py
  test_detect.py
  test_associate.py
  test_catalog.py
  test_dashboard.py
  test_lib.py
```

### Guidelines

- Use `pytest` as the test runner.
- Test functions should be named `test_<what_is_tested>`.
- Use fixtures for shared setup (config dicts, temporary directories, mock data).
- Mock external dependencies (FDSNWS client, file I/O) to keep tests fast and offline.
- Aim for coverage of critical paths: configuration loading, file discovery, data transformations, magnitude computation, API endpoint responses.
- Coverage threshold: 70% for new code (aspirational; not currently enforced by CI).

### Testing the Dashboard

```bash
# Run dashboard tests with the FastAPI test client
pytest tests/test_dashboard.py -v
```

Use `fastapi.testclient.TestClient` to test API endpoints without starting a server.

---

## Development Setup

```bash
# Clone and set up environment
git clone <repository-url> elpaso-quake-network
cd elpaso-quake-network
conda env create -f environment.yml
conda activate elpaso-quake

# Run the dashboard in browser mode for development
python -m dashboard --browser
```

### Useful Commands

```bash
# Run a single pipeline stage
python 3-detection/detect.py --config 3-detection/config.yaml --start 2026-01-15 --end 2026-01-15 --debug

# Rebuild the catalog from scratch
python 5-catalog/catalog.py --config 5-catalog/config.yaml --rebuild

# Check FDSNWS server availability
curl -s https://data.raspberryshake.org/fdsnws/dataselect/1/version
```
