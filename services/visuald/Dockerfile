FROM public.ecr.aws/docker/library/python@sha256:57cd7c3a7a273101a6485ba99423ee568157882804b1124b4dd04266317710de

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    MPLCONFIGDIR=/tmp/matplotlib

RUN apt-get update \
    && apt-get install -y --no-install-recommends graphviz fonts-noto-cjk \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY packages/visuals /app/packages/visuals
COPY services/visuald /app/services/visuald
# pip 的 wheel 缓存挂成 buildkit cache mount，而不是 `--no-cache-dir`。
#
# 这一层挂在上面两个**源码** COPY 层之下，所以改任何一行业务代码都会让它失效重跑。
# 配上 --no-cache-dir 就等于每次改代码都要重新从 PyPI 拉 matplotlib、pillow、
# fastapi、pydantic……实测一次约 60 秒——开发期每次 `./scripts/dev restart`
# 都在下载，正是这么来的。cache mount 跨构建持久，重跑也只是从本地缓存装。
RUN --mount=type=cache,target=/root/.cache/pip \
    pip install /app/packages/visuals /app/services/visuald

USER 65534:65534
EXPOSE 8082
CMD ["python", "-m", "paperforge_visuald"]
