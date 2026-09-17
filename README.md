# Simple Merge vs. Full Merge — a GraphRAG-level comparison

Applies a hierarchical GraphRAG retriever (**WeamRAG**) to two OWL subgraphs that contain
*the same biomedical knowledge* integrated under two different ontology-merge strategies,
and measures whether the choice of strategy changes what a RAG system can actually answer.

Merge-strategy definitions follow Osman, Ben Yahia & Diallo, *"Ontology integration:
approaches and challenging issues"*, **Information Fusion** 71 (2021) 38–63 —
DOI [`10.1016/j.inffus.2021.01.007`](https://doi.org/10.1016/j.inffus.2021.01.007)
([open access](https://hal.science/hal-03136348v1/document)).

> Note: the DOI `10.1016/j.inffus.2021.01.001` sometimes cited for this paper is wrong —
> it resolves to a different article.

---

## The two strategies

| | Simple Merge | Full Merge |
|---|---|---|
| Paper's formula | **O₃ = O₁ ∪ O₂ ∪ O_A** | **O₃ = O₁ ∪ O₂** |
| Equivalent entities | kept separate, linked by `owl:equivalentClass` | collapsed into one entity |
| Linkage (paper's Table 2) | *weak* | *strong* |
| Also called | Reduced Semantics, Simple Union, bridge ontology | Complete Merge, Symmetric Merge |

The paper's construction for Full Merge is what this data does, exactly:

> "authors identify the merged entities by … the name of the entity that belongs to the
> preferred input ontology; then, they add the short names of the original entities
> (that have been merged) as **additional labels** to the newly merged entity."

e.g. `UBERON_0000053` keeps the DRKG IRI and absorbs "macula" (from `UBERON_0000054`) as an extra label.

---

## Measured on this data

| Metric | Simple Merge | Full Merge |
|---|---:|---:|
| File size | 954 MB | 659 MB |
| Entities | 268,654 | **97,581** |
| Edges | 3,375,438 | 2,531,908 |
| `owl:equivalentClass` correspondences | 208,688 | **0** |
| `confidenceScore` annotations | 208,689 | 183,058 |
| Classes with ≥1 parent | 62,807 | 52,591 |
| `rdfs:subClassOf` edges | 98,986 | 80,942 |
| Multi-parent classes | 22,924 (36.5%) | 18,499 (35.2%) |
| Max parents on one class | 15 | 17 |

**Entity collapse: 171,073 entities removed (63.7%).**

The paper predicts the reduction should ideally equal the number of equivalence
correspondences (208,688). The actual reduction is *smaller* — **37,615 correspondences are
redundant**, i.e. they link entities already in the same merge group (chains and cycles
within connected components of the alignment graph).

**Provenance loss.** Full Merge drops all 208,688 equivalence axioms but keeps only
183,058 confidence annotations — **25,630 merge decisions retain no trace** of why they
were made, and none of them can be undone.

**Multiple inheritance is not caused by the merge here.** The paper warns that full merge
generates "multiple root is-a paths." Both graphs sit at ~36%, so the multiple inheritance
is inherited from the OBO source ontologies rather than introduced by merging — though the
maximum parent count does rise (15 → 17). Reported as measured, not as the paper predicts.

---

## Why this matters for GraphRAG specifically

The paper's central claim is that the two strategies are **semantically equivalent**:

> "performing a full merge or a simple merge is exactly the same from a semantic point of
> view. In other terms, if one leads to unsatisfiable entities or an inconsistency, then
> the other will do so."

That holds *given a correct alignment*. This data violates the premise: a set of merges
collapse entities that are genuinely distinct — e.g. **Atracurium/Cisatracurium**,
**BRCA2/RAD51**, **HLA-A/HLA-E**, **LGALS2/LGALS3** — via an unscored DBpedia join with no
corresponding confidence-scored correspondence. Those collapses are irreversible in Full
Merge and merely *asserted* (and therefore auditable and repairable) in Simple Merge.

For retrieval this is not cosmetic. In matched 3,000-node samples:

| | Simple Merge | Full Merge |
|---|---:|---:|
| Sampled entities | 3,000 | 3,000 |
| Sampled relations | 38,244 | **114,164** |
| Entities carrying >1 label | 0 | **2,205 (73.5%)** |

Same node budget, **3× the edges** — merged nodes absorb every constituent's relations.
A retriever pulling a fixed neighbourhood therefore gets a denser but less discriminative
context, and a question like *"is BRCA2 the same gene as RAD51?"* has no distinguishing
evidence left in the graph.

---

## Pipeline

WeamRAG has no OWL/RDF support; its ingestion boundary is `entity.jsonl` + `relation.jsonl`.

```
Subgraph_*.owl  ──owl_extract.py──▶  extract/*.tsv.gz
                                          │
                                          ├── build_weamrag_input.py ──▶ entity.jsonl + relation.jsonl
                                          │                                      │
                                          │                            build_graph.py (WeamRAG)
                                          │                                      │
                                          └── compare_merge_rag.py ◀── milvus + sqlite + graphml
```

### One non-obvious detail

WeamRAG's `get_common_rag_res` **keys entities by `entity_name` and silently merges
duplicates**. Using bare labels as `entity_name` would make WeamRAG itself Full-Merge the
Simple-Merge graph and destroy the very thing being measured. Identity is therefore encoded
as `label [namespace:local_id]`, which preserves each strategy's decisions:

- Simple Merge → `macula lutea [hetionet:UBERON_0000053]` and `macula [optimuskg:UBERON_0000054]` stay distinct
- Full Merge → one `macula lutea [drkg:UBERON_0000053]` node carrying both labels

### Reproduce

```bash
python3 owl_extract.py                      # OWL → TSV  (~50s for both files)
python3 build_weamrag_input.py 3000 weamrag_in

cd /path/to/VATRAG                          # WeamRAG checkout
.venv/bin/python build_graph.py -p .../weamrag_in/simple_merge \
    -n 4 --port 11434 --model llama3.1:8b-4k --cluster-size 30
# repeat for full_merge

python3 compare_merge_rag.py                # identical probes against both indices
```

Runs fully locally: Ollama (`llama3.1:8b-4k` + `nomic-embed-text`), Milvus Lite, SQLite.
No API keys.

---

## Status

- [x] OWL streaming extractor (validated 428/428 axioms on a held-out slice)
- [x] Structural comparison of both merge variants
- [x] Matched-sample WeamRAG input generation
- [x] Simple-Merge index built (3,000 nodes)
- [x] Full-Merge index built (3,057 nodes / 3,977 aggregation edges)
- [x] Index-level comparison — see Results
- [ ] QA-level probes — inconclusive as run, see Results

## Known limitation

`rdfs:subClassOf` is **not** in the extract — the parser captures only `<p:*>` predicates.
WeamRAG builds its own hierarchy by GMM clustering, so this is not fatal, but feeding the
real OWL taxonomy into its `parent`/`level` columns is the obvious next step. That path
needs a DAG→tree reduction first: `BinaryLiftingLCA` assumes a strict tree, and ~36% of
classes here have multiple parents.

## Files

| File | Purpose |
|---|---|
| `owl_extract.py` | Streams large OWL/RDF-XML → entities, edges, alignments TSV |
| `build_weamrag_input.py` | Matched subgraph sampling → WeamRAG `entity.jsonl`/`relation.jsonl` |
| `compare_merge_rag.py` | Identical retrieval probes against both indices |

The two `.owl` inputs (954 MB / 659 MB) are gitignored — both exceed GitHub's 100 MB limit.


---

## Results

### Full Merge collapses entities that are not equivalent

Every one of these is a single node in Full Merge and two-or-more separate nodes in
Simple Merge. All of them are chemically or genetically **distinct**:

| Full Merge node | Absorbed entities | Source IDs | Actually? |
|---|---|---|---|
| `Cisatracurium Besylate [dbpedia:Atracurium_besilate]` | Atracurium + Cisatracurium | `DB00732` + `DB00565` | cisatracurium is *one of ten* stereoisomers of atracurium |
| `RAD51 [dbpedia:BRCA2]` | BRCA2 + RAD51 | `Gene::675` + `Gene::5888` | different genes; they *interact*, they are not the same |
| `Estrone [dbpedia:Estropipate]` | Estrone + Estropipate | `DB00655` + `DB04574` | estropipate is a piperazine sulfate salt, a different compound |
| `LGALS2 [dbpedia:Galectin-3]` | LGALS2 + LGALS3 | `Gene::3957` + `Gene::3958` | different genes |
| `HLA-A [dbpedia:HLA-A]` | HLA-A + HLA-E + HLA-H + HLA-J | `Gene::3105`, `3133`, `3136` | different loci; HLA-H/J are pseudogenes |

The same region in Simple Merge, with identity intact:

```
Cisatracurium Besylate [hetionet:DB00565]   src: Compound::DB00565
Atracurium             [hetionet:DB00732]   src: Compound::DB00732
HLA-A                  [hetionet:3105]      src: Gene::3105
HLA-E                  [hetionet:3133]      src: Gene::3133
LGALS2                 [hetionet:3957]      src: Gene::3957
LGALS3                 [hetionet:3958]      src: Gene::3958
```

**Every false merge routes through a `dbpedia:` IRI.** The Hetionet/DRKG-native entities merge
correctly; the damage enters through the DBpedia join, which follows `sameAs`/redirect links
that conflate *related* biomedical entities with *identical* ones. This is why these collapses
carry no `confidenceScore` — they were never scored correspondences in the first place.

### The merged entity's chosen name is unstable

`RAD51 [dbpedia:BRCA2]` and `LGALS2 [dbpedia:Galectin-3]` carry a label from one source and an
IRI from another. The paper prescribes naming the merged entity after "the entity that belongs
to the preferred input ontology", but with no preference order encoded in the data, the surviving
label is effectively arbitrary — so the merged node is not reliably findable under *either*
original name.

### Why this breaks the paper's equivalence claim

The paper argues simple and full merge are semantically equivalent, so full merge's only gain is
a smaller entity count. That holds **given a correct alignment**. Here the alignment is wrong, and
the two strategies diverge sharply in consequence:

- **Simple Merge** records the bad equivalence as an *axiom* — inspectable, scoreable, removable.
- **Full Merge** has already destroyed the evidence: 208,688 equivalence axioms are gone, and
  25,630 merge decisions retain no `confidenceScore` at all. A wrong merge is unrecoverable.

For retrieval this is not cosmetic: a query about BRCA2 in Full Merge cannot return anything that
distinguishes it from RAD51, because no such distinction survives in the graph.

### QA-level probes: inconclusive as run

`compare_merge_rag.py` ran six natural-language probes against both indices. `dp_beam_retrieve`
returned 39–57 nodes out of ~3,000 and **failed to surface the target entities in 11 of 12 cases** —
the beam seeds on community summaries rather than leaf entities, so the probes mostly retrieved
aggregation nodes. The one hit (`LGALS2 [dbpedia:Galectin-3]` in Full Merge) is consistent with
the table above.

This is a limitation of the probe design, not evidence that the merges do not matter — the
index-level comparison above is direct. Making the QA comparison meaningful needs entity-anchored
seeding rather than free-text questions. Raw output: `merge_rag_comparison.json`.

---

## Multi-hop SPARQL engine (`kg_engine.py`)

Answers questions directly over the OWL files. Every graph access is a SPARQL query against an
embedded, on-disk [Oxigraph](https://github.com/oxigraph/oxigraph) store; a local LLM only writes
the final answer from numbered evidence paths.

```bash
python3 -m venv .venv && .venv/bin/pip install pyoxigraph==0.5.11
.venv/bin/python kg_engine.py load simple        # 5,343,202 triples, 37 s, 421 MB
.venv/bin/python kg_engine.py load full          # 3,065,773 triples, 24 s, 238 MB
.venv/bin/python kg_engine.py selfcheck

.venv/bin/python kg_engine.py ask simple "How is BRCA2 related to tamoxifen?"
.venv/bin/python kg_engine.py ask simple "Which drugs are indicated for diseases associated with BRCA2?" --no-llm
.venv/bin/python kg_engine.py sparql full 'SELECT (COUNT(*) AS ?n) WHERE { ?s ?p ?o }'
```

**How a question is answered**

1. **Retrieval.** Phrases in the question are matched to `rdfs:label`s, longest first; a leftover
   word falls back to the shortest labels containing it ("tamoxifen" → "tamoxifen citrate").
2. **Identity.** `owl:equivalentClass` links, plain or reified with `pkg:confidenceScore`, are
   followed only above `--min-conf` (default 0.9; `--allow-unscored` to include unscored links).
   Simple Merge needs these to cross sources; Full Merge has none.
3. **Reasoning.** Two entities → bidirectional shortest-path search (default ≤ 3 relation hops).
   One entity → hop-by-hop expansion: type words in the question pick the target type per hop
   ("drugs … diseases … BRCA2" → disease, then drug), and each hop follows the relations whose
   embedding similarity to the question is within 0.05 of the best. The choices are printed.
4. **Answer.** Llama 3.1 8B answers from the evidence only and cites paths like `[P2]`.

**Safety checks printed with every answer**

- `WARNING` — a node holds two IDs from the same source database (a fused entity, e.g. BRCA2 + RAD51).
- `NOTE` — both named entities resolve to one node by label alone; that node is excluded from the
  search when other candidates exist.

**What it shows on this data**

| Question | Simple Merge | Full Merge |
|---|---|---|
| Are BRCA2 and RAD51 the same gene? | `BRCA2 —protein protein→ RAD51` (they interact); NOTE that `dbpedia:RAD51` is labelled with both names | only answer is one fused node; NOTE + WARNING `Gene::5888, Gene::675` |
| How is BRCA2 related to tamoxifen? | BRCA2 ←associated with— ovarian cancer (PrimeKG) ≡0.95≡ DOID ≡0.95≡ MONDO ←INDICATION— tamoxifen citrate | shorter paths, but every one starts from the fused BRCA2/RAD51 node (WARNING) |

Typical time is 3–6 s without the LLM; the answer adds ~35 s on a shared GPU.

**Known limits:** only the first two entities in a question are connected; partial linking is
single-word; target types come from IRI substrings; hubs are capped at 500 edges per node; a seed
with a polluted label (e.g. `dbpedia:RAD51` labelled "BRCA2") is still used in one-entity questions.
