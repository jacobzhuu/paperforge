from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
OPS = ROOT / "scripts" / "ops"
INSTALLER = ROOT / "scripts" / "install-ubuntu-container-tools"
LXD_SMOKE = ROOT / "scripts" / "compat" / "lxd-smoke"
IMAGE_LOADER = ROOT / "scripts" / "load-offline-images"
DEV = ROOT / "scripts" / "dev"


def _bash(script: str, *, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    process_env = os.environ.copy()
    process_env.update(env or {})
    return subprocess.run(
        ["/bin/bash", "-c", script],
        cwd=ROOT,
        env=process_env,
        check=False,
        capture_output=True,
        text=True,
    )


def test_operational_scripts_are_bash_32_compatible_syntax() -> None:
    result = subprocess.run(
        [
            "/bin/bash",
            "-n",
            ROOT / "scripts" / "dev",
            OPS,
            INSTALLER,
            ROOT / "scripts" / "compat" / "compose-smoke",
            LXD_SMOKE,
            IMAGE_LOADER,
        ],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    sources = (OPS.read_text() + INSTALLER.read_text()).replace("${BASH_SOURCE[0]}", "")
    for bash4_only in ("declare -A", "mapfile", "readarray", ",,"):
        assert bash4_only not in sources


@pytest.mark.parametrize(
    ("current", "minimum", "expected"),
    [
        ("26.0.0", "26.0.0", True),
        ("v2.35.1", "2.35.1", True),
        ("2.35.0", "2.35.1", False),
        ("1.99.99", "2.0.0", False),
        ("27.1.0-ubuntu1", "26.0.0", True),
    ],
)
def test_ops_semantic_version_gate(current: str, minimum: str, expected: bool) -> None:
    result = _bash(f'PAPERFORGE_OPS_LIBRARY=1 . "{OPS}"; version_at_least "{current}" "{minimum}"')
    assert (result.returncode == 0) is expected


@pytest.mark.parametrize(
    ("host", "release", "expected"),
    [
        ("Darwin", None, "Darwin:"),
        ("Linux", "20.04", "Linux:20.04"),
        ("Linux", "22.04", "Linux:22.04"),
    ],
)
def test_ops_host_detection(tmp_path: Path, host: str, release: str | None, expected: str) -> None:
    env = {"PAPERFORGE_HOST_KIND_OVERRIDE": host}
    if release:
        os_release = tmp_path / "os-release"
        os_release.write_text(f'ID=ubuntu\nVERSION_ID="{release}"\n', encoding="utf-8")
        env["PAPERFORGE_OS_RELEASE_FILE"] = str(os_release)
    result = _bash(
        f'PAPERFORGE_OPS_LIBRARY=1 . "{OPS}"; '
        'detect_host; printf "%s:%s" "$HOST_KIND" "$UBUNTU_VERSION"',
        env=env,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout == expected


def test_non_focal_esm_check_is_a_successful_noop() -> None:
    result = _bash(f'PAPERFORGE_OPS_LIBRARY=1 . "{OPS}"; UBUNTU_VERSION=22.04; check_focal_esm')
    assert result.returncode == 0, result.stderr


def test_production_env_validation_accepts_configured_secure_file(tmp_path: Path) -> None:
    env_file = tmp_path / "paperforge.env"
    contents = (ROOT / ".env.production.example").read_text(encoding="utf-8")
    env_file.write_text(contents.replace("CHANGE_ME", "configured"), encoding="utf-8")
    env_file.chmod(0o600)
    result = _bash(f'PAPERFORGE_OPS_LIBRARY=1 . "{OPS}"; ENV_FILE="{env_file}"; validate_env')
    assert result.returncode == 0, result.stderr


def test_ops_rejects_production_env_inside_repository() -> None:
    result = _bash(
        f'export PAPERFORGE_ENV_FILE="{ROOT / ".env.production.example"}"; '
        f'PAPERFORGE_OPS_LIBRARY=1 . "{OPS}"; prepare_env'
    )
    assert result.returncode != 0
    assert "must live outside the repository" in result.stderr


def test_focal_installer_pins_verified_plugin_versions() -> None:
    source = INSTALLER.read_text(encoding="utf-8")
    assert 'COMPOSE_VERSION="2.35.1"' in source
    assert 'BUILDX_VERSION="0.23.0"' in source
    for digest in (
        "7bdb2ce2916e5dd0354e5d129892bf96fdcdb1a9ab8eed69b9173e131db4c230",
        "a91e930a076b91e6c69f11d1dbe3c06729ae765fb9dbb3f97cb808e784647399",
        "55838fdd095084e158e06a63635a07fe8a8bc6cb4db507f203394dc1ffa7fb8b",
        "50b0b14770127b292e3e2f756d0137152b08afc1d7daa09f49d2b6e6fa1b1b81",
    ):
        assert digest in source


def test_lxd_acceptance_uses_the_development_postgres_host_port() -> None:
    source = LXD_SMOKE.read_text(encoding="utf-8")
    assert "127.0.0.1:15432/paperforge_test" in source
    assert "127.0.0.1:5432/paperforge_test" not in source


def test_production_stack_supports_verified_preloaded_images() -> None:
    ops_source = OPS.read_text(encoding="utf-8")
    compose_source = (ROOT / "infra" / "docker-compose.prod.yml").read_text(encoding="utf-8")
    assert "PAPERFORGE_SKIP_BUILD" in ops_source
    assert "--no-build" in ops_source
    assert "image: paperforge-texd:local" in compose_source
    assert "image: paperforge-visuald:local" in compose_source
    assert "PAPERFORGE_POSTGRES_IMAGE" in compose_source
    assert "PAPERFORGE_REDIS_IMAGE" in compose_source
    assert "PAPERFORGE_MINIO_IMAGE" in compose_source


def test_storage_restore_runs_as_the_backup_owner() -> None:
    source = OPS.read_text(encoding="utf-8")
    assert source.count('--user "$(id -u):$(id -g)"') == 3
    assert source.count("restore-storage --archive /backup/objects.tar.gz") == 2


def test_texd_installs_the_amd64_tectonic_runtime_library() -> None:
    source = (ROOT / "services" / "texd" / "Dockerfile").read_text(encoding="utf-8")
    assert "libgraphite2-3" in source


def test_blue_green_deploy_rebuilds_and_preflights_the_texd_runtime() -> None:
    """渲染器/模板优化不能只进 worker、却让 Funnel 继续配旧 TeX 缓存。"""
    source = DEV.read_text(encoding="utf-8")
    assert '-f "${ROOT_DIR}/services/texd/Dockerfile"' in source
    assert '-t "paperforge-texd:${tag}"' in source
    assert 'texd_image="${PAPERFORGE_DEPLOY_TEXD_IMAGE:-paperforge-texd:${tag}}"' in source
    assert 'verify_texd_runtime "${compose_project}"' in source
    assert source.index('verify_texd_runtime "${compose_project}"') < source.index(
        'tailscale_cli funnel --bg "${port}"'
    )


def test_blue_green_deploy_preflights_the_shared_object_store() -> None:
    """共享 MinIO 不在蓝绿 compose 里；没人断言它，它停了 11 天也照样滚部署。"""
    source = DEV.read_text(encoding="utf-8")
    assert 'verify_object_store "${compose_project}"' in source
    assert "Object storage is unusable from the new worker; refusing to switch Funnel." in source
    assert source.index('verify_object_store "${compose_project}"') < source.index(
        'tailscale_cli funnel --bg "${port}"'
    )


def test_worker_healthcheck_covers_the_object_store_not_only_the_queue() -> None:
    """只探队列的 worker 在对象存储停摆时仍报 healthy，故障就此隐身。"""
    for compose in ("docker-compose.bluegreen.yml", "docker-compose.prod.yml"):
        source = (ROOT / "infra" / compose).read_text(encoding="utf-8")
        assert '"CMD", "python", "-m", "paperforge_worker.healthcheck"' in source, compose
        assert "socket.create_connection(('redis', 6379)" not in source, compose


def test_blue_green_reaper_protects_live_and_active_deployments() -> None:
    source = DEV.read_text(encoding="utf-8")
    funnel_guard = "Cannot identify the deployment serving the Funnel; refusing to remove anything."
    assert 'reap) reap_deployments "${2:---dry-run}"' in source
    assert funnel_guard in source
    assert 'match="arq:in-progress:*"' in source
    assert "await redis.zcard(redis.default_queue_name)" in source
    assert source.count('deployment_drain_state "${project}"') >= 2
    assert source.index("sleep 2") < source.index("docker rm -f ${containers}")
    assert "application images retained for recovery" in source


def test_offline_image_loader_verifies_before_loading() -> None:
    source = IMAGE_LOADER.read_text(encoding="utf-8")
    assert source.index("bundle SHA-256 mismatch") < source.index("docker load")
    assert "image archive verification failed" in source
    assert "unexpected architecture" in source
