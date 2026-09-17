#!/usr/bin/env python3
"""Multi-hop question answering over the merged pharma OWL graphs, driven by SPARQL.

Each OWL file is bulk-loaded into its own on-disk Oxigraph store. A question is
answered in four steps, and every graph access is a SPARQL query:

  1. retrieval  link phrases in the question to entity IRIs through rdfs:label
  2. identity   follow owl:equivalentClass links, plain or reified with a
                pkg:confidenceScore, above a confidence threshold. Simple Merge
                needs this to hop between sources; Full Merge has no such links.
  3. reasoning  two entities: bidirectional search for connecting paths
                one entity:  relation-guided expansion, hop by hop
  4. answer     a local LLM answers from the numbered evidence paths only

Nodes holding two IDs from the same source database are flagged: in this data
those are fused distinct entities (e.g. BRCA2 + RAD51).

usage:
  kg_engine.py load   {simple,full} [owl]
  kg_engine.py sparql {simple,full} QUERY
  kg_engine.py ask    {simple,full} "How is BRCA2 related to tamoxifen?" [options]
  kg_engine.py selfcheck
"""
import argparse
import copy
import shutil
import json
import re
import sys
import time
import urllib.request
from collections import Counter, defaultdict
from pathlib import Path

import pyoxigraph as ox

from build_weamrag_input import NS, local

HERE = Path(__file__).resolve().parent
STORES = HERE / "stores"
OWL_FILES = {"simple": HERE / "Subgraph_Simple-Merge.owl", "full": HERE / "Subgraph_Full-Merge.owl"}
OLLAMA = "http://localhost:11434"
LLM_MODEL = "llama3.1:8b"          # num_ctx is passed per request, so the plain tag works on any Ollama install
EMBED_MODEL = "nomic-embed-text"

RDF_TYPE = "http://www.w3.org/1999/02/22-rdf-syntax-ns#type"
LABEL = "http://www.w3.org/2000/01/rdf-schema#label"
OWL = "http://www.w3.org/2002/07/owl#"
EQC, EQP = OWL + "equivalentClass", OWL + "equivalentProperty"
ANN_S, ANN_P, ANN_T = OWL + "annotatedSource", OWL + "annotatedProperty", OWL + "annotatedTarget"
CONF = "http://pharma-kg.local/vocab#confidenceScore"
# Not reasoning edges: typing, naming, identity (handled separately), axiom plumbing, xref and wiki-link noise.
SKIP = [RDF_TYPE, LABEL, EQC, EQP, ANN_S, ANN_P, ANN_T,
        "http://primekg.org/relation/hasDbXref", "http://percuro.org/relation/wiki_page_wiki_link"]
SOURCES = {**NS, "www.ncbi.nlm.nih.gov": "ncbi", "uswest.ensembl.org": "ensembl"}
SAFE_IRI = re.compile(r'^[^\s<>"{}|^`\\]+$')

STOP = set("""a about affect affects all also an and any are as associated at be between both by can
cause caused causes connect connected connection do does for from give how in indicated interact
interacts is it link linked list me name of on or relate related relation relationship same show
target targeted targets that the their them through to treat treated treats used via what which who
why with""".split())
# ponytail: IRI-substring typing is enough for these seven sources; switch to rdf:type classes if a source is added
TYPES = {
    "drug": (r"\b(drugs?|compounds?|medications?|medicines?)\b", ("/compound/", "/drug/", "chebi_", "chembl", "drugbank")),
    "gene": (r"\b(genes?|proteins?)\b", ("/gene", "gene_protein", "/protein/", "ensg")),
    "disease": (r"\b(diseases?|disorders?|conditions?)\b", ("/disease/", "mondo_", "doid_")),
    "phenotype": (r"\b(side effects?|symptoms?|phenotypes?|adverse)\b", ("side_effect", "symptom", "phenotype", "/hp_")),
    "pathway": (r"\bpathways?\b", ("pathway", "react_")),
    "anatomy": (r"\b(tissues?|organs?|anatomy)\b", ("anatomy", "uberon_")),
    "process": (r"\b(process(es)?|functions?)\b", ("biological_process", "molecular_function", "/go_", "bioprocess", "molfunc")),
}
SYSTEM = """You answer questions from evidence paths retrieved from a biomedical knowledge graph.
Rules:
- Use ONLY the evidence. Cite the paths you rely on, like [P3].
- '—r→' is a relation. '≡0.93≡' is a cross-database identity link with its confidence score.
- If a WARNING names a node your answer depends on, say the answer may conflate distinct entities.
- If the evidence does not answer the question, say so plainly."""


