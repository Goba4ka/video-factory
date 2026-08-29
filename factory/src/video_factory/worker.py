"""Headless queue worker with fenced heartbeats and resource-first claiming.

The worker is intentionally executor-agnostic.  Editorial agent backends can be
registered as Python callables, while the CLI adapter executes one explicitly
configured argv command (never a shell string) and exchanges JSON over stdio.
"""

from __future__ import annotations

import hashlib
import json
import os
import signal
import sqlite3
import subprocess
import tempfile
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol, Sequence

from .errors import FactoryError, LeaseConflictError, ValidationError
from .queue import Dispatcher
from .validators import canonical_json, require_nonempty_string


def _utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


class TaskExecutor(Protocol):
    """Callable contract used by the worker and future agent backends."""

    def __call__(
        self, task: Mapping[str, Any], stop_event: threading.Event
    ) -> dict[str, Any]: ...


class LockHandle(Protocol):
    def acquire(self, timeout_seconds: float) -> bool: ...

    def release(self) -> None: ...


class ExecutorError(Exception):
    """Bounded, serializable handler failure."""

    def __init__(self, code: str, message: str, *, retryable: bool = True):
        super().__init__(message)
        self.code = code
        self.retryable = retryable


class ExecutorRegistry:
    """Resolve executors by exact task kind with an optional ``*`` fallback."""

    def __init__(self) -> None:
        self._executors: dict[str, TaskExecutor] = {}

    def register(self, kind: str, executor: TaskExecutor) -> None:
        normalized = require_nonempty_string(kind, "kind")
        if normalized in self._executors:
            raise ValidationError(f"executor already registered for kind {normalized!r}")
        if not callable(executor):
            raise ValidationError("executor must be callable")
        self._executors[normalized] = executor

    def resolve(self, task: Mapping[str, Any]) -> TaskExecutor:
        kind = require_nonempty_string(task.get("kind"), "task.kind")
        executor = self._executors.get(kind) or self._executors.get("*")
        if executor is None:
            raise ExecutorError(
                "executor_not_registered",
                f"no executor is registered for task kind {kind!r}",
                retryable=False,
            )
        return executor


class ResourceLock:
    """Small cross-platform advisory file lock kept for one complete task."""

    def __init__(self, path: str | Path):
        self.path = Path(path).expanduser().resolve()
        self._handle: Any = None

    def acquire(self, timeout_seconds: float) -> bool:
        if timeout_seconds < 0:
            raise ValidationError("lock timeout must be non-negative")
        if self._handle is not None:
            raise RuntimeError("resource lock is already held")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = self.path.open("a+b")
        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(b"\0")
            handle.flush()
        deadline = time.monotonic() + timeout_seconds
        while True:
            try:
                handle.seek(0)
                if os.name == "nt":
                    import msvcrt

                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                self._handle = handle
                return True
            except (BlockingIOError, OSError):
                if time.monotonic() >= deadline:
                    handle.close()
                    return False
                time.sleep(min(0.05, max(0.0, deadline - time.monotonic())))

    def release(self) -> None:
        handle, self._handle = self._handle, None
        if handle is None:
            return
        try:
            handle.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()

    def __enter__(self) -> ResourceLock:
        if not self.acquire(0):
            raise RuntimeError(f"resource lock is busy: {self.path}")
        return self

    def __exit__(self, *_: object) -> None:
        self.release()


class NullResourceLock:
    """Lock-compatible no-op for roles without a scarce local resource."""

    def acquire(self, timeout_seconds: float) -> bool:
        if timeout_seconds < 0:
            raise ValidationError("lock timeout must be non-negative")
        return True

    def release(self) -> None:
        return None


def _public_task(
    task: Mapping[str, Any], upstream_results: Sequence[Mapping[str, Any]]
) -> dict[str, Any]:
    """Remove fencing credentials before handing a task to an executor."""

    allowed = (
        "id",
        "job_id",
        "dependency_task_id",
        "role",
        "pod",
        "kind",
        "payload",
        "priority",
        "attempt_count",
        "max_attempts",
        "created_at",
    )
    public = {key: task.get(key) for key in allowed}
    public["upstream_results"] = [dict(item) for item in upstream_results]
    return public


