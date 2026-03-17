# roomie-tools-planner

Quantized floor plan + product catalog pipeline for LLM-generated furniture layouts.

## Coordinate Systems

### Source JSON (floor plans)
- Standard 2D: X increases rightward, Y increases **upward**
- Units: meters
- Origin: varies per plan (bounding box computed at quantization time)

### CSS Output (quantized plans)
- CSS convention: X increases rightward, Y (`top`) increases **downward**
- Units: grid pixels (256x256 canvas)
- `quantize_plan.py` flips Y during conversion: `to_gy(m) = (outer_max_y - m) * scale + offset_y`
- Plans are centered within the grid (equal padding on shorter axis)
- The inverse transform in rasterization must match: `my = outer_max_y - (y + 0.5 - offset_y) / scale`
- Door/window inward direction is negated for horizontal walls to account for the Y flip

### Placement JSON (LLM output)
- Uses CSS coordinate space (Y-down, grid pixels)
- `(x, y)` = center point of placed item
- Rotation: 0, 90, 180, 270 degrees clockwise; 90/270 swap width and height

## Pipeline

The pipeline transforms raw floor plan geometry + vendor product data into furnished room layouts via four scripts and a viewer. Each stage produces intermediate files consumed by the next.

```
floor_plan.sample/*.json ──┐
                           ├─► quantize_plan.py ──► quantize_room.output/*_plan.css
products/**/*.catalog.json ─┘                   └──► quantize_room.output/*_catalog.json
                                                          │
                                                          ▼
                                                   generate_curation.py
                                                     ┌─ rag_filter.py (if >300 products + --products)
                                                     │    vibe filter → room filter → ~150 products
                                                     └─ curate (whole plan)
                                                          │
                                                          ▼
                                                   *_curation.json
                                                          │
                                                          ▼
                                                   generate_arrangement.py
                                                     arrange (per room)
                                                          │
                                                          ▼
                                               *_placement.json  (*_report.json)
                                                          │
                                                          ▼
                                                    viewer.html
```

### 1. build_catalog.py (product profiling + embeddings)

Processes vendor product directories under `products/`. For each product:
- Calls LLM vision to generate a `.catalog.json` profile containing tier (anchor/accent/fill), placement (floor/wall/surface), tags, and category.
- Generates a multimodal embedding via Gemini (`gemini-embedding-2-preview`, 768d) from product image + metadata. Written as `<stem>.embeddings.json` per product.
- Merges all per-product embeddings into `catalog.embeddings.json` at the end of the run.

Run independently before the rest of the pipeline. Embeddings are consumed by `rag_filter.py` for large catalog pre-filtering.

### 2. quantize_plan.py (floor plan quantization)

Converts floor plan JSON (meters, Y-up) to a 256x256 pixel grid in CSS format (Y-down).

**Inputs:** `floor_plan.sample/*.json` (rooms, walls, doors, windows as polygon/line geometry)
**Outputs per plan:**
- `<stem>_plan.css` — Room shapes, obstacles (walls), doors (clearance zones), windows as CSS rules with `left`, `top`, `width`, `height`. Diagonal walls use `clip-path: polygon(...)`.
- `<stem>_catalog.json` — Filtered product catalog with `footprints` array: `#i<item_no> { width: Wpx; height: Hpx; }` quantized to the plan's grid scale.

**Key behaviors:**
- Auto-scales geometry to maximize grid usage; centers the plan with equal padding.
- Y-flip: `to_gy(m) = (outer_max_y - m) * scale + offset_y`
- Flat-object detection: if `depth < 0.0254m`, swaps depth/height for floor footprint (rugs, mats).
- Rasterizes room polygons to determine pixel-level room/obstacle membership.
- Batch mode: pass a directory of JSON files to process all at once.

### 3. generate_curation.py (product curation)

LLM selects products from catalog and assigns to rooms using `claude --print`. Uses `prompts/curate.md`.

**Inputs:** `*_plan.css` + `*_catalog.json` from `quantize_room.output/`
**Output:** `<stem>_curation.json`

