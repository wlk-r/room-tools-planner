# Floor Plan CSS Format Specification

Quantized floor plan layout for LLM spatial reasoning.
All values are unitless integers representing grid pixels on a 256x256 canvas.

## Header

```css
/* <W>x<H> grid | 1px = <M>m | ceiling: <C>px */
```

| Field | Type | Description |
|-------|------|-------------|
| W, H  | int  | Canvas dimensions (always 256x256) |
| M     | float | Meters per pixel — scale factor |
| C     | int  | Ceiling height in grid pixels |

## Selectors

Each rule is `#<id>.<class> { ... }` where class determines the element type.

| Class      | ID pattern           | Description |
|------------|----------------------|-------------|
| `.room`    | `#r<N>` or `#r<N>_<I>` | Room component. Multi-component rooms use `_<I>` suffix (0-indexed) |
| `.obstacle`| `#structure_<N>`     | Structural element (walls, columns) |
| `.door`    | `#door_<id>_clearance` | Door swing clearance zone — keep-out area |
| `.window`  | `#window_<id>`       | Window zone along wall |

## Properties

Only the following properties are valid. No units — all values are grid pixels.

### Required (all elements)

| Property | Type | Description |
|----------|------|-------------|
| `left`   | int  | X offset from canvas left edge |
| `top`    | int  | Y offset from canvas top edge |
| `width`  | int  | Horizontal extent |
| `height` | int  | Vertical extent |

### Optional

| Property    | Type   | Applies to       | Description |
|-------------|--------|------------------|-------------|
| `clip-path` | polygon | `.room`, `.obstacle` | Clips the bounding box to a triangle. See Geometry below |
| `--sill`    | int    | `.window`        | Window sill height above floor in grid pixels |

## Geometry

### Rectangles

Most elements are axis-aligned rectangles defined entirely by `left`, `top`, `width`, `height`.

```css
#r1.room { left: 3; top: 3; width: 183; height: 182; /* Sunroom (1/2) */ }
```

### Triangles (clip-path)

Non-rectangular shapes use `clip-path: polygon(...)` with exactly 3 percentage-based vertices relative to the element's bounding box. Only triangles are permitted — no polygons with 4+ vertices.

```css
#r1_1.room { left: 186; top: 3; width: 67; height: 182; clip-path: polygon(0% 0%, 100% 50%, 0% 100%); /* Sunroom (2/2) */ }
```

Percentage coordinates:
- `0%` = left/top edge of bounding box
- `100%` = right/bottom edge of bounding box
- Intermediate values place vertices proportionally within the bounding box

### Room decomposition

Rooms with only axis-aligned walls produce one or more rectangular components.

Rooms with diagonal walls are decomposed into:
- **Rectangle(s)**: the axis-aligned core of the room
- **Triangle(s)**: `clip-path: polygon()` components for diagonal edge regions

Multi-component rooms share a base ID with indexed suffixes:

```css
#r1_0.room { left: 3; top: 3; width: 183; height: 182; /* Sunroom (1/2) */ }
#r1_1.room { left: 186; top: 3; width: 67; height: 182; clip-path: polygon(...); /* Sunroom (2/2) */ }
```

All components of a room together tile the full room area with no gaps or overlaps.

### Obstacle triangles

Structural obstacles along diagonal walls also use triangular clip-paths:

```css
#structure_4.obstacle { left: 184; top: 3; width: 69; height: 92; clip-path: polygon(1% 0%, 99% 0%, 99% 99%); }
```

## Comments

Each room component includes a trailing comment with the room name and component index:

```css
/* Sunroom (1/2) */
```

Single-component rooms omit the index:

```css
/* Studio */
```

## Z-order (viewer only)

For visual rendering, elements layer as:
1. `.obstacle` (bottom)
2. `.room`
3. `.door`, `.window` (top)

This is a rendering concern — the CSS plan file does not include z-index values.

## Example

```css
/* 256x256 grid | 1px = 0.02208m | ceiling: 127px */

#r1_0.room                  { left:   3; top:   3; width: 183; height: 182; /* Sunroom (1/2) */ }
#r1_1.room                  { left: 186; top:   3; width:  67; height: 182; clip-path: polygon(0% 0%, 100% 50%, 0% 100%); /* Sunroom (2/2) */ }

#structure_0.obstacle       { left:   0; top:   0; width: 256; height:   3; }
#structure_1.obstacle       { left:   0; top:   3; width:   3; height: 253; }
#structure_2.obstacle       { left: 252; top:  93; width:   4; height: 163; }
#structure_3.obstacle       { left:   3; top: 185; width: 182; height:  71; }
#structure_4.obstacle       { left: 184; top:   3; width:  69; height:  92; clip-path: polygon(1% 0%, 99% 0%, 99% 99%); }
#structure_5.obstacle       { left: 184; top:  94; width:  69; height:  91; clip-path: polygon(99% 0%, 99% 100%, 1% 100%); }
#structure_6.obstacle       { left: 184; top: 185; width:  69; height:  71; }
#door_o1_clearance.door     { left:   3; top:  71; width:  37; height:  42; }
```
