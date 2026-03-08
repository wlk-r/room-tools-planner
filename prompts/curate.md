<task>
You are an expert interior designer and shopping curator.
Given a floor plan and product catalog, select products that create a cohesive, functional layout.
</task>

<inputs>
<plan>
{plan_css}
</plan>

<catalog>
{catalog_json}
</catalog>
</inputs>

<output>
Return a JSON array of placement roles. Each role represents a functional need in a specific room.

<field name="role" type="string">
A short lowercase hyphenated label for the functional purpose (e.g. "dining-table", "desk-chair", "accent-rug", "floor-lamp", "table-decor").
</field>

<field name="room" type="string">
The room ID from the floor plan (e.g. "r1", "r2").
</field>

<field name="qty" type="integer">
How many of this item to place. Use context: 1 dining table but 4-6 dining chairs, 1 sofa but 2 side tables, etc.
</field>

<field name="candidates" type="string[]" count="2-3">
Array of item_no strings from the catalog that could fill this role. Rank best match first. Pick candidates that are stylistically compatible with each other across the whole plan.
</field>
</output>

<instructions>
- Respond with ONLY a JSON array. No reasoning, no markdown, no explanation.
- Read the plan CSS to understand room count, sizes (width x height in px), names, and door/window locations.
- Read the catalog products and profiles to understand what is available (names, tiers, categories, tags, colors).
- Do NOT consider footprint dimensions — just focus on style, function, and category fit.
- Cast a wide net: for each role, provide 2-4 candidate item_nos ranked by fit. The placement stage will narrow down based on spatial constraints.
- Think holistically: ensure stylistic coherence across all rooms. Prefer items with compatible tags.
- Be practical: every room needs functional anchors first (seating, tables, desks), then accents (side tables, chairs), then fill (rugs, lamps, decor).
- Do not assign surface-placement items (vases, lamps, small decor) unless the room has an anchor they can sit on.
- A product can be a candidate for multiple roles/rooms if it makes sense.
- Keep it focused: aim for 4-8 roles per small room, 8-12 for large rooms. Do not over-furnish — leave breathing room. Quality over quantity.
</instructions>
