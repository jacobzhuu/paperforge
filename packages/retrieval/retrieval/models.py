"""Local ONNX inference, explicit opt-in and bounded CPU concurrency. No paid calls."""

import os
import threading
from functools import lru_cache

MODEL = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"
RERANKER = "Xenova/ms-marco-MiniLM-L-6-v2"
_lock = threading.Lock()


def enabled() -> bool:
    return os.getenv("EVIDENCE_EMBEDDING_ENABLED", "false").lower() == "true"


@lru_cache(maxsize=1)
def _embedder():
    from fastembed import TextEmbedding

    return TextEmbedding(
        model_name=MODEL,
        threads=2,
        cache_dir=os.getenv("FASTEMBED_CACHE_PATH", "/tmp/fastembed"),
        local_files_only=True,
    )


@lru_cache(maxsize=1)
def _reranker():
    from fastembed.rerank.cross_encoder import TextCrossEncoder

    return TextCrossEncoder(
        model_name=RERANKER,
        threads=2,
        cache_dir=os.getenv("FASTEMBED_CACHE_PATH", "/tmp/fastembed"),
        local_files_only=True,
    )


def embed(texts: list[str]) -> list[list[float]]:
    if not enabled():
        raise RuntimeError("embedding_disabled")
    with _lock:
        return [row.tolist() for row in _embedder().embed(texts, batch_size=16)]


def rerank(query: str, texts: list[str]) -> list[float]:
    # The small local reranker is English-only; never silently demote cross-language results.
    if not query.isascii() or any(not text.isascii() for text in texts):
        raise RuntimeError("reranker_language_unsupported")
    with _lock:
        return [float(score) for score in _reranker().rerank(query, texts)]


def download():
    from fastembed import TextEmbedding
    from fastembed.rerank.cross_encoder import TextCrossEncoder

    cache = os.getenv("FASTEMBED_CACHE_PATH", "/tmp/fastembed")
    TextEmbedding(model_name=MODEL, cache_dir=cache, threads=2)
    TextCrossEncoder(model_name=RERANKER, cache_dir=cache, threads=2)


if __name__ == "__main__":
    download()
