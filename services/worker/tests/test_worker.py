import importlib
from types import SimpleNamespace

import paperforge_worker.config as worker_config
import paperforge_worker.worker as worker
import pytest
from arq.connections import RedisSettings


def test_assisted_prewrite_uses_pinned_profile():
    assert worker._prewrite_quality_profile(
        SimpleNamespace(checkpoint={"execution_profile": "fast_draft"})
    ) == "draft"
    assert worker._prewrite_quality_profile(
        SimpleNamespace(checkpoint={"execution_profile": "standard"})
    ) == "scholarly"


def test_submission_and_scholarly_runs_ignore_fast_draft_shortcut():
    pinned = {"execution_profile": "fast_draft"}
    assert worker._effective_delivery_mode(pinned, "draft", "standard") == "fast_draft"
    assert worker._effective_delivery_mode(pinned, "scholarly", "standard") == "standard"
    assert worker._effective_delivery_mode(pinned, "submission", "standard") == "standard"
    assert worker._effective_delivery_mode(
        {"execution_profile": "standard"}, "draft", "fast_draft"
    ) == "standard"


async def test_fast_draft_verification_keeps_preliminary_record_and_snapshot_boundary():
    class Context:
        def __init__(self):
            self.checkpoint = {
                "fast_draft_preview": {
                    "paper_snapshot_hash": "original",
                    "items": [{"index": 0, "weak": True}],
                },
                "fast_draft_verifier": {
                    "paper_snapshot_hash": "original",
                    "items": [{"index": 0, "weak": False}],
                },
            }

        async def emit(self, _event, _payload, *, checkpoint, **_kwargs):
            self.checkpoint.update(checkpoint)

    context = Context()
    await worker._publish_fast_draft_verification(
        context, SimpleNamespace(paper_snapshot_hash="original", report_id="report-1")
    )
    assert context.checkpoint["fast_draft_preview"]["items"][0]["weak"] is True
    assert context.checkpoint["fast_draft_verification"]["items"][0]["weak"] is False
    assert context.checkpoint["fast_draft_verification"]["status"] == "complete"

    await worker._publish_fast_draft_verification(
        context, SimpleNamespace(paper_snapshot_hash="edited", report_id="report-2")
    )
    assert context.checkpoint["fast_draft_verification"]["status"] == "superseded"
    assert context.checkpoint["fast_draft_verification"]["items"] == []


def test_worker_settings_resolve_yunwu_specific_image_credentials():
    settings = worker_config.WorkerSettings(
        _env_file=None,
        image_provider="yunwu",
        image_api_key="cloudflare-key",
        image_model="@cf/model",
        image_base_url="https://api.cloudflare.com/client/v4",
        yunwu_api_key="yunwu-key",
        yunwu_image_model="gpt-image-1",
        yunwu_api_base_url="https://yunwu.ai/v1",
        yunwu_image_timeout_seconds=240,
    )
    config = settings.image_provider_config()
    assert config.provider == "yunwu"
    assert config.api_key == "yunwu-key"
    assert config.model == "gpt-image-1"
    assert config.base_url == "https://yunwu.ai/v1"
    assert config.timeout_seconds == 240


def test_worker_uses_bounded_llm_concurrency_and_role_thinking_defaults():
    settings = worker_config.WorkerSettings(_env_file=None)
    assert settings.card_concurrency == 6
    assert settings.qmatrix_concurrency == 4
    assert settings.claim_entailment_mode == "shadow"
    llm = settings.llm_config()
    assert llm.thinking_for_role("extractor") == "disabled"
    assert llm.thinking_for_role("reranker") == "disabled"
    assert llm.thinking_for_role("verifier") == "disabled"
    # 写作角色的输出（一节正文 + 逐句 evidence_ids）和推理抢同一份 max_output_tokens，
    # 而 deepseek 系被 clamp 在 8192：开着思考就会零内容返回并降级。
    assert llm.thinking_for_role("writer") == "disabled"


def test_worker_exposes_disabled_typesafe_defaults_and_explicit_rollout_config():
    defaults = worker_config.WorkerSettings(_env_file=None)
    assert defaults.typesafe_soft_check_mode == "off"
    assert defaults.typesafe_api_key == ""
    assert defaults.typesafe_decision_config()["model"] == "jev-1.13.0"

    settings = worker_config.WorkerSettings(
        _env_file=None,
        typesafe_api_key="test-key",
        typesafe_soft_check_mode="shadow",
        typesafe_confidence_threshold=0.87,
    )
    config = settings.typesafe_decision_config()
    assert config["api_key"] == "test-key"
    assert config["soft_check_mode"] == "shadow"
    assert config["confidence_threshold"] == pytest.approx(0.87)


