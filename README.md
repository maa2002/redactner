# RedactNER

**Context-aware redaction of personal information in Microsoft Word documents, running entirely on your own machine.**

![Python](https://img.shields.io/badge/Python-3.12-3776AB?logo=python&logoColor=white)
![PyTorch](https://img.shields.io/badge/PyTorch-CUDA%20%7C%20CPU-EE4C2C?logo=pytorch&logoColor=white)
![Transformers](https://img.shields.io/badge/Hugging%20Face-Transformers-FFD21E?logo=huggingface&logoColor=black)
![FastAPI](https://img.shields.io/badge/FastAPI-local%20web%20app-009688?logo=fastapi&logoColor=white)
![Platforms](https://img.shields.io/badge/Platforms-macOS%20%7C%20Linux%20%7C%20Windows-555555)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)

![RedactNER reviewing a CV: every detected item is shown as a tracked change before export](docs/images/app-markup.png)

## Overview

RedactNER finds and removes personally identifiable information (PII) from `.docx` files. A fine-tuned transformer model detects names, contact details, addresses, government and financial identifiers, credentials and more. Checksum-validated pattern rules and document-level consistency checks support it, and a document engine writes each replacement inside the original Word structure. The redacted file therefore keeps its formatting and opens normally in Word.

Everything runs locally. Documents are never uploaded to an external service, so the tool can be used on material that cannot leave the organisation.

## Features

- **Context-Aware Detection.** A fine-tuned ModernBERT token classifier recognises 13 categories of personal information from the surrounding sentence rather than from fixed word lists, so ambiguous words such as names that are also months or places are judged in context.
- **Validated Identifiers.** Payment cards (Luhn), IBANs (mod 97), Australian Tax File, ABN and Medicare numbers, phone numbers and IP addresses are confirmed with checksum and format validators.
- **Document-Wide Consistency.** Once a name or identifier is found, every other mention of it in the document is redacted as well.
- **Complete Document Coverage.** Body text, tables, headers and footers, footnotes and endnotes, comments and their authors, text boxes, hyperlink targets, field codes, tracked changes and document properties.
- **Formatting Preserved.** Replacements are written into the original text runs, so fonts, styles, tables and layout are unchanged.
- **Review Before Export.** Every detection can be inspected, kept or removed, and missed text can be added by selecting it.
- **Verified Output.** Before a file is returned, every part of the package is scanned again to confirm that no redacted value remains, and an audit report summarises the changes.
- **Two Redaction Styles.** Consistent labels such as `[PERSON_1]` keep the document readable; fixed-width blocks hide the length of each value.
- **Three Interfaces.** A browser application, a command-line tool and a Python API.

## How It Works

1. **Read.** The `.docx` package is opened with strict safety limits. Encrypted, macro-enabled and malformed files are refused, tracked changes are accepted, and every paragraph is mapped to the exact Word runs that hold its text.
2. **Detect.** The document text is analysed by the fine-tuned model in overlapping windows, merged with the validated pattern rules, and confident names and identifiers are propagated to their other occurrences.
3. **Redact.** Each item is replaced inside its original runs. Hyperlinks, field codes, comment authors, document properties and the embedded preview image are cleaned at the same time.
4. **Verify.** All XML parts of the output are scanned for any remaining value before the document is saved.

## Detected Categories

| Category | Examples |
|---|---|
| Person | Full names, first and last names, initials |
| Email | Email addresses |
| Phone | Mobile, landline and fax numbers, Australian and international |
| Address | Street addresses, unit and building numbers, postcodes |
| Location | Cities, suburbs, states, coordinates |
| Date | Dates of birth and other personal dates |
| ID Number | Tax file, Medicare, passport, licence, social security, employee and customer numbers |
| Financial | Card numbers, bank accounts, BSB, IBAN, SWIFT/BIC |
| Username | Account names and handles |
| Credential | Passwords, PINs, security codes, API keys |
| IP Address | IPv4, IPv6 and MAC addresses |
| URL | Personal web addresses and profile links |
| Organisation | Employer and company names (optional, off by default) |

Terms that must always be removed, such as a project codename, and terms that must always be kept can be supplied at run time.

## Results

Three transformer encoders were fine-tuned on the same 25,000 synthetic documents with an identical training recipe and evaluated on three held-out test sets: in-domain documents, in-domain short texts, and documents from a generator never seen during training. The **leak rate** is the share of PII mentions with any character left visible in the output, so lower is better.

| System | In-Domain Documents | In-Domain Short Texts | Unseen Generator |
|---|---|---|---|
| Rule-Based Detection Only | 77.6 % | 85.8 % | 93.7 % |
| RoBERTa-base | 0.62 % | 0.13 % | 27.1 % |
| DeBERTa-v3-base | 0.78 % | 0.26 % | 29.2 % |
| ModernBERT-base | 0.55 % | 0.09 % | 25.5 % |
| **RedactNER (ModernBERT with Rules and Propagation)** | **0.47 %** | **0.04 %** | **24.3 %** |

![Character-level F5 and strict entity F1 for each system and test set](docs/images/model-comparison.png)

- ModernBERT-base was selected because it leaked the least on every test set. DeBERTa-v3-base was the most precise, with the highest strict entity F1 (0.966 and 0.989 on the two in-domain sets).
- Combining the model with validated rules and document-wide propagation lowered the leak rate further on all three test sets.
- Documents from an unfamiliar source remain the hardest case and are the focus of further training.

## Installation

### Requirements

- [uv](https://docs.astral.sh/uv/), which also installs Python 3.12 automatically when it is missing
- Git
- Optional: an NVIDIA GPU with a current driver for CUDA acceleration on Windows or Linux

PyTorch is installed with CUDA 12.8 support on Windows and Linux (it also runs on the CPU when no GPU is present) and as the standard build on macOS. macOS requires an Apple Silicon Mac.

### macOS

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
git clone https://github.com/maa2002/redactner.git
cd redactner
uv sync
```

### Linux

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
git clone https://github.com/maa2002/redactner.git
cd redactner
uv sync
```

Check that the NVIDIA driver is visible with `nvidia-smi` if you want GPU acceleration.

### Windows (PowerShell)

```powershell
powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
git clone https://github.com/maa2002/redactner.git
cd redactner
uv sync
```

### Verify the Installation

```bash
uv run python -c "import torch; print(torch.__version__, 'CUDA available:', torch.cuda.is_available())"
uv run pytest -q
```

### Model Weights

The trained model is included in `models/redactner-best/` (half-precision weights, about 300 MB) and is loaded automatically, so no training is needed to run RedactNER. Its test scores are within 0.1 percentage points of the full-precision results reported above. To train your own model instead, follow the steps under [Training](#training); the notebooks write the selected model to the same folder.

## Usage

### Web Application

```bash
uv run redactner serve
```

Then open <http://127.0.0.1:8000>. Open a `.docx` file, review the highlighted changes, and choose **Redact & Download** to save the redacted copy. The audit report and the final leak check are shown alongside the document.

![The redacted version of the same CV](docs/images/app-redacted.png)

### Command Line

```bash
# Redact with consistent labels such as [PERSON_1]
uv run redactner redact contract.docx -o contract_redacted.docx

# Black out values instead, limited to direct identifiers, and save the audit report
uv run redactner redact contract.docx --mode mask --preset direct --report report.json

# List the detected items without changing the document
uv run redactner analyze contract.docx
```

| Option | Values | Description |
|---|---|---|
| `--mode` | `label`, `mask` | Replacement style |
| `--preset` | `strict`, `direct` | All categories except organisations, or direct identifiers only |
| `--detector` | `hybrid`, `model`, `rules` | Detection method (default `hybrid`) |
| `--threshold` | `0.01` to `0.99` | Override the model decision threshold; higher values redact more |
| `--device` | `cuda`, `cpu` | Compute device (detected automatically by default) |

### Python

```python
from pathlib import Path

from redactner.pipeline import RedactionPipeline, build_detectors

pipeline = RedactionPipeline(build_detectors("models/redactner-best"))
redacted, report = pipeline.redact_bytes(Path("contract.docx").read_bytes(), mode="label")

Path("contract_redacted.docx").write_bytes(redacted)
print(report["counts_by_label"])
```

### REST API

The server documents its API at <http://127.0.0.1:8000/docs>. Every request needs the access token printed when the server starts.

```bash
curl -X POST "http://127.0.0.1:8000/api/v1/redact?mode=label" \
  -H "X-RedactNER-Token: <token>" \
  -H "Content-Type: application/vnd.openxmlformats-officedocument.wordprocessingml.document" \
  --data-binary @contract.docx -o contract_redacted.docx
```

## Training

| Notebook | Purpose |
|---|---|
| `notebooks/01_data_preparation.ipynb` | Downloads the datasets, maps them to the 13 categories and builds the training, development and test splits |
| `notebooks/02_train_token_classifier.ipynb` | Fine-tunes one encoder, selected with the `MODEL_NAME` parameter |
| `notebooks/03_evaluate_and_select.ipynb` | Compares the trained models, tunes the decision threshold and saves the best model to `models/redactner-best/` |

The notebooks run locally or on Google Colab, where each one installs its own dependencies. To use them locally, register the project environment as a Jupyter kernel once:

```bash
uv run python -m ipykernel install --sys-prefix --name redactner --display-name "Python (redactner)"
```

They can also be executed from the command line:

```bash
uv run papermill notebooks/01_data_preparation.ipynb notebooks/01_data_preparation.ipynb -k redactner --cwd notebooks
uv run papermill notebooks/02_train_token_classifier.ipynb notebooks/executed/02_train__modernbert-base.ipynb -k redactner --cwd notebooks -p MODEL_NAME answerdotai/ModernBERT-base -p RUN_NAME modernbert-base
uv run papermill notebooks/03_evaluate_and_select.ipynb notebooks/03_evaluate_and_select.ipynb -k redactner --cwd notebooks
```

Training uses 512-token windows with a 128-token overlap, three epochs and bf16 mixed precision, and needs about 4 GB of GPU memory for ModernBERT-base.

## Project Structure

```
redactner/
├── src/redactner/
│   ├── docx_engine.py      Word Package Reader, Rewriter and Verifier
│   ├── inference.py        Transformer Detector with Sliding-Window Inference
│   ├── rules.py            Validated Pattern Recognisers
│   ├── fusion.py           Hybrid Detection and Document-Wide Propagation
│   ├── pipeline.py         Analysis and Redaction Workflow
│   ├── metrics.py          Character-Level and Entity-Level Evaluation
│   ├── operators.py        Replacement Styles
│   ├── cli.py              Command-Line Interface
│   └── api/                Web Application and REST API
├── notebooks/              Data Preparation, Training and Evaluation
├── scripts/                Sample Document Generator and Word Compatibility Check
├── samples/                Synthetic Example Documents
├── tests/                  Test Suite
├── results/                Evaluation Tables and Figures
└── docs/images/            Screenshots and Figures Used in This README
```

## Testing

```bash
uv run pytest -q
```

The suite covers the pattern validators, detection fusion, metrics, the Word engine (formatting, tracked changes, comments, hyperlinks, text boxes, metadata and leak checks) and the REST API, including its security checks.

## Privacy and Security

- The server listens on `127.0.0.1` only and every request requires a per-session access token.
- Uploaded documents are held in memory for a limited time and are never written to disk by the application.
- Requests from other websites are rejected, and the web interface runs under a strict Content Security Policy.
- Encrypted, macro-enabled and Strict Open XML documents are refused rather than partially processed.

## Limitations

- Text inside images, charts and embedded objects is not analysed; the audit report lists these as warnings.
- The model is trained on synthetic data, and accuracy is lower on documents that differ strongly from it.
- Automated redaction reduces risk but cannot guarantee that every identifier is removed. Review redacted documents before sharing them.

## Data and Model Credits

| Resource | Use | Licence |
|---|---|---|
| [NVIDIA Nemotron-PII](https://huggingface.co/datasets/nvidia/Nemotron-PII) | Training and Evaluation | CC BY 4.0 |
| [ai4privacy OpenPII](https://huggingface.co/datasets/ai4privacy/pii-masking-openpii-1.5m) (English subset) | Training and Evaluation | CC BY 4.0 |
| [Gretel Synthetic PII Finance](https://huggingface.co/datasets/gretelai/synthetic_pii_finance_multilingual) (English subset) | Evaluation Only | Apache 2.0 |
| [ModernBERT-base](https://huggingface.co/answerdotai/ModernBERT-base) | Selected Base Model | Apache 2.0 |
| [DeBERTa-v3-base](https://huggingface.co/microsoft/deberta-v3-base) | Compared Model | MIT |
| [RoBERTa-base](https://huggingface.co/FacebookAI/roberta-base) | Compared Model | MIT |

All training and evaluation data is synthetic.

## License

RedactNER is released under the [MIT License](LICENSE). The datasets and base models listed above remain subject to their own licences.
