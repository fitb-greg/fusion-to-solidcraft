"""Turn extract.py's JSON into SolidCraft commands.

Each command is {command, params, step, optional}. An optional command (a sketch constraint or
dimension) may be dropped when SolidCraft refuses it; the geometry is drawn where Fusion has it,
so the shape is right without them.
"""

import math
import re

ROOT = None  # set per design: the root component's name


# Fusion's feet-and-inches form: 1'-6 1/2" is 1 ft 6.5 in, not 1 ft minus 6.5 in.
FT_IN = re.compile(r"""(\d+(?:\.\d+)?)\s*'\s*-\s*(\d+(?:\.\d+)?(?:\s+\d+/\d+)?|\d+/\d+)\s*\"""")
NAME_KEYS = {"name", "body", "bodies", "targets", "target", "tools", "component", "new_name", "body_name", "material", "comment"}


def feet_inches(v, key=None):
    if isinstance(v, str):
        return FT_IN.sub(r"""(\1' + \2")""", v) if key not in NAME_KEYS and "'" in v else v
    if isinstance(v, dict):
        return {k: feet_inches(x, k) for k, x in v.items()}
    if isinstance(v, list):
        return [feet_inches(x, key) for x in v]
    return v


def is_zero(e):
    return not e or re.fullmatch(r"\s*[-+]?0*\.?0*\s*(mm|cm|m|in|ft|\"|')?\s*", e) is not None


def norm(v):
    n = math.sqrt(sum(c * c for c in v)) or 1.0
    return [c / n for c in v]