def test_worker_rejects_unknown_claim_entailment_mode():
    with pytest.raises(ValueError):
        worker_config.WorkerSettings(_env_file=None, claim_entailment_mode="unsafe")


def test_model_prices_reach_the_llm_config_and_default_to_unpriced():
    """P1-4：没配价格时每次调用都记为未定价，而不是记成 $0。"""
    unconfigured = worker_config.WorkerSettings(_env_file=None).llm_config()
    assert unconfigured.estimate_cost("deepseek-v4-pro", input_tokens=10, output_tokens=10) is None

    settings = worker_config.WorkerSettings(
        _env_file=None,
        llm_model_prices='{"deepseek-v4-pro": {"input": 0.27, "output": 1.10}}',
    )
    assert settings.llm_config().estimate_cost(
        "deepseek-v4-pro", input_tokens=1_000_000, output_tokens=0
    ) == pytest.approx(0.27)


def test_malformed_model_prices_do_not_break_startup():
    """价目表写坏了只该让成本记账退回未定价，不该让 worker 起不来。"""
    settings = worker_config.WorkerSettings(_env_file=None, llm_model_prices="not json")

    assert settings.llm_config().model_prices == {}


def test_full_pipeline_can_force_yunwu_even_if_manual_provider_is_cloudflare():
    settings = worker_config.WorkerSettings(
        _env_file=None,
        image_provider="cloudflare",
        image_api_key="cloudflare-key",
        image_account_id="0123456789abcdef0123456789abcdef",
        yunwu_api_key="yunwu-key",
        yunwu_image_model="gpt-image-1",
    )
    manual = settings.image_provider_config()
    summary = settings.image_provider_config("yunwu")
    assert manual.provider == "cloudflare"
    assert summary.provider == "yunwu"
    assert summary.api_key == "yunwu-key"


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
        "run_polish_pipeline",
        "run_full_pipeline",
    } <= names


def test_worker_registers_private_pdf_pipeline_functions():
    assert {
        "run_pdf_match_pipeline",
        "run_uploaded_pdf_pipeline",
    } <= _registered_names()


def test_pdf_workers_wait_for_api_commit_before_emitting_stage_events():
    import inspect

    match_source = inspect.getsource(worker.run_pdf_match_pipeline)
    parse_source = inspect.getsource(worker.run_uploaded_pdf_pipeline)
    assert match_source.index("wait_for_pdf_upload_visibility") < match_source.index(
        "_mark_running"
    )
    assert parse_source.index("prepare_uploaded_pdf") < parse_source.index("_mark_running")


@pytest.mark.parametrize(
    "function_name",
    [
        "run_write_pipeline",
        "run_draft_rebuild_pipeline",
        "run_polish_pipeline",
        "run_quality_repair_pipeline",
        "run_full_pipeline",
    ],
)
def test_multi_section_pipelines_get_a_longer_timeout(function_name: str):
    """按章节串行调用模型的任务不能跟有界单阶段任务共用默认超时。

    真实基线：7 节 / 46 篇的一轮约 22 分钟，其中写作约 18 分钟；章节数、重写轮次
    与模型延迟叠加后可越过 3600 秒。arq 超时直接失败而不重试，撞上就是整轮白跑。
    """
    registered = next(
        fn for fn in worker.WorkerSettings.functions if getattr(fn, "name", "") == function_name
    )
    assert registered.timeout_s == worker.LONG_RUNNING_PIPELINE_TIMEOUT_SECONDS
    assert registered.timeout_s > worker.WorkerSettings.job_timeout


def test_every_pipeline_context_receives_the_worker_event_publisher() -> None:
    """One forgotten task would silently fall back to high-frequency database polling."""
    import inspect

    source = inspect.getsource(worker)
    assert source.count("async with job_context(") == source.count(
        'event_publisher=ctx.get("redis")'
    )


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


def test_full_pipeline_enables_fulltext_before_cards():
    """一键入口必须显式打开文献管线中的 INGEST，不能再生成摘要级卡片。"""
    import inspect

    source = inspect.getsource(worker.run_full_pipeline)
    assert "acquire_fulltext=True" in source
    library_source = inspect.getsource(worker.run_library_pipeline)
    assert library_source.index('"ingest"') < library_source.index('"cards"')
    assert "fulltexts=fulltexts" in library_source


def _repair_question(**overrides):
    from types import SimpleNamespace
    from uuid import uuid4

    defaults = {
        "id": uuid4(),
        "text": "How effective are poisoning attacks?",
        "search_query": "poisoning attack sequential recommendation",
        "term_aliases_json": {"poisoning attack": ["data poisoning", "shilling attack"]},
        "comparison_dimensions_json": ["dataset"],
        "answer_status": "insufficient_evidence",
    }
    return SimpleNamespace(**{**defaults, **overrides})