- **Scope:** Whole plan (all rooms + full catalog, or RAG-filtered subset)
- **Role:** Interior designer / shopping curator
- **Sees:** Plan CSS (room sizes, names, layout) + catalog products/profiles (names, tiers, tags, colors). Does NOT see footprint dimensions.
- **Produces:** JSON array of roles — each has `room`, `role` (functional label e.g. `dining-table`), `qty`, and `candidates` (2-3 ranked `item_no`s).
- **Goal:** Cast a wide net for candidates; the arrange stage narrows down based on spatial fit. Ensures stylistic coherence across rooms. Caps at 4-8 roles per small room, 8-12 for large.
- **Validation:** Checks required keys (`room`, `role`, `qty`, `candidates`) and types before writing.
- **Post-curation cleanup:** `clean_curation()` validates against the **full** catalog (not the RAG-filtered one), so candidates the LLM picks are checked against all available products.

**Flags:**
- `--model`: Model for LLM calls (default: sonnet).
- `--vibe`: Style brief (e.g. `'warm scandinavian, earth tones'`).
- `--timeout`: LLM call timeout in seconds (default: 300).
- `--verbose` / `-v`: Prints raw LLM responses (first 1000 chars) to console.
- `--report` / `-r`: Writes `<stem>_report.curation.json` and `<stem>_report.rag.json` with diagnostics.
- `--force`: Regenerates curation even if `*_curation.json` already exists.
- `--products`: Path to catalog source directory (for RAG embedding lookup). Required to enable RAG filtering.
- `--no-rag`: Disable RAG filtering even for large catalogs.
- `--rag-top`: Override target product count for RAG filter (default: 150).

### 4. generate_arrangement.py (spatial placement)

LLM places curated items per room with exact coordinates using `claude --print`. Uses `prompts/arrange.md`. Rooms are arranged **in parallel** (one LLM call per room, all fired concurrently via ThreadPoolExecutor). Surface items (placement: "surface") are resolved deterministically — placed at anchor coordinates without an LLM call.

**Inputs:** `*_plan.css` + `*_catalog.json` + `*_curation.json` from `quantize_room.output/`
**Output:** `<stem>_placement.json` conforming to `placement.schema.json`

- **Scope:** One room at a time (isolated LLM call per room, all rooms in parallel)
- **Role:** Spatial reasoning engine
- **Sees:** Room-specific CSS geometry (room components + adjacent obstacles/doors/windows extracted via bounding-box intersection) + candidates with footprints (width/height in px).
- **Produces:** JSON array of `{ item_no, x, y, r }` placements — one entry per physical instance.
- **Goal:** Pick one candidate per role based on spatial fit, place at exact grid coordinates. Respects obstacle/door clearances, maintains walkable pathways, groups furniture functionally.

**Flags:**
- `--model`: Model for LLM calls (default: sonnet). Can use a faster model (e.g. haiku) since this stage is spatial constraint satisfaction, not aesthetic judgment.
- `--room r1`: Re-run only specific room(s). Merges results into existing placement file, replacing only the specified room's items.
- `--timeout`: LLM call timeout in seconds (default: 600). Generous since rooms run in parallel — a slow room only blocks itself.
- `--verbose` / `-v`: Prints raw LLM responses (first 1000 chars) to console.
- `--report` / `-r`: Writes `<stem>_report.arrange.json` with diagnostics.
- `--force`: Regenerates placement even if `*_placement.json` already exists.

**Architecture note:** The module retains unused tier-splitting utilities (`group_roles_by_tier`, `build_occupied_block`, `format_occupied_css`) that support a two-pass arrangement strategy (anchor+accent first, then fill seeing occupied zones). This was tested but found slower than single-call due to ~50s fixed overhead per `claude --print` invocation. If the LLM backend switches to direct API calls (sub-second overhead), re-enabling tier splitting would improve placement quality on rooms with 10+ items.

### Shared: rag_filter.py (RAG pre-filter)

Hybrid search module that reduces large catalogs (~3000 products) to ~150 relevant items before the curation LLM sees them. Uses multimodal embeddings generated by `build_catalog.py`.

**Activation:** Only triggers when catalog has >300 products AND `--products` flag is provided. Below 300 products, the full catalog passes through unchanged (no overhead).

**Two-stage hybrid filtering:**
1. **Vibe filter** (semantic): Embeds the `--vibe` string, ranks all products by cosine similarity, keeps top 500. Passthrough if no vibe provided.
2. **Room filter** (hybrid — hard metadata + semantic): For each room, embeds `"{room_name} {vibe}"` and ranks survivors. Uses compound `(placement, category)` bucket keys to guarantee all placement types (floor/wall/surface) are represented. Tier floor ensures minimum anchor (8), accent (8), and fill (5) items survive.

