"""The write gate: shadow-tree validation, a process-level lock, atomic replace.

Every ledger write is prepared as candidate file content, mirrored into a
disposable shadow of the ledger, validated there by the real ``bean-check``, and
only then renamed over the real file. A failed check cannot leave a half-written
or invalid ``.bean`` behind.
"""

from __future__ import annotations

import json
import os
import random
import shutil
import subprocess
import sys
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterable, Iterator

from .errors import LedgerError, fail

DEFAULT_EXCLUDES = {
    ".git",
    ".hg",
    ".svn",
    ".venv",
    "venv",
    "node_modules",
    "__pycache__",
    ".DS_Store",
    ".trae",
    "ledger-domain",
    "ledger_domain",
}


def resolve_program(root: str | Path, configured: str | None, name: str) -> str:
    """Resolve ``bean-check``/``bean-query``.

    Order: an explicit absolute path, an explicit ledger-relative path, the
    ledger's own ``.venv``, the interpreter's virtualenv sibling, then PATH.
    The sibling fallback is what lets a brand-new ledger root (which has no venv)
    validate through the same managed interpreter that runs the core.
    """
    if isinstance(configured, str) and configured.strip():
        value = configured.strip()
        if os.path.isabs(value):
            return value
        if "/" in value or "\\" in value:
            candidate = Path(root) / value
            if candidate.exists():
                return str(candidate)
        else:
            return value
    local = Path(root) / ".venv" / _venv_bin_dir() / f"{name}{_program_suffix()}"
    if local.exists():
        return str(local)
    sibling = Path(sys.executable).with_name(f"{name}{_program_suffix()}")
    if sibling.exists():
        return str(sibling)
    return name


def _venv_bin_dir() -> str:
    """The program directory a virtualenv uses on this platform."""
    return "Scripts" if os.name == "nt" else "bin"


def _program_suffix() -> str:
    """The executable suffix a virtualenv program has on this platform."""
    return ".exe" if os.name == "nt" else ""