def test_second_source_repair_keeps_the_base_query_and_adds_synonym_variants() -> None:
    """只缺第二个独立来源时，换一套术语才够得到另一个作者群。"""
    queries = worker._gap_repair_queries([_repair_question()], "needs_second_source")
    assert queries[0] == "poisoning attack sequential recommendation"
    assert "data poisoning sequential recommendation" in queries
    assert "shilling attack sequential recommendation" in queries


def test_zero_candidate_repair_does_not_resend_the_query_that_already_failed() -> None:
    queries = worker._gap_repair_queries([_repair_question()], "no_candidates")
    assert "poisoning attack sequential recommendation" not in queries
    assert "data poisoning sequential recommendation" in queries
    # 过窄的长查询被截短，好让零召回的问题有机会拿回候选。
    assert "poisoning attack sequential" in queries


def test_repair_queries_drop_cjk_because_providers_only_index_english() -> None:
    question = _repair_question(search_query="序列推荐投毒攻击", term_aliases_json={})
    assert worker._gap_repair_queries([question], "needs_second_source") == []


def test_evidence_gaps_distinguish_missing_source_from_missing_candidates() -> None:
    from paperforge_worker.pipelines.readiness import EvidenceReadinessReport

    single_source = _repair_question()
    barren = _repair_question(search_query="model extraction recommender")
    report = EvidenceReadinessReport(
        question_details=[
            {
                "question_id": str(single_source.id),
                "eligible_evidence_count": 3,
                "distinct_work_count": 1,
                "ready": False,
            },
            {
                "question_id": str(barren.id),
                "eligible_evidence_count": 0,
                "distinct_work_count": 0,
                "ready": False,
            },
        ]
    )
    gaps = dict(
        (question.id, kind)
        for question, kind in worker._classify_evidence_gaps([single_source, barren], report)
    )
    assert gaps[single_source.id] == "needs_second_source"
    assert gaps[barren.id] == "no_candidates"


def test_ready_questions_are_not_re_searched() -> None:
    from paperforge_worker.pipelines.readiness import EvidenceReadinessReport

    question = _repair_question()
    report = EvidenceReadinessReport(
        question_details=[
            {
                "question_id": str(question.id),
                "eligible_evidence_count": 4,
                "distinct_work_count": 2,
                "ready": True,
            }
        ]
    )
    assert worker._classify_evidence_gaps([question], report) == []


def test_evidence_repair_recovers_selected_fulltexts_left_half_processed() -> None:
    from uuid import uuid4

    complete = uuid4()
    missing_card = uuid4()
    missing_evidence = uuid4()
    checkpointed = uuid4()
    selected = {complete, missing_card, missing_evidence, checkpointed}

    result = worker._repair_processing_ids(
        selected_work_ids=selected,
        new_work_ids=set(),
        fulltext_work_ids={str(item) for item in selected},
        card_work_ids={complete, missing_evidence, checkpointed},
        eligible_evidence_work_ids={complete, missing_card, checkpointed},
        checkpoint_work_ids=[str(checkpointed), "not-a-uuid"],
    )

    assert set(result) == {missing_card, missing_evidence, checkpointed}


def test_library_pipeline_assigns_keys_after_screen_promotions():
    """Candidates promoted by SCREEN must be citable in later stages."""
    import inspect

    source = inspect.getsource(worker.run_library_pipeline)
    assert source.index('"screen"') < source.index('"curate"') < source.index('"ingest"')


def test_full_pipeline_delivers_first_draft_before_optional_polish():
    """一键全流程必须显式关闭自动润色，并留下完成后的用户决策点。"""
    import inspect

    source = inspect.getsource(worker.run_full_pipeline)
    assert "coherence=False" in source
    assert '"polish.available"' in source
    assert 'POLISH_DECISION_KEY: "pending"' in source


def test_submission_full_pipeline_runs_quality_before_visuals_and_export():
    """A technically successful submission job must stop before producing an export."""
    import inspect

    source = inspect.getsource(worker.run_full_pipeline)
    quality_at = source.index('"quality"')
    visual_at = source.index('"visual_plan"')
    render_at = source.index('"render"')
    assert quality_at < visual_at < render_at
    assert '"quality.blocked"' in source
    assert "_converge_scholarly_quality" in source
    assert "_finish_needs_input" in source


