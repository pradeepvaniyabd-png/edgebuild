from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest

from superagent.config import Settings, reset_settings_cache
from superagent.db.repo import Repo
from superagent.db.session import Database, run_migrations

_PROVIDER_ENV = {
    "ANTHROPIC_API_KEY", "ANTHROPIC_MODEL", "ANTHROPIC_AUTH_TOKEN", "OPENAI_API_KEY", "OPENAI_BASE_URL",
    "OPENAI_MODEL", "OLLAMA_BASE_URL", "OLLAMA_MODEL", "REPLICATE_API_TOKEN", "FAL_KEY",
    "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_REGION", "BEDROCK_MODEL_ID", "COMFYUI_BASE_URL",
    "SEARXNG_URL", "PIPER_MODEL",
}  # fmt: skip


@pytest.fixture(autouse=True)
def _isolated_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """Each test gets its own home directory and no ambient provider credentials."""
    keep = {"SAX_TEST_POSTGRES_URL"}
    for name in list(os.environ):
        if (name.startswith("SAX_") and name not in keep) or name in _PROVIDER_ENV:
            monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("SAX_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("SAX_ENV", "test")
    monkeypatch.setenv("SAX_OLLAMA_AUTODETECT", "false")
    monkeypatch.setenv("SAX_WEB_SEARCH", "none")
    monkeypatch.chdir(tmp_path)  # never pick up a developer's .env
    reset_settings_cache()
    yield
    reset_settings_cache()


@pytest.fixture
def settings() -> Settings:
    settings = Settings()
    settings.ensure_dirs()
    return settings


@pytest.fixture(scope="session")
def _template_db(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Run the real Alembic migrations once; tests copy the resulting file."""
    path = tmp_path_factory.mktemp("template") / "template.db"
    run_migrations(f"sqlite+aiosqlite:///{path}")
    return path


@pytest.fixture
async def db(settings: Settings, _template_db: Path):
    shutil.copy(_template_db, settings.home_path / "superagent.db")
    database = Database(settings.db_url)
    yield database
    await database.dispose()


@pytest.fixture
def repo(db: Database) -> Repo:
    return Repo(db)


def has_binary(name: str) -> bool:
    return shutil.which(name) is not None


requires_ffmpeg = pytest.mark.skipif(not has_binary("ffmpeg"), reason="ffmpeg not installed")
requires_espeak = pytest.mark.skipif(not has_binary("espeak-ng"), reason="espeak-ng not installed")


@pytest.fixture(scope="session")
def _vendor_server():
    from tests.fakes import FakeVendors

    server = FakeVendors()
    server.start()
    if has_binary("ffmpeg"):  # a real, tiny MP4 for the download paths
        import subprocess
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            clip = Path(tmp) / "clip.mp4"
            subprocess.run(
                ["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", "testsrc=size=160x284:rate=12:duration=1",
                 "-c:v", "libx264", "-pix_fmt", "yuv420p", str(clip)],
                check=True,
            )
            server.files["clip.mp4"] = clip.read_bytes()
    yield server
    server.stop()


@pytest.fixture
def vendors(_vendor_server):
    _vendor_server.reset()
    return _vendor_server


@pytest.fixture
def tool_ctx(settings: Settings, repo: Repo):
    """A ToolContext wired like production, with an image double so media tools can run offline."""
    from superagent.core.approval import ApprovalManager
    from superagent.core.cost import CostEngine
    from superagent.memory.manager import MemoryManager
    from superagent.providers.router import ProviderRouter
    from superagent.providers.services import AI
    from superagent.providers.vendors.ffmpeg_motion import FFmpegMotionVideo
    from superagent.providers.vendors.hashing import HashingEmbedding
    from superagent.providers.vendors.local_voice import EspeakVoice
    from superagent.security import CommandPolicy, PathSandbox
    from superagent.tools import ToolContext
    from superagent.types import PermissionLevel
    from tests.fakes import PaintedImages

    sandbox = PathSandbox([settings.workspace], settings.read_roots)
    approvals = ApprovalManager(repo, settings)
    router = ProviderRouter()
    for provider in (PaintedImages(), FFmpegMotionVideo(), EspeakVoice(), HashingEmbedding()):
        router.register(provider)
    ai = AI(router, CostEngine(repo, settings, approvals))
    return ToolContext(
        settings=settings, sandbox=sandbox, policy=CommandPolicy(sandbox), approvals=approvals, ai=ai, repo=repo,
        memory=MemoryManager(repo, settings, ai), execution_id="exe_t", task_id="tsk_t", agent="tester",
        permission=PermissionLevel.SYSTEM_WRITE,
    )
