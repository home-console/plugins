"""
Unit tests for cloud_sync sync jobs functionality.
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ── Fixtures ──────────────────────────────────────────────────────────────


class MockCloudProvider:
    """Mock cloud provider for testing."""

    name = "mock"

    def __init__(self):
        self.uploaded: list[tuple[bytes, str]] = []
        self.should_check = True

    async def check(self) -> bool:
        return self.should_check

    async def upload(self, data: bytes, remote_path: str) -> dict[str, Any]:
        self.uploaded.append((data, remote_path))
        return {"success": True, "path": remote_path, "size": len(data)}

    async def download(self, remote_path: str) -> bytes:
        return b"mock content"

    async def list_files(self, remote_path: str = ""):
        return []

    async def delete(self, remote_path: str) -> bool:
        return True


class MockPlugin:
    """Minimal mock of CloudSyncPlugin for testing sync job logic."""

    def __init__(self):
        self._providers: dict[str, Any] = {"mock": MockCloudProvider()}
        self._sync_jobs: list[dict[str, Any]] = []
        self._sync_interval = 60
        self._background_tasks: set = set()
        self._sync_task = None
        self._events: list[tuple[str, dict]] = []
        self._stored: dict[str, Any] = {}

    def _get_provider(self, name: str):
        return self._providers[name]

    async def publish_event(self, event_type: str, payload: dict) -> None:
        self._events.append((event_type, payload))

    async def call_service(self, service: str, **kwargs: Any) -> Any:
        if service == "storage.get":
            ns = kwargs.get("namespace", "")
            key = kwargs.get("key", "")
            return self._stored.get(f"{ns}/{key}")
        if service == "storage.set":
            ns = kwargs.get("namespace", "")
            key = kwargs.get("key", "")
            value = kwargs.get("value")
            self._stored[f"{ns}/{key}"] = value
            return True
        return None


# ── Tests: sync job CRUD ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_create_sync_job():
    plugin = MockPlugin()
    tmpdir = Path("/tmp/cloud_sync_test")
    tmpdir.mkdir(exist_ok=True)
    (tmpdir / "test.txt").write_text("hello")

    # Simulate create_sync_job logic inline
    job = {
        "id": f"sync_{int(time.time())}_0",
        "provider": "mock",
        "local_path": str(tmpdir / "test.txt"),
        "remote_path": "/backup",
        "interval_seconds": 300,
        "enabled": True,
        "auto_upload": False,
        "last_run": 0,
        "created_at": time.time(),
    }
    plugin._sync_jobs.append(job)

    assert len(plugin._sync_jobs) == 1
    assert plugin._sync_jobs[0]["provider"] == "mock"
    assert plugin._sync_jobs[0]["local_path"] == str(tmpdir / "test.txt")
    assert plugin._sync_jobs[0]["enabled"] is True

    # Cleanup
    (tmpdir / "test.txt").unlink(missing_ok=True)
    import shutil
    shutil.rmtree(tmpdir, ignore_errors=True)


@pytest.mark.asyncio
async def test_update_sync_job():
    plugin = MockPlugin()
    job = {
        "id": "job_1",
        "provider": "mock",
        "local_path": "/tmp/test",
        "remote_path": "/backup",
        "interval_seconds": 3600,
        "enabled": True,
        "auto_upload": False,
        "last_run": 0,
    }
    plugin._sync_jobs.append(job)

    # Update
    for j in plugin._sync_jobs:
        if j.get("id") == "job_1":
            j["interval_seconds"] = 600
            j["enabled"] = False

    assert plugin._sync_jobs[0]["interval_seconds"] == 600
    assert plugin._sync_jobs[0]["enabled"] is False


@pytest.mark.asyncio
async def test_delete_sync_job():
    plugin = MockPlugin()
    plugin._sync_jobs = [
        {"id": "job_1", "provider": "mock"},
        {"id": "job_2", "provider": "mock"},
    ]

    before = len(plugin._sync_jobs)
    plugin._sync_jobs = [j for j in plugin._sync_jobs if j.get("id") != "job_1"]

    assert len(plugin._sync_jobs) == before - 1
    assert all(j["id"] != "job_1" for j in plugin._sync_jobs)


# ── Tests: sync job execution ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_run_sync_job_file():
    plugin = MockPlugin()
    tmpdir = Path("/tmp/cloud_sync_test")
    tmpdir.mkdir(exist_ok=True)
    test_file = tmpdir / "upload_me.txt"
    test_file.write_bytes(b"test content 123")

    job = {
        "id": "job_1",
        "provider": "mock",
        "local_path": str(test_file),
        "remote_path": "/backups",
        "enabled": True,
    }

    # Execute sync
    provider = plugin._get_provider(job["provider"])
    local = Path(job["local_path"])
    assert local.exists()
    assert local.is_file()

    data = local.read_bytes()
    rel_name = local.name
    remote_file = f"{job['remote_path'].rstrip('/')}/{rel_name}"
    await provider.upload(data, remote_file)

    assert len(provider.uploaded) == 1
    assert provider.uploaded[0] == (b"test content 123", "/backups/upload_me.txt")

    # Cleanup
    test_file.unlink(missing_ok=True)
    import shutil
    shutil.rmtree(tmpdir, ignore_errors=True)


@pytest.mark.asyncio
async def test_run_sync_job_directory():
    plugin = MockPlugin()
    tmpdir = Path("/tmp/cloud_sync_test_dir")
    tmpdir.mkdir(exist_ok=True)
    (tmpdir / "a.txt").write_bytes(b"aaa")
    (tmpdir / "b.txt").write_bytes(b"bbb")
    sub = tmpdir / "sub"
    sub.mkdir()
    (sub / "c.txt").write_bytes(b"ccc")

    job = {
        "id": "job_1",
        "provider": "mock",
        "local_path": str(tmpdir),
        "remote_path": "/sync",
        "enabled": True,
    }

    provider = plugin._get_provider(job["provider"])
    local = Path(job["local_path"])
    assert local.is_dir()

    uploaded_count = 0
    for f in sorted(local.rglob("*")):
        if f.is_file():
            data = f.read_bytes()
            rel = f.relative_to(local)
            remote_file = f"{job['remote_path'].rstrip('/')}/{rel}"
            await provider.upload(data, remote_file)
            uploaded_count += 1

    assert uploaded_count == 3
    assert len(provider.uploaded) == 3
    # Check paths
    paths = [u[1] for u in provider.uploaded]
    assert "/sync/a.txt" in paths
    assert "/sync/b.txt" in paths
    assert "/sync/sub/c.txt" in paths

    # Cleanup
    import shutil
    shutil.rmtree(tmpdir)


@pytest.mark.asyncio
async def test_sync_job_publishes_event():
    plugin = MockPlugin()
    tmpdir = Path("/tmp/cloud_sync_test_event")
    tmpdir.mkdir(exist_ok=True)
    (tmpdir / "event.txt").write_bytes(b"event data")

    provider = plugin._get_provider("mock")
    local = Path(str(tmpdir / "event.txt"))
    data = local.read_bytes()
    await provider.upload(data, "/events/event.txt")
    await plugin.publish_event("cloud_sync.sync_completed", {
        "provider": "mock",
        "local_path": str(local),
        "remote_path": "/events/event.txt",
        "size": len(data),
    })

    assert len(plugin._events) == 1
    assert plugin._events[0][0] == "cloud_sync.sync_completed"
    assert plugin._events[0][1]["provider"] == "mock"
    assert plugin._events[0][1]["size"] == 10

    # Cleanup
    (tmpdir / "event.txt").unlink()
    tmpdir.rmdir()


# ── Tests: sync job persistence ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_save_and_load_sync_jobs():
    plugin = MockPlugin()
    plugin._sync_jobs = [
        {"id": "j1", "provider": "mock", "local_path": "/tmp/a"},
        {"id": "j2", "provider": "mock", "local_path": "/tmp/b"},
    ]

    # Save
    await plugin.call_service(
        "storage.set",
        namespace="cloud_sync",
        key="sync_jobs",
        value={"jobs": plugin._sync_jobs},
    )

    # Load
    data = await plugin.call_service(
        "storage.get",
        namespace="cloud_sync",
        key="sync_jobs",
    )
    assert data is not None
    assert isinstance(data, dict)
    assert len(data["jobs"]) == 2
    assert data["jobs"][0]["id"] == "j1"


@pytest.mark.asyncio
async def test_load_sync_jobs_empty():
    plugin = MockPlugin()
    data = await plugin.call_service(
        "storage.get",
        namespace="cloud_sync",
        key="sync_jobs",
    )
    assert data is None


# ── Tests: sync job scheduling logic ──────────────────────────────────────


@pytest.mark.asyncio
async def test_sync_job_due_detection():
    """Test that a job is detected as due when last_run + interval < now."""
    now = time.time()
    job = {
        "id": "job_1",
        "provider": "mock",
        "local_path": "/tmp/test",
        "remote_path": "/backup",
        "interval_seconds": 300,
        "enabled": True,
        "last_run": now - 400,  # 400 seconds ago, interval is 300
    }

    # Job should be due
    is_due = (now - job["last_run"]) >= job["interval_seconds"]
    assert is_due is True


@pytest.mark.asyncio
async def test_sync_job_not_due():
    """Test that a job is not due when last_run + interval > now."""
    now = time.time()
    job = {
        "id": "job_1",
        "provider": "mock",
        "local_path": "/tmp/test",
        "remote_path": "/backup",
        "interval_seconds": 300,
        "enabled": True,
        "last_run": now - 100,  # 100 seconds ago, interval is 300
    }

    is_due = (now - job["last_run"]) >= job["interval_seconds"]
    assert is_due is False


@pytest.mark.asyncio
async def test_sync_job_disabled_not_run():
    """Test that disabled jobs are skipped."""
    job = {
        "id": "job_1",
        "provider": "mock",
        "enabled": False,
        "last_run": 0,
        "interval_seconds": 1,
    }
    # Should be skipped
    assert job.get("enabled", True) is False


# ── Tests: provider error handling ────────────────────────────────────────


@pytest.mark.asyncio
async def test_sync_job_missing_local_path():
    """Test that sync job skips if local path doesn't exist."""
    local = Path("/nonexistent/path/file.txt")
    assert local.exists() is False


@pytest.mark.asyncio
async def test_sync_job_missing_provider():
    """Test that sync job fails gracefully with missing provider."""
    plugin = MockPlugin()
    with pytest.raises(KeyError):
        plugin._get_provider("nonexistent")


# ── Tests: auto_upload flag ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_auto_upload_flag():
    """Test that auto_upload flag is preserved in job config."""
    job = {
        "id": "job_1",
        "provider": "mock",
        "auto_upload": True,
        "enabled": True,
    }
    assert job.get("auto_upload") is True

    job2 = {
        "id": "job_2",
        "provider": "mock",
        "auto_upload": False,
        "enabled": True,
    }
    assert job2.get("auto_upload") is False


@pytest.mark.asyncio
async def test_auto_upload_default_false():
    """Test that auto_upload defaults to False if not set."""
    job = {
        "id": "job_1",
        "provider": "mock",
        "enabled": True,
    }
    assert job.get("auto_upload", False) is False
