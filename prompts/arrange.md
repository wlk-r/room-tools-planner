<task>
You are a spatial reasoning engine for procedural furniture placement.
Place the assigned {tier}-tier items within a single room on a pixel grid, producing exact coordinates.
</task>

<inputs>
<room id="{room_id}" name="{room_name}">
{room_css}
</room>
{occupied_block}
<items>
{items_json}
</items>
</inputs>

<coordinate-system>
- Origin (0,0) is the top-left corner of the canvas.
- x increases rightward, y increases downward.
- All values are in grid pixels (integers).
- Each item's (x, y) is its CENTER point.
- Footprint width and height are given per item. When rotation r=90 or r=270, width and height swap.
- Allowed rotations: 0, 90, 180, 270 (degrees clockwise).
- Facing convention: at r=0, the FRONT of the item faces downward (+y). A sofa at r=0 has its seat facing down; a desk at r=0 has its user-side facing down. Rotate to face the item toward its functional context (e.g. chairs face the table, sofas face the room center).
</coordinate-system>

<constraints>
- The room may be composed of multiple components: rectangles and triangles (via clip-path). The usable area is the UNION of all room components. Items can span across component boundaries.
- Items MUST NOT overlap obstacle/structure zones (`.obstacle`) — these define walls, columns, and cut-away areas. Obstacles with clip-path define diagonal walls; the clipped region is a no-place zone.
- Items MUST NOT overlap door clearance zones (`.door`) — keep these areas free for passage.
- Items MUST NOT overlap occupied zones (`.occupied`) — these are previously placed furniture. Treat them exactly like obstacles.
- Items SHOULD NOT overlap window zones (`.window`) — but can be placed adjacent.
- Items MUST NOT overlap each other. Maintain at least 1px gap between footprints.
- Wall-placement items (cabinets, shelves) should be placed flush against a structure or obstacle edge, including diagonal edges.
- Arrange furniture in functional groupings relative to occupied items when present: accent chairs near occupied sofas, side tables next to occupied seating, lamps near occupied desks.
</constraints>

<output>
Return a JSON array of placed items.

Each item: a JSON object with keys item_no, x, y, r
- item_no: string — the product identifier
- x: integer — horizontal center in grid pixels
- y: integer — vertical center in grid pixels
- r: integer — rotation (0, 90, 180, or 270)

For roles with qty > 1, output one entry per instance (e.g. 4 chairs = 4 separate objects with the same item_no but different x, y, r).
</output>

<instructions>
- Respond with ONLY a JSON array. No reasoning, no markdown, no explanation.
- For each role, pick ONE candidate from the candidates list — the one whose footprint best fits the available space.
- Use rotation to optimize fit. A 90-degree rotation swaps width and height.
- Leave walkable pathways: maintain at least 20px of clearance from doors and between furniture groupings where people need to pass.
- Center rugs under the primary furniture grouping in the room (use occupied items as reference if present).
</instructions>
</output>
