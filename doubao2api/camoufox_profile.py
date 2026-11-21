"""Persistent Camoufox profile management, OS kernel locks, startup cache, and disk pruning.

Ported from flow2api-Camoufox `src/services/camoufox_profile.py`.
"""

from __future__ import annotations

from datetime import datetime, timezone
import json
import logging
import os
from pathlib import Path
import shutil
import threading
import time
from typing import Dict, Optional, Tuple, Union

try:
    import fcntl as _fcntl
except ImportError:
    _fcntl = None  # type: ignore[assignment]

try:
    import msvcrt as _msvcrt
except ImportError:
    _msvcrt = None  # type: ignore[assignment]

log = logging.getLogger("doubao2api.camoufox.profile")

PROFILE_STARTUP_DIR_NAME = "profile-startup"
STARTUP_CACHE_ENTRIES = ("compatibility.ini", "startupCache")
PROFILE_KERNEL_LOCK_FILENAME = ".dola2api-profile.lock"

_ACTIVE_PROFILE_KERNEL_LEASES: Dict[Path, Tuple["ProfileLease", str]] = {}
_LEASE_LOCK = threading.Lock()


class ProfileLeaseBusyError(RuntimeError):
    """Raised when another worker or OS process holds the profile directory lock."""


def _canonical_lease_path(account_dir: Path) -> Path:
    try:
        return account_dir.resolve()
    except Exception:
        return account_dir


class ProfileLease:
    """Cross-process + in-process OS kernel lock (`flock`/`msvcrt`) for a Camoufox profile dir."""

    def __init__(self, account_dir: Path, lock_path: Path, fd: int, holder: str) -> None:
        self.account_dir = _canonical_lease_path(account_dir)
        self.lock_path = lock_path
        self.holder = holder
        self._fd: Optional[int] = fd
        self._released = False

    @property
    def active(self) -> bool:
        return not self._released and self._fd is not None

    def release(self) -> None:
        if self._released:
            return
        self._released = True
        with _LEASE_LOCK:
            existing = _ACTIVE_PROFILE_KERNEL_LEASES.get(self.account_dir)
            if existing is not None and existing[0] is self:
                _ACTIVE_PROFILE_KERNEL_LEASES.pop(self.account_dir, None)
        fd = self._fd
        self._fd = None
        if fd is None:
            return
        try:
            if _fcntl is not None:
                _fcntl.flock(fd, _fcntl.LOCK_UN)
            elif _msvcrt is not None:
                os.lseek(fd, 0, os.SEEK_SET)
                _msvcrt.locking(fd, _msvcrt.LK_UNLCK, 1)
        except OSError:
            pass
        finally:
            try:
                os.close(fd)
            except OSError:
                pass

    def __enter__(self) -> "ProfileLease":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.release()

    def __del__(self) -> None:
        try:
            self.release()
        except Exception:
            pass


def _read_lock_holder_diag(lock_path: Path) -> str:
    try:
        raw = lock_path.read_bytes()
        payload = raw.lstrip(b"\x00").decode("utf-8", errors="ignore").strip()
        if payload:
            info = json.loads(payload)
            if isinstance(info, dict):
                return f"pid={info.get('pid')}, holder={info.get('holder')}"
    except Exception:
        pass
    return "unknown holder"


