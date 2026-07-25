import importlib

import paperforge_worker.config as worker_config
import paperforge_worker.worker as worker
from arq.connections import RedisSettings


def test_worker_reads_redis_url(monkeypatch):
    monkeypatch.setenv("REDIS_URL", "redis://redis.example:6380/4")
    # 配置是进程级缓存的：重载 config 后再重载 worker，才能拿到新的 RedisSettings。
    importlib.reload(worker_config)
    reloaded = importlib.reload(worker)
    settings = reloaded.WorkerSettings.redis_settings
    assert isinstance(settings, RedisSettings)
    assert settings.host == "redis.example"
    assert settings.port == 6380
    assert settings.database == 4
    monkeypatch.delenv("REDIS_URL", raising=False)
    importlib.reload(worker_config)
    importlib.reload(worker)


def test_worker_registers_m1_pipeline_functions():
    names = {fn.__name__ for fn in worker.WorkerSettings.functions}
    assert {
        "run_library_pipeline",
        "run_import_pipeline",
        "run_cards_pipeline",
        "run_full_pipeline",
    } <= names


async def test_library_pipeline_can_defer_finalization():
    """run_full_pipeline 复用文献管线时不得提前收尾。

    否则前端看到 succeeded 就停止轮询，而 outline/write/render 还在后台跑。
    """
    import inspect

    signature = inspect.signature(worker.run_library_pipeline)
    assert signature.parameters["finalize"].default is True

    source = inspect.getsource(worker.run_full_pipeline)
    assert "finalize=False" in source
