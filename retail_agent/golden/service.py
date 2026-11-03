"""Query-time Golden Knowledge retrieval: hybrid scoring, threshold, top-k, budget."""

import json
import logging
from time import monotonic

from retail_agent.golden.models import GoldenTrio

logger = logging.getLogger(__name__)

# A short follow-up ("and by month?") carries little intent on its own.
_FOLLOW_UP_MAX_WORDS = 6
# Lexical overlap only breaks near-ties; semantic similarity decides relevance.
_LEXICAL_WEIGHT = 0.05


def query_text(question: str, previous_question: str | None = None) -> str:
    """Text to embed: a short follow-up is joined to the previous question."""
    if previous_question and len(question.split()) <= _FOLLOW_UP_MAX_WORDS:
        return f"{previous_question}\n{question}"
    return question


class GoldenKnowledge:
    def __init__(
        self, repository, embedder, *, top_k: int = 3, min_similarity: float = 0.65, max_prompt_chars: int = 9000
    ):
        self.repository = repository
        self.embedder = embedder
        self.top_k = top_k
        self.min_similarity = min_similarity
        self.max_prompt_chars = max_prompt_chars

    def retrieve(self, question: str, *, previous_question: str | None = None, turn_id: str | None = None) -> dict:
        """Never raises: retrieval is advisory, so an outage degrades to schema-only planning."""
        started = monotonic()
        try:
            text = query_text(question, previous_question)
            vector = self.embedder.embed(text, document=False, observation_name="golden_query_embedding")
            rows = self.repository.search(vector=vector, query_text=text, model=self.embedder.model)
            candidates = []
            for row in rows:
                trio = GoldenTrio.model_validate(row["document"])
                similarity = float(row["similarity"])
                score = similarity + _LEXICAL_WEIGHT * min(float(row["lexical"] or 0) * 10, 1.0)
                candidates.append((score, similarity, trio))
            candidates.sort(key=lambda item: (-item[0], item[2].id))
            items = []
            for _, similarity, trio in candidates:
                if similarity < self.min_similarity or len(items) >= self.top_k:
                    continue
                view = trio.prompt_view(similarity)
                if len(json.dumps([*items, view], ensure_ascii=False)) > self.max_prompt_chars:
                    break
                items.append(view)
            result = {"status": "ready", "items": items}
            logger.info(
                "golden_retrieved",
                extra={
                    "turn_id": turn_id,
                    "status": "ok",
                    "row_count": len(items),
                    "candidate_count": len(candidates),
                    "golden_ids": [item["trio_id"] for item in items],
                    "top_similarity": round(candidates[0][1], 3) if candidates else None,
                    "duration_ms": round((monotonic() - started) * 1000),
                },
            )
            return result
        except Exception as error:
            logger.warning(
                "golden_retrieval_failed",
                extra={
                    "turn_id": turn_id,
                    "error_code": "golden_unavailable",
                    "error_type": type(error).__name__,
                    "duration_ms": round((monotonic() - started) * 1000),
                },
            )
            return {"status": "unavailable", "items": []}