def _terminate_process(process: subprocess.Popen[bytes], *, kill: bool = False) -> None:
    try:
        if os.name != "nt":
            os.killpg(process.pid, signal.SIGKILL if kill else signal.SIGTERM)
        elif kill:
            process.kill()
        else:
            try:
                process.send_signal(signal.CTRL_BREAK_EVENT)
            except (AttributeError, OSError):
                process.terminate()
    except (OSError, ProcessLookupError):
        pass


class SubprocessExecutor:
    """Execute a trusted argv handler using a bounded JSON stdio contract."""

    def __init__(
        self,
        argv: Sequence[str],
        *,
        cwd: str | Path | None = None,
        timeout_seconds: float = 7200,
        shutdown_grace_seconds: float = 30,
        max_input_bytes: int = 4 * 1024 * 1024,
        max_output_bytes: int = 4 * 1024 * 1024,
        max_stderr_bytes: int = 1024 * 1024,
        poll_seconds: float = 0.1,
    ):
        if not argv or not all(isinstance(value, str) and value for value in argv):
            raise ValidationError("handler argv must contain non-empty strings")
        if timeout_seconds <= 0 or shutdown_grace_seconds < 0 or poll_seconds <= 0:
            raise ValidationError("handler timing values are invalid")
        if not 1024 <= max_input_bytes <= 64 * 1024 * 1024:
            raise ValidationError("max_input_bytes must be from 1024 to 67108864")
        if not 1024 <= max_output_bytes <= 64 * 1024 * 1024:
            raise ValidationError("max_output_bytes must be from 1024 to 67108864")
        if not 1024 <= max_stderr_bytes <= 64 * 1024 * 1024:
            raise ValidationError("max_stderr_bytes must be from 1024 to 67108864")
        self.argv = tuple(argv)
        self.cwd = str(Path(cwd).expanduser().resolve()) if cwd else None
        self.timeout_seconds = float(timeout_seconds)
        self.shutdown_grace_seconds = float(shutdown_grace_seconds)
        self.max_input_bytes = int(max_input_bytes)
        self.max_output_bytes = int(max_output_bytes)
        self.max_stderr_bytes = int(max_stderr_bytes)
        self.poll_seconds = float(poll_seconds)

    def __call__(
        self, task: Mapping[str, Any], stop_event: threading.Event
    ) -> dict[str, Any]:
        request = (canonical_json(dict(task)) + "\n").encode("utf-8")
        if len(request) > self.max_input_bytes:
            raise ExecutorError(
                "handler_input_too_large",
                f"handler input exceeded {self.max_input_bytes} bytes",
                retryable=False,
            )
        started = time.monotonic()
        shutdown_seen_at: float | None = None
        creation_flags = (
            subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
        )
        with (
            tempfile.TemporaryFile() as stdin_file,
            tempfile.TemporaryFile() as stdout_file,
            tempfile.TemporaryFile() as stderr_file,
        ):
            stdin_file.write(request)
            stdin_file.seek(0)
            try:
                process = subprocess.Popen(
                    self.argv,
                    cwd=self.cwd,
                    stdin=stdin_file,
                    stdout=stdout_file,
                    stderr=stderr_file,
                    shell=False,
                    start_new_session=os.name != "nt",
                    creationflags=creation_flags,
                )
            except OSError as exc:
                raise ExecutorError("handler_start_failed", str(exc)) from exc
            try:
                while process.poll() is None:
                    elapsed = time.monotonic() - started
                    stdout_size = os.fstat(stdout_file.fileno()).st_size
                    stderr_size = os.fstat(stderr_file.fileno()).st_size
                    if stdout_size > self.max_output_bytes:
                        _terminate_process(process, kill=True)
                        process.wait(timeout=5)
                        raise ExecutorError(
                            "handler_output_too_large",
                            f"handler stdout exceeded {self.max_output_bytes} bytes",
                            retryable=False,
                        )
                    if stderr_size > self.max_stderr_bytes:
                        _terminate_process(process, kill=True)
                        process.wait(timeout=5)
                        raise ExecutorError(
                            "handler_stderr_too_large",
                            f"handler stderr exceeded {self.max_stderr_bytes} bytes",
                            retryable=False,
                        )
                    if elapsed >= self.timeout_seconds:
                        _terminate_process(process)
                        try:
                            process.wait(timeout=5)
                        except subprocess.TimeoutExpired:
                            _terminate_process(process, kill=True)
                            process.wait(timeout=5)
                        raise ExecutorError(
                            "handler_timeout",
                            f"handler exceeded {self.timeout_seconds:g} seconds",
                        )
                    if stop_event.is_set():
                        if shutdown_seen_at is None:
                            shutdown_seen_at = time.monotonic()
                            _terminate_process(process)
                        if time.monotonic() - shutdown_seen_at >= self.shutdown_grace_seconds:
                            _terminate_process(process, kill=True)
                            process.wait(timeout=5)
                            raise ExecutorError(
                                "handler_stopped_for_shutdown",
                                "handler did not finish within the shutdown grace period",
                            )
                    time.sleep(self.poll_seconds)
                return_code = int(process.returncode or 0)
            finally:
                if process.poll() is None:
                    _terminate_process(process, kill=True)
                    process.wait(timeout=5)

            stdout_file.seek(0, os.SEEK_END)
            stdout_size = stdout_file.tell()
            stderr_file.seek(0, os.SEEK_END)
            stderr_size = stderr_file.tell()
            stdout_file.seek(0)
            stdout_bytes = stdout_file.read(self.max_output_bytes + 1)
            if stdout_size > self.max_output_bytes:
                raise ExecutorError(
                    "handler_output_too_large",
                    f"handler stdout exceeded {self.max_output_bytes} bytes",
                    retryable=False,
                )
            if stderr_size > self.max_stderr_bytes:
                raise ExecutorError(
                    "handler_stderr_too_large",
                    f"handler stderr exceeded {self.max_stderr_bytes} bytes",
                    retryable=False,
                )
            if return_code != 0:
                raise ExecutorError(
                    "handler_exit_nonzero",
                    f"handler exited with code {return_code}; stderr_bytes={stderr_size}",
                )
            try:
                result = json.loads(stdout_bytes.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise ExecutorError(
                    "handler_invalid_json",
                    "handler stdout must be one UTF-8 JSON object",
                    retryable=False,
                ) from exc
            if not isinstance(result, dict):
                raise ExecutorError(
                    "handler_invalid_result",
                    "handler result must be a JSON object",
                    retryable=False,
                )
            return result


class _LeaseHeartbeat:
    def __init__(
        self,
        dispatcher: Dispatcher,
        task: Mapping[str, Any],
        *,
        worker_id: str,
        lease_seconds: int,
        interval_seconds: float,
    ):
        self.dispatcher = dispatcher
        self.task_id = str(task["id"])
        self.lease_token = str(task["lease_token"])
        self.worker_id = worker_id
        self.lease_seconds = lease_seconds
        self.interval_seconds = interval_seconds
        self._stop = threading.Event()
        self.lost = threading.Event()
        self.error: str | None = None
        self.count = 0
        self._thread = threading.Thread(
            target=self._run,
            name=f"lease-heartbeat-{self.task_id}",
            daemon=True,
        )

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=max(35.0, self.interval_seconds + 1.0))
        if self._thread.is_alive():
            self.error = "heartbeat thread did not stop before acknowledgement"
            self.lost.set()

    def _run(self) -> None:
        while not self._stop.wait(self.interval_seconds):
            try:
                self.dispatcher.renew_lease(
                    self.task_id,
                    lease_token=self.lease_token,
                    worker_id=self.worker_id,
                    lease_seconds=self.lease_seconds,
                )
                self.count += 1
            except Exception as exc:
                self.error = f"{type(exc).__name__}: {exc}"
                self.lost.set()
                return