def run_process(command: str, args: list[str], cwd: str | Path, timeout: float = 60.0) -> dict[str, Any]:
    """Run one bounded child process and return its outcome without raising on exit codes."""
    try:
        completed = subprocess.run(
            [command, *args],
            cwd=str(cwd),
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
        return {
            "code": completed.returncode,
            "stdout": completed.stdout,
            "stderr": completed.stderr,
            "timed_out": False,
            "spawn_error": None,
        }
    except FileNotFoundError as error:
        return {"code": None, "stdout": "", "stderr": "", "timed_out": False, "spawn_error": error}
    except subprocess.TimeoutExpired:
        return {"code": None, "stdout": "", "stderr": "", "timed_out": True, "spawn_error": None}
    except OSError as error:
        return {"code": None, "stdout": "", "stderr": "", "timed_out": False, "spawn_error": error}


def format_diagnostic(stderr: str, stdout: str, timed_out: bool = False) -> str:
    if timed_out:
        return "bean-check timed out"
    text = f"{stderr}\n{stdout}".strip()
    if not text:
        return "bean-check failed without a diagnostic"
    return "\n".join(text.splitlines()[:40])


def validate_ledger(
    *,
    root: str | Path,
    main_file: str | Path,
    executable: str,
    overrides: dict[str, str] | None = None,
    extra_files: dict[str, str] | None = None,
    pending_directive: str | None = None,
    timeout: float = 60.0,
    exclude: Iterable[str] | None = None,
) -> dict[str, Any]:
    """Validate candidate content in a disposable shadow tree."""
    root_path = Path(root).resolve()
    main_rel = os.path.relpath(Path(main_file).resolve(), root_path)
    if main_rel.startswith(".."):
        fail("invalid_ledger_root", "the root bean file is outside the ledger root")
    overrides = dict(overrides or {})
    extra_files = dict(extra_files or {})

    if pending_directive and pending_directive.strip():
        pending_rel = "__ledger_domain_pending__.bean"
        include_spec = os.path.relpath(pending_rel, os.path.dirname(main_rel) or ".")
        base = overrides.get(main_rel)
        if base is None:
            base = (root_path / main_rel).read_text(encoding="utf-8")
        overrides[main_rel] = f"{base if base.endswith(chr(10)) else base + chr(10)}include \"{include_spec}\"\n"
        extra_files[pending_rel] = pending_directive.strip() + "\n"

    shadow = Path(tempfile.mkdtemp(prefix="dsh-ledger-validate-"))
    try:
        mirror_tree(
            root_path,
            shadow,
            overrides=overrides,
            extra_files=extra_files,
            excludes=set(DEFAULT_EXCLUDES) | set(exclude or ()),
        )
        command = executable
        if not os.path.isabs(command) and ("/" in command or "\\" in command):
            command = str(root_path / command)
        outcome = run_process(command, [main_rel], shadow, timeout=timeout)
        if outcome["spawn_error"] is not None:
            error = outcome["spawn_error"]
            if isinstance(error, FileNotFoundError):
                fail(
                    "bean_check_unavailable",
                    f"cannot run {command}; install Beancount or set beanCheck",
                    {"executable": command},
                )
            fail("bean_check_failed", f"cannot run {command}: {error}", {"executable": command})
        ok = outcome["code"] == 0
        return {
            "ok": ok,
            "code": outcome["code"],
            "stdout": outcome["stdout"],
            "stderr": outcome["stderr"],
            "diagnostic": "" if ok else format_diagnostic(outcome["stderr"], outcome["stdout"], outcome["timed_out"]),
        }
    finally:
        shutil.rmtree(shadow, ignore_errors=True)


def mirror_tree(
    root: str | Path,
    shadow: str | Path,
    *,
    overrides: dict[str, str],
    extra_files: dict[str, str],
    excludes: set[str],
) -> None:
    """Mirror the real ledger with symlinks, writing only overridden paths."""
    root_path = Path(root)
    shadow_path = Path(shadow)
    needed_dirs: set[str] = set()
    for key in [*overrides, *extra_files]:
        parent = os.path.dirname(key)
        while parent not in ("", "."):
            needed_dirs.add(parent)
            parent = os.path.dirname(parent)

    def visit(real_dir: Path, shadow_dir: Path, prefix: str) -> None:
        try:
            with os.scandir(real_dir) as iterator:
                entries = list(iterator)
        except OSError:
            return
        for entry in entries:
            rel = entry.name if prefix == "" else f"{prefix}/{entry.name}"
            if prefix == "" and entry.name in excludes:
                continue
            real_path = Path(entry.path)
            target = shadow_dir / entry.name
            if rel in overrides:
                target.write_text(overrides[rel], encoding="utf-8")
                continue
            if rel in extra_files:
                continue
            if entry.is_symlink():
                os.symlink(real_path, target)
            elif entry.is_dir():
                if rel in needed_dirs:
                    target.mkdir(parents=True, exist_ok=True)
                    visit(real_path, target, rel)
                else:
                    os.symlink(real_path, target, target_is_directory=True)
            elif entry.is_file():
                os.symlink(real_path, target)

    visit(root_path, shadow_path, "")
    for rel, content in [*overrides.items(), *extra_files.items()]:
        target = shadow_path / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")


def atomic_write(file: str | Path, content: str) -> None:
    """Write one file atomically: same-directory temporary, fsync, replace."""
    path = Path(file)
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(
        dir=path.parent, prefix=f".{path.name}.", suffix=".tmp"
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        if path.exists():
            os.chmod(temporary, path.stat().st_mode & 0o777)
        os.replace(temporary, path)
    except Exception:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


@contextmanager
def write_lock(lock_file: str | Path, timeout: float = 20.0, stale: float = 120.0) -> Iterator[None]:
    """Serialize one ledger write behind an O_EXCL lock file."""
    path = Path(lock_file)
    path.parent.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    while True:
        try:
            descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            try:
                os.write(descriptor, json.dumps({"pid": os.getpid(), "at": time.time()}).encode("utf-8"))
            finally:
                os.close(descriptor)
            break
        except FileExistsError:
            try:
                current = path.stat().st_mtime
            except FileNotFoundError:
                continue
            if time.time() - current > stale:
                try:
                    path.unlink()
                except OSError:
                    pass
                continue
            if time.monotonic() - started > timeout:
                fail("lock_timeout", "the ledger is being written by another operation; try again shortly", {"lockFile": str(path)})
            time.sleep(0.04 + random.random() * 0.08)
    try:
        yield
    finally:
        try:
            path.unlink()
        except OSError:
            pass
