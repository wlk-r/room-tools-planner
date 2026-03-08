<task>
You are an expert interior design curator and classifier for a spatial reasoning system.
Classify this product for the procedural furniture placement pipeline by populating the required schema.
</task>

<inputs>
Use your Read tool to view the product image at {image_path}.

<metadata>
{metadata_content}
</metadata>
</inputs>

<output>
<field name="tier" type="enum" values="anchor,accent,fill">
Determine the layout priority based on spatial dependency and hierarchy, NOT physical size.
- **anchor**: Independent root items that define a functional zone and dictate traffic flow. Placed first. (Examples: sofas, dining tables, beds, large cabinets, desks).
- **accent**: Dependent items that must be oriented relative to an anchor to make sense. (Examples: chairs orient to tables, side tables orient to sofas, nightstands orient to beds).
- **fill**: Terminal decor, surface items, or floor coverings placed last. These do not dictate the primary traffic flow, regardless of how large they are. (Examples: massive rugs, vases, artwork, table lamps, small objects).
</field>

<field name="placement" type="enum" values="floor,wall,surface">
Determine the spatial mounting mode based on how the item physically occupies space.
- **floor**: Rests on the ground plane. (Examples: sofas, dining tables, chairs, rugs, floor lamps, freestanding cabinets).
- **wall**: Must be placed flush against or mounted on a structure edge. (Examples: wall-mounted shelves, mirrors, wall art, mounted cabinets, sconces).
- **surface**: Rests on top of another item within its bounding box. (Examples: vases on tables, table lamps on desks, candles on shelves, decorative objects on cabinets).
</field>

<field name="categories" type="string[]" count="1-3">
Identify functional placement categories for spatial reasoning.
- **CRITICAL**: Do NOT simply copy the vendor's merchandising categories from the metadata. You must generate your own functional categories based on how the item is used in a room.
- Use short, lowercase strings.
- Examples: seating, surface, storage, lighting, decor, tabletop, rug, shelving, textile.
</field>

<field name="tags" type="string[]" count="2-5">
Describe the visual style and vibe based on a synthesis of the image's visual cues and the metadata's description/materials. Use short, lowercase strings.
- Examples: japandi, scandinavian, cozy, minimal, organic, warm, industrial, mid-century, rustic, contemporary.
</field>
</output>

<instructions>
- Respond with ONLY a single JSON object. No reasoning, no markdown, no explanation.
- Format: a JSON object with keys: tier, placement, categories, tags
- Focus your reasoning internally: Use the physical dimensions and functional descriptions from the metadata to determine the item's dependency in a layout graph (Tier/Placement/Category), and use the visual aesthetic from the image to determine its style (Tags).
</instructions>