@dataclass(frozen=True)
class WorkerConfig:
    worker_id: str
    role: str
    pod: str | None = None
    kind: str | None = None
    lease_seconds: int = 900
    heartbeat_seconds: float = 300
    poll_seconds: float = 2
    lock_timeout_seconds: float = 0
    max_tasks: int = 0
    max_idle_polls: int = 0
    max_runtime_seconds: float = 0
    acknowledgement_attempts: int = 3
    acknowledgement_retry_seconds: float = 0.25
    terminal_on_executor_error: bool = False

    def validate(self) -> None:
        require_nonempty_string(self.worker_id, "worker_id")
        require_nonempty_string(self.role, "role")
        if self.pod is not None:
            require_nonempty_string(self.pod, "pod")
        if self.kind is not None:
            require_nonempty_string(self.kind, "kind")
        if not 5 <= self.lease_seconds <= 86400:
            raise ValidationError("lease_seconds must be from 5 to 86400")
        if not 0 < self.heartbeat_seconds < self.lease_seconds:
            raise ValidationError("heartbeat_seconds must be positive and below lease_seconds")
        if self.poll_seconds <= 0 or self.lock_timeout_seconds < 0:
            raise ValidationError("poll and lock timeout values are invalid")
        if self.max_tasks < 0 or self.max_idle_polls < 0 or self.max_runtime_seconds < 0:
            raise ValidationError("worker bounds must be non-negative")
        if not 1 <= self.acknowledgement_attempts <= 10:
            raise ValidationError("acknowledgement_attempts must be from 1 to 10")
        if not 0 <= self.acknowledgement_retry_seconds <= 30:
            raise ValidationError(
                "acknowledgement_retry_seconds must be from 0 to 30"
            )


