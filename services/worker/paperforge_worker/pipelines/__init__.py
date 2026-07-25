"""生成管线阶段（方案 §4.4 / §9）。

每个模块对应一条迁移/改造来源。M0：定义阶段契约与迁移 TODO；
实现随 M1–M4 逐阶段落地。改造自旧模块的对照见各文件头注释。

阶段顺序（综述管线）：
  scope → search → curate → ingest → cards → outline → write → citecheck → render → review
研究型管线在 write 前插入 INPUT（素材摄取）与 NUMLINT（数字一致性 lint）。
"""
