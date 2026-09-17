#!/usr/bin/env python3
"""Ask both WeamRAG indices whether pairs that Full Merge collapsed are the same entity.

v1 asked free-text questions and let WeamRAG seed on embeddings alone. Gene symbols
embed poorly next to boilerplate descriptions, so 11 of 12 probes never reached the
target entities. v2 added entity linking: nodes whose label or alias contains a name
from the question are prepended to WeamRAG's own anchors, then its unchanged beam
search and context builder run as usual.

v3 makes the verdicts trustworthy: the answerer must reason before its verdict, the
context window is widened so the prompt is never silently truncated (prompt tokens
are logged per call), and two never-fused control pairs measure the answerer's own
bias toward SAME.

usage: compare_merge_rag.py [index_root] [out.json] [model] [modes]
       modes is a comma list of baseline,anchored
"""
import json
import re
import sys
import time
import urllib.request
from functools import lru_cache
from pathlib import Path

sys.path.insert(0, "/home/aims-dtu/HMKGRAG/VATRAG")
import dp_lca_retrieval as D   # noqa: E402

BASE = sys.argv[1] if len(sys.argv) > 1 else "/home/aims-dtu/Owl_Merge/weamrag_in"
OUT = sys.argv[2] if len(sys.argv) > 2 else "/home/aims-dtu/Owl_Merge/merge_rag_comparison.json"
MODEL = sys.argv[3] if len(sys.argv) > 3 else "llama3.1:8b-4k"
MODES = sys.argv[4].split(",") if len(sys.argv) > 4 else ["baseline", "anchored"]
OLLAMA = "http://localhost:11434/api/chat"
CHAR_BUDGET = 10_000
NUM_CTX = 8192                  # a 10k-char context is ~4.1k tokens; the 8b-4k Modelfile default truncates it
ORIG_ANCHORS = D._find_anchors_hybrid

# (a, b, kind, fused_in_full). Ground truth for every pair: DIFFERENT.
PAIRS = [
    ("Atracurium", "Cisatracurium", "drug", True),
    ("Estrone", "Estropipate", "drug", True),
    ("BRCA2", "RAD51", "gene", True),
    ("LGALS2", "LGALS3", "gene", True),
    ("HLA-A", "HLA-E", "gene", True),
    # Controls: fused nowhere, so a SAME verdict here measures the answerer's own bias.
    ("HLA-E", "LGALS2", "gene", False),
    ("Atracurium", "Estropipate", "drug", False),
]
VERDICT_RULE = ("First give one or two sentences of reasoning that cite the identifiers you used. "
                "Then end with exactly one line: VERDICT: SAME, VERDICT: DIFFERENT, or VERDICT: UNKNOWN.")
SYS_CTX = "Answer ONLY from the knowledge-graph context. Use UNKNOWN if the context does not settle it. " + VERDICT_RULE
SYS_NOCTX = "Answer from your own knowledge. " + VERDICT_RULE


def word_in(term, text):
    return re.search(rf"(?<![\w-]){re.escape(term)}(?![\w-])", text, re.I) is not None


@lru_cache(maxsize=None)
def names(wd):
    """entity_name -> its label plus aliases, from the entity.jsonl the index was built from."""
    out = {}
    for ln in open(f"{wd}/entity.jsonl"):
        e = json.loads(ln)
        labs = [e["entity_name"].rsplit(" [", 1)[0]]
        m = re.search(r"Also known as: (.*?)\. (?:Source identifiers|Ontology IRI):", e["description"])
        if m:
            labs += [a.strip() for a in m.group(1).split(";")]
        out[e["entity_name"]] = labs
    return out


def links(wd, term, within=None):
    pool = names(wd) if within is None else {n: names(wd).get(n, []) for n in within}
    return sorted(n for n, labs in pool.items() if any(word_in(term, l) for l in labs))


def retrieve(wd, question, terms, anchored):
    if anchored:
        linked = list(dict.fromkeys(n for t in terms for n in links(wd, t)))

        def seeded(query, working_dir, lca_obj, ollama_url="http://localhost:11434", top_k=15):
            lex = [n for n in linked if n in lca_obj]
            rest = [a for a in ORIG_ANCHORS(query, working_dir, lca_obj, ollama_url, top_k) if a not in lex]
            return (lex + rest)[:max(top_k, len(lex))]
        D._find_anchors_hybrid = seeded
    else:
        D._find_anchors_hybrid = ORIG_ANCHORS
    try:
        return D.dp_beam_retrieve(question, wd, char_budget=CHAR_BUDGET, max_nodes=40, beam_width=6)
    finally:
        D._find_anchors_hybrid = ORIG_ANCHORS


def verdict_of(text):
    v = re.findall(r"VERDICT:\s*\**\s*(SAME|DIFFERENT|UNKNOWN)", text, re.I)
    if v:
        return v[-1].upper()
    lines = text.strip().splitlines()
    bare = re.fullmatch(r"\W*(SAME|DIFFERENT|UNKNOWN)\W*", lines[-1], re.I) if lines else None
    return bare.group(1).upper() if bare else "UNPARSED"


