#!/usr/bin/env python3
"""Sample a matched subgraph from each merge variant and emit WeamRAG input.

WeamRAG (build_graph.py:get_common_rag_res) keys entities by `entity_name` and
silently merges duplicates. So entity_name must encode the identity the merge
strategy actually chose, or the framework would re-merge Simple-Merge's
deliberately-separate nodes and destroy the thing we are measuring. Hence
`label [namespace:local_id]`.

Both variants are sampled from the same seed set (matched on sourceId) so the
two indices cover the same biology and answers are comparable.
"""
import gzip
import json
import random
import re
import sys
from collections import defaultdict, deque
from pathlib import Path

EXTRACT = Path("extract")
NS = {
    "hetionet.org": "hetionet",
    "drkg.gnn4dr.org": "drkg",
    "primekg.org": "primekg",
    "optimuskg.ai": "optimuskg",
    "purl.obolibrary.org": "obo",
    "percuro.org": "percuro",
    "dbpedia.org": "dbpedia",
}

# Seeds chosen to include the confirmed false-merge pairs, so the QA probes hit them.
SEED_SOURCE_IDS = {
    "Compound::DB00565", "Compound::DB00732",   # Cisatracurium / Atracurium
    "Compound::DB00655", "Compound::DB04574",   # Estrone / Estropipate
    "Gene::675", "Gene::5888",                  # BRCA2 / RAD51
    "Gene::3105", "Gene::3133",                 # HLA-A / HLA-E
    "Gene::3957", "Gene::3958",                 # LGALS2 / LGALS3
    "Gene::23208", "Gene::91683",               # SYT11 / SYT12
    "Gene::10715", "Gene::2657",                # CERS1 / GDF1
    "Compound::DB00945", "Compound::DB00316",   # aspirin, acetaminophen
    "Gene::7157", "Gene::1956",                 # TP53, EGFR
}


def host(iri):
    m = re.match(r"https?://([^/]+)/", iri)
    return NS.get(m.group(1), m.group(1).split(".")[0]) if m else "x"


def local(iri):
    return re.split(r"[/#]", iri.rstrip("/"))[-1]


def etype(iri):
    m = re.search(r"/entity/([^/]+)/", iri)
    if m:
        return m.group(1).replace("_", " ")
    return "Concept" if "/resource/" in iri else "Entity"


def load(tag):
    ents = {}
    with gzip.open(EXTRACT / f"{tag}.entities.tsv.gz", "rt") as f:
        next(f)
        for ln in f:
            p = ln.rstrip("\n").split("\t")
            if len(p) < 4:
                p += [""] * (4 - len(p))
            labs = [x for x in p[2].split("|") if x]
            sids = [x for x in p[3].split("|") if x]
            ents[p[0]] = (labs, sids)
    adj = defaultdict(list)
    n = 0
    with gzip.open(EXTRACT / f"{tag}.edges.tsv.gz", "rt") as f:
        next(f)
        for ln in f:
            p = ln.rstrip("\n").split("\t")
            if len(p) != 3:
                continue
            s, pr, o = p
            if s in ents and o in ents:
                adj[s].append((pr, o))
                adj[o].append((pr, s))
                n += 1
    return ents, adj, n


def sample(ents, adj, cap, seed=0):
    rng = random.Random(seed)
    seeds = [i for i, (_, sids) in ents.items() if any(s in SEED_SOURCE_IDS for s in sids)]
    seeds.sort()
    if not seeds:
        seeds = sorted(ents)[:50]
    keep, q = set(seeds), deque(seeds)
    while q and len(keep) < cap:
        cur = q.popleft()
        nb = adj.get(cur, [])
        if len(nb) > 60:                       # cap hubs so one gene can't eat the budget
            nb = rng.sample(nb, 60)
        for _, o in nb:
            if o not in keep:
                keep.add(o)
                q.append(o)
                if len(keep) >= cap:
                    break
    return keep, set(seeds)


def name_of(iri, labs):
    lab = labs[0] if labs else local(iri)
    return f"{lab} [{host(iri)}:{local(iri)}]"


def emit(tag, cap, outroot):
    ents, adj, n_edges = load(tag)
    keep, seeds = sample(ents, adj, cap)
    out = Path(outroot) / f"{tag}_merge"
    out.mkdir(parents=True, exist_ok=True)

    names = {i: name_of(i, ents[i][0]) for i in keep}
    with open(out / "entity.jsonl", "w") as f:
        for i in sorted(keep):
            labs, sids = ents[i]
            alias = "; ".join(labs[1:12])
            desc = f"{labs[0] if labs else local(i)} is a {etype(i)} in a pharmacological knowledge graph."
            if alias:
                desc += f" Also known as: {alias}."
            if sids:
                desc += f" Source identifiers: {', '.join(sids[:8])}."
            desc += f" Ontology IRI: {i}."
            f.write(json.dumps({
                "entity_name": names[i], "description": desc,
                "source_id": "|".join(sids[:8]) or local(i),
                "type": etype(i), "doc_name": f"{tag}_merge",
            }) + "\n")

    seen, nrel = set(), 0
    with open(out / "relation.jsonl", "w") as f:
        for s in sorted(keep):
            for pr, o in adj.get(s, []):
                if o not in keep:
                    continue
                k = (names[s], names[o], pr)
                if k in seen:
                    continue
                seen.add(k)
                readable = pr.replace("__", " ").replace("_", " ").lower()
                f.write(json.dumps({
                    "src_tgt": names[s], "tgt_src": names[o], "source": pr,
                    "description": f"{names[s]} {readable} {names[o]}.",
                    "weight": 1, "source_id": f"{tag}_merge",
                }) + "\n")
                nrel += 1

    multi = sum(1 for i in keep if len(ents[i][0]) > 1)
    print(f"=== {tag} ===")
    print(f"  graph            : {len(ents):,} entities / {n_edges:,} usable edges")
    print(f"  seeds matched    : {len(seeds)}")
    print(f"  sampled entities : {len(keep):,}  (multi-label: {multi:,})")
    print(f"  sampled relations: {nrel:,}")
    print(f"  -> {out}")
    return len(keep), nrel


if __name__ == "__main__":
    cap = int(sys.argv[1]) if len(sys.argv) > 1 else 3000
    outroot = sys.argv[2] if len(sys.argv) > 2 else "weamrag_in"
    for tag in ("simple", "full"):
        emit(tag, cap, outroot)
