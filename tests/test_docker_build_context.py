"""Validates the Docker/Render image-size fix WITHOUT needing a Docker
daemon (none is available in CI/this sandbox): it (a) re-implements
Docker's .dockerignore matching semantics closely enough to check which
real files in the repo would/wouldn't be sent to the build context, and
(b) statically inspects the Dockerfile for the required multi-stage
structure and explicit COPY list.

Pure stdlib - runs standalone (`python3 tests/test_docker_build_context.py`)
as well as under pytest, so it doesn't depend on the project's runtime
dependencies (fastapi/sqlalchemy/etc.) being installed.
"""

import fnmatch
import os
import sys

BACKEND_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _load_dockerignore_patterns():
    path = os.path.join(BACKEND_ROOT, ".dockerignore")
    patterns = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            patterns.append(line)
    return patterns


def _pattern_matches(pattern, rel_path):
    """Simplified re-implementation of Docker's (gitignore-derived)
    .dockerignore matching: a pattern with an internal '/' is anchored
    to the build-context root; a bare name (no internal '/') matches
    that name at ANY depth in the tree. A trailing '/' restricts the
    pattern to directories (and everything under them)."""
    is_dir_pattern = pattern.endswith("/")
    core = pattern[:-1] if is_dir_pattern else pattern
    path_parts = rel_path.split("/")
    anchored = "/" in core

    if anchored:
        core_parts = core.split("/")
        if len(path_parts) < len(core_parts):
            return False
        for cp, pp in zip(core_parts, path_parts):
            if not fnmatch.fnmatch(pp, cp):
                return False
        return True if is_dir_pattern else len(path_parts) == len(core_parts)

    for i, part in enumerate(path_parts):
        if fnmatch.fnmatch(part, core):
            if is_dir_pattern:
                return True  # `part` is an ignored directory - everything under/at it is excluded
            if i == len(path_parts) - 1:
                return True  # bare filename/pattern match on the final path component
    return False


def is_excluded(rel_path, patterns):
    rel_path = rel_path.replace(os.sep, "/")
    return any(_pattern_matches(p, rel_path) for p in patterns)


# --- Required production runtime files (must NOT be excluded) -----------

REQUIRED_FILES = [
    "app/main.py",
    "app/core/config.py",
    "app/simulator/csv_replay_simulator.py",
    "app/simulator/train_simulator.py",
    "app/ai_engine/model_bundle.py",
    "app/ai_engine/saved_models/crowd_model.pkl",
    "app/ai_engine/saved_models/delay_model.pkl",
    "app/ai_engine/saved_models/frequency_model.pkl",
    "datasets/source/passenger_flow.csv.gz",
    "datasets/source/stations.csv.gz",
    "datasets/source/train_operations.csv.gz",
    "datasets/source/trains.csv.gz",
    "migrations/env.py",
    "migrations/versions/0001_baseline_schema.py",
    "alembic.ini",
]

# --- Dev/training/test artifacts (MUST be excluded) ----------------------

EXCLUDED_PATHS = [
    "tests/conftest.py",
    "tests/test_health.py",
    "docs/crowd-data-correctness.md",
    "postman/postman/environments/AI MetroFlow - Local.environment.yaml",
    "colab_training/train_crowd_model.py",
    "colab_training/train_metroflow_models_colab.ipynb",
    "scripts/verify_ws_manager.py",
    "datasets/ml/crowd_training.csv.gz",
    "datasets/ml/delay_training.csv.gz",
    "datasets/ml/frequency_training.csv.gz",
    "app/simulator/__pycache__/csv_replay_simulator.cpython-312.pyc",
    ".pytest_cache/README.md",
    ".git/HEAD",
    "README.md",
    "DEPLOYMENT.md",
]


def test_dockerignore_file_exists():
    assert os.path.isfile(os.path.join(BACKEND_ROOT, ".dockerignore"))


def test_required_runtime_files_are_not_excluded():
    patterns = _load_dockerignore_patterns()
    excluded = [f for f in REQUIRED_FILES if is_excluded(f, patterns)]
    assert not excluded, f"Required runtime files wrongly excluded by .dockerignore: {excluded}"


def test_required_runtime_files_actually_exist_on_disk():
    """Sanity check that the paths above are real, not just typos that
    would trivially 'pass' the exclusion check above."""
    missing = [f for f in REQUIRED_FILES if not os.path.exists(os.path.join(BACKEND_ROOT, f))]
    assert not missing, f"Expected runtime files missing from repo: {missing}"


def test_dev_training_test_artifacts_are_excluded():
    patterns = _load_dockerignore_patterns()
    not_excluded = [f for f in EXCLUDED_PATHS if not is_excluded(f, patterns)]
    assert not not_excluded, f".dockerignore fails to exclude: {not_excluded}"


def test_dockerignore_still_excludes_original_baseline_patterns():
    """Guard against accidentally dropping any pattern that was already
    present before this fix (.git, .env, venv, __pycache__, *.pyc, tests/,
    docs/, postman/, colab_training/, scripts/, *.md)."""
    patterns = set(_load_dockerignore_patterns())
    required_patterns = {
        ".git/", ".env", ".venv", "venv/", "__pycache__/", "*.pyc", "*.pyo",
        ".pytest_cache/", "tests/", "docs/", "postman/", "colab_training/",
        "scripts/", "*.md", ".DS_Store",
    }
    missing = required_patterns - patterns
    assert not missing, f"Pre-existing .dockerignore patterns dropped: {missing}"