class Translator:
    def __init__(self, data):
        self.data = data
        self.out = []
        self.sketch_names = {}  # sketch uid -> SolidCraft sketch name
        self.notes = []  # (step index, message) for what could not be carried over
        self.step = None
        self.features = {}  # (component, Fusion feature name) -> SolidCraft feature name
        global ROOT
        ROOT = data["document"].split(" v")[0]

    def emit(self, command, params, optional=False, **meta):
        params = feet_inches(params)
        for k in ("alts", "tries"):
            if meta.get(k):
                meta[k] = [feet_inches(a) for a in meta[k]]
        c = {"command": command, "params": params, "step": self.step, "optional": optional}
        c.update(meta)
        self.out.append(c)

    def note(self, msg):
        self.notes.append((self.step, msg))

    # --- whole design -------------------------------------------------------------------------

    def run(self):
        units = self.data.get("units", "mm")
        self.emit("document.units", {"units": units})
        for p in ordered(self.data["parameters"]):
            params = {"name": p["name"], "expression": p["expression"]}
            if p.get("comment"):
                params["comment"] = p["comment"]
            self.emit("parameters.change", params)
        prev = {}
        for s in self.data["steps"]:
            self.step = s["index"]
            after = s.get("after", prev)
            if s.get("suppressed"):
                prev = after
                continue
            if "error" in s:
                self.note("%s: not read from Fusion (%s)" % (s["name"], s["error"]))
            t = s.get("type")
            try:
                if t == "Sketch":
                    self.sketch(s["sketch"])
                elif t == "Occurrence" and s.get("linked"):
                    self.linked(s)
                elif t == "Occurrence":
                    # Bodies moved into a new component (Create Components from Bodies).
                    self.moved_bodies(prev, after)
                elif t in ("Occurrence", "Snapshot", None, "NoneType", "ConstructionPlane", "ConstructionAxis", "ConstructionPoint"):
                    pass
                else:
                    handler = getattr(self, "f_" + t, None)
                    f = s.get("feature", {})
                    self.prev = prev
                    new = [k for k in after if k not in prev]
                    gone = [k for k in prev if k not in after]
                    if handler is None:
                        self.fallback(s, new, after)
                    else:
                        handler(s, f, new, gone, after)
            except Exception as e:  # a translator bug is reported, the rest still runs
                self.note("%s: %s" % (s["name"], e))
            prev = after
        self.finish(prev)
        return self.out

    BUILT_IN = {"Default", "Steel", "Stainless Steel", "Aluminum", "Brass", "Copper", "Titanium", "Cast Iron",
                "ABS Plastic", "PLA", "Nylon", "Polycarbonate", "Oak", "Glass"}

    def looks(self, final, skip):
        """Fusion's physical materials and appearances on the bodies, and faces painted apart."""
        looks = self.data.get("looks", {})
        bodies = [k for k in final if k in looks and k.split("/", 1)[0] not in skip]
        made = set()
        by_material = {}
        for k in bodies:
            m = looks[k].get("material")
            if not m or not m.get("name"):
                continue
            name = m["name"]
            if name not in self.BUILT_IN and name not in made and m.get("density_g_cm3"):
                self.emit("material.create", {"name": name, "density_g_cm3": m["density_g_cm3"]}, optional=True)
                made.add(name)
            by_material.setdefault(name, []).append(k)
        for name, ks in by_material.items():
            self.emit("material.assign", {"bodies": ks, "material": name}, optional=True)
        by_look = {}
        for k in bodies:
            a = looks[k].get("appearance")
            if a:
                by_look.setdefault(json_key(a), (a, []))[1].append(k)
        for a, ks in by_look.values():
            self.emit("appearance.assign", dict(look_params(a), bodies=ks), optional=True)
        for k in bodies:
            for f in looks[k].get("faces", []):
                self.emit("appearance.assign", dict(look_params(f["appearance"]), faces=[f["point"]], body=k), optional=True)

    def moved_bodies(self, prev, after):
        new = [k for k in after if k not in prev]
        gone = [k for k in prev if k not in after]
        for g in gone:
            pv = prev[g]
            for n in new:
                av = after[n]
                if (pv.get("volume") and av.get("volume") and abs(pv["volume"] - av["volume"]) <= 1e-6 * pv["volume"] + 1e-3
                        and pv.get("center") and av.get("center") and math.dist(pv["center"], av["center"]) < 1e-3):
                    self.emit("body.rename", {"body": g, "name": n})
                    new.remove(n)
                    break

    def linked(self, s):
        """A design inserted from another file: linked to that design converted on its own."""
        occ = next((o for o in self.data["occurrences"] if o.get("linked") and o["component"] == s["component"]), None)
        m = occ["matrix"] if occ else s.get("matrix", [1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 0])
        if abs(m[0] - 1) + abs(m[5] - 1) + abs(m[10] - 1) > 1e-9:
            self.note("%s: the linked design is turned in Fusion; it is inserted unturned" % s["name"])
        self.emit("component.insert_design", {"path": {"linked": s["linked"]}, "name": s["component"],
                                              "translate": ["%.9g mm" % (m[i] * 10) for i in (3, 7, 11)]})

    def linked_components(self):
        return {o["component"] for o in self.data["occurrences"] if o.get("linked") or o.get("inside_linked")}

    def finish(self, final):
        # Components: one per Fusion component, and each body moved into its own.
        comps = []
        skip = self.linked_components()
        for o in self.data["occurrences"]:
            if o["component"] not in comps and o["component"] not in skip:
                comps.append(o["component"])
        self.step = None
        for c in comps:
            bodies = [k for k in final if k.split("/", 1)[0] == c]
            self.emit("component.create", {"name": c, "parent": 0, "activate": False})
            if bodies:
                self.emit("component.move_bodies", {"bodies": bodies, "component": c})
        # A component placed more than once: one copy per further placement, moved by its
        # placement relative to the first (whose bodies are already where they are in the world).
        for c in comps:
            occs = [o for o in self.data["occurrences"] if o["component"] == c]
            if len(occs) < 2 or not any(k.split("/", 1)[0] == c for k in final):
                continue
            first = mat4(occs[0]["matrix"])
            inv = invert_rigid(first)
            for o in occs[1:]:
                m = mul(mat4(o["matrix"]), inv)
                flat = [m[i][j] * (10.0 if j == 3 and i < 3 else 1.0) for i in range(3) for j in range(4)]
                self.emit("occurrence.copy", {"component": c}, then_move=[round(v, 9) for v in flat])
        self.looks(final, skip)
        hidden_comps = {o["component"] for o in self.data["occurrences"] if o.get("visible") is False}
        hidden = [k for k, v in final.items() if (v.get("visible") is False or k.split("/", 1)[0] in hidden_comps)
                  and k.split("/", 1)[0] not in skip]
        for o in self.data["occurrences"]:
            if o.get("linked") and o.get("visible") is False:
                self.emit("browser.visibility", {"paths": [["@occurrence:" + o["component"]]], "visible": False}, optional=True)
        if hidden:
            self.emit("browser.visibility", {"items": hidden, "visible": False}, optional=True)
        # Bodies were "component/body" while building; keep Fusion's own name where it's unique.
        plain = [k.split("/", 1)[-1] for k in final]
        for k in final:
            n = k.split("/", 1)[-1]
            if n != k and plain.count(n) == 1 and k.split("/", 1)[0] not in skip:
                self.emit("body.rename", {"body": k, "name": n}, optional=True)

    # --- sketches -----------------------------------------------------------------------------

    def sketch(self, sk):
        placed = {o["component"] for o in self.data["occurrences"]}
        name = "%s (%s)" % (sk["name"], sk["component"]) if sk["component"] in placed else sk["name"]
        if name in self.sketch_names.values():
            name = "%s #%d" % (name, self.step)
        self.sketch_names[sk["uid"]] = name
        fr = sk["frame"]
        self.emit("sketch.create", {"plane": {"origin": fr["origin"], "x_dir": norm(fr["x_dir"]), "y_dir": norm(fr["y_dir"])}, "name": name})
        pts = {p["id"]: p for p in sk["points"]}
        ref = {}  # Fusion point id -> SolidCraft point ref
        used = set()
        for c in sk["curves"]:
            for k in ("a", "b", "center"):
                if c.get(k):
                    used.add(c[k])
        origin = sk["points"][0]["id"] if sk["points"] and sk["points"][0]["at"] == [0.0, 0.0] and sk["points"][0]["fixed"] else None
        if origin:
            ref[origin] = "origin"
        fixed = []
        for c in sk["curves"]:
            cid = c["id"]
            k = c["kind"]
            cons = {"construction": True} if c["construction"] else {}
            if k == "line":
                self.emit("sketch.line", dict(points=[pts[c["a"]]["at"], pts[c["b"]]["at"]], ids=[cid], **cons))
                ref.setdefault(c["a"], cid + ".start")
                ref.setdefault(c["b"], cid + ".end")
            elif k == "circle":
                self.emit("sketch.circle.center", dict(center=pts[c["center"]]["at"], radius=c["r"], id=cid, **cons))
                ref.setdefault(c["center"], cid + ".center")
            elif k == "arc":
                self.emit("sketch.arc.three_point", dict(start=pts[c["a"]]["at"], end=pts[c["b"]]["at"], through=c["mid"], id=cid, **cons))
                ref.setdefault(c["a"], cid + ".start")
                ref.setdefault(c["b"], cid + ".end")
                ref.setdefault(c["center"], cid + ".center")
            elif c.get("samples"):
                s = c["samples"]
                self.emit("sketch.line", dict(points=s, ids=["%s_%d" % (cid, i) for i in range(len(s) - 1)], **cons))
                self.note("%s: %s drawn as %d straight pieces" % (name, k, len(s) - 1))
                continue
            else:
                self.note("%s: %s left out" % (name, k))
                continue
            if c["ref"] or c["fixed"]:
                fixed.append(cid)
        for p in sk["points"]:
            if p["id"] not in used and p["id"] != origin:
                self.emit("sketch.point", {"point": p["at"], "id": p["id"]})
                ref[p["id"]] = p["id"]
                if p["fixed"] or p["ref"]:
                    fixed.append(p["id"])
        self.ref = ref
        self.curve_ids = {c["id"] for c in sk["curves"]}
        if fixed:
            self.emit("sketch.constraint.fix", {"entities": fixed, "fixed": True}, optional=True)
        for g in sk["constraints"]:
            self.constraint(g)
        for d in sk["dimensions"]:
            self.dimension(d)
        self.emit("sketch.finish", {})

    def r(self, x):
        """A Fusion sketch entity id as SolidCraft refers to it."""
        if x is None:
            return None
        if x in self.curve_ids:
            return x
        return self.ref.get(x, x)

    def constraint(self, g):
        k = g["kind"]
        r = self.r
        p = None
        if k in ("Horizontal", "Vertical"):
            mode = k.lower()
            if g.get("line"):
                p = ("sketch.constraint.horizontal_vertical", {"line": r(g["line"]), "mode": mode})
        elif k in ("HorizontalPoints", "VerticalPoints"):
            p = ("sketch.constraint.horizontal_vertical", {"points": [r(g["pointOne"]), r(g["pointTwo"])], "mode": k[:-6].lower()})
        elif k == "Coincident":
            a, b = r(g.get("point")), r(g.get("entity"))
            if a and b and a != b:
                p = ("sketch.constraint.coincident", {"a": a, "b": b})
        elif k in ("Perpendicular", "Parallel", "Collinear"):
            p = ("sketch.constraint." + k.lower(), {"a": r(g.get("lineOne")), "b": r(g.get("lineTwo"))})
        elif k in ("Tangent", "Equal", "Smooth"):
            p = ("sketch.constraint." + ("tangent" if k == "Tangent" else "equal" if k == "Equal" else "curvature"),
                 {"a": r(g.get("curveOne")), "b": r(g.get("curveTwo"))})
        elif k == "Concentric":
            p = ("sketch.constraint.concentric", {"a": r(g.get("entityOne")), "b": r(g.get("entityTwo"))})
        elif k == "MidPoint":
            p = ("sketch.constraint.midpoint", {"point": r(g.get("point")), "line": r(g.get("midPointCurve"))})
        elif k == "Symmetry":
            p = ("sketch.constraint.symmetry", {"a": r(g.get("entityOne")), "b": r(g.get("entityTwo")), "line": r(g.get("symmetryLine"))})
        if p is None or any(v is None for v in p[1].values() if not isinstance(v, list)):
            return
        self.emit(p[0], p[1], optional=True)

    def dimension(self, d):
        if not d.get("driving") or not d.get("expr"):
            return
        r = self.r
        k = d["kind"]
        value = d["expr"]
        params = None
        if k == "Linear":
            t = {0: "distance", 1: "horizontal", 2: "vertical"}.get(d.get("orientation"), "distance")
            params = {"entities": [r(d.get("entityOne")), r(d.get("entityTwo"))], "type": t}
        elif k == "Offset":
            params = {"entities": [r(d.get("line")), r(d.get("entityTwo"))], "type": "distance"}
        elif k == "Diameter":
            params = {"entities": [r(d.get("entity"))], "type": "diameter"}
        elif k == "Radial":
            params = {"entities": [r(d.get("entity"))], "type": "radius"}
        elif k == "Angular":
            params = {"entities": [r(d.get("lineOne")), r(d.get("lineTwo"))], "type": "angle"}
            if d.get("text"):
                params["text_at"] = d["text"]
        if params is None or None in params["entities"]:
            return
        params["value"] = value
        self.emit("sketch.dimension", params, optional=True)

    # --- features -----------------------------------------------------------------------------

    def profiles(self, f, key="profiles"):
        """The sketch and its profiles by their outer curves, and the same profiles picked by a
        point inside each (tried when SolidCraft doesn't find a profile by its curves)."""
        sketch = None
        out, points = [], []
        for p in f.get(key, []):
            if "sketch" in p:
                sketch = self.sketch_names.get(p["sketch"], p["sketch"])
                outer = [lo for lo in p["loops"] if lo["outer"]]
                if outer:
                    out.append({"curves": outer[0]["curves"]})
                points.append({"point": p["point"]} if p.get("point") else out[-1] if outer else None)
        return sketch, out, points

    def match_info(self, f):
        """Each profile's area and centre, for finding it among SolidCraft's profiles."""
        out = [{"area": p.get("area"), "centroid": p.get("centroid")} for p in f.get("profiles", []) if "sketch" in p]
        return out if all(m["area"] and m["centroid"] for m in out) else None

    def alt_points(self, params, points):
        """The same command with its profiles picked by points."""
        if points and None not in points and points != params.get("profiles"):
            return dict(params, profiles=points, exact_regions=True)
        return None

    def f_ExtrudeFeature(self, s, f, new, gone, after):
        sketch, profs, points = self.profiles(f)
        faces = [p["face"] for p in f.get("profiles", []) if "face" in p]
        params = {"name": self.fname(s)}
        if profs:
            params.update(sketch=sketch, profiles=profs)
        elif faces:
            params["faces" if len(faces) > 1 else "face"] = faces if len(faces) > 1 else faces[0]
        else:
            return self.fallback(s, new, after)
        one = f["one"]
        if f.get("symmetric"):
            params["direction"] = "symmetric"
            params["distance"] = one["expr"] if one.get("whole") else "2 * (%s)" % one["expr"]
        elif one["type"] == "DistanceExtentDefinition":
            params["distance"] = one["expr"]
        elif one["type"] == "ToEntityExtentDefinition" and one.get("to") and not f.get("two_sided"):
            params["to"] = one["to"]
            if not is_zero(one.get("to_offset")):
                params["to_offset"] = one["to_offset"]
        elif one["type"] == "ThroughAllExtentDefinition":
            params["through_all"] = True
            if one.get("positive") is False:
                params["direction"] = "negative"
        else:
            return self.fallback(s, new, after)
        if f.get("two_sided"):
            two = f["two"]
            if two["type"] != "DistanceExtentDefinition":
                return self.fallback(s, new, after)
            if (one.get("value") or 0) < 0 and not f.get("start_offset"):
                # Fusion's solid lies between the two ends: side one at d1 along the normal,
                # side two at d2 against it. With d1 negative both ends are below the sketch,
                # which SolidCraft's two sides (always opposite ways) can't say: start at side
                # two's end and go to side one's.
                params["start_offset"] = "-(%s)" % two["expr"]
                params["distance"] = "(%s) + (%s)" % (one["expr"], two["expr"])
            else:
                params["distance2"] = two["expr"]
        if f.get("taper"):
            params["taper"] = f["taper"]
        if f.get("start_offset"):
            params["start_offset"] = f["start_offset"]
        elif f.get("from") and is_zero(f.get("from_offset")) and "to" in params:
            params["from"] = f["from"]
        elif f.get("start"):
            return self.fallback(s, new, after)
        op = f["operation"]
        params["operation"] = "new" if op in ("new", "new_component") else op
        if op in ("join", "cut", "intersect") and f.get("participants"):
            params["targets"] = f["participants"]
        elif op in ("join", "cut", "intersect"):
            targets = [b for b in f.get("bodies", []) if b not in new]
            if targets:
                params["targets"] = targets
        self.emit("solid.extrude", params, new=new, alts=[self.alt_points(params, points)] if profs else None,
                  match=self.match_info(f) if profs else None)

    def f_CombineFeature(self, s, f, new, gone, after):
        self.emit("solid.combine", {"target": f["target"], "tools": f["tools"], "operation": f["operation"],
                                    "keep_tools": f["keep_tools"]}, new=new)

    def by_body(self, f):
        """The feature's faces grouped by their body, in order."""
        groups = {}
        for p, b in zip(f["faces"], f.get("face_bodies") or [f.get("body")] * len(f["faces"])):
            groups.setdefault(b, []).append(p)
        return groups

    def f_ShellFeature(self, s, f, new, gone, after):
        if not f.get("faces"):
            return self.fallback(s, new, after)
        for body, faces in self.by_body(f).items():
            self.shell(dict(f, faces=faces, body=body), new)

    def shell(self, f, new):
        if f.get("inside") and f.get("outside"):
            params = {"thickness": f["inside"], "direction": "both"}
        elif f.get("outside"):
            params = {"thickness": f["outside"], "direction": "outside"}
        else:
            params = {"thickness": f["inside"], "direction": "inside"}
        params.update(faces=f["faces"], tangent_chain=f.get("tangent_chain", False))
        if f.get("body"):
            params["body"] = f["body"]
        self.emit("solid.shell", params, new=new)

    def f_OffsetFacesFeature(self, s, f, new, gone, after):
        for body, faces in self.by_body(f).items():
            params = {"faces": faces, "distance": f["distance"]}
            if body:
                params["body"] = body
            self.emit("solid.offset_face", params, new=new)

    def f_CopyPasteBody(self, s, f, new, gone, after):
        self.emit("solid.move", {"bodies": f["sources"], "translate": [0, 0, 0], "copy": True}, new=new)

    def f_CutPasteBody(self, s, f, new, gone, after):
        # Moves bodies to another component; components are sorted out at the end.
        self.rename_pairs(f["sources"], new)

    def rename_pairs(self, old, new):
        for a, b in zip(old, new):
            self.emit("body.rename", {"body": a, "name": b})

    def f_MoveFeature(self, s, f, new, gone, after):
        self.move(f["bodies_in"], f["matrix"], new)

    def f_Joint(self, s, f, new, gone, after):
        """A joint moves its components: their bodies move by the change in placement."""
        before, now = s.get("placements_before"), s.get("placements_after")
        if before is None or now is None:
            return self.fallback(s, new, after)
        moved = False
        for comp, m1 in now.items():
            m0 = before.get(comp)
            if m0 is None or max(abs(a - b) for a, b in zip(m0, m1)) < 1e-9:
                continue
            bodies = [k for k in self.prev if k.split("/", 1)[0] == comp]
            if bodies:
                d = mul(mat4(m1), invert_rigid(mat4(m0)))
                self.move(bodies, [v for row in d for v in row], new)
                moved = True
        if not moved and any(k in after and self.prev.get(k, {}).get("center") != after[k].get("center") for k in self.prev):
            return self.fallback(s, new, after)

    f_AsBuiltJoint = f_Joint
    f_RigidGroup = f_Joint

    def move(self, bodies, m, new):
        """Move bodies by a row-major 4x4 (translation in cm)."""
        R = [[m[0], m[1], m[2]], [m[4], m[5], m[6]], [m[8], m[9], m[10]]]
        t = [m[3] * 10, m[7] * 10, m[11] * 10]
        params = {"bodies": bodies}
        # Rotation about the world origin, then the move.
        tr = R[0][0] + R[1][1] + R[2][2]
        ang = math.acos(max(-1.0, min(1.0, (tr - 1) / 2)))
        if ang > 1e-9:
            if abs(ang - math.pi) < 1e-6:
                # Half a turn: the axis is any non-zero column of R + I.
                j = max(range(3), key=lambda i: R[i][i])
                axis = norm([R[i][j] + (1.0 if i == j else 0.0) for i in range(3)])
            else:
                axis = norm([R[2][1] - R[1][2], R[0][2] - R[2][0], R[1][0] - R[0][1]])
            params.update(axis=axis, angle="%.9f deg" % math.degrees(ang))
            # solid.move turns about the origin before moving: the move stays as Fusion gives it.
        params["translate"] = ["%.9g mm" % v for v in t]  # a bare number would be in the design's units
        self.emit("solid.move", params, new=new)

    def f_ScaleFeature(self, s, f, new, gone, after):
        params = {"bodies": f["bodies_in"]}
        if f.get("uniform", True):
            params["factor"] = f.get("factor") or "1"
        else:
            params["factors"] = f["factors"]
        if f.get("origin"):
            params["origin"] = f["origin"]
        self.emit("solid.scale", params, new=new)

    def f_RemoveFeature(self, s, f, new, gone, after):
        # A removed body keeps its name in SolidCraft's timeline, and Fusion reuses names:
        # free it first.
        names = []
        for k in f.get("bodies_in", []):
            names.append("%s (removed at step %d)" % (k, s["index"]))
            self.emit("body.rename", {"body": k, "name": names[-1]})
        if names:
            self.emit("solid.remove", {"bodies": names})

    def f_SplitBodyFeature(self, s, f, new, gone, after):
        for body in f["bodies_in"]:
            if f.get("sketch"):
                params = {"body": body, "sketch": self.sketch_names.get(f["sketch"], f["sketch"]), "line": f["line"]}
            elif f.get("plane"):
                params = {"body": body, "plane": f["plane"]}
            elif f.get("face"):
                params = {"body": body, "tool": f["face"]}
            else:
                return self.fallback(s, new, after)
            self.emit("solid.split_body", params, new=new, renames=[body] + list(new))

    def f_RectangularPatternFeature(self, s, f, new, gone, after):
        bodies = [b for b in f.get("inputs", []) if isinstance(b, str)]
        if not bodies or len(bodies) != len(f.get("inputs", [])) or not isinstance(f.get("dir1"), list):
            return self.fallback(s, new, after)
        params = {"bodies": bodies}
        for n in ("1", "2"):
            d = f.get("dir" + n)
            count = f.get("count" + n) or "1"
            if n == "2" and (not isinstance(d, list) or count.strip() == "1"):
                continue
            dist = f["distance" + n]
            if dist.strip().startswith("-"):
                dist = "-(%s)" % dist
            # Which way the copies go: from where Fusion put the nearest one (the direction
            # entity's own sense isn't what the API reports).
            if n == "1" and len(bodies) == 1 and new:
                src = getattr(self, "prev", {}).get(bodies[0], {}).get("center")
                cs = [after[k]["center"] for k in new if after.get(k, {}).get("center")]
                if src and cs:
                    near = min(cs, key=lambda c: math.dist(c, src))
                    v = [a - b for a, b in zip(near, src)]
                    if math.sqrt(sum(x * x for x in v)) > 1e-6:
                        d = v
            params["dir" + n] = norm(d)
            params["count" + n] = int(float(count)) if count.replace(".", "").isdigit() else count
            params["spacing" + n] = dist if f.get("spacing") else "(%s) / (%s - 1)" % (dist, count)
            if f.get("symmetric" + n):
                params["alignment" + ("" if n == "1" else "2")] = "center"
        self.emit("solid.pattern.rectangular", params, new=new)

    def f_SweepFeature(self, s, f, new, gone, after):
        sketch, profs, points = self.profiles(f)
        if not profs or not f.get("path_sketch") or None in f["path"]:
            return self.fallback(s, new, after)
        params = {"sketch": sketch, "profiles": profs, "path_sketch": self.sketch_names.get(f["path_sketch"], f["path_sketch"]),
                  "path": f["path"], "operation": "new" if f["operation"].startswith("new") else f["operation"], "name": self.fname(s)}
        if f["operation"] in ("join", "cut", "intersect") and f.get("participants"):
            params["targets"] = f["participants"]
        rev = dict(params, path=list(reversed(params["path"])))
        self.emit("solid.sweep", params, new=new, alts=[rev, self.alt_points(params, points), self.alt_points(rev, points)],
                  match=self.match_info(f))

    # --- features added for blends, mirrors, revolves, patterns and lofts --------------------

    def fname(self, s):
        """A SolidCraft feature name for this step, unique even where components reuse names."""
        name = s["name"]
        if name in self.features.values():
            name = "%s (%s)" % (name, s.get("component") or "")
        if name in self.features.values():
            name = "%s #%d" % (name, s["index"])
        self.features[(s.get("component"), s["name"])] = name
        return name

    def delta(self, after):
        """How the step changes the total volume and its moment, for checking alternatives."""
        def tot(b):
            v, m = 0.0, [0.0, 0.0, 0.0]
            for x in b.values():
                if x.get("volume") and x.get("center"):
                    v += x["volume"]
                    m = [a + x["volume"] * c for a, c in zip(m, x["center"])]
            return v, m
        v0, m0 = tot(getattr(self, "prev", {}))
        v1, m1 = tot(after)
        return [v1 - v0] + [a - b for a, b in zip(m1, m0)]

    def targets(self, f, new):
        if f["operation"] in ("join", "cut", "intersect"):
            t = f.get("participants") or [b for b in f.get("bodies", []) if b not in new]
            return {"targets": t} if t else {}
        return {}

    def f_FilletFeature(self, s, f, new, gone, after):
        self.blend(s, f, new, after, "solid.fillet")

    def f_ChamferFeature(self, s, f, new, gone, after):
        self.blend(s, f, new, after, "solid.chamfer")

    def blend(self, s, f, new, after, command):
        cmds = []
        for es in f["sets"]:
            if es.get("other") or not es["edges"]:
                return self.fallback(s, new, after)
            k = es["kind"]
            if k == "ConstantRadiusFilletEdgeSet":
                size = {"radius": es["radius"]}
            elif k == "ChordLengthFilletEdgeSet":
                size = {"radius": es.get("chordLength") or es.get("radius"), "type": "chord"}
            elif k == "EqualDistanceChamferEdgeSet":
                size = {"distance": es["distance"]}
            elif k == "TwoDistancesChamferEdgeSet":
                size = {"distance": es["distanceOne"], "distance2": es["distanceTwo"]}
            elif k == "DistanceAndAngleChamferEdgeSet":
                size = {"distance": es["distance"], "angle": es["angle"]}
            else:
                return self.fallback(s, new, after)
            if not all(size.values()):
                return self.fallback(s, new, after)
            groups = {}
            for p, b in zip(es["edges"], es["edge_bodies"]):
                groups.setdefault(b, []).append(p)
            for body, edges in groups.items():
                cmds.append(dict(size, edges=edges, body=body))
        one = len(cmds) == 1
        base = self.fname(s)
        for i, params in enumerate(cmds):
            params["name"] = base if one else "%s (%d)" % (base, i + 1)
            tries = []
            if "distance2" in params or "angle" in params:
                tries.append(dict(params, flip=True))
            self.emit(command, params, new=new, tries=tries if one else None, delta=self.delta(after) if one else None)

    def inputs(self, f):
        """A pattern's or mirror's inputs as SolidCraft parameters, or None."""
        ins = f.get("inputs") or []
        if ins and all(isinstance(x, str) for x in ins):
            return {"bodies": ins}
        if ins and all(isinstance(x, dict) and x.get("feature") for x in ins):
            names = [self.features.get((x.get("component"), x["feature"])) for x in ins]
            if None not in names:
                return {"features": names}
        return None

    def f_MirrorFeature(self, s, f, new, gone, after):
        ins = self.inputs(f)
        pl = f.get("plane") or {}
        if not ins or "origin" not in pl:
            return self.fallback(s, new, after)
        params = dict(ins, plane={"origin": pl["origin"], "normal": pl["normal"]}, name=self.fname(s))
        if "bodies" in ins and f.get("combine"):
            params["combine"] = True
        alts = None
        if "features" in ins and f.get("bodies") and not new:
            # A symmetric part: the whole body mirrored onto itself is the same shape, and
            # SolidCraft refuses some feature mirrors whose copy touches the original.
            alts = [dict(params, features=None, bodies=f["bodies"], combine=True)]
            del alts[0]["features"]
        self.emit("solid.mirror", params, new=new, alts=alts)

    def f_CircularPatternFeature(self, s, f, new, gone, after):
        ins = self.inputs(f)
        ax = f.get("axis") or {}
        if not ins or "dir" not in ax or f.get("suppressed"):
            return self.fallback(s, new, after)
        count = f["count"]
        params = dict(ins, axis={"origin": ax["origin"], "dir": ax["dir"]},
                      count=int(float(count)) if count.replace(".", "").isdigit() else count,
                      angle=f.get("angle") or "360 deg", name=self.fname(s))
        if f.get("symmetric"):
            params["alignment"] = "center"
        rev = dict(params, axis={"origin": ax["origin"], "dir": [-c for c in ax["dir"]]})
        self.emit("solid.pattern.circular", params, new=new, tries=[rev], delta=self.delta(after))

    def f_RevolveFeature(self, s, f, new, gone, after):
        sketch, profs, points = self.profiles(f)
        ax = f.get("axis") or {}
        if not profs or f.get("other_profiles") or "dir" not in ax:
            return self.fallback(s, new, after)
        params = {"sketch": sketch, "profiles": profs, "axis": {"origin": ax["origin"], "dir": ax["dir"]}, "name": self.fname(s)}
        if f["extent"] == "AngleExtentDefinition":
            params["angle"] = f["angle"]
            if f.get("symmetric"):
                params["direction"] = "symmetric"
        elif f["extent"] == "TwoSidesAngleExtentDefinition":
            params.update(angle=f["angle"], angle2=f["angle2"])
        else:
            return self.fallback(s, new, after)
        params["operation"] = "new" if f["operation"].startswith("new") else f["operation"]
        params.update(self.targets(f, new))
        full = abs(abs(f.get("angle_value") or 0) - 360) < 1e-6 or f.get("symmetric")
        rev = None if full else dict(params, axis={"origin": ax["origin"], "dir": [-c for c in ax["dir"]]})
        self.emit("solid.revolve", params, new=new, alts=[self.alt_points(params, points)],
                  match=self.match_info(f), tries=[rev] if rev else None, delta=self.delta(after))

    def f_LoftFeature(self, s, f, new, gone, after):
        if f.get("rails") or f.get("closed") or not f.get("solid", True):
            return self.fallback(s, new, after)
        sections = []
        holes = {sum(1 for lo in sec.get("loops", []) if not lo["outer"]) for sec in f["sections"]}
        if len(holes) > 1:
            # SolidCraft needs the same number of holes in every section, even filled ones.
            return self.fallback(s, new, after)
        for sec in f["sections"]:
            if "sketch" not in sec:
                return self.fallback(s, new, after)
            if any(None in lo["curves"] for lo in sec["loops"]) or not any(lo["outer"] for lo in sec["loops"]):
                return self.fallback(s, new, after)
            # Fusion lofts the outer loop only; SolidCraft makes an inner loop a hole unless the
            # region inside it is given too.
            profs = [{"curves": lo["curves"]} for lo in sorted(sec["loops"], key=lambda lo: not lo["outer"])]
            sections.append({"sketch": self.sketch_names.get(sec["sketch"], sec["sketch"]), "profiles": profs})
        params = {"sections": sections, "operation": "new" if f["operation"].startswith("new") else f["operation"], "name": self.fname(s)}
        params.update(self.targets(f, new))
        self.emit("solid.loft", params, new=new)

    def fallback(self, s, new, after):
        """A step SolidCraft has no command for here: its result comes in as a solid."""
        changed = new + [k for k in s.get("feature", {}).get("bodies", []) if k not in new]
        self.emit("fusion.fallback", {"bodies": changed}, step_name=s["name"], kind=s.get("type"))
        self.note("%s (%s): carried over as a solid, without its history" % (s["name"], s.get("type")))


