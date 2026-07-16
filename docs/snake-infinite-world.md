# Architecture: Browser Infinite World Snake

A single-HTML-file snake game with no fixed borders. The world is infinite;
the camera follows the snake head. No server, no build step.

---

## Core idea

The snake lives in **world coordinates** (integer grid cells, unbounded in all
directions). A camera maps a visible window of that grid onto a `<canvas>`.
The world is stored as a sparse set — only occupied cells are tracked — so
"infinite" costs no memory until visited.

---

## Coordinate systems

```
World coords  (wx, wy)   integer grid, origin 0,0, unbounded
Screen coords (sx, sy)   pixels on the canvas

sx = (wx - camera.x) * CELL + canvas.width  / 2
sy = (wy - camera.y) * CELL + canvas.height / 2
```

`camera.x/y` is the snake head position (or a smoothed version of it).
Everything outside the canvas is simply not drawn.

---

## Data model

```
CELL = 20          // px per grid cell
TICK_MS = 120      // ms per snake step

snake: {
  segments: Array<{x, y}>   // [0] = head, last = tail
  dir: {x, y}               // current heading, one of ±{1,0} or {0,±1}
  nextDir: {x, y}           // buffered from keydown (applied on next tick)
  growing: number            // cells still to add before tail shrinks
}

world: {
  food:  Set<string>         // "wx,wy" keys
  body:  Set<string>         // occupied by snake body (for collision)
}

camera: { x, y }             // smoothly interpolated toward head
```

---

## Modules (all in one file, or split by `<script type="module">`)

```
main.js
  init()          wire canvas, start loop, attach input handler
  loop(ts)        requestAnimationFrame callback
    tick(dt)      advance game state every TICK_MS
    render()      draw world onto canvas

snake.js
  step(snake, world)
    1. compute newHead = head + nextDir
    2. check self-collision  → game over
    3. push newHead onto segments
    4. if growing > 0: growing--  else pop tail and remove from body Set
    5. update body Set with newHead
    6. check food at newHead → eat(snake, world, newHead)

food.js
  eat(snake, world, pos)
    remove from food Set, snake.growing += GROW_AMOUNT, score++
  spawnFood(world, camera, count)
    pick `count` random cells in the visible + buffer area not in body

camera.js
  update(camera, head, dt)
    lerp camera toward head  (or snap — simpler)

render.js
  draw(ctx, snake, world, camera)
    compute visible cell range from camera + canvas size
    fill background
    draw food cells in range
    draw snake segments (head distinct colour)
    draw score overlay
```

---

## Game loop

```
let lastTick = 0
let accumulator = 0

function loop(ts) {
  const dt = ts - (lastTs ?? ts)
  lastTs = ts
  accumulator += dt

  while (accumulator >= TICK_MS) {
    tick()
    accumulator -= TICK_MS
  }

  render(accumulator / TICK_MS)   // fractional for smooth camera lerp
  requestAnimationFrame(loop)
}
```

Fixed-timestep tick keeps snake speed frame-rate independent.
The fractional value passed to `render` lets the camera interpolate
between the last and next tick positions without visual stutter.

---

## Infinite world / food spawning

The world has no boundary. Food is spawned lazily:

```
on each tick:
  if food.size < TARGET_FOOD_COUNT:
    spawnFood() — pick random cell within SPAWN_RADIUS of head,
                  not already in body or food
```

`SPAWN_RADIUS` is larger than the visible area (e.g. 3× viewport diagonal)
so food is always visible ahead of the snake. Old food behind the snake is
never removed — it stays in the Set until eaten. The Set stays small because
the snake will eventually eat or pass nearby most of it.

---

## Collision detection

Self-collision: O(1) lookup in `world.body` Set.
No wall collision (infinite world — there are no walls).

```
if world.body.has(`${newHead.x},${newHead.y}`) → game over
```

The tail cell is removed from the Set *before* the collision check so the
snake can "chase its own tail" without false positives.

---

## Input

```
document.addEventListener('keydown', e => {
  const map = {
    ArrowUp:    {x:0,  y:-1},
    ArrowDown:  {x:0,  y:1},
    ArrowLeft:  {x:-1, y:0},
    ArrowRight: {x:1,  y:0},
  }
  const d = map[e.key]
  if (!d) return
  // Ignore 180° reversal
  if (d.x !== -snake.dir.x || d.y !== -snake.dir.y)
    snake.nextDir = d
})
```

`nextDir` is consumed once per tick, not per frame, so rapid key presses
do not cause the snake to reverse into itself.

---

## Rendering notes

- Draw only cells within the visible canvas + 1-cell border. The visible
  range is `[(camera.x - halfW/CELL)..(camera.x + halfW/CELL)]` in x, same
  in y. Iterate over that integer range — no need to loop over the entire Set.
- Draw the grid lines optionally (helps show the infinite space).
- Snake body: one `fillRect` per segment. Head: a different colour.
- Food: a circle (`arc`) or contrasting rect.

---

## File layout (single-file version)

```html
<!-- index.html -->
<canvas id="c"></canvas>
<style>
  body { margin: 0; background: #111; display: flex; }
  canvas { margin: auto; }
</style>
<script>
  // ~250 lines: constants, state, init, loop, tick, render, input
</script>
```

No dependencies, no build, open in any browser.

---

## Extension points (not in MVP)

| Feature | Approach |
|---|---|
| Obstacles | Add `world.walls` Set, spawn near head as world "generates" |
| Speed ramp | Decrease TICK_MS by 1ms every N foods eaten |
| Multiplayer | WebSocket server; each client sends direction, server is authoritative tick |
| Persistent high score | `localStorage.setItem('hi', score)` |
| Mobile controls | Four `<button>` elements overlaid on canvas |
