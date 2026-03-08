<task>Classify a furniture product for a placement system.</task>

<inputs>
Read the product image at {image_path} and metadata at {metadata_path}.
</inputs>

<output>
<field name="tier" type="enum" values="anchor,accent,fill">
- anchor: large structural pieces placed first (sofas, dining tables, beds, large cabinets, desks)
- accent: placed relative to anchors (chairs, side tables, lamps, small shelves)
- fill: decor and accessories placed last (vases, rugs, artwork, candles, small objects)
</field>

<field name="categories" type="string[]" count="1-3">
Functional placement categories, short lowercase strings.
Examples: seating, surface, storage, lighting, decor, tabletop, rug, shelving, textile
</field>

<field name="tags" type="string[]" count="2-5">
Style/vibe descriptors, short lowercase strings.
Examples: japandi, scandinavian, cozy, minimal, organic, warm, industrial, mid-century, rustic
</field>
</output>

<instructions>
- Base classification on physical size, function, and visual appearance
- Vendor categories are merchandising hierarchies — generate your own functional categories
</instructions>
