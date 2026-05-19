"""Embedding generation and cross-encoder reranking for semantic search."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import numpy as np

_embedder_instance = None
_reranker_instance = None


class Embedder:
    """Wrapper around FastEmbed for generating text embeddings."""

    MODEL = "nomic-ai/nomic-embed-text-v1.5-Q"  # 130MB, 768d, 8K context, ONNX
    DIM = 768

    def __init__(self):
        from fastembed import TextEmbedding

        self._model = TextEmbedding(model_name=self.MODEL)

    def embed(self, texts: list[str]) -> list["np.ndarray"]:
        """Embed a batch of documents (uses model's passage prefix)."""
        # Cast to float32 — nomic returns float64, sqlite-vec stores float32.
        return [arr.astype("float32", copy=False) for arr in self._model.passage_embed(texts)]

    def embed_single(self, text: str) -> "np.ndarray":
        """Embed a single query (uses model's query prefix)."""
        arr = list(self._model.query_embed([text]))[0]
        return arr.astype("float32", copy=False)


class Reranker:
    """Cross-encoder reranker for precise relevance scoring.

    Takes (query, document) pairs and scores them with full
    cross-attention — much more accurate than bi-encoder similarity.
    """

    MODEL = "jinaai/jina-reranker-v1-tiny-en"  # 130MB, 8K context, ONNX

    def __init__(self):
        from fastembed.rerank.cross_encoder import TextCrossEncoder

        self._model = TextCrossEncoder(model_name=self.MODEL)

    def rerank(
        self, query: str, documents: list[str]
    ) -> list[tuple[int, float]]:
        """Rerank documents by relevance to query.

        Returns list of (original_index, score) sorted by score descending.
        """
        scores = list(self._model.rerank(query, documents))
        indexed_scores = list(enumerate(scores))
        indexed_scores.sort(key=lambda x: x[1], reverse=True)
        return indexed_scores


def get_embedder() -> Embedder | None:
    """Get the singleton embedder instance, or None if not available."""
    global _embedder_instance
    if _embedder_instance is not None:
        return _embedder_instance

    try:
        _embedder_instance = Embedder()
        return _embedder_instance
    except ImportError:
        return None
    except Exception:
        return None


def get_reranker(allow_download: bool = False) -> Reranker | None:
    """Get the singleton reranker instance, or None if not available.

    Args:
        allow_download: If True, allow downloading the model on first use.
            If False (default for search), skip if model not cached.
    """
    global _reranker_instance
    if _reranker_instance is not None:
        return _reranker_instance

    if not allow_download:
        try:
            from fastembed.common.utils import define_cache_dir

            cache = define_cache_dir()
            model_dirs = list(cache.glob("*jina-reranker*tiny*"))
            if not model_dirs:
                return None
        except Exception:
            pass

    try:
        _reranker_instance = Reranker()
        return _reranker_instance
    except (ImportError, Exception):
        return None


def ensure_models_downloaded() -> None:
    """Pre-download all models. Called during 'index' command."""
    try:
        get_embedder()
    except Exception:
        pass
    try:
        get_reranker(allow_download=True)
    except Exception:
        pass
