# Model Tournament

Compare LLM models on the curation → arrangement → placement pipeline.

## Setup

Place source files in this directory:
```
<stem>.json              # room geometry (from floor_plan.sample/)
<stem>_plan.css          # quantized plan (from quantize_plan.py)
<stem>_catalog.json      # product catalog with footprints (from quantize_plan.py)
<stem>_AO.ktx.glb        # ambient occlusion mesh
```

## Usage

```bash
# Single plan — run all default models
python model-tournament/run_tournament.py model-tournament/<stem>

# Batch mode — auto-discovers all plans in the directory
python model-tournament/run_tournament.py model-tournament/

# Specific models only
python model-tournament/run_tournament.py model-tournament/<stem> --models gemini-flash nvidia-devstral sonnet

# Re-run everything from scratch
python model-tournament/run_tournament.py model-tournament/<stem> --force

# Curation + arrangement only (skip place.py)
python model-tournament/run_tournament.py model-tournament/<stem> --skip-place

# Longer timeout for slow models (default: 300s)
python model-tournament/run_tournament.py model-tournament/<stem> --timeout 600

# Archive previous results, then re-run
python model-tournament/run_tournament.py model-tournament/<stem> --archive
python model-tournament/run_tournament.py model-tournament/<stem>

# Delete all generated output
python model-tournament/run_tournament.py model-tournament/<stem> --clean
```

Batch mode discovers plans by scanning for `*_plan.css` files that have matching `*_catalog.json` and `*.json`. Each plan gets its own tournament report. `--clean` and `--archive` also work in batch mode.

## Available Models

| Alias | Provider | Notes |
|---|---|---|
| `sonnet` | Anthropic | Default pipeline model. Needs `ANTHROPIC_API_KEY` or falls back to `claude --print` CLI. |
| `opus` | Anthropic | Highest quality, slowest. |
| `haiku` | Anthropic | Fastest Anthropic model. |
| `gemini-flash` | Google | Fast, good quality. Needs `GEMINI_API_KEY`. |
| `gemini-pro` | Google | Higher quality, slower. |
| `nvidia-glm` | NVIDIA NIM | GLM 4.7. Needs `NVIDIA_API_KEY`. |
| `nvidia-deepseek` | NVIDIA NIM | DeepSeek V3.2 (thinking model). |
| `nvidia-devstral` | NVIDIA NIM | Devstral 2 123B. Fastest NVIDIA option. |
| `nvidia-kimi` | NVIDIA NIM | Kimi K2.5. Too slow for default runs — use `--models nvidia-kimi --timeout 600`. |

API keys are configured in `.env` at the project root.

## Output Structure

```
model-tournament/
  <stem>.<model>.layout.json    # layout files (root, one per model)
  products/                     # shared GLBs (copied by place.py)
  <stem>_tournament.json        # timing/token report
  <model>/                      # per-model subfolder
    <stem>_curation.json
    <stem>_placement.json
    <stem>_report.curation.json
    <stem>_report.arrange.json
  z.archive/                    # previous runs (from --archive)
```

## Report Columns

| Column | Meaning |
|---|---|
| Curate | Time for LLM to select products and assign to rooms |
| Arrange | Time for LLM to place items with exact coordinates |
| Place | Time for place.py to resolve geometry and copy GLBs |
| Clock | Wall clock time for entire model pipeline |
| Roles | Number of furniture roles curated |
| Items | Number of items in final placement |
| Tok In | Total input tokens (curation + arrangement) |
| Tok Out | Total output tokens (curation + arrangement) |
