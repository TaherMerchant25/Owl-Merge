# Simple Merge vs. Full Merge: How Ontology Merging Changes What a GraphRAG System Can Know

*Research brief for the Owl-Merge project — github.com/TaherMerchant25/Owl-Merge*
*Status as of 10 September 2026: structural analysis complete; question-answering evaluation in progress.*

---

## 1. Summary

We compared two versions of the same biomedical knowledge graph, built with the two ontology-merging strategies defined by Osman, Ben Yahia and Diallo (2021):

- **Simple Merge** keeps matching entities from different sources as separate entities and links them with equivalence statements.
- **Full Merge** fuses matching entities into a single entity.

The paper argues that the two strategies are logically equivalent. On real data, we found they are not interchangeable.

**Main findings**

1. **Full Merge is far more compact.** It has 63.7% fewer declared entities (268,654 → 97,581) and 25% fewer edges.
2. **Part of that compaction is wrong.** 96 Full Merge entities fuse two or more *different* identifiers from the *same* source database. Simple Merge has zero such cases. We checked 7 by hand, and all 7 fuse genuinely different drugs or genes — for example BRCA2 with RAD51, and atracurium with cisatracurium.
3. **The errors come from two places.** Two of the seven come from chains of individually plausible equivalence links. The other five cannot be traced to any link in the Simple Merge file, and several of the entities involved are not even declared there — the two files were not built from the same inputs.
4. **Full Merge changes the input to GraphRAG.** A 3,000-node sample of Full Merge carries about three times as many relations as the same-size Simple Merge sample, and 73.5% of its nodes carry more than one name.
5. **Our first question-answering test failed for technical reasons.** The retriever almost never reached the entities being asked about. A corrected test is in progress.

---

## 2. Background

### 2.1 Ontologies, IRIs and alignments

An **ontology** is a formal, machine-readable vocabulary: a set of entities (genes, drugs, diseases, anatomical parts), their categories, and the relations between them. Our files use **OWL**, the standard ontology language of the Semantic Web. Every entity has a unique web-style identifier called an **IRI**, such as `http://hetionet.org/entity/Gene/675`.

Biomedical knowledge is spread across many databases. This graph combines Hetionet, DRKG, PrimeKG, OptimusKG, OBO Foundry ontologies (such as UBERON, MONDO, ChEBI and GO), DBpedia, NCBI Gene, and Wikidata and DrugBank records. The same real-world thing — say, the BRCA2 gene — appears in several of these databases under a different IRI in each.

An **alignment** is a list of correspondences stating that entity A in one source is the same as entity B in another. Each correspondence usually carries a **confidence score** between 0 and 1. In OWL, such a correspondence is written as an `owl:equivalentClass` statement.

### 2.2 GraphRAG

**Retrieval-augmented generation (RAG)** gives a language model relevant facts before it answers a question, so the answer is grounded in data rather than memory. **GraphRAG** retrieves those facts from a knowledge graph instead of plain text. It finds the entities that match the question, walks to connected facts, and passes them to the model as context.

This makes GraphRAG directly sensitive to how entities are defined. If two different genes share one node, the retriever cannot hand the model anything that tells them apart.

### 2.3 WeamRAG, the framework used here

We used **WeamRAG** (*Path Flexible Beam Search for Hierarchical Knowledge Graph Retrieval*), a hierarchical GraphRAG framework:

- It groups entities into clusters and communities, each with a summary written by a language model.
- To answer a question, it picks **anchor entities** that match the question, then runs a beam search up and down this hierarchy to collect context.
- It runs fully locally: Llama 3.1 8B through Ollama for generation, nomic-embed-text for embeddings, Milvus Lite for vector search and SQLite for storage, on one NVIDIA TITAN RTX (24 GB).

---

## 3. The two merge strategies

Osman et al. (2021) survey ontology integration and define three approaches. Two of them are compared here.