def ask(question, ctx):
    user = question if ctx is None else f"CONTEXT:\n{ctx}\n\nQUESTION: {question}"
    body = {"model": MODEL, "stream": False,
            "options": {"temperature": 0, "num_ctx": NUM_CTX, "num_predict": 300},
            "messages": [{"role": "system", "content": SYS_NOCTX if ctx is None else SYS_CTX},
                         {"role": "user", "content": user}]}
    req = urllib.request.Request(OLLAMA, json.dumps(body).encode(), {"Content-Type": "application/json"})
    try:
        r = json.loads(urllib.request.urlopen(req, timeout=1800).read())
    except Exception as e:
        return f"[LLM error: {e}]", "ERROR", 0
    text = r["message"]["content"].strip()
    return text, verdict_of(text), r.get("prompt_eval_count", 0)


def structure(wd, nodes, a, b):
    ca, cb = links(wd, a, nodes), links(wd, b, nodes)
    if not ca or not cb:
        return "missing", ca, cb
    return ("collapsed" if set(ca) & set(cb) else "separate"), ca, cb


def selfcheck():
    assert word_in("HLA-A", "HLA-A (human)")
    assert not word_in("RAD51", "RAD51-AS1")
    assert not word_in("Atracurium", "Cisatracurium Besylate")
    assert word_in("atracurium", "Atracurium besylate")
    assert verdict_of("Different IDs.\nVERDICT: **DIFFERENT**") == "DIFFERENT"
    assert verdict_of("They share an ID.\nVERDICT: SAME.") == "SAME"
    assert verdict_of("UNKNOWN") == "UNKNOWN"
    assert verdict_of("no idea") == "UNPARSED"


if __name__ == "__main__":
    selfcheck()
    rows, max_tok = [], 0
    for a, b, kind, fused in PAIRS:
        q = f"Are {a} and {b} the same {kind}?"
        text, verdict, tok = ask(q, None)
        row = {"pair": [a, b], "question": q, "fused_in_full": fused, "truth": "DIFFERENT",
               "no_context": {"verdict": verdict, "answer": text, "prompt_tokens": tok}}
        print(f"\n=== {q} ({'fused' if fused else 'control'}) ===\n  no-context            verdict={verdict}", flush=True)
        for tag in ("simple", "full"):
            wd = f"{BASE}/{tag}_merge"
            in_index = {a: len(links(wd, a)), b: len(links(wd, b))}
            for mode in MODES:
                t0 = time.time()
                nodes, ctx = retrieve(wd, q, (a, b), mode == "anchored")
                state, ca, cb = structure(wd, nodes, a, b)
                text, verdict, tok = ask(q, ctx)
                max_tok = max(max_tok, tok)
                row[f"{tag}_{mode}"] = {
                    "in_index": in_index, "n_nodes": len(nodes), "secs": round(time.time() - t0, 1),
                    "structure": state, "carriers": {a: ca, b: cb}, "prompt_tokens": tok,
                    "verdict": verdict, "answer": text, "context": ctx}
                print(f"  {tag:6s} {mode:8s} nodes={len(nodes):3d} tok={tok:5d} {state:9s} verdict={verdict:9s}"
                      f" {a}->{ca[:2]} {b}->{cb[:2]}", flush=True)
        rows.append(row)
    Path(OUT).write_text(json.dumps({"model": MODEL, "num_ctx": NUM_CTX, "char_budget": CHAR_BUDGET,
                                     "rows": rows}, indent=2))

    print(f"\nmodel={MODEL}  max prompt tokens={max_tok} (num_ctx {NUM_CTX})"
          + ("  WARNING: prompt may be truncated" if max_tok >= NUM_CTX else ""))
    for group, want in (("fused", True), ("control", False)):
        sub = [r for r in rows if r["fused_in_full"] is want]
        print(f"\n=== {group} pairs ({len(sub)}), truth = DIFFERENT ===")
        print(f"  no-context            DIFFERENT={sum(r['no_context']['verdict'] == 'DIFFERENT' for r in sub)}/{len(sub)}")
        for tag in ("simple", "full"):
            for mode in MODES:
                cells = [r[f"{tag}_{mode}"] for r in sub]
                count = lambda k, v: sum(c[k] == v for c in cells)
                print(f"  {tag:6s} {mode:8s}  reached={len(cells) - count('structure', 'missing')}/{len(cells)}"
                      f"  collapsed={count('structure', 'collapsed')}  separate={count('structure', 'separate')}"
                      f"  DIFFERENT={count('verdict', 'DIFFERENT')}  SAME={count('verdict', 'SAME')}"
                      f"  UNKNOWN={count('verdict', 'UNKNOWN')}  UNPARSED={count('verdict', 'UNPARSED')}")
    print(f"\nwrote {OUT}")
