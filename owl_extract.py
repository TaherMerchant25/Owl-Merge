#!/usr/bin/env python3
"""Stream a one-statement-per-line RDF/XML pharma-KG dump into flat TSVs.

These dumps are line-oriented (verified: every <rdf:Description> closes on its
own line) except a trailing block of reified owl:Axiom alignments. That lets us
regex-stream them instead of using rdflib, which would need ~50GB RAM here.
"""
import gzip
import re
import sys
from collections import defaultdict
from pathlib import Path

RE_SUBJ = re.compile(r'rdf:about="([^"]*)"')
RE_NODEID = re.compile(r'rdf:nodeID="([^"]*)"')
RE_TYPE = re.compile(r'<rdf:type rdf:resource="([^"]*)"')
RE_LABEL = re.compile(r'<rdfs:label[^>]*>(.*?)</rdfs:label>')
RE_PRED_RES = re.compile(r'<p:([A-Za-z0-9_]+)[^>]*rdf:resource="([^"]*)"')
RE_PRED_LIT = re.compile(r'<p:([A-Za-z0-9_]+)[^>]*>(.*?)</p:[A-Za-z0-9_]+>')
RE_ANN = re.compile(r'<owl:annotated(Source|Property|Target) rdf:resource="([^"]*)"')
RE_CONF = re.compile(r'<pkg:confidenceScore[^>]*>([^<]*)</pkg:confidenceScore>')

UNESCAPE = [("&lt;", "<"), ("&gt;", ">"), ("&quot;", '"'), ("&apos;", "'"), ("&amp;", "&")]


def unescape(s):
    for a, b in UNESCAPE:
        s = s.replace(a, b)
    return s


def clean(s):
    return unescape(s).replace("\t", " ").replace("\n", " ").strip()


def extract(path, outdir, tag):
    outdir.mkdir(parents=True, exist_ok=True)
    labels = defaultdict(list)
    source_ids = defaultdict(list)
    is_class = set()

    n_edges = n_align = n_lit = 0
    axiom = {}

    edges_f = gzip.open(outdir / f"{tag}.edges.tsv.gz", "wt")
    align_f = gzip.open(outdir / f"{tag}.alignments.tsv.gz", "wt")
    edges_f.write("subject\tpredicate\tobject\n")
    align_f.write("source\tproperty\ttarget\tconfidence\n")

    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        for i, line in enumerate(fh):
            if "<owl:annotated" in line or "confidenceScore" in line or "nodeID" in line:
                # trailing reified-axiom region: accumulate until the block closes
                m = RE_NODEID.search(line)
                if m:
                    axiom = {}
                    continue
                m = RE_ANN.search(line)
                if m:
                    axiom[m.group(1)] = m.group(2)
                    continue
                m = RE_CONF.search(line)
                if m and axiom.get("Source"):
                    align_f.write(
                        f"{axiom['Source']}\t{axiom.get('Property','')}\t"
                        f"{axiom.get('Target','')}\t{m.group(1)}\n"
                    )
                    n_align += 1
                    axiom = {}
                    continue
                if "</rdf:Description>" in line:
                    continue

            ms = RE_SUBJ.search(line)
            if not ms:
                continue
            subj = ms.group(1)

            mt = RE_TYPE.search(line)
            if mt:
                if mt.group(1).endswith("owl#Class"):
                    is_class.add(subj)
                continue

            ml = RE_LABEL.search(line)
            if ml:
                lab = clean(ml.group(1))
                if lab and lab not in labels[subj]:
                    labels[subj].append(lab)
                continue

            mp = RE_PRED_RES.search(line)
            if mp:
                edges_f.write(f"{subj}\t{mp.group(1)}\t{mp.group(2)}\n")
                n_edges += 1
                continue

            mp = RE_PRED_LIT.search(line)
            if mp:
                pred, val = mp.group(1), clean(mp.group(2))
                if pred == "sourceId" and val:
                    source_ids[subj].append(val)
                n_lit += 1

    edges_f.close()
    align_f.close()

    ents = set(is_class) | set(labels) | set(source_ids)
    with gzip.open(outdir / f"{tag}.entities.tsv.gz", "wt") as f:
        f.write("iri\tn_labels\tlabels\tsource_ids\n")
        for e in sorted(ents):
            f.write(
                f"{e}\t{len(labels.get(e, []))}\t"
                f"{'|'.join(labels.get(e, []))}\t{'|'.join(source_ids.get(e, []))}\n"
            )

    multi = sum(1 for e in ents if len(labels.get(e, [])) > 1)
    stats = {
        "file": str(path),
        "entities": len(ents),
        "classes": len(is_class),
        "entities_with_multiple_labels": multi,
        "total_labels": sum(len(v) for v in labels.values()),
        "edges": n_edges,
        "alignment_axioms": n_align,
        "literal_statements": n_lit,
    }
    return stats


if __name__ == "__main__":
    outdir = Path(sys.argv[1] if len(sys.argv) > 1 else "extract")
    for src, tag in [
        ("Subgraph_Simple-Merge.owl", "simple"),
        ("Subgraph_Full-Merge.owl", "full"),
    ]:
        p = Path(src)
        if not p.exists():
            print(f"skip missing {p}")
            continue
        st = extract(p, outdir, tag)
        print(f"=== {tag} ===")
        for k, v in st.items():
            print(f"  {k}: {v}")
        sys.stdout.flush()
