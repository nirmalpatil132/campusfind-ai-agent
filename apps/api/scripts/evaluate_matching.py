"""Offline evaluation of the matching engine on the PROJECT_GUIDE.md §22 dataset. No database needed.

Run from apps/api:   python scripts/evaluate_matching.py            (structured signals only)
                     python scripts/evaluate_matching.py --embeddings (also uses Ollama embeddings)

Prints: recall@3 for the 10 true pairs, trap pairs scoring >= 0.55, duplicates flagged.
Uses the SAME scoring functions as the live API (app/services/matching.py).
"""

from __future__ import annotations

import argparse
import csv
import math
import sys
from datetime import date, time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.campus import zone_of  # noqa: E402
from app.services.matching import (  # noqa: E402
    ItemFeatures, is_probable_duplicate, score_pair, stage1_filter,
)

SEED = Path(__file__).resolve().parents[3] / "db" / "seed"
TRUE_PAIRS = [(f"L{n:02d}", f"F{n:02d}") for n in range(1, 11)]
TRAP_PAIRS = [(f"L{n:02d}", f"F{n:02d}") for n in range(11, 16)]


def load(name: str) -> list[dict]:
    with (SEED / name).open(newline="", encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    for r in rows:
        r["event_date"] = date.fromisoformat(r["date"])
        r["event_time"] = time.fromisoformat(r["time"]) if r.get("time") else None
        for k in ("brand", "color"):
            r[k] = r[k] or None
    return rows


def features(r: dict) -> ItemFeatures:
    return ItemFeatures(category=r["category"], color=r["color"], brand=r["brand"], location=r["location"],
                        zone=zone_of(r["location"]), event_date=r["event_date"], event_time=r["event_time"],
                        description=r["description"])


def cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    return dot / (math.sqrt(sum(x * x for x in a)) * math.sqrt(sum(y * y for y in b)))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--embeddings", action="store_true", help="use Ollama embeddings for the semantic signal")
    args = ap.parse_args()

    lost, found = load("lost.csv"), load("found.csv")
    vectors: dict[str, list[float]] = {}
    if args.embeddings:
        from app.services.embeddings import embed_item
        for r in lost + found:
            v = embed_item(r["category"], r["item_name"], r["color"], r["brand"], r["description"])
            if v is None:
                sys.exit("Embeddings unavailable (is Ollama running with nomic-embed-text pulled?).")
            vectors[r["code"]] = v
    mode = "with Ollama embeddings" if vectors else "structured signals only (semantic weight renormalised)"
    print(f"\nMatching evaluation, {mode}\n")

    ranked: dict[str, list[tuple[float, str]]] = {}
    for l in lost:
        lf = features(l)
        scores = []
        for f in found:
            ff = features(f)
            if l["created_by"] == f["created_by"] or not stage1_filter(lf, "lost", ff):
                continue
            cos = cosine(vectors[l["code"]], vectors[f["code"]]) if vectors else None
            r = score_pair(lf, ff, cos)
            scores.append((r["total_score"], f["code"], r["rank_score"], r["band"]))
        ranked[l["code"]] = [(s, c, b) for s, c, _, b in
                             sorted(scores, key=lambda x: (x[0], x[2]), reverse=True)]

    print("True pairs (target: partner in top 3)")
    hits = 0
    for lc, fc in TRUE_PAIRS:
        top = ranked[lc][:3]
        rank = next((i + 1 for i, (_, c, _b) in enumerate(ranked[lc]) if c == fc), None)
        score = next((s for s, c, _b in ranked[lc] if c == fc), None)
        ok = rank is not None and rank <= 3
        hits += ok
        print(f"  {lc}-{fc}: rank {rank}, score {score}  top3={[(c, s) for s, c, _b in top]}  {'OK' if ok else 'MISS'}")
    order = [c for _, c, _b in ranked["L01"]]
    f01_first = order.index("F01") < order.index("F11")
    print(f"  L01 ranks F01 above F11 (tie broken by the shared red keychain): {f01_first}")

    print("\nTrap pairs (target: not shown as candidates; L13-F13 may pass and is caught by verification)")
    above = 0
    for lc, fc in TRAP_PAIRS:
        hit = next(((s, b) for s, c, b in ranked[lc] if c == fc), None)
        flag = hit is not None and hit[1] != "hidden"
        above += flag
        shown = f"{hit[0]} ({hit[1]})" if hit else "filtered out by stage 1"
        print(f"  {lc}-{fc}: {shown}  {'SHOWN AS CANDIDATE' if flag else 'ok'}")

    print("\nDuplicates (target: flagged)")
    by_code = {r["code"]: r for r in lost + found}
    flagged = 0
    for d in load("duplicates.csv"):
        d["report_type"] = d["type"]
        orig = dict(by_code[d["duplicate_of"]])
        orig["report_type"] = "lost" if orig["code"].startswith("L") else "found"
        hit = is_probable_duplicate(d, orig)
        flagged += hit
        print(f"  {d['code']} vs {d['duplicate_of']}: {'flagged' if hit else 'MISSED'}")

    print("\nSummary")
    print(f"  recall@3 on true pairs : {hits}/10   (target >= 8)")
    print(f"  trap pairs shown       : {above}/5    (target <= 1)")
    print(f"  L01: F01 above F11     : {f01_first}")
    print(f"  duplicates flagged     : {flagged}/5    (target >= 4)\n")


if __name__ == "__main__":
    main()
