"""Turn text into vectors with a small static model that runs on the CPU.

The model is minishlab/potion-base-8M (model2vec, MIT licence, 30 MB, 256 dimensions),
loaded from a local directory (scripts/fetch_model.py puts it there). Nothing is
downloaded at run time. A static model looks each token up and averages, so it is
deterministic and a short query takes well under a millisecond.
"""

import warnings
from collections.abc import Sequence
from pathlib import Path

import numpy as np
from model2vec import StaticModel

from handbook_server import EMBEDDING_DIM


class ModelNotFoundError(RuntimeError):
    """The model directory is missing; run scripts/fetch_model.py."""


class Embedder:
    def __init__(self, model_path: Path) -> None:
        if not (model_path / "model.safetensors").is_file():
            raise ModelNotFoundError(
                f"no embedding model in {model_path}; run scripts/fetch_model.py --dest <directory>"
            )
        # A local directory: from_pretrained reads it and never reaches for the network.
        # model2vec reads config.json with json.load(open(...)) and never closes the file;
        # that is its slip, so the one ResourceWarning it causes is silenced here.
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", ResourceWarning)
            self._model = StaticModel.from_pretrained(str(model_path))
        dimension = int(self._model.dim)
        if dimension != EMBEDDING_DIM:
            raise ModelNotFoundError(
                f"the model has {dimension} dimensions, the schema expects {EMBEDDING_DIM}"
            )

    def embed(self, texts: Sequence[str]) -> list[list[float] | None]:
        """One unit-length vector per text, or None for a text with no known tokens."""
        matrix = np.asarray(self._model.encode(list(texts)), dtype=np.float32)
        norms = np.linalg.norm(matrix, axis=1)
        vectors: list[list[float] | None] = []
        for row, norm in zip(matrix, norms, strict=True):
            vectors.append(None if norm == 0 else (row / norm).tolist())
        return vectors


def vector_literal(vector: Sequence[float]) -> str:
    """pgvector's text form of a vector, to send as a parameter and cast on the server."""
    return "[" + ",".join(f"{value:.7g}" for value in vector) + "]"
