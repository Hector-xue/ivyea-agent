"""Small cross-process file locks for local mutable stores."""
from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
import threading
import time
from typing import Any, Callable, Iterator, TypeVar, cast

try:  # pragma: no cover - exercised on supported Unix deployments
    import fcntl
except ImportError:  # pragma: no cover - fallback for non-Unix development
    fcntl = None  # type: ignore[assignment]


F = TypeVar("F", bound=Callable[..., Any])
_PROCESS_LOCKS: dict[str, threading.RLock] = {}
_PROCESS_LOCKS_GUARD = threading.Lock()


class LockTimeoutError(TimeoutError):
    """Raised when another writer holds a local-store lock too long."""


def _process_lock(path: Path) -> threading.RLock:
    key = str(path.resolve())
    with _PROCESS_LOCKS_GUARD:
        return _PROCESS_LOCKS.setdefault(key, threading.RLock())


@contextmanager
def exclusive_file_lock(path: Path, *, timeout: float = 10.0) -> Iterator[None]:
    """Take a bounded process and OS-level exclusive lock for ``path``."""
    lock_path = Path(path)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    process_lock = _process_lock(lock_path)
    if not process_lock.acquire(timeout=max(0.0, float(timeout))):
        raise LockTimeoutError(f"timed out waiting for lock: {lock_path}")
    fh = None
    try:
        fh = lock_path.open("a+", encoding="utf-8")
        if fcntl is not None:
            deadline = time.monotonic() + max(0.0, float(timeout))
            while True:
                try:
                    fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError as exc:
                    if time.monotonic() >= deadline:
                        raise LockTimeoutError(f"timed out waiting for lock: {lock_path}") from exc
                    time.sleep(0.02)
        yield
    finally:
        if fh is not None:
            if fcntl is not None:
                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
            fh.close()
        process_lock.release()


def serialized(path_factory: Callable[[], Path], *, timeout: float = 10.0) -> Callable[[F], F]:
    """Serialize a function across processes using a lazily resolved lock path."""
    def decorate(func: F) -> F:
        def wrapped(*args: Any, **kwargs: Any) -> Any:
            with exclusive_file_lock(path_factory(), timeout=timeout):
                return func(*args, **kwargs)

        wrapped.__name__ = func.__name__
        wrapped.__doc__ = func.__doc__
        wrapped.__module__ = func.__module__
        return cast(F, wrapped)

    return decorate