def norm(s):
    return re.sub(r"[^0-9a-z+]+", " ", s.casefold()).strip()


def iris(xs):
    return " ".join(f"<{x}>" for x in xs if SAFE_IRI.match(x))


def chunks(xs, n=300):
    xs = sorted(xs)
    return [xs[i:i + n] for i in range(0, len(xs), n)]


def post(path, body, timeout=600):
    req = urllib.request.Request(OLLAMA + path, json.dumps(body).encode(), {"Content-Type": "application/json"})
    return json.loads(urllib.request.urlopen(req, timeout=timeout).read())


def chain(parent, node):
    steps = []
    while parent[node] is not None:
        prev, edge = parent[node]
        steps.append((prev, edge, node))
        node = prev
    return steps[::-1]


def flip(edge):
    kind, val, fwd = edge
    return (kind, val, not fwd) if kind == "rel" else edge


def path_rank(path):
    confs = [e[1] for _, e, _ in path if e[0] == "eq"]
    weakest = min((c if c is not None else 0.0 for c in confs), default=1.0)
    return sum(e[0] == "rel" for _, e, _ in path), -weakest, len(path)


def hop_types(question):
    """Type words, innermost first: 'drugs for diseases linked to BRCA2' -> [disease, drug]."""
    hits = sorted((m.start(), t) for t, (rx, _) in TYPES.items() for m in re.finditer(rx, question.lower()))
    out = []
    for _, t in reversed(hits):
        if not out or out[-1] != t:
            out.append(t)
    return out


def is_type(iri, t):
    low = iri.lower()
    return any(k in low for k in TYPES[t][1])


def stem(tokens):
    return {t[:-1] if len(t) > 3 and t.endswith("s") else t for t in tokens}


def pick_relations(scores, beam, margin):
    """Relations within margin of the best score, at most beam: 'indication' 0.58 drops 'contraindication' 0.50."""
    ranked = sorted(scores, key=scores.get, reverse=True)
    return [k for k in ranked[:beam] if scores[k] >= scores[ranked[0]] - margin]


def llm_answer(question, evidence, warnings):
    user = f"EVIDENCE:\n{evidence[:12000]}\n\n{warnings}\n\nQUESTION: {question}"
    try:
        r = post("/api/chat", {"model": LLM_MODEL, "stream": False,
                               "options": {"temperature": 0, "num_ctx": 8192, "num_predict": 400},
                               "messages": [{"role": "system", "content": SYSTEM},
                                            {"role": "user", "content": user}]})
        return r["message"]["content"].strip()
    except Exception as e:
        return f"[LLM unavailable: {e}]"


