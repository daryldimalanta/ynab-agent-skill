# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Daryl Dimalanta

"""Store private drafts and recovery progress on verified local filesystems."""

from __future__ import annotations

import json
import fcntl
import hashlib
import os
import re
import stat
import subprocess
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


# Keep this independent of the public command name: changing it would hide
# existing acknowledgment, locks, and pending recovery records from new callers.
APP_DIR_NAME = "Budget Tools for YNAB"
TERMS_VERSION = "ynab-third-party-notice-v1"
# Seal reviewed intent, not operational progress such as status or readback results.
REVIEW_FIELDS = ("id", "operation", "workflow", "created_at", "plan_id", "payload",
                "proposal", "before_fingerprint", "before_invariants", "before_summary",
                "before_detail", "changes", "summary")


class StateError(RuntimeError):
    """Raised for invalid, unsafe, or missing local state."""


def _review_digest(draft: dict[str, Any]) -> str:
    """Checksum reviewed fields; this is not authentication against the same user."""
    reviewed = {field: draft.get(field) for field in REVIEW_FIELDS}
    # Creation learns an ID after the write. Existing targets must never change.
    if draft.get("operation") not in {"create", "receipt_create"}:
        reviewed["transaction_id"] = draft.get("transaction_id")
    raw = json.dumps(reviewed, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _verify_review(draft: dict[str, Any]) -> None:
    if not draft.get("review_digest"):
        raise StateError("Legacy draft has no review seal; reassess and create a fresh draft")
    if draft["review_digest"] != _review_digest(draft):
        raise StateError("Reviewed draft content changed; reassess and create a fresh draft")


def default_state_root() -> Path:
    return Path.home() / "Library" / "Application Support" / APP_DIR_NAME


def _local_path(path: Path) -> Path:
    # Normalize the OS's documented /tmp alias only, never arbitrary symlinks.
    path = path.expanduser().absolute()
    if ".." in path.parts:
        raise StateError("Parent traversal is not allowed in private state paths")
    if path == Path("/tmp") or Path("/tmp") in path.parents:
        path = Path("/private/tmp") / path.relative_to("/tmp")
    roots = (Path.home() / "Library", Path("/private/tmp"))
    if not any(path == root or root in path.parents for root in roots):
        raise StateError("State must remain in the local user Library or a local temporary directory")
    return path


def assert_local_storage(path: Path) -> None:
    """Check cached mount metadata lexically; never resolve unrelated mounts."""
    probe = _local_path(path)
    try:
        result = subprocess.run(
            ["/sbin/mount"], check=False, capture_output=True, text=True
        )
    except OSError as exc:
        raise StateError("Could not verify the state directory's underlying filesystem") from exc
    if result.returncode != 0:
        raise StateError("Could not verify the state directory's underlying filesystem")

    selected_mount = None
    selected_type = None
    for line in result.stdout.splitlines():
        match = re.match(r"^.+ on (.+) \(([^,)]+)", line)
        if not match:
            continue
        mountpoint = Path(match.group(1).replace("\\040", " "))
        if probe == mountpoint or mountpoint in probe.parents:
            if selected_mount is None or len(str(mountpoint)) > len(str(selected_mount)):
                selected_mount = mountpoint
                selected_type = match.group(2).lower()

    if selected_mount is None or selected_type is None:
        raise StateError("Could not determine the state directory's underlying filesystem")
    if selected_type not in {"apfs", "hfs"}:
        raise StateError("Refusing private state on an uncertain or non-local filesystem")


@contextmanager
def _directory(path: Path, *, create: bool = False, private: bool = False):
    """Walk pinned directory descriptors; never follow a directory symlink."""
    path = _local_path(path)
    assert_local_storage(path)
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    descriptor = os.open("/", flags)
    try:
        for component in path.parts[1:]:
            try:
                child = os.open(component, flags, dir_fd=descriptor)
            except FileNotFoundError:
                if not create:
                    raise
                try:
                    os.mkdir(component, mode=0o700, dir_fd=descriptor)
                except FileExistsError:
                    pass
                child = os.open(component, flags, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
        info = os.fstat(descriptor)
        if private and (info.st_uid != os.getuid() or info.st_mode & 0o077):
            raise StateError("State directories must be owned by the current user with mode 700")
        yield descriptor
    except OSError as exc:
        raise StateError("Could not safely access the local state directory") from exc
    finally:
        os.close(descriptor)


def _private_file(info: os.stat_result) -> None:
    # Reject shared inodes as well as symlinks: a hard link could expose private
    # writes through another filename even when this path's permissions look safe.
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise StateError("Private state must be one regular local file")
    if info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise StateError("Private files must be owned by the current user with mode 600")


@contextmanager
def _open_private(path: Path, flags: int):
    path = _local_path(path)
    assert_local_storage(path)
    with _directory(path.parent) as parent:
        safe_flags = flags | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC
        if flags & os.O_CREAT:
            # Claim a new inode exclusively. Competing callers then open that
            # existing inode, rather than race two nonexclusive create calls.
            try:
                descriptor = os.open(path.name, safe_flags | os.O_EXCL, 0o600, dir_fd=parent)
            except FileExistsError:
                descriptor = os.open(path.name, safe_flags & ~os.O_CREAT, dir_fd=parent)
        else:
            descriptor = os.open(path.name, safe_flags, dir_fd=parent)
        try:
            _private_file(os.fstat(descriptor))
            yield descriptor
        finally:
            os.close(descriptor)


class StateStore:
    """Manage private configuration, sealed drafts, audit records, and locks.

    Atomic file replacement does not serialize a whole workflow. Callers must
    hold exclusive_lock across a read/modify/write sequence when concurrency matters.
    """
    def __init__(self, root: Path | None = None) -> None:
        self.root = _local_path(root or default_state_root())
        assert_local_storage(self.root)
        self.drafts_dir = self.root / "drafts"
        self.purchases_dir = self.root / "purchases"
        self.locks_dir = self.root / "locks"
        self.config_path = self.root / "config.json"
        self.audit_path = self.root / "audit.jsonl"
        self.deletion_history_path = self.root / "deletion-history.md"

    def ensure(self) -> None:
        for path in (self.root, self.drafts_dir, self.locks_dir):
            with _directory(path, create=True, private=True):
                pass

    @contextmanager
    def exclusive_lock(self, scope: str):
        """Serialize cooperating local callers using the same state root and scope.

        Keep the lock file after release; deleting it could let concurrent callers
        lock different inodes for the same scope. This cannot lock remote clients.
        """
        self.ensure()
        digest = hashlib.sha256(scope.encode("utf-8")).hexdigest()
        path = self.locks_dir / f"{digest}.lock"
        with _open_private(path, os.O_RDWR | os.O_CREAT) as descriptor:
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(descriptor, fcntl.LOCK_UN)

    def load_config(self) -> dict[str, Any]:
        self.ensure()
        with _directory(self.root, private=True) as parent:
            try:
                os.stat(self.config_path.name, dir_fd=parent, follow_symlinks=False)
            except FileNotFoundError:
                return {}
        return self._read_json(self.config_path)

    def save_config(self, config: dict[str, Any]) -> None:
        self.ensure()
        self._atomic_json(self.config_path, config)

    def load_rate_limit(self, path: Path) -> dict[str, Any] | None:
        """Read optional accounting under the caller's token-scoped lock."""
        self.ensure()
        with _directory(self.root, private=True) as parent:
            try:
                os.stat(path.name, dir_fd=parent, follow_symlinks=False)
            except FileNotFoundError:
                return None
        return self._read_json(path)

    def require_terms_acknowledged(self) -> None:
        config = self.load_config()
        if config.get("terms_acknowledged_version") != TERMS_VERSION:
            raise StateError(
                "YNAB's third-party application notice has not been acknowledged. "
                "Run: ynab-agent-skill terms acknowledge"
            )

    def configured_plan_id(self) -> str:
        config = self.load_config()
        plan_id = config.get("plan_id")
        if not isinstance(plan_id, str) or not plan_id:
            raise StateError("No plan is selected. Run: ynab-agent-skill plans list, then ynab-agent-skill plans use PLAN_ID")
        return plan_id

    def draft_path(self, draft_id: str) -> Path:
        if not re.fullmatch(r"[0-9a-f-]{36}", draft_id):
            raise StateError("Invalid draft id")
        return self.drafts_dir / f"{draft_id}.json"

    def save_draft(self, draft: dict[str, Any]) -> None:
        """Seal a new draft or advance progress without replacing reviewed content.

        The first save adds review_digest to the supplied dictionary. Subsequent
        saves verify both the submitted seal and its agreement with the saved draft.
        """
        self.ensure()
        draft_id = draft.get("id")
        if not isinstance(draft_id, str):
            raise StateError("Draft id is missing")
        path = self.draft_path(draft_id)
        with _directory(self.drafts_dir, private=True) as parent:
            try:
                os.stat(path.name, dir_fd=parent, follow_symlinks=False)
            except FileNotFoundError:
                exists = False
            else:
                exists = True
        if exists:
            previous = self.load_draft(draft_id)
            _verify_review(draft)
            if previous["review_digest"] != draft["review_digest"]:
                raise StateError("The reviewed proposal cannot be replaced under the same draft id")
        else:
            draft["review_digest"] = _review_digest(draft)
        self._atomic_json(path, draft)

    def load_draft(self, draft_id: str) -> dict[str, Any]:
        path = self.draft_path(draft_id)
        draft = self._read_json(path)
        if draft.get("id") != draft_id:
            raise StateError("Draft identity does not match its filename")
        _verify_review(draft)
        return draft

    def list_drafts(self) -> list[dict[str, Any]]:
        self.ensure()
        drafts = []
        with _directory(self.drafts_dir, private=True) as parent:
            names = sorted(os.listdir(parent), reverse=True)
        for name in names:
            if not name.endswith(".json"):
                continue
            path = self.draft_path(name[:-5])
            draft = self._read_json(path)
            drafts.append(
                {
                    "id": draft.get("id"),
                    "operation": draft.get("operation"),
                    "status": draft.get("status"),
                    "created_at": draft.get("created_at"),
                    "summary": draft.get("summary"),
                }
            )
        return drafts

    def purchase_path(self, purchase_id: str) -> Path:
        # Reuse the existing strict UUID filename contract, never a user path.
        return self.purchases_dir / self.draft_path(purchase_id).name

    def save_purchase(self, record: dict[str, Any]) -> None:
        self.ensure()
        with _directory(self.purchases_dir, create=True, private=True):
            pass
        if type(record.get("schema_version")) is not int or record["schema_version"] != 1:
            raise StateError("Unsupported purchase ledger version")
        self._atomic_json(self.purchase_path(record["id"]), record)

    def load_purchase(self, purchase_id: str) -> dict[str, Any]:
        record = self._read_json(self.purchase_path(purchase_id))
        if record.get("id") != purchase_id or type(record.get("schema_version")) is not int or record["schema_version"] != 1:
            raise StateError("Invalid purchase ledger identity/version")
        return record

    def purchase_exists(self, purchase_id: str) -> bool:
        self.ensure()
        with _directory(self.purchases_dir, create=True, private=True) as directory:
            try:
                _private_file(os.stat(self.purchase_path(purchase_id).name, dir_fd=directory,
                                      follow_symlinks=False))
            except FileNotFoundError:
                return False
        return True

    def list_purchases(self) -> list[dict[str, Any]]:
        self.ensure()
        with _directory(self.purchases_dir, create=True, private=True) as directory:
            names = sorted(os.listdir(directory))
        return [self.load_purchase(name[:-5]) for name in names if name.endswith(".json")]

    def append_audit(self, record: dict[str, Any]) -> None:
        self.ensure()
        line = json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n"
        with _open_private(self.audit_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND) as descriptor:
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            try:
                remaining = memoryview(line.encode("utf-8"))
                while remaining:
                    written = os.write(descriptor, remaining)
                    if written <= 0:
                        raise StateError("Could not persist the private audit")
                    remaining = remaining[written:]
                os.fsync(descriptor)
            finally:
                fcntl.flock(descriptor, fcntl.LOCK_UN)

    def append_deletion_history(self, record: dict[str, Any] | None = None) -> str:
        """Append/fsync a shared private Markdown journal; never overwrite history.

        Indented JSON preserves arbitrary memo text as data. The sealed draft is
        the restoration authority; Markdown is a readable redundant record.
        """
        self.ensure()
        header = ("# YNAB deletion history\n\n"
                  "Private shared record for agents using this state root. Do not publish.\n"
                  "Recreation requires a reviewed draft and creates a new transaction ID; "
                  "import links and reconciliation history cannot be restored.\n\n")
        with _open_private(self.deletion_history_path, os.O_RDWR | os.O_CREAT | os.O_APPEND) as descriptor:
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            try:
                text = header if os.fstat(descriptor).st_size == 0 else ""
                if record is not None:
                    text += "## Deletion event\n\n" + "\n".join(
                        "    " + line for line in json.dumps(record, indent=2, sort_keys=True).splitlines()
                    ) + "\n\n"
                remaining = memoryview(text.encode("utf-8"))
                while remaining:
                    written = os.write(descriptor, remaining)
                    if written <= 0:
                        raise StateError("Could not persist deletion history")
                    remaining = remaining[written:]
                os.fsync(descriptor)
            finally:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
        return str(self.deletion_history_path)

    @staticmethod
    def now() -> str:
        return datetime.now(timezone.utc).isoformat()

    @staticmethod
    def _read_json(path: Path) -> dict[str, Any]:
        try:
            data = read_private_json(path)
        except StateError as exc:
            raise StateError(f"Could not read valid state from {path.name}") from exc
        return data

    @staticmethod
    def _atomic_json(path: Path, data: dict[str, Any]) -> None:
        """Replace one private JSON file; callers own any surrounding workflow lock."""
        serialized = json.dumps(data, indent=2, sort_keys=True) + "\n"
        path = _local_path(path)
        assert_local_storage(path)
        with _directory(path.parent, private=True) as parent:
            try:
                _private_file(os.stat(path.name, dir_fd=parent, follow_symlinks=False))
            except FileNotFoundError:
                pass
            temporary = f".{path.name}.{uuid.uuid4().hex}"
            descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                                 0o600, dir_fd=parent)
            try:
                with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                    handle.write(serialized)
                    handle.flush()
                    os.fsync(handle.fileno())
                # Ensure the pinned parent is still the intended directory before publication.
                with _directory(path.parent, private=True) as current:
                    a, b = os.fstat(parent), os.fstat(current)
                    if (a.st_dev, a.st_ino) != (b.st_dev, b.st_ino):
                        raise StateError("State destination changed during the write")
                os.replace(temporary, path.name, src_dir_fd=parent, dst_dir_fd=parent)
                # Flush the directory entry as well as file contents so recovery
                # checkpoints are not dependent only on an in-memory rename.
                os.fsync(parent)
            finally:
                try:
                    os.unlink(temporary, dir_fd=parent)
                except FileNotFoundError:
                    pass


def read_private_json(path: Path, *, maximum_bytes: int = 1_000_000) -> dict[str, Any]:
    """Read a small private JSON object from verified local storage."""
    with _open_private(path, os.O_RDONLY) as descriptor:
        info = os.fstat(descriptor)
        if info.st_size > maximum_bytes:
            raise StateError("The proposal file is too large")
        chunks = []
        remaining = maximum_bytes + 1
        while remaining:
            chunk = os.read(descriptor, min(65_536, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        raw = b"".join(chunks)
        if len(raw) > maximum_bytes:
            raise StateError("The proposal file is too large")
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise StateError("The proposal file is not valid UTF-8 JSON") from exc
    if not isinstance(value, dict):
        raise StateError("The proposal file must contain one JSON object")
    return value