| | Simple Merge | Full Merge |
|---|---|---|
| Formal definition | O₃ = O₁ ∪ O₂ ∪ O_A | O₃ = O₁ ∪ O₂ |
| In words | both ontologies, plus the alignment as explicit statements | both ontologies, with matching entities fused |
| Matching entities | kept separate, linked by `equivalentClass` | combined into one entity |
| Strength of link (paper's term) | weak | strong |
| Other names | Reduced Semantics, Simple Union, bridge ontology | Complete Merge, Symmetric Merge |
| Expected size | sum of the input entities | sum of the input entities minus the merged ones |

How the paper describes naming in a Full Merge:

> "authors identify the merged entities by … the name of the entity that belongs to the preferred input ontology; then, they add the short names of the original entities (that have been merged) as additional labels to the newly merged entity."

Our Full Merge file follows this pattern. A fused entity keeps one source's IRI, and the other entities' names become extra labels.

**The paper's central claim** is that the choice does not change meaning:

> "performing a full merge or a simple merge is exactly the same from a semantic point of view. In other terms, if one leads to unsatisfiable entities or an inconsistency, then the other will do so."

The paper's third approach, the **asymmetric merge** (enriching one trusted target ontology with the others), is the one it recommends overall. It was not part of this comparison.

---

## 4. Data

| File | Size | What it is |
|---|---:|---|
| `Subgraph_Simple-Merge.owl` | 954 MB | the combined graph, Simple Merge strategy |
| `Subgraph_Full-Merge.owl` | 659 MB | the combined graph, Full Merge strategy |

Both files are too large to open with standard OWL libraries, which load the whole file into memory.

---

## 5. Method

**Step 1 — Read the files.** We wrote a streaming extractor that reads each OWL file line by line and produces three tables: entities (IRI, names, source identifiers), edges, and equivalence links with confidence scores. It processes both files in about 50 seconds.

**Step 2 — Compare structure.** Counts of entities, edges, equivalence links, class hierarchy, and fused identifiers.

**Step 3 — Build matched samples.** WeamRAG summarises every cluster with a language model, so the full graphs are too large to index. We took a 3,000-node sample from each file, starting from the same seed drugs and genes and expanding outward along relations, so both samples cover the same biology.

**Step 4 — Convert for WeamRAG.** WeamRAG reads two JSON-lines files, one for entities and one for relations. One design decision was essential: WeamRAG automatically combines any two entities with the same name. If we had used plain names, WeamRAG would have fused the Simple Merge graph itself and erased the difference being measured. So every entity name carries its origin, for example `HLA-A [hetionet:3105]`.

**Step 5 — Build the indices.** Both samples were indexed with WeamRAG. The Simple Merge sample became 3,000 entities under 45 clusters and one top node; the Full Merge sample became 3,000 entities under 56 clusters and one top node. The Simple Merge build took about 1 hour 47 minutes.

**Step 6 — Ask questions.** We asked both indices the same questions about entities that Full Merge had fused (see Section 6.5).

---

## 6. Results

### 6.1 Structure

| Measure | Simple Merge | Full Merge |
|---|---:|---:|
| Declared entities | 268,654 | 97,581 |
| Edges | 3,375,438 | 2,531,908 |
| Equivalence links (`equivalentClass`) | 246,934 | 0 |
| — with a confidence score | 208,688 | — |
| — without a confidence score | 38,246 | — |
| Class-hierarchy (`subClassOf`) edges | 98,986 | 80,942 |
| Classes with more than one parent | 22,924 (36.5%) | 18,499 (35.2%) |
| Most parents on a single class | 15 | 17 |
| Entities holding ≥ 2 IDs from the same source | **0** | **96** |
| IRIs used in edges but never declared | 190,138 | 93,369 |
| Edges touching an undeclared IRI | 2,128,001 (63.0%) | 1,336,509 (52.8%) |

- Full Merge removes 171,073 entities (63.7%) and every equivalence link, as the definition requires.
- **Multiple inheritance is not caused by the merge.** The paper warns that Full Merge creates multiple "is-a" paths. Both files sit at about 36%, so this comes from the source ontologies. The maximum does rise slightly (15 → 17).
- **15% of equivalence links in Simple Merge have no confidence score** (38,246 of 246,934).
- **Both files are cut-out subgraphs with loose ends.** Many edges point to IRIs that the file never declares — no name, no identifier. Some are external cross-reference links, such as Ensembl web addresses; others are real graph entities. Simple Merge has more undeclared Hetionet entities (21,270) than Full Merge (11,223). The entity counts above cover declared entities only.

### 6.2 Full Merge fuses entities that are different

A fused entity should combine records of *one* real-world thing from *different* databases. It should never contain two different identifiers from the *same* database, because a database does not normally list one gene twice.

Full Merge has **96 entities that do**: 50 genes, 27 molecular functions, 5 anatomical entities, 5 pathways, 4 compounds, 4 biological processes and 1 cellular component. Most fuse two identifiers (81), some three (12), four (2) or five (1). Simple Merge has none.

This is a strong warning sign rather than proof for all 96, since a database can occasionally carry a retired duplicate ID. We checked seven cases by hand. **All seven fuse genuinely different entities:**

| Fused node in Full Merge | What was fused | Source IDs | Why they are different |
|---|---|---|---|
| `dbpedia:Atracurium_besilate` | atracurium + cisatracurium | DrugBank DB00732 + DB00565 | Atracurium is a mixture of ten stereoisomers; cisatracurium is one of them, sold as a separate drug with a different potency and side-effect profile |
| `dbpedia:Estropipate` | estrone + estropipate | DrugBank DB00655 + DB04574 | Estropipate is estrone sulfate stabilised with piperazine — a different compound that the body converts to estrone |
| `dbpedia:BRCA2` | BRCA2 + RAD51 | Gene 675 + 5888 | Separate genes on chromosomes 13 and 15. The BRCA2 protein loads RAD51 onto damaged DNA: they interact, they are not the same |
| `dbpedia:Galectin-3` | LGALS2 + LGALS3 | Gene 3957 + 3958 | Galectin-2 and galectin-3, separate genes on chromosomes 22 and 14 |
| `dbpedia:HLA-A` | HLA-A + HLA-E (+ HLA-H, HLA-J) | Gene 3105 + 3133 | HLA-A is a classical immune-presentation gene, HLA-E a non-classical one; HLA-H and HLA-J are pseudogenes |
| `dbpedia:SYT11` | SYT11 + SYT12 | Gene 23208 + 91683 | Synaptotagmin-11 and -12, separate genes on chromosomes 1 and 11 |
| `dbpedia:GDF1` | CERS1 + GDF1 | Gene 10715 + 2657 | Different genes and proteins that happen to share one transcript — a genuinely confusable case |

In Simple Merge, each of these stays a separate entity with its own identifier, for example `Atracurium [hetionet:DB00732]` and `Cisatracurium Besylate [hetionet:DB00565]`.

**The surviving name is arbitrary.** The fused BRCA2 node is labelled "RAD51" but has the IRI of BRCA2; the fused galectin node is labelled "LGALS2" but has the IRI of Galectin-3. A user searching for either original name cannot reliably find it.

### 6.3 How the errors got in

We traced each of the seven pairs through all 246,934 equivalence links in the Simple Merge file.

**Two errors come from chains of links.** Equivalence is transitive: if A = B and B = C, a Full Merge also treats A = C. A single wrong link, or a chain of reasonable-looking ones, can therefore fuse two different entities.

- **LGALS2 → LGALS3:** Hetionet's LGALS2 was linked to the Wikidata entry for LGALS3 with confidence **0.871**. That entry was correctly linked to Hetionet's LGALS3 (0.940). One wrong link fused two genes.
- **HLA-A → HLA-E:** Hetionet's HLA-A was linked to DBpedia's HLA-A page (**0.701**), which was linked to NCBI Gene 3137, an HLA-J record (0.882), which was linked to Hetionet's HLA-E (0.883). Three links, each above 0.7, joined two different genes.

**Five errors have no path in the Simple Merge file at all.** For atracurium/cisatracurium, estrone/estropipate, BRCA2/RAD51, SYT11/SYT12 and CERS1/GDF1, no chain of equivalence links connects the two entities in Simple Merge. The Full Merge must therefore have used links that are not recorded in the Simple Merge file.

The same pattern shows up in the entities themselves. Hetionet's BRCA2, RAD51, estrone, SYT11, SYT12 and CERS1 are never declared in the Simple Merge file — they appear only as the targets of relations — yet Full Merge declares every one of them inside its fused nodes.

This matters for the comparison itself. Osman et al. treat the two strategies as two ways of applying *one* alignment. **These two files were not built from the same alignment, and they do not declare the same entities**, so some differences between them reflect different inputs, not only different merge strategies.

### 6.4 Effect on the GraphRAG index

| Measure (3,000-node samples) | Simple Merge | Full Merge |
|---|---:|---:|
| Entities | 3,000 | 3,000 |
| Relations | 38,244 | 114,164 |
| Entities with more than one name | 0 | 2,205 (73.5%) |
| Entities holding ≥ 2 IDs from one source | 0 | at least 14 |
| Clusters built by WeamRAG | 45 | 56 |

With the same number of nodes, Full Merge gives the retriever about **three times as many relations**, because each fused node inherits the relations of everything fused into it.

- **Benefit:** one node collects every fact about a concept, which can help recall.
- **Cost:** when a fused node is wrong, a question such as "Is BRCA2 the same gene as RAD51?" has no distinguishing evidence left in the graph. The context itself says that RAD51 is "also known as" BRCA2.

### 6.5 Question-answering test: first attempt and why it failed

**What we did.** Six questions about fused pairs, asked of both indices (12 retrievals), with Llama 3.1 8B answering from the retrieved context.

**What happened.** The retriever returned 39–57 nodes per question but reached the entities being asked about in **only 1 of the 12 retrievals**. The comparison was inconclusive.

**Why.** Investigation found three separate causes:

1. **Anchor selection.** WeamRAG picks anchors by comparing the question's embedding with embeddings of each entity's name plus the start of its description. Our descriptions all began with the same template sentence ("X is a Gene in a pharmacological knowledge graph"), so short gene symbols such as BRCA2 made almost no difference to the embeddings. The retriever anchored on cluster summaries instead.
2. **Context overflow.** The model has a 4,096-token window. The retriever's default budget is 40,000 characters, and our test cut the context at 12,000 characters. For multi-entity questions WeamRAG places entity facts last, so they were most likely cut off.
3. **Sample coverage.** BRCA2, RAD51 and estrone are never declared in the Simple Merge file (Section 6.3), so they could not appear in the Simple Merge sample. The sample also expanded only along relations, never along equivalence links, so it contained none of the Simple Merge alignment — the O_A part of its definition.

**The corrected test (in progress).**

- **Entity-anchored seeding:** nodes whose name or alias matches an entity named in the question are added to WeamRAG's anchors. The rest of the retrieval pipeline is unchanged.
- Context budget reduced to 10,000 characters so the full context fits the model.
- Direct questions ("Are BRCA2 and RAD51 the same gene?") with a required final verdict of SAME, DIFFERENT or UNKNOWN. The correct answer for every pair is DIFFERENT.
- A **no-context control**: the same question without any graph context, to separate what the model already knows from what the graph tells it.
- Both the original and corrected seeding are reported side by side.
- New samples will include Simple Merge's equivalence links, each with its confidence score. Pairs whose entities Simple Merge never declares will be reported separately.

---

## 7. What this means

**The paper's equivalence claim depends on its assumptions.** Simple and Full Merge are logically equivalent *when both apply the same, correct alignment*. With an imperfect alignment — which every large automatic alignment is — they behave very differently:

- **Simple Merge keeps mistakes visible.** A wrong equivalence stays an explicit statement, often with a confidence score, that can be inspected, filtered by threshold or removed.
- **Full Merge makes mistakes permanent.** Once two entities are fused, the graph no longer records that they were ever separate, nor which link joined them.

**For GraphRAG, the trade-off is recall against precision.** Full Merge gathers related facts in one place, but it can hand the model context that asserts two different genes or drugs are the same thing. In biomedicine that is a safety problem, not a cosmetic one: atracurium and cisatracurium are dosed differently.

**Practical recommendations (ours, not the paper's):**

1. Keep the Simple Merge graph, with its equivalence links and confidence scores, as the source of truth.
2. If a fused view is needed for retrieval, derive it from Simple Merge and block any fusion that would put two IDs from the same source into one node. That single rule catches all 96 cases found here.
3. Do not apply transitivity blindly. Reject chains whose weakest link is below a chosen threshold, or that pass through encyclopaedic sources such as DBpedia.
4. Record which link caused each fusion, so it can be undone.

---

## 8. Limitations and corrections

- **Loose ends in both files.** 63.0% of Simple Merge edges and 52.8% of Full Merge edges touch an IRI the file never declares. Entity counts cover declared entities only, and the GraphRAG samples use only edges between declared entities. The class hierarchy (`subClassOf`) was counted but not passed to GraphRAG.
- **96 is a signal, not a verdict.** Seven cases were checked by hand; the other 89 still need review.
- **Not a pure A/B test.** The two files were not built from the same alignment and do not declare the same entities (Section 6.3).
- **Small scale.** 3,000-node samples, one 8-billion-parameter local model.
- **Not yet analysed.** Full Merge still carries 183,058 confidence annotations; what they are attached to has not been checked.
- **Corrections to earlier project notes.** An earlier project README stated that the false merges carried no confidence scores, and that 25,630 merge decisions had lost their provenance. The first is wrong for LGALS2/LGALS3 and HLA-A/HLA-E, and the second rested on a subtraction that does not hold. Both are being corrected.

---

## 9. Next steps

1. Run the corrected question-answering test (Section 6.5).
2. Rebuild both samples so Simple Merge keeps its equivalence links and confidence scores.
3. Add the class hierarchy to the GraphRAG index, and find out why the two files declare different entities.
4. Review all 96 same-source fusions and classify each as a true error or a duplicate ID.
5. Find out which link set the Full Merge was built from.

---

## 10. Key takeaways

- Full Merge has 63.7% fewer declared entities, but part of that shrinkage fuses different drugs and genes.
- 96 fused entities contain two or more IDs from the same database; all 7 checked by hand are real errors.
- Errors enter through chains of plausible links (weakest links 0.701 and 0.871) and through links that exist only in the Full Merge build.
- Simple Merge keeps errors visible and reversible; Full Merge makes them permanent.
- For biomedical GraphRAG, keep equivalence links and confidence scores, and never fuse two IDs from the same source.
- The question-answering evaluation is being redone after the first attempt failed for technical reasons.

---

## 11. Suggested slide outline

1. **Title** — Simple Merge vs. Full Merge: what ontology merging does to GraphRAG
2. **The problem** — biomedical knowledge is spread across many databases with different IDs for the same thing
3. **Two ways to merge** — the comparison table from Section 3, and the paper's claim that they are equivalent
4. **Our data and pipeline** — two OWL files, streaming extractor, matched samples, WeamRAG
5. **Structure at a glance** — the table from Section 6.1; Full Merge is 63.7% smaller
6. **The catch** — 96 same-source fusions in Full Merge, zero in Simple Merge
7. **Seven real examples** — the table from Section 6.2
8. **How errors get in** — the LGALS2 and HLA-A link chains; five errors with no path in Simple Merge
9. **Effect on GraphRAG** — three times the relations, 73.5% multi-name nodes; recall versus precision
10. **The first QA test and what went wrong** — 1 of 12 retrievals reached the target; three causes
11. **The corrected test** — entity anchoring, verdicts, no-context control
12. **Recommendations** — the four points from Section 7
13. **Limitations and next steps**

---

## 12. Glossary

- **Alignment** — a set of correspondences saying which entities in different sources are the same.
- **Anchor entity** — an entity the retriever starts its search from.
- **Confidence score** — a number from 0 to 1 attached to a correspondence; higher means the matching system is more certain.
- **`equivalentClass`** — the OWL statement that two classes mean the same thing.
- **GraphRAG** — retrieval-augmented generation that retrieves from a knowledge graph.
- **IRI** — the unique web-style identifier of an entity.
- **Multiple inheritance** — a class with more than one parent category.
- **OWL** — Web Ontology Language, the standard format of the ontology files.
- **Pseudogene** — a non-functional copy of a gene.
- **Stereoisomer** — molecules with the same atoms and bonds but a different 3D arrangement.
- **`subClassOf`** — the OWL statement that one class is a narrower kind of another.
- **Transitive closure** — following equivalences in chains, so A = B and B = C gives A = C.

---

## 13. Reference

Osman, I., Ben Yahia, S., & Diallo, G. (2021). *Ontology integration: approaches and challenging issues.* Information Fusion. https://doi.org/10.1016/j.inffus.2021.01.007 — open-access version: https://hal.science/hal-03136348v1/document
