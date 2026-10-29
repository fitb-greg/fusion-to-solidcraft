# How it works

1. **Read** (`extract.py`, inside Fusion). The timeline is walked one step at a time. Before each
   feature the timeline is rolled back to it, and the feature's inputs are read: profiles, faces,
   extents and parameter expressions. After each step the add-in records every body's volume,
   centre and bounding box, and reads sketches with their points, curves, constraints and
   dimensions. The timeline marker is put back at the end, and the design is never saved.
2. **Translate** (`translate.py`). Each step becomes SolidCraft commands: `sketch.create` on the
   sketch's exact plane, sketch geometry, constraints and dimensions, then `solid.extrude`,
   `solid.shell`, `solid.combine` and so on. Parameters are created first, in dependency order.
3. **Build and check** (`build.py`). The commands run in a headless SolidCraft
   (`solidcraft-cli mcp --headless`). After every step, each body is measured and compared with
   Fusion's numbers (volume and centre of mass).
4. **Fallback.** If a step doesn't map, or its bodies don't match, Fusion's own result for that
   step is exported as STEP and imported in its place. Steps before and after it keep their
   history.
5. **Finish.** Bodies are moved into their components and copies are placed. Appearances,
   materials and visibility are applied. The design is saved, with a `.report.txt` beside it.

## Fusion API notes

These behaviours of the API were found while building this tool. They may help anyone who
reads Fusion timelines.

- A feature's inputs can only be read with the timeline rolled to just before it
  (`rollTo(True)`). Otherwise you get "Didn't roll editing feature back".
- Read sketches with the timeline rolled to just after them; read earlier, their curve
  numbering can change.
- `rollTo` only works on the active document.
- Sketches inside components come as proxies. Compare curves through `.nativeObject`, because
  `entityToken` can differ from call to call.
- `ProfileCurve.sketchEntity` can name the wrong one of two collinear lines. Match profile
  edges to sketch curves by geometry.
- A feature's profile list can be stale after its sketch was edited later. For new and join
  extrudes, read the regions from the feature's start faces.
- STEP export leaves out hidden bodies, and it renames duplicate body names.
- Lengths in the API are in cm.
- A rectangular pattern's direction entity can point opposite to the direction Fusion actually
  uses.
- For a two-sided extrude, the solid lies between the two ends.
