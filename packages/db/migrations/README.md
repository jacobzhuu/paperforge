# Alembic 迁移

约定沿用自 DeepSearch（迁移纪律 + 命名）。模型全新（见 `db/models/`）。
`env.py` 以 `db.base.Base.metadata` 为 target，并 `import db.models` 触发 19 张表注册；
连接串统一读 `DATABASE_URL`（asyncpg，async engine 执行迁移）。

## 日常命令
```bash
uv run alembic upgrade head                      # 应用迁移
uv run alembic revision --autogenerate -m "..."  # 改模型后生成新迁移
uv run alembic check                             # 校验模型与迁移无漂移（CI 用）
uv run alembic downgrade -1                      # 回退一版
```

`DATABASE_URL` 未设置时回退到 `.env.example` 里的本地默认
（`postgresql+asyncpg://paperforge:paperforge@localhost:15432/paperforge`）。

## 版本
- `0001_initial` — 设计 §4.3 的 19 张表：scholarly_work 系 5 张
  （scholarly_work / work_identifier / work_url / work_author / scholarly_http_cache）
  + 项目与论文结构域 14 张（paper_project / generation_job / job_event / library_entry /
  literature_card / document_file / user_asset / outline / paper_document / paper_section /
  citation_usage / search_run / export_artifact / llm_call_log）。
