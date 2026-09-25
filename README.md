# Zotero PDF Harvester

Fast, resumable bulk retrieval of **legal open-access PDFs**, followed by automatic
attachment to their parent items through Zotero's Local API.

## Why this exists

General PDF downloaders often become slow because one publisher request can hang an
entire worker pool. This project uses:

- parallel metadata resolution through Unpaywall, OpenAlex, Europe PMC, Crossref,
  and Semantic Scholar;
- short timeout per candidate URL;
- immediate Zotero attachment after each successful download;
- disk caching and safe reruns;
- an optional `fetchpdf` fallback launched once per DOI under a real OS-level timeout.

It never uses Sci-Hub or attempts to bypass a paywall. A missing result can mean that
no legal public PDF exists. If you have institutional access, add an authenticated
publisher/TDM or browser layer separately.

## Requirements

- Python 3.10+
- Zotero desktop running with **Settings → Advanced → Allow other applications on
  this computer to communicate with Zotero** enabled
- A Zotero Local API key authorized once by `pyzotero`

## Install

```bash
python -m venv .venv
.venv/bin/pip install -e .
```

Optional deep fallback:

```bash
.venv/bin/pip install 'fetchpdf @ git+https://github.com/The-Metascience-Observatory/fetchpdf.git'
```

## Run

Collection names and keys are both accepted. Repeat `--collection` for several
libraries:

```bash
.venv/bin/zotero-pdf-harvester \
  --collection '楔状缺损_NCCL_有限元' \
  --collection lys \
  --email you@example.com \
  --workers 20 \
  --fallback-cli .venv/bin/fetchpdf \
  --output downloads \
  --report reports/latest.json
```

Re-run the same command at any time. Existing Zotero PDF attachments and cached
downloads are skipped.

## Performance notes

The fast API pass usually handles hundreds of Zotero items in about a minute. The
deep fallback is intentionally bounded per DOI. Increase `--fallback-timeout` only
when you accept slower completion for a small chance of retrieving more papers.

## Related projects

- [fetchpdf](https://github.com/The-Metascience-Observatory/fetchpdf): broad
  multi-source fallback coverage.
- [auto-paper-harvester](https://github.com/jxtse/auto-paper-harvester): publisher
  routing, TDM APIs, and optional institutional browser sessions.
- [OpenAlex bulk downloader](https://github.com/ourresearch/openalex-official):
  official OpenAlex PDF/TEI bulk tooling.

## License

MIT
