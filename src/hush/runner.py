import contextlib
import os
import select
import signal
import subprocess
import threading
import time
from collections import deque
from collections.abc import Generator, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

CHUNK_BYTES = 64 * 1024
POLL_SECONDS = 0.1
# How long to keep reading after the command has exited. Covers output still in the pipe, and
# gives up on background processes that inherited the pipe and never close it.
DRAIN_GRACE_SECONDS = 1.0
# How long a command gets to stop after SIGTERM before the whole process group is killed.
TERM_GRACE_SECONDS = 2.0
EXIT_TIMEOUT = 124
EXIT_NOT_EXECUTABLE = 126
EXIT_NOT_FOUND = 127
_SIGNAL_EXIT_BASE = 128
_HANDLED_SIGNALS = (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)


@dataclass(frozen=True, slots=True)
class RunResult:
    exit_code: int
    seconds: float
    output: bytes = b""
    timed_out: bool = False
    interrupted_by: int | None = None
    omitted_bytes: int = 0
    spawn_error: str | None = None


class _Interrupted(Exception):
    pass


@dataclass(slots=True)
class _Signals:
    # While armed, the first signal raises `_Interrupted`. Every signal after that kills the group.
    armed: bool = False
    first: int | None = None


class _Capture:
    """Keeps the first and last halves of the output, so memory is bounded by `max_bytes`.

    The end matters most: test runners print their failure summary last.
    """

    def __init__(self, max_bytes: int) -> None:
        self._head_limit = max_bytes // 2
        self._tail_limit = max_bytes - self._head_limit
        self._head = bytearray()
        self._tail: deque[bytes] = deque()
        self._tail_size = 0
        self._total = 0
        self._lock = threading.Lock()

    def add(self, chunk: bytes) -> None:
        with self._lock:
            self._total += len(chunk)
            room = self._head_limit - len(self._head)
            if room > 0:
                self._head += chunk[:room]
                chunk = chunk[room:]
            if chunk:
                self._tail.append(chunk)
                self._tail_size += len(chunk)
                while self._tail_size - len(self._tail[0]) >= self._tail_limit:
                    self._tail_size -= len(self._tail.popleft())

    def snapshot(self) -> tuple[bytes, int]:
        """The kept output, and how many bytes were left out between its two halves."""
        with self._lock:
            head = bytes(self._head)
            tail = b"".join(self._tail)[-self._tail_limit :]
            total = self._total
        if total <= len(head) + len(tail):
            return head + tail, 0
        tail = tail[tail.find(b"\n") + 1 :]
        omitted = total - len(head) - len(tail)
        return head + f"\n[hush: {omitted} bytes omitted]\n".encode() + tail, omitted


def run_command(
    argv: Sequence[str],
    *,
    cwd: Path,
    env: Mapping[str, str],
    timeout: float | None,
    max_capture_bytes: int,
) -> RunResult:
    """Run argv with stdin closed and stdout and stderr merged, and return what it printed.

    The command gets its own process group. On timeout, or when hush is interrupted
    (SIGINT, SIGTERM, SIGHUP), the whole group is stopped, so no test process is left behind.
    A further signal while the group is stopping kills it at once.
    The run ends when the command exits. A background process that keeps the output pipe open
    cannot hold hush up.
    """
    with _default_sigchld():
        return _run(argv, cwd=cwd, env=env, timeout=timeout, max_capture_bytes=max_capture_bytes)


def _run(
    argv: Sequence[str],
    *,
    cwd: Path,
    env: Mapping[str, str],
    timeout: float | None,
    max_capture_bytes: int,
) -> RunResult:
    started = time.monotonic()
    try:
        proc = subprocess.Popen(
            argv,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            cwd=cwd,
            env=env,
            start_new_session=True,
        )
    except OSError as error:
        code = EXIT_NOT_FOUND if isinstance(error, FileNotFoundError) else EXIT_NOT_EXECUTABLE
        return RunResult(code, time.monotonic() - started, spawn_error=f"{argv[0]}: {error.strerror}")

    assert proc.stdout is not None
    capture = _Capture(max_capture_bytes)
    stop_reading = threading.Event()
    reader = threading.Thread(target=_pump, args=(proc.stdout.fileno(), capture, stop_reading), daemon=True)
    reader.start()
    timed_out = False
    with _interruptible(proc) as signals:
        try:
            signals.armed = True
            try:
                proc.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                signals.armed = False
                timed_out = True
                _stop_group(proc)
            signals.armed = False
        except _Interrupted:
            _stop_group(proc)
        finally:
            reader.join(DRAIN_GRACE_SECONDS)
            stop_reading.set()
            reader.join()
            proc.stdout.close()
    interrupted_by = signals.first

    output, omitted = capture.snapshot()
    if timed_out:
        exit_code = EXIT_TIMEOUT
    elif interrupted_by is not None:
        exit_code = _SIGNAL_EXIT_BASE + interrupted_by
    else:
        return_code = proc.returncode
        exit_code = return_code if return_code >= 0 else _SIGNAL_EXIT_BASE - return_code
    return RunResult(
        exit_code,
        time.monotonic() - started,
        output=output,
        timed_out=timed_out,
        interrupted_by=interrupted_by,
        omitted_bytes=omitted,
    )


def _pump(fd: int, capture: _Capture, stop: threading.Event) -> None:
    while not stop.is_set():
        try:
            ready, _, _ = select.select([fd], [], [], POLL_SECONDS)
            chunk = os.read(fd, CHUNK_BYTES) if ready else None
        except (OSError, ValueError):
            return
        if chunk == b"":
            return
        if chunk:
            capture.add(chunk)


@contextlib.contextmanager
def _default_sigchld() -> Generator[None]:
    """Undo an inherited SIGCHLD of SIG_IGN for the run.

    With SIGCHLD ignored, Linux reaps children by itself and `waitpid` fails, which `Popen`
    reports as exit status 0. A failing command would then pass.
    """
    if threading.current_thread() is not threading.main_thread():
        yield
        return
    previous = signal.signal(signal.SIGCHLD, signal.SIG_DFL)
    try:
        yield
    finally:
        signal.signal(signal.SIGCHLD, previous)


@contextlib.contextmanager
def _interruptible(proc: subprocess.Popen[bytes]) -> Generator[_Signals]:
    """Handle SIGINT, SIGTERM and SIGHUP until the block ends, including while the group stops.

    The first signal while armed raises `_Interrupted`. Any signal after that, or one that arrives
    while not armed, kills the process group so that shutdown cannot be stalled by a command that
    ignores SIGTERM.
    """
    signals = _Signals()
    if threading.current_thread() is not threading.main_thread():
        yield signals
        return

    def handle(signum: int, _frame: object) -> None:
        if signals.first is None:
            signals.first = signum
        if signals.armed:
            signals.armed = False
            raise _Interrupted
        _signal_group(proc, signal.SIGKILL)

    previous = {sig: signal.signal(sig, handle) for sig in _HANDLED_SIGNALS}
    try:
        yield signals
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)


def _stop_group(proc: subprocess.Popen[bytes]) -> None:
    """Ask the process group to stop, then kill whatever is left."""
    _signal_group(proc, signal.SIGTERM)
    with contextlib.suppress(subprocess.TimeoutExpired):
        proc.wait(TERM_GRACE_SECONDS)
    _signal_group(proc, signal.SIGKILL)
    proc.wait()


def _signal_group(proc: subprocess.Popen[bytes], sig: signal.Signals) -> None:
    # PermissionError: macOS refuses to signal a group whose leader has already exited.
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(proc.pid, sig)