def test_independent_quality_repair_reuses_monotonic_convergence():
    """写作页修复入口必须和全流程共用同一套可回滚收敛器。"""
    import inspect

    source = inspect.getsource(worker.run_quality_repair_pipeline)
    assert "_converge_scholarly_quality" in source
    assert '"quality.final"' in source
    assert "quality_repair_history" in source


def test_full_pipeline_only_auto_generates_one_yunwu_graphical_abstract():
    """普通建议入口不变；只有一键全流程启用单摘要图 + Yunwu 自动生成策略。"""
    import inspect

    source = inspect.getsource(worker.run_full_pipeline)
    assert "summary_only=True" in source
    assert "auto_generate=True" in source


def test_only_a_passing_submission_reaches_visuals_and_export():
    """质量门本身按行为断言。

    上一版在 `run_full_pipeline` 的源码里匹配
    `quality_outcome.readiness_status in {...}`，于是一次把判定提成局部变量的
    等价重构就让它永久失败——行为一点没变。判定现在是具名函数，直接断言它。
    """
    # 草稿模式成稿优先：评不出来也照样出图出稿。
    assert worker.can_render_after_quality("draft", "unassessed")
    assert worker.can_render_after_quality("draft", "blocked")
    # 投稿模式必须过门，否则用户会拿着一份没过质量门的导出件去投。
    assert not worker.can_render_after_quality("submission", "unassessed")
    assert not worker.can_render_after_quality("submission", "blocked")
    assert worker.can_render_after_quality("submission", "preflight_ready")
    assert worker.can_render_after_quality("submission", "submission_ready")
    assert not worker.can_render_after_quality("scholarly", "needs_revision")
    assert worker.can_render_after_quality("scholarly", "preflight_ready")


def test_independent_export_resolves_its_quality_report_from_storage():
    """The caller's unassessed readiness must not be a bypass mechanism."""
    import inspect

    source = inspect.getsource(worker.run_export_pipeline)
    assert "latest_quality_report(session, project_uuid)" in source
    assert "independent_export_requires_passing_quality_report" in source


def test_repairable_finding_count_only_counts_what_the_convergence_loop_can_fix():
    """概览页那句「另有 N 处可以再加强」必须和收敛器的能力对齐。

    draft 档把学术阻断项降级成 warning，所以计数要读 warnings；但只算
    SCHOLARLY_BLOCKER_CODES 里的码——收敛器重写的是这些码对应的章节。把
    「文献过度集中于近五年」这类提示也算进去，用户点完修复会发现什么都没变。
    """
    from paperforge_worker.pipelines.quality import QualityReport, repairable_finding_count

    draft = QualityReport(
        quality_profile="draft",
        warnings=[
            {"code": "core_claim_fulltext_missing"},
            {"code": "citation_resolution_failed"},
            {"code": "year_imbalance"},
            {"code": "short_manuscript"},
        ],
    )
    assert repairable_finding_count(draft) == 2

    # 干净的草稿不该弹修复邀请。
    assert repairable_finding_count(QualityReport(quality_profile="draft")) == 0
    assert (
        repairable_finding_count(
            QualityReport(quality_profile="draft", warnings=[{"code": "year_imbalance"}])
        )
        == 0
    )

    # 严谨档的同一批问题挂在 blockers 上，计数要跟着换一边读。
    scholarly = QualityReport(
        quality_profile="scholarly",
        blockers=[{"code": "placeholders_present"}],
        warnings=[{"code": "core_claim_fulltext_missing"}],
    )
    assert repairable_finding_count(scholarly) == 1


def test_full_pipeline_leaves_quality_repair_to_the_user():
    """质量修复是交付之后的决策点，不是管线的一环。

    draft 档跑完一次评估就交付，发现项写进 checkpoint 供概览页发出邀请；
    收敛循环只在用户主动选了 scholarly / submission 时才留在管线里。
    """
    import inspect

    source = inspect.getsource(worker.run_full_pipeline)
    assert '"quality_repair.available"' in source
    assert 'QUALITY_REPAIR_DECISION_KEY: "pending"' in source
    # 邀请只在 draft 档发出：严谨档的收敛已经在管线内跑过了。
    assert 'quality_profile == "draft" and quality_outcome is not None' in source


def test_quality_repair_refreshes_the_export_it_invalidated():
    """收敛器接受了改写就意味着正文变了，此前的导出件必须重出。

    全流程现在一定先产出一份 PDF，修复后不重导出的话，用户点完「开始修复」
    下载到的还是修复前那一份——比不修复更容易误导。
    """
    import inspect

    source = inspect.getsource(worker.run_quality_repair_pipeline)
    assert "export_document" in source
    assert 'entry["accepted"] for entry in repair_history' in source
    assert "QUALITY_REPAIR_DECISION_KEY" in source