def test_dockerignore_adds_the_newly_requested_patterns():
    patterns = set(_load_dockerignore_patterns())
    newly_required = {
        ".github/", "notebooks/", "datasets/ml/", ".pytest_cache/",
        ".idea/", ".vscode/",
    }
    missing = newly_required - patterns
    assert not missing, f"Newly requested .dockerignore patterns missing: {missing}"


# --- Dockerfile static checks --------------------------------------------

def _read_dockerfile():
    with open(os.path.join(BACKEND_ROOT, "Dockerfile")) as f:
        return f.read()


def test_dockerfile_no_longer_blindly_copies_everything():
    text = _read_dockerfile()
    lines = [ln.strip() for ln in text.splitlines()]
    assert "COPY . ." not in lines, "Dockerfile still uses a blind `COPY . .`"


def test_dockerfile_explicitly_copies_all_required_runtime_paths():
    text = _read_dockerfile()
    for expected in ["COPY app/", "COPY datasets/source/", "COPY migrations/", "COPY alembic.ini"]:
        assert expected in text, f"Dockerfile missing explicit copy: {expected}"


def test_dockerfile_does_not_copy_excluded_directories():
    text = _read_dockerfile()
    copy_lines = [ln.strip() for ln in text.splitlines() if ln.strip().startswith("COPY")]
    for forbidden in ["tests", "docs", "postman", "colab_training", "scripts", "datasets/ml"]:
        offending = [ln for ln in copy_lines if forbidden in ln]
        assert not offending, f"Dockerfile COPYs an excluded path ({forbidden}): {offending}"


def test_dockerfile_keeps_multi_stage_build():
    text = _read_dockerfile()
    assert text.count("FROM python:3.12-slim") == 2, "Expected exactly 2 build stages"
    assert "AS builder" in text


def test_dockerfile_keeps_python_3_12_slim():
    text = _read_dockerfile()
    assert "python:3.12-slim" in text


def test_dockerfile_keeps_xgboost_sklearn_postgres_runtime_libs():
    text = _read_dockerfile()
    for lib in ["libgomp1", "libpq5"]:
        assert lib in text, f"Missing required runtime system library: {lib}"


def test_dockerfile_keeps_nonroot_appuser():
    text = _read_dockerfile()
    assert "useradd" in text and "appuser" in text
    assert "USER appuser" in text


def test_dockerfile_keeps_healthcheck():
    text = _read_dockerfile()
    assert "HEALTHCHECK" in text
    assert "/healthz" in text


def test_dockerfile_keeps_web_concurrency_default_of_1():
    text = _read_dockerfile()
    assert "WEB_CONCURRENCY:-1" in text


def test_dockerfile_startup_command_unchanged():
    text = _read_dockerfile()
    assert 'CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT} --workers ${WEB_CONCURRENCY:-1}"]' in text


def test_dockerfile_keeps_requirements_install_in_builder_stage():
    text = _read_dockerfile()
    assert "COPY requirements.txt ." in text
    assert "pip install --no-cache-dir --prefix=/install -r requirements.txt" in text


# --- Simulated build-context size comparison (informational) -----------

def _dir_size_bytes(rel_dir):
    total = 0
    abs_dir = os.path.join(BACKEND_ROOT, rel_dir)
    for root, _dirs, files in os.walk(abs_dir):
        for fn in files:
            total += os.path.getsize(os.path.join(root, fn))
    return total


def test_build_context_shrinks_after_excluding_dev_and_training_artifacts():
    """Informational/regression guard: the set of directories now
    excluded from the build context (chiefly datasets/ml/, the largest
    single offender) must be non-trivial - i.e. this fix is actually
    saving meaningful image size, not just adding no-op patterns."""
    excluded_dirs = ["datasets/ml", "tests", "docs", "postman", "colab_training", "scripts"]
    excluded_bytes = sum(
        _dir_size_bytes(d) for d in excluded_dirs if os.path.isdir(os.path.join(BACKEND_ROOT, d))
    )
    kept_bytes = sum(
        _dir_size_bytes(d) for d in ["app", "datasets/source", "migrations"]
        if os.path.isdir(os.path.join(BACKEND_ROOT, d))
    )
    assert excluded_bytes > 1_000_000, "Expected >1MB of dev/training artifacts to be excludable"
    assert kept_bytes > 0


if __name__ == "__main__":
    # Minimal standalone runner so this can be executed without pytest
    # installed (see module docstring).
    import traceback

    tests = [(name, obj) for name, obj in list(globals().items())
              if name.startswith("test_") and callable(obj)]
    passed, failed = 0, 0
    for name, fn in tests:
        try:
            fn()
            print(f"PASS  {name}")
            passed += 1
        except AssertionError as e:
            print(f"FAIL  {name}: {e}")
            failed += 1
        except Exception:
            print(f"ERROR {name}")
            traceback.print_exc()
            failed += 1
    print(f"\n{passed} passed, {failed} failed out of {len(tests)}")
    sys.exit(1 if failed else 0)
