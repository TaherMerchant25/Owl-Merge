# Owl-Merge

Does the way you merge biomedical ontologies change what an AI system can correctly answer?

This project takes one pharmacological knowledge graph integrated two ways, **Simple Merge** and
**Full Merge**, and compares them:

1. structurally, by streaming the raw OWL files;
2. through a GraphRAG framework (WeamRAG) and a question-answering test;
3. through a **multi-hop SPARQL question-answering engine** with a **web app**, where you can ask
   the same question of both graphs side by side.

A two-page write-up is in [`docs/Owl_Merge_Report.pdf`](docs/Owl_Merge_Report.pdf)
(LaTeX source: [`docs/Owl_Merge_Report.tex`](docs/Owl_Merge_Report.tex)).

---

## Quick start: ask the graphs a question

Requirements: Python 3.10+, [Ollama](https://ollama.com) running locally, and the two OWL files
(`Subgraph_Simple-Merge.owl`, `Subgraph_Full-Merge.owl`) in the project folder. They are not in
the repository because each exceeds GitHub's 100 MB limit.

```bash
python3 -m venv .venv
.venv/bin/pip install pyoxigraph==0.5.11
ollama pull llama3.1:8b
ollama pull nomic-embed-text

# One-time: load both OWL files into on-disk triple stores (about 1 minute)
.venv/bin/python kg_engine.py load simple        # 5,343,202 triples, 421 MB
.venv/bin/python kg_engine.py load full          # 3,065,773 triples, 238 MB

.venv/bin/python kg_engine.py selfcheck
.venv/bin/python web_app.py selfcheck
```

**Web app**

```bash
.venv/bin/python web_app.py                      # http://127.0.0.1:8780
.venv/bin/python web_app.py --port 8790          # if 8780 is taken
```

Type a question, choose Simple Merge, Full Merge or both side by side, and get the answer, the
evidence paths it cites, the search steps, warnings about fused entities, and every SPARQL query
that ran. The server listens on localhost only. On a remote machine, forward the port (for example
in the VS Code **Ports** panel, or `ssh -N -L 8780:127.0.0.1:8780 user@host`).

**Command line**

```bash
.venv/bin/python kg_engine.py ask simple "How is BRCA2 related to tamoxifen?"
.venv/bin/python kg_engine.py ask full "Are BRCA2 and RAD51 the same gene?" --no-llm
.venv/bin/python kg_engine.py ask simple "Which drugs are indicated for diseases associated with BRCA2?" --show-sparql
.venv/bin/python kg_engine.py sparql full 'SELECT (COUNT(*) AS ?n) WHERE { ?s ?p ?o }'
```

| Option | Effect |
|---|---|
| `--no-llm` | evidence only, much faster |
| `--min-conf 0.8` | lowest identity-link confidence to follow (default 0.9) |
| `--allow-unscored` | also follow identity links that have no confidence score |
| `--hops 2` | number of relation steps |
| `--entity BRCA2` | use this entity name instead of matching names in the question |
| `--show-sparql` | print every SPARQL query |

`load` refuses to load into a store that already has data, because reloading would duplicate the
blank-node alignment axioms. Use `--replace` to rebuild a store.

---

## The two merge strategies

Definitions follow Osman, Ben Yahia & Diallo, *Ontology integration: approaches and challenging
issues*, Information Fusion (2021),
DOI [10.1016/j.inffus.2021.01.007](https://doi.org/10.1016/j.inffus.2021.01.007)
([open access](https://hal.science/hal-03136348v1/document)).

| | Simple Merge | Full Merge |
|---|---|---|
| Formula | O₃ = O₁ ∪ O₂ ∪ O_A | O₃ = O₁ ∪ O₂ |
| Matching entities | kept separate, linked by `owl:equivalentClass` with a confidence score | fused into one entity that keeps one IRI and adds the other names as labels |
| Link strength (paper's term) | weak | strong |

The paper argues the two are semantically equivalent. That holds when both apply the same, correct
alignment. This project looks at what happens with a real, imperfect one.

---

## How the SPARQL engine answers a question

Both OWL files are bulk-loaded into embedded [Oxigraph](https://github.com/oxigraph/oxigraph)
stores. Every graph access is a SPARQL 1.1 query; the LLM only writes the final answer.

1. **Retrieval.** Phrases in the question are matched to `rdfs:label`s, longest first. A leftover
   word falls back to the shortest labels containing it ("tamoxifen" → "tamoxifen citrate").
2. **Identity.** `owl:equivalentClass` links, plain or reified as `owl:Axiom` with
   `pkg:confidenceScore`, are followed only above the confidence threshold. This is how Simple
   Merge crosses databases; Full Merge has no such links.
3. **Reasoning.**
   - Two entities: bidirectional shortest-path search, up to 3 relation steps by default.
   - One entity: step-by-step expansion. Type words in the question set what each step looks for
     ("drugs … diseases … BRCA2" → diseases, then drugs), and each step follows the relations
     closest in meaning to the question, by embedding similarity (e.g. *indication* 0.58 followed,
     *contraindication* 0.50 skipped).
4. **Answer.** Llama 3.1 8B answers only from the numbered evidence paths and cites them (`[P1]`).

Every answer also carries safety checks:

- **WARNING**: a node holds two IDs from the same source database, i.e. a fused entity such as
  BRCA2 + RAD51.
- **NOTE**: both entities named in the question resolve to one node by label alone. That is a
  naming overlap, not evidence they are the same, and the node is left out of the search when
  other candidates exist.

---

## Findings

### Structure

| Measure | Simple Merge | Full Merge |
|---|---:|---:|
| File size | 954 MB | 659 MB |
| Declared entities | 268,654 | 97,581 |
| Edges | 3,375,438 | 2,531,908 |
| `owl:equivalentClass` links | 246,934 (208,688 scored, 38,246 unscored) | 0 |
| `rdfs:subClassOf` edges | 98,986 | 80,942 |
| Classes with multiple parents | 22,924 (36.5%) | 18,499 (35.2%) |
| Entities holding ≥ 2 IDs from the same source database | **0** | **96** |

- Full Merge has 63.7% fewer declared entities.
- Multiple inheritance comes from the source ontologies, not the merge: both files sit near 36%.
- Both files contain many edges that point to IRIs they never declare (63.0% of Simple Merge edges,
  52.8% of Full Merge edges), so entity counts cover declared entities only.

### Full Merge fuses entities that are different

A fused node should combine one real-world thing from *different* databases, never two IDs from the
*same* database. Full Merge has 96 such nodes (50 genes, 27 molecular functions, 5 anatomy,
5 pathways, 4 compounds, 4 biological processes, 1 cellular component). Seven were checked by hand
and all seven are real errors:

| Fused in Full Merge | Source IDs | Why they are different |
|---|---|---|
| atracurium + cisatracurium | DrugBank DB00732 + DB00565 | cisatracurium is one of atracurium's ten stereoisomers, sold as a separate drug |
| estrone + estropipate | DrugBank DB00655 + DB04574 | estropipate is estrone sulfate stabilised with piperazine |
| BRCA2 + RAD51 | Gene 675 + 5888 | separate genes that interact |
| LGALS2 + LGALS3 | Gene 3957 + 3958 | galectin-2 and galectin-3 |
| HLA-A + HLA-E (+ HLA-H, HLA-J) | Gene 3105 + 3133 | different HLA loci; HLA-H and HLA-J are pseudogenes |
| SYT11 + SYT12 | Gene 23208 + 91683 | synaptotagmin-11 and -12 |
| CERS1 + GDF1 | Gene 10715 + 2657 | different genes and proteins that share one transcript |

In Simple Merge each stays a separate entity with its own identifier. The other 89 still need
review; a database can occasionally carry a retired duplicate ID.

### How the errors get in

Equivalence is transitive, so a chain of individually plausible links can join two different
entities:

- **LGALS2 → LGALS3:** Hetionet LGALS2 ≡ Wikidata LGALS3 (confidence 0.871) ≡ Hetionet LGALS3 (0.940).
- **HLA-A → HLA-E:** Hetionet HLA-A ≡ DBpedia HLA-A (0.701) ≡ NCBI Gene 3137 (0.882) ≡ Hetionet HLA-E (0.883).
- **BRCA2 → RAD51:** NCBI BRCA2 ≡ `dbpedia:RAD51` (unscored) ≡ NCBI RAD51 (0.72). The DBpedia
  RAD51 entry is itself labelled "BRCA2".

The remaining pairs have not yet been traced through all identity links. The two OWL files were
also not built from exactly identical inputs (Full Merge declares some entities that Simple Merge
only references), so some differences reflect inputs rather than strategy alone.

### Effect on GraphRAG and on answers

In matched 3,000-node samples, Full Merge carries 114,164 relations against Simple Merge's 38,244,
and 73.5% of its nodes have more than one name: denser context, fewer distinctions.

In the question-answering test (`compare_merge_rag.py`, local Llama 3.1 8B, 5 fused pairs plus
2 never-fused control pairs, correct answer always "different"):

| Context given to the model | Fused pairs answered "same" | Control pairs answered "same" |
|---|---:|---:|
| none | 0 of 5 | 0 of 2 |
| Simple Merge | 1 (of the 3 pairs reachable) | 0 of 2 |
| Full Merge | **4 of 5** | 1 of 2 |

With Full Merge context the model quoted the fused node as its evidence ("RAD51 is also known as
BRCA2 … This suggests that they refer to the same gene"), even though it answers every pair
correctly without the graph. The test is small (7 pairs, one run, one 8B model that also makes
mistakes of its own), so this is suggestive rather than conclusive. Results:
`merge_rag_comparison_v3_llama.json`.

The SPARQL engine shows the same difference directly: on Simple Merge,
"Are BRCA2 and RAD51 the same gene?" returns `BRCA2 —protein protein→ RAD51`; on Full Merge it
returns only the fused node, with a WARNING.

---

## GraphRAG pipeline (WeamRAG)

WeamRAG (hierarchical GraphRAG: GMM clustering, LLM community summaries,
beam-search retrieval) reads `entity.jsonl` and `relation.jsonl`, so the OWL files are converted
first:

```
Subgraph_*.owl ──owl_extract.py──▶ extract/*.tsv.gz ──build_weamrag_input.py──▶ entity.jsonl + relation.jsonl
                                                                                        │
                                               compare_merge_rag.py ◀── build_graph.py (WeamRAG index)
```

WeamRAG silently combines entities with the same name, so entity names carry their source
(`HLA-A [hetionet:3105]`); otherwise WeamRAG would full-merge the Simple Merge graph by itself.

```bash
python3 owl_extract.py                              # OWL → TSV, about 50 s for both files
python3 build_weamrag_input.py 3000 weamrag_in      # matched 3,000-node samples

cd /path/to/weamrag                                  # WeamRAG checkout
.venv/bin/python build_graph.py -p /path/to/Owl_Merge/weamrag_in/simple_merge \
    -n 4 --port 11434 --model llama3.1:8b --cluster-size 30
# repeat for full_merge

/path/to/weamrag/.venv/bin/python compare_merge_rag.py weamrag_in merge_rag_comparison_v3.json llama3.1:8b
```

---

## Repository contents

| Path | What it is |
|---|---|
| `kg_engine.py` | multi-hop SPARQL question-answering engine (load, ask, sparql, selfcheck) |
| `web_app.py`, `web/index.html` | web app: standard-library HTTP server and a single HTML page |
| `owl_extract.py` | streams the OWL files into entity, edge and alignment tables |
| `build_weamrag_input.py` | matched subgraph sampling and conversion to WeamRAG input |
| `compare_merge_rag.py` | question-answering test against both WeamRAG indices |
| `weamrag_in/`, `weamrag_smoke/` | WeamRAG inputs and built hierarchies (3,000- and 300-node samples) |
| `merge_rag_comparison*.json` | question-answering results (v1, v2, v3) |
| `docs/Owl_Merge_Report.pdf` | two-page project report |
| `NotebookLM_Simple_vs_Full_Merge.md` | longer research brief |

Not committed: the OWL files, `extract/`, `stores/`, `.venv/`, and binary WeamRAG indices. All can
be regenerated with the commands above.

## Limitations

- Only the first two entities named in a question are connected.
- Name matching is exact or single-word, so misspellings (e.g. "metformonin") find nothing.
- Target types per step come from IRI patterns, not `rdf:type`.
- High-degree nodes are capped at 500 edges per search step.
- `owl_extract.py` does not extract `rdfs:subClassOf`; the SPARQL engine does traverse it.
- The question-answering test is small, and a larger evaluation with a stronger model is still to do.

All models run locally through Ollama; no API keys are needed.