class HeadlessWorker:
    """Resource-safe, bounded queue consumer with graceful drain semantics."""

    def __init__(
        self,
        dispatcher: Dispatcher,
        executors: ExecutorRegistry,
        config: WorkerConfig,
        *,
        resource_lock: LockHandle | None = None,
        event_callback: Callable[[dict[str, Any]], None] | None = None,
    ):
        config.validate()
        self.dispatcher = dispatcher
        self.executors = executors
        self.config = config
        self.resource_lock = resource_lock or NullResourceLock()
        self.event_callback = event_callback or (lambda event: None)

    def _event(self, name: str, **fields: Any) -> None:
        event = {"event": name, "at": _utc_now(), **fields}
        try:
            self.event_callback(event)
        except Exception:
            # Logging must never change queue state or lose a lease.
            pass

    @staticmethod
    def _ack_key(prefix: str, task: Mapping[str, Any]) -> str:
        fenced = hashlib.sha256(str(task["lease_token"]).encode("utf-8")).hexdigest()[:20]
        return f"worker-{prefix}:{task['id']}:{fenced}"

    def _acknowledge(self, operation: Callable[[], dict[str, Any]]) -> dict[str, Any]:
        """Retry an ambiguous local I/O failure with the exact same operation key."""

        last_error: BaseException | None = None
        for attempt in range(1, self.config.acknowledgement_attempts + 1):
            try:
                return operation()
            except (sqlite3.Error, OSError) as exc:
                last_error = exc
                if attempt >= self.config.acknowledgement_attempts:
                    break
                time.sleep(self.config.acknowledgement_retry_seconds)
        assert last_error is not None
        raise ExecutorError(
            "acknowledgement_unavailable",
            f"queue acknowledgement failed after "
            f"{self.config.acknowledgement_attempts} attempts: "
            f"{type(last_error).__name__}",
        ) from last_error

    def run(self, stop_event: threading.Event | None = None) -> dict[str, Any]:
        stop = stop_event or threading.Event()
        runtime_expired = threading.Event()
        runtime_timer: threading.Timer | None = None
        if self.config.max_runtime_seconds:
            def expire_runtime() -> None:
                runtime_expired.set()
                stop.set()

            runtime_timer = threading.Timer(
                self.config.max_runtime_seconds, expire_runtime
            )
            runtime_timer.daemon = True
            runtime_timer.start()
        started_at = _utc_now()
        started_monotonic = time.monotonic()
        run_id = uuid.uuid4().hex
        claim_sequence = 0
        claimed = succeeded = failed = retried = dead = lease_lost = idle_polls = 0
        stop_reason = "shutdown_requested" if stop.is_set() else "running"
        fatal_error: dict[str, str] | None = None

        while not stop.is_set():
            elapsed = time.monotonic() - started_monotonic
            if self.config.max_runtime_seconds and elapsed >= self.config.max_runtime_seconds:
                runtime_expired.set()
                stop_reason = "max_runtime"
                break
            if self.config.max_tasks and claimed >= self.config.max_tasks:
                stop_reason = "max_tasks"
                break
            if self.config.max_idle_polls and idle_polls >= self.config.max_idle_polls:
                stop_reason = "max_idle_polls"
                break

            # Scarce resource is acquired before claim, so a queued task never
            # burns lease time while this process waits for GPU/media capacity.
            if not self.resource_lock.acquire(self.config.lock_timeout_seconds):
                idle_polls += 1
                self._event("resource_busy", role=self.config.role)
                stop.wait(self.config.poll_seconds)
                continue
            try:
                claim_sequence += 1
                claimed_response = self.dispatcher.claim(
                    worker_id=self.config.worker_id,
                    role=self.config.role,
                    pod=self.config.pod,
                    kind=self.config.kind,
                    lease_seconds=self.config.lease_seconds,
                    idempotency_key=f"worker-claim:{run_id}:{claim_sequence}",
                )
                task = claimed_response["task"]
                if task is None:
                    idle_polls += 1
                    self._event("queue_idle", role=self.config.role)
                    # Release the scarce slot before the idle backoff.  The
                    # enclosing finally is deliberately idempotent.
                    self.resource_lock.release()
                    stop.wait(self.config.poll_seconds)
                    continue

                idle_polls = 0
                claimed += 1
                task_started = time.monotonic()
                task_fields = {
                    "task_id": task["id"],
                    "job_id": task.get("job_id"),
                    "role": task["role"],
                    "pod": task["pod"],
                    "attempt": task["attempt_count"],
                }
                self._event("task_started", **task_fields)
                heartbeat = _LeaseHeartbeat(
                    self.dispatcher,
                    task,
                    worker_id=self.config.worker_id,
                    lease_seconds=self.config.lease_seconds,
                    interval_seconds=self.config.heartbeat_seconds,
                )
                heartbeat.start()
                result: dict[str, Any] | None = None
                executor_error: ExecutorError | None = None
                context_lease_error: LeaseConflictError | None = None
                context_runtime_error: BaseException | None = None
                try:
                    context = self.dispatcher.execution_context(
                        task["id"],
                        lease_token=task["lease_token"],
                        worker_id=self.config.worker_id,
                    )
                except LeaseConflictError as exc:
                    context_lease_error = exc
                except FactoryError as exc:
                    executor_error = ExecutorError(
                        "upstream_context_invalid",
                        f"{exc.code}: {exc}",
                        retryable=False,
                    )
                except Exception as exc:
                    context_runtime_error = exc
                else:
                    try:
                        executor = self.executors.resolve(task)
                        result = executor(
                            _public_task(task, context["upstream_results"]), stop
                        )
                        if not isinstance(result, dict):
                            raise ExecutorError(
                                "executor_invalid_result",
                                "executor must return a JSON object",
                                retryable=False,
                            )
                    except ExecutorError as exc:
                        executor_error = exc
                    except Exception as exc:
                        executor_error = ExecutorError(
                            "executor_exception",
                            f"{type(exc).__name__}: {exc}",
                        )
                finally:
                    heartbeat.stop()

                duration = round(time.monotonic() - task_started, 3)
                if context_runtime_error is not None:
                    fatal_error = {
                        "code": "upstream_context_unavailable",
                        "message": type(context_runtime_error).__name__,
                    }
                    stop_reason = "upstream_context_unavailable"
                    self._event(
                        "upstream_context_unavailable",
                        **task_fields,
                        duration_seconds=duration,
                        heartbeat_count=heartbeat.count,
                    )
                    break
                if context_lease_error is not None or heartbeat.lost.is_set():
                    lease_lost += 1
                    fatal_error = {
                        "code": "lease_lost",
                        "message": (
                            str(context_lease_error)
                            if context_lease_error is not None
                            else heartbeat.error or "heartbeat failed"
                        ),
                    }
                    stop_reason = "lease_lost"
                    self._event(
                        "lease_lost",
                        **task_fields,
                        duration_seconds=duration,
                        heartbeat_count=heartbeat.count,
                    )
                    break

                if executor_error is None:
                    try:
                        self._acknowledge(
                            lambda: self.dispatcher.complete(
                                task["id"],
                                lease_token=task["lease_token"],
                                result=result,
                                idempotency_key=self._ack_key("complete", task),
                            )
                        )
                    except LeaseConflictError as exc:
                        lease_lost += 1
                        fatal_error = {"code": exc.code, "message": str(exc)}
                        stop_reason = "lease_lost"
                        break
                    except ExecutorError as exc:
                        fatal_error = {"code": exc.code, "message": str(exc)}
                        stop_reason = "acknowledgement_unavailable"
                        break
                    except FactoryError as exc:
                        executor_error = ExecutorError(
                            "completion_rejected",
                            f"{exc.code}: {exc}",
                            retryable=False,
                        )
                    else:
                        succeeded += 1
                        self._event(
                            "task_succeeded",
                            **task_fields,
                            duration_seconds=duration,
                            heartbeat_count=heartbeat.count,
                        )
                        continue

                assert executor_error is not None
                error_body = {
                    "code": executor_error.code,
                    "message": str(executor_error)[:4000],
                    "retryable": executor_error.retryable,
                }
                terminal = (
                    self.config.terminal_on_executor_error
                    or not executor_error.retryable
                )
                try:
                    failure = self._acknowledge(
                        lambda: self.dispatcher.fail(
                            task["id"],
                            lease_token=task["lease_token"],
                            error=error_body,
                            terminal=terminal,
                            idempotency_key=self._ack_key("fail", task),
                        )
                    )
                except LeaseConflictError as exc:
                    lease_lost += 1
                    fatal_error = {"code": exc.code, "message": str(exc)}
                    stop_reason = "lease_lost"
                    break
                except ExecutorError as exc:
                    fatal_error = {"code": exc.code, "message": str(exc)}
                    stop_reason = "acknowledgement_unavailable"
                    break
                failed += 1
                if failure["retried"]:
                    retried += 1
                else:
                    dead += 1
                self._event(
                    "task_failed",
                    **task_fields,
                    duration_seconds=duration,
                    error_code=executor_error.code,
                    retried=failure["retried"],
                    heartbeat_count=heartbeat.count,
                )
            finally:
                self.resource_lock.release()

            if not stop.is_set():
                stop.wait(self.config.poll_seconds)

        if runtime_timer is not None:
            runtime_timer.cancel()
        if runtime_expired.is_set() and stop_reason == "running":
            stop_reason = "max_runtime"
        elif stop.is_set() and stop_reason == "running":
            stop_reason = "shutdown_requested"
        finished_at = _utc_now()
        return {
            "ok": fatal_error is None,
            "command": "worker",
            "worker_id": self.config.worker_id,
            "role": self.config.role,
            "pod": self.config.pod,
            "kind": self.config.kind,
            "started_at": started_at,
            "finished_at": finished_at,
            "duration_seconds": round(time.monotonic() - started_monotonic, 3),
            "stop_reason": stop_reason,
            "shutdown_requested": stop.is_set() and not runtime_expired.is_set(),
            "claimed": claimed,
            "succeeded": succeeded,
            "failed": failed,
            "retried": retried,
            "dead": dead,
            "lease_lost": lease_lost,
            "idle_polls": idle_polls,
            "fatal_error": fatal_error,
        }