class KG:
    def __init__(self, store, min_conf=0.9, allow_unscored=False, hub_cap=500, frontier_cap=2000):
        self.store, self.min_conf, self.allow_unscored = store, min_conf, allow_unscored
        self.hub_cap, self.frontier_cap = hub_cap, frontier_cap
        self.log, self._labels, self._index, self._tokens, self._vecs = [], None, None, None, {}
        self.trace = []

    @classmethod
    def open(cls, kg, **kw):
        path = STORES / kg
        if not path.exists():
            sys.exit(f"no store at {path}; run: kg_engine.py load {kg}")
        opener = getattr(ox.Store, "read_only", None)
        return cls(opener(str(path)) if opener else ox.Store(str(path)), **kw)

    def fork(self, **settings):
        """Per-request view: shares the store, label index and embedding cache; own settings, log and trace."""
        other = copy.copy(self)
        other.log, other.trace = [], []
        for k, v in settings.items():
            setattr(other, k, v)
        return other

    def select(self, query):
        self.log.append(query)
        res = self.store.query(query)
        names = [v.value for v in res.variables]
        return [{k: (row[k].value if row[k] is not None else None) for k in names} for row in res]

    # --- 1. retrieval -------------------------------------------------------
    def labels(self):
        if self._labels is None:
            self._labels, self._index = defaultdict(list), defaultdict(set)
            for r in self.select(f"SELECT ?e ?l WHERE {{ ?e <{LABEL}> ?l FILTER(isIRI(?e)) }}"):
                self._labels[r["e"]].append(r["l"])
                self._index[norm(r["l"])].add(r["e"])
        return self._labels

    def link(self, question, max_iris=25):
        """Exact label n-grams, longest first; leftover words fall back to the shortest labels containing them."""
        self.labels()
        toks, found = norm(question).split(), []
        taken = [False] * len(toks)
        for n in range(min(8, len(toks)), 0, -1):
            for i in range(len(toks) - n + 1):
                gram = toks[i:i + n]
                phrase = " ".join(gram)
                if any(taken[i:i + n]) or all(t in STOP for t in gram) or len(phrase) < 3:
                    continue
                if any(re.fullmatch(rx, phrase) for rx, _ in TYPES.values()):
                    continue
                if phrase in self._index:
                    taken[i:i + n] = [True] * n
                    found.append((i, phrase, sorted(self._index[phrase])[:max_iris]))
        # ponytail: single-word fallback only ("tamoxifen" -> "tamoxifen citrate"); add fuzzy/embedding search if typos matter
        for i, t in enumerate(toks):
            if taken[i] or t in STOP or len(t) < 4 or t.isdigit() or any(re.fullmatch(rx, t) for rx, _ in TYPES.values()):
                continue
            keys = self.token_index().get(t)
            if keys:
                best = min(len(k.split()) for k in keys)
                taken[i] = True
                found.append((i, t, sorted({e for k in keys if len(k.split()) == best for e in self._index[k]})[:max_iris]))
        return [(p, e) for _, p, e in sorted(found)]

    def token_index(self):
        if self._tokens is None:
            self._tokens = defaultdict(set)
            for key in self._index:
                for t in set(key.split()):
                    self._tokens[t].add(key)
        return self._tokens

    def name(self, iri):
        labs = self.labels().get(iri)
        host = re.match(r"https?://([^/]+)/", iri)
        src = SOURCES.get(host.group(1), host.group(1)) if host else "?"
        return f"{labs[0] if labs else local(iri)} [{src}:{local(iri)}]"

    # --- 2. identity --------------------------------------------------------
    def _ok(self, conf):
        return self.allow_unscored if conf is None else conf >= self.min_conf

    def equivalents(self, nodes):
        """node -> {equivalent IRI: best confidence, or None if unscored}."""
        out = defaultdict(dict)
        for chunk in chunks(nodes):
            v = iris(chunk)
            # Two flat queries: one OPTIONAL joining axioms back onto plain pairs took minutes on 208k axioms.
            for r in self.select(f"""SELECT ?a ?b WHERE {{ VALUES ?a {{ {v} }}
                {{ ?a <{EQC}> ?b }} UNION {{ ?b <{EQC}> ?a }} FILTER(isIRI(?b) && ?a != ?b) }}"""):
                out[r["a"]].setdefault(r["b"], None)
            for r in self.select(f"""SELECT ?a ?b ?c WHERE {{ VALUES ?a {{ {v} }}
                {{ ?y <{ANN_S}> ?a ; <{ANN_T}> ?b }} UNION {{ ?y <{ANN_T}> ?a ; <{ANN_S}> ?b }}
                ?y <{ANN_P}> <{EQC}> ; <{CONF}> ?c FILTER(isIRI(?b) && ?a != ?b) }}"""):
                c, old = float(r["c"]), out[r["a"]].get(r["b"])
                if old is None or c > old:
                    out[r["a"]][r["b"]] = c
        return out

    def expand_identity(self, parent, nodes, depth):
        added, wave = set(), set(nodes)
        for _ in range(depth):
            nxt = set()
            for a, others in self.equivalents(wave).items():
                for b, c in others.items():
                    if b not in parent and self._ok(c):
                        parent[b] = (a, ("eq", c, True))
                        nxt.add(b)
            added |= nxt
            wave = nxt
            if not wave:
                break
        return added

    # --- 3. reasoning -------------------------------------------------------
    def neighbors(self, nodes):
        """(node, predicate, other, forward) for IRI-to-IRI edges touching nodes."""
        seen = Counter()
        skip = ", ".join(f"<{p}>" for p in SKIP)
        for chunk in chunks(nodes):
            for r in self.select(f"""SELECT ?n ?p ?m ?fwd WHERE {{
                VALUES ?n {{ {iris(chunk)} }}
                {{ ?n ?p ?m . BIND(true AS ?fwd) }} UNION {{ ?m ?p ?n . BIND(false AS ?fwd) }}
                FILTER(isIRI(?m) && ?p NOT IN ({skip})) }}"""):
                seen[r["n"]] += 1
                # ponytail: hubs keep their first hub_cap edges in index order; rank edges if hub paths matter
                if seen[r["n"]] <= self.hub_cap:
                    yield r["n"], r["p"], r["m"], r["fwd"] == "true"

    def paths(self, A, B, hops):
        """Shortest connecting paths between two seed sets, expanding the smaller side first."""
        sides = []
        for seeds in (A, B):
            parent = {s: None for s in seeds}
            sides.append([parent, set(seeds) | self.expand_identity(parent, seeds, 2)])
        used = 0
        while used < hops and not (sides[0][0].keys() & sides[1][0].keys()):
            side = min((s for s in sides if s[1]), key=lambda s: len(s[1]), default=None)
            if side is None:
                break
            parent, new = side[0], set()
            for n, p, m, fwd in self.neighbors(side[1]):
                if len(new) >= self.frontier_cap:
                    break
                if m not in parent:
                    parent[m] = (n, ("rel", p, fwd))
                    new.add(m)
            side[1] = new | self.expand_identity(parent, new, 1)
            used += 1
        out = []
        for m in sides[0][0].keys() & sides[1][0].keys():
            left = chain(sides[0][0], m)
            right = [(b, flip(e), a) for a, e, b in reversed(chain(sides[1][0], m))]
            out.append(left + right or [(m, ("same", None, True), m)])
        return sorted(out, key=path_rank)

    def pred_text(self, p):
        labs = self.labels().get(p)
        # PrimeKG encodes spaces in predicate IRIs as "_20" (off-label_20use)
        return labs[0] if labs else re.sub(r"_+", " ", re.sub(r"_20(?=[A-Za-z])", " ", local(p)))

    def embed(self, texts):
        if self._vecs is None:
            return None
        missing = [t for t in dict.fromkeys(texts) if t not in self._vecs]
        if missing:
            try:
                vecs = post("/api/embed", {"model": EMBED_MODEL, "input": missing}, 60)["embeddings"]
                self._vecs.update(zip(missing, vecs))
            except Exception:
                self._vecs = None   # Ollama unavailable: word overlap for the rest of the run
                return None
        return [self._vecs[t] for t in texts]

    def relevance(self, question, p):
        text = self.pred_text(p)
        vecs = self.embed([question, text])
        if vecs:
            q, v = vecs
            return sum(a * b for a, b in zip(q, v)) / ((sum(a * a for a in q) * sum(b * b for b in v)) ** 0.5 or 1)
        pt = stem(norm(text).split())
        return len(stem(norm(question).split()) & pt) / (len(pt) or 1)

    def explore(self, seeds, question, hops, beam=3, margin=0.05):
        """Expand from one entity; each hop follows the relations scoring within margin of the best (at most beam)."""
        parent = {s: None for s in seeds}
        frontier = set(seeds) | self.expand_identity(parent, seeds, 2)
        wanted, self.trace = hop_types(question), []
        for h in range(hops):
            edges = [e for e in self.neighbors(frontier) if e[2] not in parent]
            t = wanted[h] if h < len(wanted) else None
            edges = [e for e in edges if t and is_type(e[2], t)] or edges
            groups = defaultdict(list)
            for e in edges:
                groups[(e[1], e[3])].append(e)
            self.embed([question] + [self.pred_text(p) for p, _ in groups])
            scores = {k: self.relevance(question, k[0]) for k in groups}
            chosen = pick_relations(scores, beam, margin)
            self.trace.append((h + 1, t, [(self.pred_text(k[0]), k[1], scores[k], len(groups[k]), k in chosen)
                                          for k in sorted(scores, key=scores.get, reverse=True)[:6]]))
            new = set()
            for key in chosen:
                for n, p, m, fwd in groups[key]:
                    if m not in parent and len(new) < self.frontier_cap:
                        parent[m] = (n, ("rel", p, fwd))
                        new.add(m)
            if not new:
                break
            frontier = new | self.expand_identity(parent, new, 1)
        return [chain(parent, n) for n in frontier if parent[n] is not None]

    def group_answers(self, paths):
        """One best path per distinct answer label, most-supported answers first."""
        best, support = {}, Counter()
        for p in paths:
            key = norm(self.name(p[-1][2]).rsplit(" [", 1)[0])
            support[key] += 1
            if key not in best or path_rank(p) < path_rank(best[key]):
                best[key] = p
        return [best[k] for k, _ in support.most_common()]

    def collisions(self, nodes):
        """Nodes holding two or more distinct source IDs of one type -> (those IDs, node merge confidence)."""
        ids, conf = defaultdict(lambda: defaultdict(set)), {}
        for chunk in chunks(nodes):
            for r in self.select(f"""SELECT ?n ?id ?c WHERE {{ VALUES ?n {{ {iris(chunk)} }}
                ?n ?p ?id FILTER(isLiteral(?id) && STRENDS(STR(?p), "/sourceId"))
                OPTIONAL {{ ?n <{CONF}> ?c }} }}"""):
                if "::" in r["id"]:
                    ids[r["n"]][r["id"].split("::", 1)[0]].add(r["id"])
                if r["c"] is not None:
                    conf[r["n"]] = float(r["c"])
        return {n: (sorted(i for s in by.values() if len(s) > 1 for i in s), conf.get(n))
                for n, by in ids.items() if any(len(s) > 1 for s in by.values())}

    # --- 4. answer ----------------------------------------------------------
    def render(self, path):
        out = self.name(path[0][0])
        for _, (kind, val, fwd), b in path:
            if kind == "same":
                return out + "  (one node carries both names)"
            if kind == "eq":
                out += f" ≡{val:.2f}≡ " if val is not None else " ≡unscored≡ "
            else:
                out += f" —{self.pred_text(val)}→ " if fwd else f" ←{self.pred_text(val)}— "
            out += self.name(b)
        return out

    def ask(self, question, entities=(), hops=None, max_paths=25, llm=True):
        t0 = time.time()
        self.labels()
        if entities:
            linked = [(norm(e), sorted(self._index.get(norm(e), ()))[:25]) for e in entities]
            missing = [p for p, e in linked if not e]
            if missing:
                return {"error": f"no rdfs:label matches {missing}"}
        else:
            linked = self.link(question)
        if not linked:
            return {"error": "no phrase in the question matches an rdfs:label; pass --entity"}
        notes = []
        if len(linked) >= 2:
            # ponytail: only the first two entities are connected; chain pairs if 3-entity questions matter
            mode, hops = "path", hops or 3
            (a, A), (b, B) = (linked[0][0], set(linked[0][1])), (linked[1][0], set(linked[1][1]))
            notes = [f"NOTE: {self.name(n)} carries the labels of both '{a}' and '{b}' — a label collision, "
                     f"not evidence that they are the same" for n in sorted(A & B)]
            if A - B and B - A:          # search between the unambiguous nodes; keep the shared one only if nothing else is left
                A, B = A - B, B - A
            paths = self.paths(A, B, hops)
        else:
            mode, hops = "explore", hops or max(1, min(3, len(hop_types(question))))
            paths = self.group_answers(self.explore(linked[0][1], question, hops))
        paths = paths[:max_paths]
        nodes = {x for p in paths for a, _, b in p for x in (a, b)} | {i for _, e in linked for i in e}
        evidence = "\n".join(f"[P{i}] {self.render(p)}" for i, p in enumerate(paths, 1))
        warnings = "\n".join(notes + [
            f"WARNING: {self.name(n)} fuses distinct source records {', '.join(ids)}"
            + (f" (node merge confidence {c:.2f})" if c is not None else "")
            for n, (ids, c) in self.collisions(nodes).items()])
        return {"question": question, "mode": mode, "hops": hops,
                "linked": [(p, [self.name(i) for i in e]) for p, e in linked],
                "n_paths": len(paths), "evidence": evidence, "warnings": warnings,
                "paths": [[{"from": self.name(x), "to": self.name(y), "kind": k, "fwd": f,
                            "rel": self.pred_text(v) if k == "rel" else None, "conf": v if k == "eq" else None}
                           for x, (k, v, f), y in p] for p in paths],
                "trace": self.trace if mode == "explore" else [],
                "answer": llm_answer(question, evidence, warnings) if llm and paths else None,
                "n_queries": len(self.log), "secs": round(time.time() - t0, 1)}


