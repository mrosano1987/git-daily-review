# Local RAG service (optional)

A tiny, self-contained RAG service that enriches each daily review with your
own project documentation. When `rag_url` is set in `config/config.yaml`, the
review engine calls this service per commit and injects the most relevant
documentation chunks into the reviewer prompt — so findings are judged against
how your project is *actually* supposed to work, not just the knowledge base.

It is **optional**: with no `rag_url`, the engine works exactly as before.

## Design

- **Stdlib only.** No web framework, no vector DB, no `pip install`. Embeddings
  come from your local Ollama; vectors live in a single SQLite file.
- **Source-agnostic.** The service just indexes documents. Where they come from
  (Confluence, Markdown files, a wiki export…) is the ingestion job's problem.
- **Local & private.** Nothing leaves the machine. The index may contain
  client-confidential docs, so `rag/.data/` and `rag/docs*.json` are gitignored
  and must never be committed or exposed off localhost.

```
[docs source] --ingest--> POST /index --embed(Ollama bge-m3)--> SQLite store
review engine --per commit--> POST /retrieve-context --> top-k chunks --> reviewer prompt
```

## Endpoints

| Method | Path                | Body / result |
|--------|---------------------|---------------|
| GET    | `/health`           | `{status, model, chunks, sources}` |
| POST   | `/index`            | `{documents:[{source,title,url,content}]}` → embeds & upserts (idempotent per `source`) |
| POST   | `/retrieve-context` | `{repo,branch,changed_files,diff,query}` → `{contexts,dependencies,risk_score,summary}` (the contract in `scripts/rag_client.py`) |

## Run

```bash
cd rag
python3 service.py                 # http://127.0.0.1:8000
# overrides: RAG_PORT, RAG_HOST, RAG_EMBED_MODEL, RAG_OLLAMA_URL,
#            RAG_TOP_K, RAG_MIN_SCORE, RAG_DATA_DIR
```

Pull the embedding model once: `ollama pull bge-m3` (multilingual; good for
Italian + technical text).

To keep it always-on (needed for the 17:00 review), load the launchd agent in
`routine/com.gitdailyreview.rag.plist` (see that file's header).

## Wire it into the review

Add to `config/config.yaml`:

```yaml
rag_url: "http://localhost:8000"
```

## Ingestion

Indexing is decoupled from the service. The shipped path is the
`confluence-rag-ingest` scheduled task: a Claude session pulls the target
Confluence space via the Atlassian connector, writes `rag/docs.local.json`,
and runs:

```bash
python3 rag/index_docs.py rag/docs.local.json
```

You can index any documents the same way — just produce a JSON list of
`{source,title,url,content}` and feed it to `index_docs.py`.
