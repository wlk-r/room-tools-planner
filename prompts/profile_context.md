# Profile Prompt Context

Use this document to workshop `prompts/profile.md` in a separate context window.

## System

- **Project**: roomie-tools-planner
- **Purpose**: Quantized floor plan + product catalog pipeline for LLM-generated furniture layouts

## Prompt File

- **Path**: `prompts/profile.md`
- **Called by**: `build_catalog.py`
- **Method**: `claude --print --model sonnet --allowedTools Read --json-schema {schema}`
- **Execution**: One call per product, isolated (no cross-product context)

## Template Variables

- `{image_path}` — Absolute path to product thumbnail (.jpg)
- `{metadata_path}` — Absolute path to vendor metadata (.json)

## Vendor Metadata Shape

```json
{
  "item_no": "40608242",
  "name": "BERGSHYTTAN table",
  "color": "dark brown ash veneer",
  "price": "449.99",
  "currency": "USD",
  "description": "BERGSHYTTAN Table - dark brown ash veneer 94 1/2x36 5/8\"...",
  "dimensions": { "height": 2.4, "width": 0.9298, "depth": 0.7539, "unit": "m" },
  "categories": ["Tables & chairs", "Dining furniture", "Dining tables"],
  "image_url": "https://...",
  "glb_url": "https://...",
  "product_url": "https://..."
}
```

## Output Schema

Enforced by `--json-schema` flag — constrains output structure automatically.

```json
{
  "tier": "anchor | accent | fill",
  "categories": ["string", "..."],
  "tags": ["string", "..."]
}
```

## Field Definitions

### tier

- **Type**: enum — `anchor`, `accent`, `fill`
- **Purpose**: Placement priority — determines processing order in the layout pipeline
  - **anchor**: large structural pieces placed first (define the room layout)
  - **accent**: placed relative to anchors (support/complement anchors)
  - **fill**: decor and accessories placed last (finishing touches)

### categories

- **Type**: string array, 1-3 items
- **Purpose**: Functional placement categories for spatial reasoning
- Free-form lowercase strings describing what the item does in a room
- Examples: seating, surface, storage, lighting, decor, tabletop, rug, shelving, textile

### tags

- **Type**: string array, 2-5 items
- **Purpose**: Style/vibe descriptors for aesthetic coherence
- Free-form lowercase strings describing visual style
- Examples: japandi, scandinavian, cozy, minimal, organic, warm, industrial, mid-century, rustic

## Constraints

- VLM reads both the product image AND metadata — image provides visual/material cues metadata lacks
- Per-product isolation: no awareness of other products in the catalog
- Output must be pure JSON matching the schema — no explanation, no markdown
- Vendor categories (e.g. "Tables & chairs > Dining tables") are merchandising hierarchies, NOT the same as placement categories — the VLM should generate its own functional categories