def acquire_profile_lease(
    account_dir: Union[str, Path],
    holder: str = "worker",
    *,
    wait_seconds: float = 0.0,
    poll_interval: float = 0.15,
) -> ProfileLease:
    """Acquire exclusive in-process and OS kernel lock on `account_dir`."""
    target_dir = _canonical_lease_path(Path(account_dir))
    target_dir.mkdir(parents=True, exist_ok=True)
    lock_path = target_dir / PROFILE_KERNEL_LOCK_FILENAME
    deadline = time.monotonic() + max(0.0, float(wait_seconds or 0.0))
    step = max(0.02, min(0.5, float(poll_interval or 0.15)))

    while True:
        busy_err: Optional[ProfileLeaseBusyError] = None
        with _LEASE_LOCK:
            active_entry = _ACTIVE_PROFILE_KERNEL_LEASES.get(target_dir)
            if active_entry is not None and active_entry[0].active:
                busy_err = ProfileLeaseBusyError(
                    f"Browser profile {target_dir.name} is already held in-process ({active_entry[1]})"
                )
            else:
                fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
                try:
                    if _fcntl is not None:
                        _fcntl.flock(fd, _fcntl.LOCK_EX | _fcntl.LOCK_NB)
                    elif _msvcrt is not None:
                        os.lseek(fd, 0, os.SEEK_END)
                        if os.lseek(fd, 0, os.SEEK_CUR) == 0:
                            os.write(fd, b"\x00")
                        os.lseek(fd, 0, os.SEEK_SET)
                        _msvcrt.locking(fd, _msvcrt.LK_NBLCK, 1)
                except OSError as exc:
                    try:
                        os.close(fd)
                    except OSError:
                        pass
                    diag = _read_lock_holder_diag(lock_path)
                    busy_err = ProfileLeaseBusyError(
                        f"Browser profile {target_dir.name} is locked by another process ({diag})"
                    )
                    busy_err.__cause__ = exc
                else:
                    meta = json.dumps(
                        {
                            "pid": os.getpid(),
                            "holder": str(holder or "worker"),
                            "acquired_at": datetime.now(timezone.utc).isoformat(),
                        },
                        ensure_ascii=False,
                    ).encode("utf-8")
                    try:
                        os.lseek(fd, 0, os.SEEK_SET)
                        os.ftruncate(fd, 0)
                        os.write(fd, b"\x00" + meta)
                    except OSError:
                        pass
                    lease = ProfileLease(target_dir, lock_path, fd, str(holder or "worker"))
                    _ACTIVE_PROFILE_KERNEL_LEASES[target_dir] = (lease, str(holder or "worker"))
                    return lease

        if time.monotonic() >= deadline:
            assert busy_err is not None
            raise busy_err
        time.sleep(min(step, max(0.01, deadline - time.monotonic())))


def _copy_startup_cache_entries(source: Path, target: Path) -> None:
    for entry_name in STARTUP_CACHE_ENTRIES:
        src_entry = source / entry_name
        if not src_entry.exists():
            continue
        dst_entry = target / entry_name
        if src_entry.is_dir():
            shutil.copytree(src_entry, dst_entry, dirs_exist_ok=True)
        else:
            dst_entry.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src_entry, dst_entry)


def restore_startup_cache(account_dir: Path, profile_dir: Path) -> None:
    """Copy cached `compatibility.ini` and `startupCache` into `profile_dir`."""
    source_root = account_dir / PROFILE_STARTUP_DIR_NAME
    if not source_root.exists():
        return
    try:
        _copy_startup_cache_entries(source_root, profile_dir)
    except Exception as exc:
        log.debug("[Camoufox] Restore startup cache failed: %s", exc)


def save_startup_cache(profile_dir: Path, account_dir: Path) -> None:
    """Persist `compatibility.ini` and `startupCache` back to `account_dir/profile-startup`."""
    if not (profile_dir / "startupCache").exists():
        return
    target = account_dir / PROFILE_STARTUP_DIR_NAME
    staging = account_dir / f"{PROFILE_STARTUP_DIR_NAME}.new"
    shutil.rmtree(staging, ignore_errors=True)
    try:
        _copy_startup_cache_entries(profile_dir, staging)
        shutil.rmtree(target, ignore_errors=True)
        staging.rename(target)
    except Exception as exc:
        shutil.rmtree(staging, ignore_errors=True)
        log.debug("[Camoufox] Save startup cache failed: %s", exc)


def clear_stale_firefox_profile_locks(profile_dir: Path) -> None:
    """Remove stale Firefox profile lock files after a forced exit."""
    for lock_name in (".parentlock", "lock", "parent.lock"):
        lock_path = profile_dir / lock_name
        try:
            if lock_path.exists() or lock_path.is_symlink():
                lock_path.unlink(missing_ok=True)
        except Exception:
            pass


def prune_camoufox_profile_bloat(profile_dir: Path) -> None:
    """Remove non-essential heavy Firefox caches/crashes to keep profile lean."""
    for rel in ("crashes", "minidumps", "datareporting", "saved-telemetry-pings", "shader-cache"):
        target = profile_dir / rel
        if target.exists():
            shutil.rmtree(target, ignore_errors=True)
