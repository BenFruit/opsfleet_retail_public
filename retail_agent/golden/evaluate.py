"""Retrieval evaluation and threshold calibration for Golden Knowledge.

    uv run python -m retail_agent.golden.evaluate

Each case is an unseen phrasing with the trio an analyst
expects, or null when nothing should be retrieved (chit-chat, memory, report
commands). Reports hit@1, recall@3, the false-positive rate on negatives and
the threshold that best separates them. Costs one embedding call per case.
"""

import argparse
import json
from pathlib import Path

DEFAULT_CASES = Path("golden/eval/retrieval_cases.json")


def score(results: list[dict], threshold: float, top_k: int = 3) -> dict:
    """results: [{expected, ranked: [(trio_id, similarity), ...]}] -> metrics at a threshold."""
    positives = [r for r in results if r["expected"]]
    negatives = [r for r in results if not r["expected"]]
    kept = [[trio for trio, sim in r["ranked"] if sim >= threshold][:top_k] for r in results]
    hit1 = sum(1 for r, k in zip(results, kept, strict=True) if r["expected"] and k[:1] == [r["expected"]])
    recall3 = sum(1 for r, k in zip(results, kept, strict=True) if r["expected"] and r["expected"] in k)
    false_pos = sum(1 for r, k in zip(results, kept, strict=True) if not r["expected"] and k)
    return {
        "threshold": threshold,
        "hit_at_1": hit1 / len(positives) if positives else 0.0,
        "recall_at_3": recall3 / len(positives) if positives else 0.0,
        "negative_false_positive_rate": false_pos / len(negatives) if negatives else 0.0,
    }


def best_threshold(results: list[dict]) -> dict:
    """Maximize recall@3 minus false positives; ties prefer the stricter threshold."""
    grid = [round(0.40 + step * 0.01, 2) for step in range(46)]
    return max(
        (score(results, t) for t in grid),
        key=lambda m: (m["recall_at_3"] - m["negative_false_positive_rate"], m["threshold"]),
    )


def main(argv=None) -> int:
    from retail_agent.golden.repository import GoldenRepository
    from retail_agent.golden.service import query_text
    from retail_agent.llm.embeddings import Embedder
    from retail_agent.settings import Settings
    from retail_agent.storage.postgres import connection_factory

    settings = Settings()
    parser = argparse.ArgumentParser(description="Evaluate Golden Knowledge retrieval")
    parser.add_argument("--cases", type=Path, default=DEFAULT_CASES)
    args = parser.parse_args(argv)
    cases = json.loads(args.cases.read_text(encoding="utf-8"))
    embedder = Embedder.from_settings(settings)
    try:
        repository = GoldenRepository(connection_factory(settings))
        results = []
        for case in cases:
            text = query_text(case["question"])
            vector = embedder.embed(text, document=False, observation_name="golden_eval_embedding")
            rows = repository.search(vector=vector, query_text=text, model=embedder.model)
            ranked = [(row["trio_id"], float(row["similarity"])) for row in rows]
            results.append({"question": case["question"], "expected": case["expected"], "ranked": ranked})
    finally:
        embedder.close()

    for r in results:
        top = r["ranked"][0] if r["ranked"] else ("-", 0.0)
        mark = "neg " if not r["expected"] else ("ok  " if top[0] == r["expected"] else "MISS")
        print(f"{mark} {top[1]:.3f} top={top[0]:<28} expected={r['expected'] or '-':<28} {r['question']}")
    pos = sorted(r["ranked"][0][1] for r in results if r["expected"] and r["ranked"])
    neg = sorted(r["ranked"][0][1] for r in results if not r["expected"] and r["ranked"])
    print(
        f"\nTop similarity: positives min={pos[0]:.3f} median={pos[len(pos) // 2]:.3f}; "
        f"negatives max={neg[-1]:.3f} median={neg[len(neg) // 2]:.3f}"
    )
    for threshold in (0.5, 0.55, 0.6, 0.65, 0.7):
        print(json.dumps(score(results, threshold)))
    print("Best:", json.dumps(best_threshold(results)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