FIXTURE = """
@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .
@prefix owl:  <http://www.w3.org/2002/07/owl#> .
@prefix pkg:  <http://pharma-kg.local/vocab#> .
@prefix hr:   <http://hetionet.org/relation/> .
@prefix kr:   <http://primekg.org/relation/> .
<http://hetionet.org/entity/Gene/675> rdfs:label "BRCA2" ; hr:sourceId "Gene::675" ;
    hr:associates <http://hetionet.org/entity/Disease/D1> .
<http://hetionet.org/entity/Gene/5888> rdfs:label "RAD51" .
<http://hetionet.org/entity/Disease/D1> rdfs:label "breast cancer" ;
    owl:equivalentClass <http://primekg.org/entity/disease/9> .
<http://primekg.org/entity/disease/9> rdfs:label "breast carcinoma" .
<http://primekg.org/entity/drug/7> rdfs:label "tamoxifen" ; kr:indication <http://primekg.org/entity/disease/9> .
[] a owl:Axiom ; owl:annotatedSource <http://hetionet.org/entity/Disease/D1> ;
   owl:annotatedProperty owl:equivalentClass ; owl:annotatedTarget <http://primekg.org/entity/disease/9> ;
   pkg:confidenceScore 0.93 .
<http://dbpedia.org/resource/BRCA2> rdfs:label "BRCA2", "RAD51" ; hr:sourceId "Gene::675", "Gene::5888" ;
    pkg:confidenceScore 0.88 .
<http://primekg.org/entity/drug/8> rdfs:label "Letrozole Hydrochloride" .
<http://primekg.org/entity/phenotype/5> rdfs:label "letrozole-induced arthralgia" .
"""


