#!/usr/bin/env python3
"""Run identical queries against the Simple-Merge and Full-Merge WeamRAG indices.

Measures the thing that actually matters downstream: whether entities the merge
collapsed still come back as distinguishable nodes, and whether the generated
answer conflates them.
"""
import json
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, "/home/aims-dtu/HMKGRAG/VATRAG")
from dp_lca_retrieval import dp_beam_retrieve          # noqa: E402
from openai import OpenAI                              # noqa: E402

BASE = "/home/aims-dtu/Owl_Merge/weamrag_in"
LLM = OpenAI(api_key="ollama", base_url="http://localhost:11434/v1")
MODEL = "llama3.1:8b-4k"

# Each probe names two entities the Full Merge collapsed into one node.
PROBES = [
    ("HLA-A",        ["HLA-A", "HLA-E", "HLA-H", "HLA-J"],
     "What is the gene HLA-A associated with? List the specific genes involved."),
    ("Atracurium",   ["Atracurium", "Cisatracurium"],
     "What is Atracurium and what does it interact with? Is Atracurium the same drug as Cisatracurium?"),
    ("BRCA2",        ["BRCA2", "RAD51"],
     "What is BRCA2 associated with? Is BRCA2 the same gene as RAD51?"),
    ("Galectin",     ["LGALS2", "LGALS3"],
     "What is LGALS3 (Galectin-3) associated with? Is it the same gene as LGALS2?"),
    ("Estrone",      ["Estrone", "Estropipate"],
     "What is Estrone and is it the same compound as Estropipate?"),
    ("SYT11",        ["SYT11", "SYT12"],
     "What is SYT11 and is it the same gene as SYT12?"),
]


def answer(ctx, q):
    try:
        r = LLM.chat.completions.create(
            model=MODEL, temperature=0.0, max_tokens=320,
            messages=[
                {"role": "system", "content":
                 "Answer ONLY from the provided knowledge-graph context. "
                 "If two names refer to separate entities in the context, say so explicitly. "
                 "If the context cannot distinguish them, say that."},
                {"role": "user", "content": f"CONTEXT:\n{ctx[:12000]}\n\nQUESTION: {q}"},
            ])
        return r.choices[0].message.content.strip()
    except Exception as e:
        return f"[LLM error: {e}]"


def run(tag, q):
    wd = f"{BASE}/{tag}_merge"
    t0 = time.time()
    try:
        nodes, ctx = dp_beam_retrieve(q, wd, max_nodes=40, beam_width=6)
    except Exception as e:
        return {"error": str(e), "nodes": [], "ctx": "", "secs": 0}
    return {"nodes": sorted(nodes), "ctx": ctx, "secs": round(time.time() - t0, 1)}


def distinct_hits(nodes, names):
    """How many of the target entity names surface as SEPARATE retrieved nodes."""
    hit = {}
    for n in names:
        pat = re.compile(rf"(^|[^A-Za-z0-9]){re.escape(n)}([^A-Za-z0-9]|$)", re.I)
        hit[n] = [x for x in nodes if pat.search(x)]
    sep = len({tuple(v) for v in hit.values() if v})
    return hit, sep


if __name__ == "__main__":
    out = []
    for key, names, q in PROBES:
        print(f"\n{'='*78}\nPROBE: {key}  —  {q}\n{'='*78}")
        row = {"probe": key, "question": q, "targets": names}
        for tag in ("simple", "full"):
            r = run(tag, q)
            hit, sep = distinct_hits(r["nodes"], names)
            a = answer(r["ctx"], q) if r["ctx"] else "[no context]"
            row[tag] = {"n_nodes": len(r["nodes"]), "secs": r["secs"],
                        "hits": hit, "distinct_node_groups": sep, "answer": a}
            print(f"\n--- {tag.upper()} MERGE  ({len(r['nodes'])} nodes, {r['secs']}s) ---")
            for n, m in hit.items():
                print(f"   {n:12s} -> {m if m else 'NOT RETRIEVED'}")
            print(f"   distinct node groups among targets: {sep}")
            print(f"   ANSWER: {a[:600]}")
        out.append(row)
    Path("/home/aims-dtu/Owl_Merge/merge_rag_comparison.json").write_text(json.dumps(out, indent=2))
    print("\nwrote merge_rag_comparison.json")