def mat4(a):
    return [a[0:4], a[4:8], a[8:12], a[12:16]]


def mul(a, b):
    return [[sum(a[i][k] * b[k][j] for k in range(4)) for j in range(4)] for i in range(4)]


def invert_rigid(m):
    r = [[m[j][i] for j in range(3)] for i in range(3)]
    t = [-sum(r[i][k] * m[k][3] for k in range(3)) for i in range(3)]
    return [r[0] + [t[0]], r[1] + [t[1]], r[2] + [t[2]], [0, 0, 0, 1]]


def json_key(d):
    import json
    return json.dumps(d, sort_keys=True)


def look_params(a):
    p = {"color": a["color"], "name": a["name"]}
    if a.get("opacity") is not None:
        p["opacity"] = a["opacity"]
    elif a.get("metallic") is not None:
        p["pbr"] = {"metallic": a["metallic"], "roughness": a["roughness"], "dielectric_ior": 1.5}
    return p


def ordered(params):
    """Parameters with each after those its expression uses."""
    import re
    names = {p["name"] for p in params}
    uses = {p["name"]: set(re.findall(r"[A-Za-z_][A-Za-z0-9_]*", p["expression"])) & names - {p["name"]} for p in params}
    out, done = [], set()
    while len(out) < len(params):
        ready = [p for p in params if p["name"] not in done and uses[p["name"]] <= done]
        if not ready:  # a cycle: the rest as they come
            ready = [p for p in params if p["name"] not in done]
        for p in ready:
            out.append(p)
            done.add(p["name"])
    return out


def translate(data):
    t = Translator(data)
    return t.run(), t.notes