def selfcheck():
    store = ox.Store()
    store.load(input=FIXTURE.encode(), format=ox.RdfFormat.TURTLE)
    kg = KG(store, min_conf=0.9)
    kg._vecs = None                       # no Ollama in the self-check
    H, K, DB = "http://hetionet.org/entity/", "http://primekg.org/entity/", "http://dbpedia.org/resource/BRCA2"

    linked = kg.link("How is BRCA2 related to tamoxifen?")
    assert [p for p, _ in linked] == ["brca2", "tamoxifen"], linked
    assert set(linked[0][1]) == {H + "Gene/675", DB}, linked
    assert kg.link("What does letrozole target?") == [("letrozole", [K + "drug/8"])], kg.link("What does letrozole target?")
    assert kg.equivalents([H + "Disease/D1"])[H + "Disease/D1"] == {K + "disease/9": 0.93}

    p = kg.paths([H + "Gene/675"], [K + "drug/7"], hops=3)
    assert p and [e[0] for _, e, _ in p[0]] == ["rel", "eq", "rel"], p
    assert "≡0.93≡" in kg.render(p[0]), kg.render(p[0])
    assert not KG(store, min_conf=0.95).paths([H + "Gene/675"], [K + "drug/7"], hops=3)

    same = kg.paths(kg._index["brca2"], kg._index["rad51"], hops=1)
    assert same and same[0][0][1][0] == "same", same
    assert kg.collisions([DB]) == {DB: (["Gene::5888", "Gene::675"], 0.88)}, kg.collisions([DB])
    r = kg.ask("Are BRCA2 and RAD51 the same gene?", llm=False)
    assert "label collision" in r["warnings"] and "fuses distinct source records" in r["warnings"], r
    r = kg.ask("How is BRCA2 related to tamoxifen?", llm=False)
    assert [s["kind"] for s in r["paths"][0]] == ["rel", "eq", "rel"] and r["paths"][0][1]["conf"] == 0.93, r["paths"]
    f = kg.fork(min_conf=0.95)
    assert f.log == [] and f._index is kg._index and kg.min_conf == 0.9 and f.min_conf == 0.95

    q = "Which drugs are indicated for diseases associated with BRCA2?"
    assert hop_types(q) == ["disease", "drug"]
    assert pick_relations({"a": 0.58, "b": 0.50, "c": 0.575}, 3, 0.05) == ["a", "c"]
    assert kg.pred_text("http://primekg.org/relation/off-label_20use") == "off-label use"
    assert kg.pred_text("http://purl.obolibrary.org/obo/RO_0002606") == "RO 0002606"
    answers = kg.group_answers(kg.explore([H + "Gene/675"], q, hops=2))
    assert answers and answers[0][-1][2] == K + "drug/7", answers
    print("selfcheck ok")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("load", help="bulk-load an OWL file into stores/<kg>")
    s.add_argument("kg", choices=OWL_FILES)
    s.add_argument("owl", nargs="?")
    s.add_argument("--replace", action="store_true", help="delete the existing store and load again")
    s = sub.add_parser("sparql", help="run a raw SPARQL query")
    s.add_argument("kg", choices=OWL_FILES)
    s.add_argument("query")
    s = sub.add_parser("ask", help="answer a question with multi-hop retrieval")
    s.add_argument("kg", choices=OWL_FILES)
    s.add_argument("question")
    s.add_argument("--entity", action="append", default=[], help="entity name to use instead of linking (repeatable)")
    s.add_argument("--hops", type=int, help="max relation hops (default: 3 for paths, type words for explore)")
    s.add_argument("--min-conf", type=float, default=0.9, help="min confidence for identity links (default 0.9)")
    s.add_argument("--allow-unscored", action="store_true", help="also follow identity links without a score")
    s.add_argument("--max-paths", type=int, default=25)
    s.add_argument("--no-llm", action="store_true", help="print evidence only")
    s.add_argument("--show-sparql", action="store_true")
    sub.add_parser("selfcheck")
    a = ap.parse_args()

    if a.cmd == "selfcheck":
        return selfcheck()
    if a.cmd == "load":
        path = STORES / a.kg
        if path.exists() and a.replace:
            shutil.rmtree(path)
        path.mkdir(parents=True, exist_ok=True)
        store, t = ox.Store(str(path)), time.time()
        if len(store):
            # Reloading would duplicate every blank-node owl:Axiom (208,688 in simple) instead of skipping it.
            return print(f"{path} already holds {len(store):,} triples; nothing loaded (use --replace to rebuild)")
        store.bulk_load(path=str(a.owl or OWL_FILES[a.kg]), format=ox.RdfFormat.RDF_XML)
        store.flush()
        return print(f"loaded {len(store):,} triples into {path} in {time.time() - t:.0f}s")

    kg = KG.open(a.kg, **({"min_conf": a.min_conf, "allow_unscored": a.allow_unscored} if a.cmd == "ask" else {}))
    if a.cmd == "sparql":
        res = kg.store.query(a.query)
        if isinstance(res, bool):
            return print(res)
        if hasattr(res, "variables"):
            names = [v.value for v in res.variables]
            print("\t".join(names))
            for row in res:
                print("\t".join(str(row[n].value) if row[n] is not None else "" for n in names))
            return
        for triple in res:
            print(triple)
        return

    r = kg.ask(a.question, a.entity, a.hops, a.max_paths, llm=not a.no_llm)
    if "error" in r:
        sys.exit(r["error"])
    scored = f"conf ≥ {a.min_conf}" + (" or unscored" if a.allow_unscored else "")
    print(f"KG: {a.kg}   mode: {r['mode']}   hops ≤ {r['hops']}   identity links: {scored}")
    for phrase, names in r["linked"]:
        print(f"  linked '{phrase}' → {len(names)} node(s): {'; '.join(names[:4])}{' …' if len(names) > 4 else ''}")
    if r["trace"]:
        print("\nREASONING (relations considered per hop, * = followed)")
        for hop, t, rows in r["trace"]:
            print(f"  hop {hop}" + (f", looking for: {t}" if t else ""))
            for text, fwd, score, n, chosen in rows:
                print(f"    {'*' if chosen else ' '} {score:.3f}  {'' if fwd else '(inverse) '}{text}  [{n} edges]")
    print(f"\nEVIDENCE ({r['n_paths']} paths)\n{r['evidence'] or '(no paths found)'}")
    if r["warnings"]:
        print(f"\n{r['warnings']}")
    if r["answer"]:
        print(f"\nANSWER ({LLM_MODEL})\n{r['answer']}")
    print(f"\n{r['n_queries']} SPARQL queries, {r['secs']}s")
    if a.show_sparql:
        for q in kg.log:
            print("\n---\n" + q.strip())


if __name__ == "__main__":
    main()