**Key behaviors:**
- Products missing embeddings are included unconditionally (never filtered out).
- Falls back to full catalog if no embeddings found or GEMINI_API_KEY is unset.
- Embedding overhead: ~1 API call per room + 1 for vibe ≈ 6 calls for a 5-room plan (~6s total).
- `cosine_similarity()` is the canonical implementation — `validate_embeddings.py` imports from here.

**RAG report** (`<stem>_report.rag.json`, written with `--report`):
- Stage-by-stage funnel counts (vibe filter in/out, room filter in/out)
- Survivor breakdown by placement and tier
- Scored list of all filtered-out products ordered by best room similarity (near-misses first) — for evaluating filter quality

### Shared: llm_utils.py

Common utilities used by `generate_curation.py`, `generate_arrangement.py`, and `build_catalog.py`:
- `call_llm(prompt, model, verbose, timeout)` — routes text prompts to the appropriate backend (CLI, Anthropic SDK, or Gemini SDK).
- `call_llm_vision(prompt, image_path, model, verbose, timeout)` — routes multimodal (text+image) prompts. SDK backends send images directly; CLI backend uses the Read tool approach.
- `extract_json(text)` — extracts JSON from LLM response text (handles direct parse, bracket matching, markdown fences).
- `resolve_model(name)` — resolves aliases/full IDs to `(provider, full_model_id)`.

### 5. viewer.html (visual verification)

Browser-based viewer that loads CSS plan files as rendered floor plans and overlays placement data.

- Auto-loads all `*_placement.json` from `quantize_room.output/` on page load.
- Color-coded placement overlays by tier: purple = anchor, green = accent, yellow = fill.
- Item labels show `item_no` and rotation.
- "Choose folder" button with dropdown file list for manual folder selection.

## Key Directories

- `floor_plan.sample/` — Source floor plan JSON files (meters, Y-up coordinate system)
- `products/` — Vendor product directories: metadata JSON, images, GLB models, `.catalog.json` profiles
- `quantize_room.output/` — All pipeline output: `*_plan.css`, `*_catalog.json`, `*_curation.json`, `*_placement.json`, `*_report.curation.json`, `*_report.arrange.json`, `*_report.rag.json`
- `prompts/` — LLM prompt templates: `profile.md` (catalog profiling), `curate.md` (curation), `arrange.md` (arrangement)

## Multi-LLM Support

`llm_utils.py` routes LLM calls to three backends based on model name and available API keys:

| Model alias | Provider | Full model ID |
|---|---|---|
| `sonnet` | Anthropic | `claude-sonnet-4-6` |
| `opus` | Anthropic | `claude-opus-4-6` |
| `haiku` | Anthropic | `claude-haiku-4-5-20251001` |
| `gemini-flash` | Gemini | `models/gemini-2.5-flash` |
| `gemini-pro` | Gemini | `models/gemini-2.5-pro` |
| `nvidia-glm` | NVIDIA NIM | `z-ai/glm4.7` |
| `nvidia-deepseek` | NVIDIA NIM | `deepseek-ai/deepseek-v3.2` |
| `nvidia-devstral` | NVIDIA NIM | `mistralai/devstral-2-123b-instruct-2512` |
| `nvidia-kimi` | NVIDIA NIM | `moonshotai/kimi-k2.5` |

Full model IDs (e.g. `claude-sonnet-4-6`, `gemini-2.0-flash`) are also accepted directly.

**Routing logic:**
```
model contains "gemini"  →  Google genai SDK  (requires GEMINI_API_KEY)
model contains "/" (org/model)  →  NVIDIA NIM OpenAI-compatible  (requires NVIDIA_API_KEY)
model is Claude + ANTHROPIC_API_KEY set  →  Anthropic SDK  (faster, no subprocess overhead)
else  →  claude --print subprocess  (default fallback)
```

**Environment variables:**
| Variable | When needed |
|---|---|
| `ANTHROPIC_API_KEY` | Optional — enables Anthropic SDK for Claude models. Falls back to `claude --print` if unset. |
| `GEMINI_API_KEY` | Required when using `--model gemini-flash` or `--model gemini-pro`. Also required for RAG filter text embeddings and `build_catalog.py` multimodal embeddings. |
| `NVIDIA_API_KEY` | Required when using `nvidia-*` models. |

**Vision calls (`call_llm_vision`):** Used by `build_catalog.py` for multimodal product profiling. SDK backends send the image directly in the API call and strip the CLI-specific "Use your Read tool..." instruction from the prompt. The CLI backend passes the prompt as-is.

