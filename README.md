# ContractRadar test set

Synthetic UK council procurement documents with exact gold labels, the layouts that
break OCR-and-regex pipelines, the compliance flags a Procurement Act 2023 rule engine
should raise, and a scorer. Built for the [ContractRadar pilot](https://discord.com/channels/1558912731019608085/1565349622535299214/threads/1565685928394231930)
(AI for council procurement assurance). Free for anyone to use.

**Site:** https://contractradar.opentransformers.online

> Synthetic PDF in, expected CSV out, plus the wrong extraction.

## What is in the box

| Path | What |
|---|---|
| `cases/CR-0NN/document.pdf` | A synthetic contract award notice, contract details notice, contract change notice or contract document. Every page carries a visible "synthetic test document" marking. |
| `cases/CR-0NN/gold.json`, `expected.csv`, `expected_lots.csv` | The fields an extractor should return, plus `expected_flags` (what a rule engine should raise) and `hazards`. |
| `cases/CR-0NN/notes.md` | Why the case is hard. |
| `cases/manifest.json` | Index of cases. |
| `schema/fields.json` | The 39 field names, types and Act references; the flag ids. |
| `crtest/gen.py` | Generator. PDF and gold come from the same spec, so they cannot disagree. |
| `crtest/extract_naive.py` | Deliberately naive baseline (`pdftotext -layout` + label regexes). This is the "wrong extraction". |
| `crtest/extract_llm.py` | Extractor for any OpenAI-compatible endpoint (text, `--ocr`, `--vision`). No keys in the repo. |
| `crtest/score.py` | Scorer: per-field, per-case, per-hazard accuracy; critical-field pass; flag precision/recall. |
| `crtest/pa2023.py` | Reference checks: thresholds (1 Jan 2026 figures), s.51 standstill, s.52 KPIs, s.53 notice timing, s.68 payment terms, s.74/75 modifications, Sch 8 para 3 cap, s.77. |
| `crtest/collect.py` | Fetch real council notices (OCDS JSON) from Find a Tender and Contracts Finder with source URL and licence recorded per file. |
| `crtest/site.py` | Builds the static site (`site/dist`). |
| `crtest/server.py` | MCP server and HTTP API: serves the task and cases, scores and stores community submissions. |
| `crtest/submit.py` | Posts a predictions file to the community leaderboard. |
| `cases/CR-0NN/text.txt` | The `pdftotext -layout` text (tesseract OCR for the scan) that text-mode extractors and the API hand to models (`make text`). |
| `results/` | Committed predictions and scores; the leaderboard is built from `*.scored.json`. |

## The 12 cases

| Case | Document | Hazards | Expected flags |
|---|---|---|---|
| CR-001 | award notice, clean | baseline | threshold in scope |
| CR-002 | contract details notice | priced schedule crosses the page, repeated header, footer between rows, annual price printed before the total | threshold |
| CR-003 | contract details notice (works) | borderless three-column particulars, section label spanning rows, label synonyms, 01-Mar-2026 dates | threshold |
| CR-004 | contract change notice | +14.4% value and +33% term under the below-threshold ground, percent not printed, DD/MM/YYYY | s.74 exceeds below-threshold, s.74 substantial term |
| CR-005 | scanned contract particulars | image-only PDF with skew and speckle, trading-as name, 2 KPIs on £9.75m | s.52 KPIs |
| CR-006 | contract details notice | two-column stacked layout, £2.31m, 61 days to publish, 45-day terms | s.53 late, s.68 payment |
| CR-007 | multi-lot award notice (light touch) | 3 lots, one unawarded, totals row, scores table crosses page, 6-working-day standstill | s.51 standstill |
| CR-008 | contract change notice | +6.0% correctly below threshold, £4 200 000, negative control | none (high) |
| CR-009 | deed of variation + decision record | extension dated after expiry, +20% value, no change notice, values in prose | s.74 substantial term, s.75 no notice, retrospective extension |
| CR-010 | contract details notice (works) | supplier name wrapped mid-name, sum in words and figures, ordinal dates | threshold |
| CR-011 | contract change notice (works) | +60% under Sch 8 para 3 (cap 50%), 6,100,000.00 GBP, ISO dates | Sch 8 para 3 cap, s.77 publish |
| CR-012 | direct award notice | everything in prose, s.41 urgency, no standstill | direct award above threshold |

## Quick start

Regenerating documents (`make cases`) and re-running the naive extraction (`make naive`) need Poppler (`pdftotext`, `pdftoppm`): `brew install poppler` on macOS, `sudo apt-get install poppler-utils` on Debian/Ubuntu. Tesseract is only for `make text` and `extract_llm --ocr`.

```bash
make install      # creates .venv and installs the Python packages
make cases        # regenerate PDFs, gold labels, page images
make naive        # run the naive baseline and score it
make site         # build site/dist
```

`make` uses `.venv/bin/python` when that file exists, so you do not need to activate the venv. `make install` does not install Poppler. `make cases` and `make naive` stop and print the install command when it is missing.

Score your own extractor: write predictions as

```json
{"extractor": "my-pipeline", "version": "1", "cases": {"CR-001": {"fields": {"contracting_authority": "...", "...": "..."}, "flags": ["PA23-s52-kpi-min3"]}}}
```

then `python -m crtest.score --pred results/my-pipeline.json` and rebuild the site. Field names are the ones in `schema/fields.json`; dates may be in any common UK format and money in any common format, the scorer normalises both.

Run the built-in LLM extractor against any OpenAI-compatible endpoint:

```bash
export LLM_BASE_URL=https://api.deepseek.com/v1 LLM_API_KEY=sk-... LLM_MODEL=deepseek-chat
python -m crtest.extract_llm --out results/deepseek-chat.json --ocr          # text (+ tesseract for scans)
python -m crtest.extract_llm --out results/some-vlm.json --vision --ocr      # also sends page images
python -m crtest.score --pred results/deepseek-chat.json
```

Collect real notices (Open Government Licence) for a hand-labelled set:

```bash
python -m crtest.collect --from 2026-09-01 --to 2026-09-07 --out collected --source both
```

## Submit results: MCP server, HTTP API or CLI

Anyone can run the task and publish a score without cloning the repo. Submissions are scored on arrival by the same
scorer and shown in the community table on the site ([details](https://contractradar.opentransformers.online/submit.html)).

* **Agents (MCP):** `https://contractradar.opentransformers.online/mcp` (Streamable HTTP, no sign-in). Tools: `get_task`,
  `list_cases`, `get_case`, `submit_results`, `leaderboard`. Claude Code:
  `claude mcp add --transport http contractradar https://contractradar.opentransformers.online/mcp`.
* **Scripts (HTTP):** `GET /api/task`, `GET /api/cases`, `GET /api/cases/CR-001`, `POST /api/submit` (body in the
  `results/` format), `GET /api/leaderboard?source=community`.
* **CLI:** `python -m crtest.submit --pred results/my-pipeline.json --contact https://github.com/you`.

Community rows are self-reported: the gold labels ship with the cases, so that table is an honour-system comparison of
methods. Rows in the curated table come from pull requests that add `results/<name>.json` and its `.scored.json`.

Run the backend yourself with `make serve`. That installs `requirements-server.txt` into `.venv` and starts the server.
`deploy/setup-server.sh` and `deploy/contractradar-mcp.service` are the production setup behind nginx.

## Tables track

`tables/TB-00N/` holds four documents built around table failures raised in the pilot thread: a statement with no ruled
lines (TB-001), a spend statement where dates, directorates and suppliers are printed once per group (TB-002), a ruled
milestone schedule with one row split by the page break (TB-003), and a borderless weekly statement whose dates are only
group headings and one of whose weeks runs over the page (TB-004). Each has `document.pdf`, `expected.csv`, `wrong.csv`
(the naive text-dump extraction), `notes.md`, `spec.json` and the page images.

Run `make install` first. Regenerating the PDFs needs Poppler, the same packages as `make cases`.

```bash
.venv/bin/python -m crtest.gen_tables --out tables                         # regenerate (reportlab is in .venv)
python -m crtest.extract_tables_naive                                      # wrong.csv + results_tables/naive_text.json
python -m crtest.extract_tables_llm --out results_tables/my_model.json --prompt guided
python -m crtest.score_tables --pred results_tables/my_model.json          # or a directory of TB-00N.csv files
.venv/bin/python -m crtest.extract_tables_tools --tables tables --out /tmp/tools  # pdfplumber / PyMuPDF baselines (optional; install those into .venv)
```

Cell score = right cells / ((expected rows + invented rows) x columns), so invented rows cost as much as wrong ones; cell
recall is reported next to it. The gold was checked by eight blind transcriptions (four from page images, four from the
text dump), all exact.

## Scoring rules

* Gold null and prediction null: not counted. Gold null and prediction non-null: wrong.
* Dates are compared as ISO after parsing `3 March 2026`, `3rd March 2026`, `03/03/2026`, `2026-03-03`, `03-Mar-2026`.
* Money within 0.1% or £1 after parsing `£1,234.56`, `£2.31m`, `£4 200 000`, `1,234.00 GBP`, words-and-figures.
* Organisation names: case, punctuation, Limited/Ltd and trading-as suffixes normalised; similarity ≥ 0.92 is correct.
* Lots must match on number, awarded flag, supplier and value.
* A case passes when every critical field (authority, supplier, title, value, start, end; plus value change, revised end date and ground for modifications) that is non-null in the gold is correct.
* Only high-severity flags are scored (precision/recall over cases).

## Thresholds and rules used

Inclusive of VAT, from the Procurement Act 2023 (Threshold Amounts) (Amendment) Regulations 2025 for procurements commenced on or after 1 January 2026:
sub-central goods/services £207,720; works £5,193,000; light touch £663,540; central goods/services £135,018. Earlier figures (£214,904 / £5,372,609 / £663,540 / £139,688) are applied to earlier dates.
Modification limits: 10% goods/services, 15% works, 10% of maximum term (ss.74-75); Sch 8 para 3 and 5 capped at 50%; s.52 three KPIs over £5m; s.53 contract details notice within 30 days; s.68 30-day payment; s.51 eight working days standstill; s.77 publish modified contract over £5m within 90 days.
If you think a check is wrong, open an issue citing the section.

## Data rules

* Synthetic documents and labels: **CC0-1.0**. All authorities, suppliers, people, references and registration numbers are fictional; resemblance to real bodies is coincidental.
* Code: **MIT** (see `LICENSE`).
* Collected notices: **Open Government Licence v3.0**; each file carries `_provenance` with the source URL and fetch time. Attached documents are the publishers' own files: check before redistributing.
* No personal data, credentials or private conversations are in this repository. Please keep it that way.

## Contributing

See `CONTRIBUTING.md`. The most useful things right now: more layout variants of the three pain points named in the pilot thread (tables without set boundaries, grouped ID rows such as dates, page boundaries), results from more extractors, and hand-labelled real notices with provenance.
