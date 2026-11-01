"""The commit gate (Ruling 60, CTO_NOTES_12 §5): the hook tests the index, not the working tree.

Each test drives the real hook or script through real `git` in a throwaway repository holding a
tiny project, so the nested pytest takes about a second. Nothing here touches this repository.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent
HOOK_DIR = ROOT / ".githooks"
VERIFY = ROOT / "scripts" / "verify_range.sh"
GOOD = "from mockboard import VALUE\n\n\ndef test_value():\n    assert VALUE == 1\n"
BAD = GOOD.replace("== 1", "== 2")


def _env() -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.pop("PYTHONPATH", None)
    return {
        **env,
        "MOCKBOARD_HOOK_PYTHON": sys.executable,
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_NOSYSTEM": "1",
    }


def _run(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(args, cwd=repo, env=_env(), capture_output=True, text=True, timeout=120)


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    identity = ("-c", "user.name=Test", "-c", "user.email=test@example.invalid")
    return _run(repo, "git", *identity, *args)


def _write(repo: Path, relative: str, text: str) -> None:
    path = repo / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _head(repo: Path) -> str:
    return _git(repo, "rev-parse", "HEAD").stdout.strip()


@pytest.fixture
def plain_repo(tmp_path: Path) -> Path:
    """A small green project with one commit and no hook."""
    repo = tmp_path / "repo"
    repo.mkdir()
    assert _git(repo, "init", "-q").returncode == 0
    _write(repo, "src/mockboard/__init__.py", "VALUE = 1\n")
    _write(repo, "eval/__init__.py", "")
    _write(repo, "tests/test_value.py", GOOD)
    _git(repo, "add", "src", "eval", "tests")
    assert _git(repo, "commit", "-q", "-m", "first").returncode == 0
    return repo


@pytest.fixture
def repo(plain_repo: Path) -> Path:
    """The same project with the hook switched on."""
    assert _git(plain_repo, "config", "core.hooksPath", str(HOOK_DIR)).returncode == 0
    return plain_repo


def test_hook_lets_a_green_index_through(repo: Path):
    before = _head(repo)
    _write(repo, "tests/test_more.py", "def test_more():\n    assert True\n")
    _git(repo, "add", "tests/test_more.py")
    done = _git(repo, "commit", "-q", "-m", "more")
    assert done.returncode == 0, done.stdout + done.stderr
    assert _head(repo) != before


def test_hook_refuses_a_red_index(repo: Path):
    before = _head(repo)
    _write(repo, "tests/test_value.py", BAD)
    _git(repo, "add", "tests/test_value.py")
    assert _git(repo, "commit", "-q", "-m", "red").returncode != 0
    assert _head(repo) == before


def test_hook_refuses_when_the_fix_was_left_unstaged(repo: Path):
    before = _head(repo)
    _write(repo, "tests/test_value.py", BAD)
    _git(repo, "add", "tests/test_value.py")
    _write(repo, "tests/test_value.py", GOOD)  # green on disk, red in the index
    assert _git(repo, "commit", "-q", "-m", "red").returncode != 0
    assert _head(repo) == before


def test_hook_refuses_when_a_file_was_left_out(repo: Path):
    before = _head(repo)
    _write(repo, "src/mockboard/extra.py", "EXTRA = 1\n")  # never staged
    _write(repo, "tests/test_extra.py", "from mockboard import extra\n\n\n"
                                        "def test_extra():\n    assert extra.EXTRA\n")  # fmt: skip
    _git(repo, "add", "tests/test_extra.py")
    assert _git(repo, "commit", "-q", "-m", "partial").returncode != 0
    assert _head(repo) == before


def test_hook_passes_a_red_working_tree_when_the_index_is_green(repo: Path):
    _write(repo, "tests/test_more.py", "def test_more():\n    assert True\n")
    _git(repo, "add", "tests/test_more.py")
    _write(repo, "tests/test_value.py", BAD)  # unstaged, not part of this commit
    assert _git(repo, "commit", "-q", "-m", "more").returncode == 0


def test_hook_refuses_a_lint_failure(repo: Path):
    before = _head(repo)
    _write(repo, "src/mockboard/untidy.py", "import os\n")
    _git(repo, "add", "src/mockboard/untidy.py")
    assert _git(repo, "commit", "-q", "-m", "untidy").returncode != 0
    assert _head(repo) == before


def test_hook_fails_closed_without_a_python(repo: Path):
    before = _head(repo)
    _write(repo, "tests/test_more.py", "def test_more():\n    assert True\n")
    _git(repo, "add", "tests/test_more.py")
    env = {**_env(), "MOCKBOARD_HOOK_PYTHON": str(repo / "no-such-python")}
    done = subprocess.run(
        ["git", "-c", "user.name=T", "-c", "user.email=t@example.invalid",
         "commit", "-q", "-m", "x"],
        cwd=repo, env=env, capture_output=True, text=True, timeout=60,
    )  # fmt: skip
    assert done.returncode != 0
    assert _head(repo) == before


def test_hook_is_executable_and_make_venv_switches_it_on():
    assert os.access(HOOK_DIR / "pre-commit", os.X_OK)
    assert os.access(VERIFY, os.X_OK)
    venv_recipe = re.search(r"^venv:\n((?:\t.*\n)+)", (ROOT / "Makefile").read_text(), re.M)
    assert venv_recipe and "git config core.hooksPath .githooks" in venv_recipe.group(1)


# --- verify-range -----------------------------------------------------------------------------


def test_verify_range_reports_each_commit_and_fails_on_a_red_one(plain_repo: Path):
    base = _head(plain_repo)
    _write(plain_repo, "tests/test_value.py", BAD)
    _git(plain_repo, "commit", "-q", "-am", "red")
    red = _head(plain_repo)
    _write(plain_repo, "tests/test_value.py", GOOD)
    _git(plain_repo, "commit", "-q", "-am", "green")
    green = _head(plain_repo)

    done = _run(plain_repo, "bash", str(VERIFY), base)

    assert done.returncode == 1, done.stdout + done.stderr
    rows = [line for line in done.stdout.splitlines() if re.match(r"\| [0-9a-f]{7} ", line)]
    assert [row.split()[1] for row in rows] == [red[:7], green[:7]]
    assert "1 failed" in rows[0] and "exit 1" in rows[0]
    assert "1 passed" in rows[1] and "exit 1" not in rows[1]


def test_verify_range_reads_commits_not_the_working_tree(plain_repo: Path):
    base = _head(plain_repo)
    _write(plain_repo, "tests/test_more.py", "def test_more():\n    assert True\n")
    _git(plain_repo, "add", "tests/test_more.py")
    _git(plain_repo, "commit", "-q", "-m", "more")
    _write(plain_repo, "tests/test_value.py", BAD)  # dirty, uncommitted
    assert _run(plain_repo, "bash", str(VERIFY), base).returncode == 0


def test_verify_range_fails_on_lint_alone(plain_repo: Path):
    base = _head(plain_repo)
    _write(plain_repo, "src/mockboard/untidy.py", "import os\n")
    _git(plain_repo, "add", "src/mockboard/untidy.py")
    _git(plain_repo, "commit", "-q", "-m", "untidy")
    assert _run(plain_repo, "bash", str(VERIFY), base).returncode == 1


@pytest.mark.parametrize("base", ["", "not-a-commit", "HEAD"])
def test_verify_range_refuses_a_useless_base(plain_repo: Path, base: str):
    done = _run(plain_repo, "bash", str(VERIFY), base)
    assert done.returncode == 2
    assert "| commit" not in done.stdout