**Dependencies:** `pip install anthropic`, `pip install google-genai`, and/or `pip install openai` as needed. All are optional — the CLI fallback works without any SDK installed. `google-genai` is required for embeddings (RAG filter + `build_catalog.py`).

## Product Footprints

- Floor footprint = `width x depth` from vendor dimensions
- Exception: flat objects (depth < 0.0254m / 1 inch) like rugs — swap depth and height so footprint = `width x height`
- Elevation = the remaining dimension (object height above floor)

## Prompt Tuning (`prompts/`)

All LLM behavior is controlled by prompt templates and a shared config file in the `prompts/` directory. These are the primary knobs for tuning pipeline output quality.

### `prompts/llm_config.json` — Per-stage LLM settings

Central config for temperature, token limits, system prompts, and retry behavior. Loaded once at import time by `llm_utils.py`.

**Stage settings** (`curate`, `arrange`, `profile`):

| Key | Purpose | Tuning notes |
|---|---|---|
| `temperature` | Sampling randomness | Curation uses 0.8 (creative variety in product selection). Arrangement uses 0.1 (deterministic spatial precision). Profile uses 0.6 (balanced classification). |
| `max_tokens` | Output token cap | Curate=16384 (large JSON arrays), Arrange=8192 (one room at a time), Profile=4096 (single object). Increase if outputs are truncating. |
| `system_prompt` | Role/behavior instruction | Sets the LLM persona and output format constraints per stage. The system prompt is sent as the `system` parameter (Anthropic) or `system_instruction` (Gemini), separate from the user prompt template. |

**Global settings:**

| Key | Purpose | Tuning notes |
|---|---|---|
| `gemini_thinking.max_tokens_with_thinking` | Gemini 2.5 thinking budget | When set, overrides `max_tokens` for Gemini calls to give headroom for thinking + output tokens combined. Currently 32768. Set `default_budget` to cap thinking tokens specifically. |
| `retry.max_retries` | Auto-retry on failure | Currently 1. Retries on `PARSE_ERROR` and `TRUNCATED` with backoff. |
| `retry.backoff_seconds` | Delay between retries | Currently 5s, multiplied by attempt number. |
| `retry.retryable_errors` | Error prefixes that trigger retry | `["PARSE_ERROR", "TRUNCATED"]` — add more prefixes to retry on other transient errors. |

### `prompts/curate.md` — Curation prompt template

Template variables: `{plan_css}`, `{catalog_json}`, `{vibe}`.

**Key behavioral instructions to tune:**
- Candidate count per role: currently "2-4 candidate item_nos" — wider net means more spatial options at arrangement time, but larger output
- Room density caps: "4-8 roles per small room, 8-12 for large" — increase for denser furnishing
- Product-line cohesion: explicitly encourages same-collection pairings (e.g. "STOCKHOLM 2025" loveseat + side table)
- Color coordination: instructs palette-matching within rooms
- Surface item gating: "Do not assign surface-placement items unless the room has an anchor they can sit on"

### `prompts/arrange.md` — Arrangement prompt template

Template variables: `{room_id}`, `{room_name}`, `{room_css}`, `{occupied_block}`, `{items_json}`.

**Key behavioral instructions to tune:**
- Group semantics: anchor/dependent/surface hierarchy with `group_id` naming convention (`ROOM_FUNCTION_N`)
- Rotation facing: "at r=0 the FRONT faces downward (+y)" — rotations orient items toward functional context
- Rug placement: "Rugs go under their group's anchor, centered on the cluster"
- Wall items: "go against obstacle/structure edges"
- Candidate selection: "pick ONE candidate from the list — the one whose footprint fits best"
- Precision level: "Prefer plausible arrangements over pixel-perfect spacing — downstream geometry will adjust"

### `prompts/profile.md` — Product classification prompt template

Template variables: `{image_path}`, `{metadata_path}`, `{metadata_content}`.

**Key behavioral instructions to tune:**
- Tier definitions: anchor (independent root), accent (dependent on anchor), fill (terminal decor). "Based on spatial dependency and hierarchy, NOT physical size" — a massive rug is fill, not anchor
- Placement definitions: floor/wall/surface based on physical mounting mode
- Category generation: "Do NOT simply copy the vendor's merchandising categories" — forces functional categories over vendor taxonomy
- Tag generation: 2-5 style/vibe descriptors from synthesizing image + metadata

### `prompts/profile_context.md` — Prompt workshopping reference

Not used at runtime. Contains vendor metadata shape examples and field definitions for iterating on `profile.md` in a separate context window.