def install_shutdown_handlers(stop_event: threading.Event) -> Callable[[], None]:
    """Install SIGINT/SIGTERM drain handlers and return a restoration callback."""

    if threading.current_thread() is not threading.main_thread():
        return lambda: None
    previous: dict[int, Any] = {}

    def request_shutdown(signum: int, _frame: Any) -> None:
        stop_event.set()

    for signum in (signal.SIGINT, signal.SIGTERM):
        previous[signum] = signal.getsignal(signum)
        signal.signal(signum, request_shutdown)

    def restore() -> None:
        for signum, handler in previous.items():
            signal.signal(signum, handler)

    return restore


def default_resource_lock_path(role: str) -> Path | None:
    """Serialize roles that contend for the same GPU/decode-heavy host slot."""

    if role not in {
        "bgm",
        "audio_mix",
        "render",
        "qc_auto_evidence",
        "caption_transcript",
        "dedup_analyzer",
        "visual_analyzer",
        "qc",
    }:
        return None
    root = Path(
        os.environ.get(
            "VIDEO_FACTORY_RUNTIME_ROOT",
            Path.home() / ".video-factory-runtime",
        )
    ).expanduser().resolve()
    return root / "locks" / "gpu-heavy.lock"


__all__ = [
    "ExecutorError",
    "ExecutorRegistry",
    "HeadlessWorker",
    "NullResourceLock",
    "ResourceLock",
    "SubprocessExecutor",
    "TaskExecutor",
    "WorkerConfig",
    "default_resource_lock_path",
    "install_shutdown_handlers",
]
