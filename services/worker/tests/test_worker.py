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


def _registered_names() -> set[str]:
    # 带自定义超时的任务注册成 arq.Function（有 .name），其余仍是裸协程函数。
    return {getattr(fn, "name", None) or fn.__name__ for fn in worker.WorkerSettings.functions}


def test_worker_registers_m1_pipeline_functions():
    names = _registered_names()
    assert {
        "run_library_pipeline",
        "run_import_pipeline",
        "run_cards_pipeline",
        "run_full_pipeline",
    } <= names


def test_full_pipeline_gets_a_longer_timeout_than_single_stage_jobs():
    """一键生成是唯一跑到小时级的任务，不能跟单阶段任务共用默认超时。

    真实教训：7 节 / 46 篇的一轮跑了 22 分钟，默认 1800 秒只剩 8 分钟余量；
    超时在 arq 里是直接判失败（asyncio.wait_for 抛 TimeoutError，不重试），
    撞上就是整轮白跑。
    """
    full = next(
        fn
        for fn in worker.WorkerSettings.functions
        if getattr(fn, "name", "") == "run_full_pipeline"
    )
    assert full.timeout_s == worker.FULL_PIPELINE_TIMEOUT_SECONDS
    assert full.timeout_s > worker.WorkerSettings.job_timeout


def test_deterministic_fallback_scope_is_regenerated_before_search():
    """回退产物必须重生成，否则项目会被永久钉死在一份很差的关键词上。

    真实故障：planner 输出被截断 → scope 退回确定性回退 → 中文主题只切出一个巨型
    关键词 → 检索回来一批中文医学论文；用户再点「触发检索」时前端默认
    regenerate_scope=false，于是复用同一份坏 scope，症状永远不会自愈。
    """
    fallback = {"keyword_groups": [{"name": "x", "keywords": ["x"]}], "generator": "deterministic"}
    assert worker.scope_needs_regeneration(fallback)
    assert worker.scope_needs_regeneration({**fallback, "generator": "deterministic_fallback"})
    # 关键词缺失同样要重生成。
    assert worker.scope_needs_regeneration({"generator": "llm:m", "keyword_groups": []})
    # LLM 产物与用户手改过的 scope 都不该被自动覆盖。
    assert not worker.scope_needs_regeneration({**fallback, "generator": "llm:deepseek-v4-pro"})
    assert not worker.scope_needs_regeneration({**fallback, "generator": "user"})


async def test_library_pipeline_can_defer_finalization():
    """run_full_pipeline 复用文献管线时不得提前收尾。

    否则前端看到 succeeded 就停止轮询，而 outline/write/render 还在后台跑。
    """
    import inspect

    signature = inspect.signature(worker.run_library_pipeline)
    assert signature.parameters["finalize"].default is True

    source = inspect.getsource(worker.run_full_pipeline)
    assert "finalize=False" in source
