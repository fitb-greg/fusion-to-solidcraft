"""Read a Fusion design's timeline into plain JSON (runs inside Fusion).

Lengths are millimetres, points world coordinates unless named `local`. Expressions are
Fusion's own text (parameters and units), which SolidCraft's expressions also read.

extract(design, start, stop) -> dict with the steps [start, stop); the timeline marker is
put back where it was.
"""

import adsk.core
import adsk.fusion
import math
import traceback

CM = 10.0  # Fusion's internal unit is the centimetre


def items(c):
    """The members of a Fusion collection, vector or single object."""
    if c is None:
        return []
    if hasattr(c, "count") and hasattr(c, "item"):
        return [c.item(i) for i in range(c.count)]
    try:
        return list(c)
    except TypeError:
        return [c]


def mm(v):
    return round(v * CM, 6)


def pt3(p):
    return [mm(p.x), mm(p.y), mm(p.z)]


def vec(v):
    return [round(v.x, 9), round(v.y, 9), round(v.z, 9)]


def occ_transform(design, comp):
    """The world transform of a component's (first) occurrence; identity for the root."""
    if comp == design.rootComponent:
        return adsk.core.Matrix3D.create()
    occs = design.rootComponent.allOccurrencesByComponent(comp)
    if occs.count == 0:
        return adsk.core.Matrix3D.create()
    return occs.item(0).transform2


def comp_path(design, comp):
    if comp == design.rootComponent:
        return None
    return comp.name


def body_key(body):
    """A body's name, unique across the design: component/body."""
    native = body.nativeObject or body
    return "%s/%s" % (native.parentComponent.name, native.name)


def world_point(design, comp, p):
    q = p.copy()
    q.transformBy(occ_transform(design, comp))
    return pt3(q)


def face_point(design, face):
    native = face.nativeObject or face
    comp = native.body.parentComponent
    return world_point(design, comp, native.pointOnFace)


def expr(param):
    try:
        return param.expression
    except Exception:
        return None


# --- sketches ---------------------------------------------------------------------------------

def sketch_frame(design, sk):
    m = sk.transform.copy()
    m.transformBy(occ_transform(design, sk.parentComponent))
    o, x, y, z = m.getAsCoordinateSystem()
    return {"origin": pt3(o), "x_dir": vec(x), "y_dir": vec(y)}


def lp(p):
    g = p.geometry
    return [mm(g.x), mm(g.y)]


def read_sketch(design, sk):
    ids = {}  # entityToken -> our id

    def tok(e):
        return e.entityToken

    points = []
    for p in sk.sketchPoints:
        pid = "P%d" % len(points)
        ids[tok(p)] = pid
        points.append({"id": pid, "at": lp(p), "fixed": p.isFixed, "ref": p.isReference})
    curves = []
    for c in sk.sketchCurves:
        cid = "C%d" % len(curves)
        ids[tok(c)] = cid
        d = {"id": cid, "construction": c.isConstruction, "ref": c.isReference, "fixed": c.isFixed}
        if isinstance(c, adsk.fusion.SketchLine):
            d.update(kind="line", a=ids.get(tok(c.startSketchPoint)), b=ids.get(tok(c.endSketchPoint)))
        elif isinstance(c, adsk.fusion.SketchCircle):
            d.update(kind="circle", center=ids.get(tok(c.centerSketchPoint)), r=mm(c.radius))
        elif isinstance(c, adsk.fusion.SketchArc):
            d.update(kind="arc", center=ids.get(tok(c.centerSketchPoint)), a=ids.get(tok(c.startSketchPoint)),
                     b=ids.get(tok(c.endSketchPoint)), r=mm(c.radius))
            ev = c.geometry.evaluator
            _, t0, t1 = ev.getParameterExtents()
            _, m = ev.getPointAtParameter(0.5 * (t0 + t1))
            d["mid"] = [mm(m.x), mm(m.y)]
        else:
            d.update(kind=type(c).__name__)
            try:
                ev = c.geometry.evaluator
                _, pa, pb = ev.getParameterExtents()
                d["samples"] = [[mm(q.x), mm(q.y)] for q in ev.getStrokes(pa, pb, 0.001)[1]][:500]
            except Exception:
                pass
        curves.append(d)
    cons = []
    for g in sk.geometricConstraints:
        d = {"kind": type(g).__name__.replace("Constraint", "")}
        for attr in ("point", "pointOne", "pointTwo", "line", "lineOne", "lineTwo", "curveOne", "curveTwo",
                     "entityOne", "entityTwo", "entity", "circleOne", "circleTwo", "midPointCurve", "symmetryLine"):
            try:
                v = getattr(g, attr)
            except Exception:
                continue
            if v is not None:
                d[attr] = ids.get(tok(v))
        cons.append(d)
    dims = []
    for dm in sk.sketchDimensions:
        d = {"kind": type(dm).__name__.replace("Sketch", "").replace("Dimension", ""),
             "expr": expr(dm.parameter) if dm.parameter else None, "driving": dm.isDriving,
             "value": None}
        try:
            d["value"] = dm.parameter.value if dm.parameter else None
        except Exception:
            pass
        for attr in ("entityOne", "entityTwo", "entity", "line", "lineOne", "lineTwo", "pointOne", "pointTwo"):
            try:
                v = getattr(dm, attr)
            except Exception:
                continue
            if v is not None:
                d[attr] = ids.get(tok(v))
        try:
            d["orientation"] = dm.orientation
        except Exception:
            pass
        try:
            t = dm.textPosition
            d["text"] = [mm(t.x), mm(t.y)]
        except Exception:
            pass
        dims.append(d)
    profiles = [profile_ref(pr) for pr in sk.profiles]
    return {"name": sk.name, "frame": sketch_frame(design, sk), "points": points, "curves": curves,
            "constraints": cons, "dimensions": dims, "profiles": profiles, "ids": ids}


def curve_index(sk, entity):
    """The curve's position in its sketch's curve list (entity tokens differ from call to call)."""
    sk = sk.nativeObject or sk  # a sketch in an occurrence comes as a proxy; its curves too
    entity = entity.nativeObject or entity
    for i, c in enumerate(sk.sketchCurves):
        if c == entity:
            return i
    return None


def sketch_uid(sk):
    sk = sk.nativeObject or sk
    return "%s/%s#%d" % (sk.parentComponent.name, sk.name, sk.timelineObject.index)


def loop_polygon(lo):
    """The loop as a polygon: each curve's strokes, turned to carry on from the one before
    (curves in a loop don't all run the same way)."""
    runs = []
    for pc in lo.profileCurves:
        try:
            ev = pc.geometry.evaluator
            _, a, b = ev.getParameterExtents()
            _, ps = ev.getStrokes(a, b, 0.001)
            if ps:
                runs.append([(q.x, q.y) for q in ps])
        except Exception:
            pass
    if len(runs) > 1:
        a, b = runs[0], runs[1]
        if min(_d2(a[0], b[0]), _d2(a[0], b[-1])) < min(_d2(a[-1], b[0]), _d2(a[-1], b[-1])):
            runs[0] = a[::-1]
    pts = []
    for r in runs:
        if pts and _d2(pts[-1], r[-1]) < _d2(pts[-1], r[0]):
            r = r[::-1]
        pts += r
    return pts


def _d2(p, q):
    return (p[0] - q[0]) ** 2 + (p[1] - q[1]) ** 2


def inside(poly, x, y):
    c = False
    n = len(poly)
    for i in range(n):
        x1, y1 = poly[i]
        x2, y2 = poly[(i + 1) % n]
        if (y1 > y) != (y2 > y) and x < x1 + (y - y1) * (x2 - x1) / (y2 - y1):
            c = not c
    return c


def interior_point(pr):
    """A point inside the profile (in its outer loop, outside its holes), sketch coordinates in mm."""
    outer, holes = None, []
    for lo in pr.profileLoops:
        poly = loop_polygon(lo)
        if lo.isOuter:
            outer = poly
        else:
            holes.append(poly)
    if not outer or len(outer) < 3:
        return None

    def ok(x, y):
        return inside(outer, x, y) and not any(len(h) > 2 and inside(h, x, y) for h in holes)

    try:
        c = pr.areaProperties().centroid
        if ok(c.x, c.y):
            return [mm(c.x), mm(c.y)]
    except Exception:
        pass
    xs = [p[0] for p in outer]
    ys = [p[1] for p in outer]
    # Scan lines across the profile: the middle of the widest inside run.
    best = None
    for k in range(1, 40):
        y = min(ys) + (max(ys) - min(ys)) * k / 40.0
        xs_cross = []
        for poly in [outer] + holes:
            n = len(poly)
            for i in range(n):
                x1, y1 = poly[i]
                x2, y2 = poly[(i + 1) % n]
                if (y1 > y) != (y2 > y):
                    xs_cross.append(x1 + (y - y1) * (x2 - x1) / (y2 - y1))
        xs_cross.sort()
        for a, b in zip(xs_cross[::2], xs_cross[1::2]):
            if best is None or b - a > best[0]:
                if ok((a + b) / 2, y):
                    best = (b - a, (a + b) / 2, y)
    return [mm(best[1]), mm(best[2])] if best else None


def curve_under(sk, pc):
    """The sketch curve a profile edge lies on, found by its middle point: Fusion's own answer
    (sketchEntity) can name a collinear neighbour."""
    try:
        ev = pc.geometry.evaluator
        _, t0, t1 = ev.getParameterExtents()
        _, m = ev.getPointAtParameter(0.5 * (t0 + t1))
    except Exception:
        return curve_index(sk, pc.sketchEntity)
    native = sk.nativeObject or sk
    best, best_d = None, 1e-4  # cm
    for i, c in enumerate(native.sketchCurves):
        try:
            cev = c.geometry.evaluator
            ok, t = cev.getParameterAtPoint(m)
            if not ok:
                continue
            _, a, b = cev.getParameterExtents()
            if not (min(a, b) - 1e-9 <= t <= max(a, b) + 1e-9):
                continue
            _, q = cev.getPointAtParameter(t)
            d = q.distanceTo(m)
            if d < best_d:
                best, best_d = i, d
        except Exception:
            continue
    return best if best is not None else curve_index(sk, pc.sketchEntity)


def profile_info(p):
    d = {"sketch": sketch_uid(p.parentSketch), "loops": profile_ref(p), "point": interior_point(p)}
    try:
        a = p.areaProperties()
        d["area"] = a.area * 100.0  # cm2 -> mm2
        d["centroid"] = [mm(a.centroid.x), mm(a.centroid.y)]
    except Exception:
        pass
    return d


def profile_ref(pr):
    """A profile as the curve ids of each of its loops, and a point inside it."""
    sk = pr.parentSketch
    out = []
    for lo in pr.profileLoops:
        idx = [curve_under(sk, pc) for pc in lo.profileCurves]
        out.append({"outer": lo.isOuter, "curves": ["C%d" % i if i is not None else None for i in idx]})
    return out


# --- features ---------------------------------------------------------------------------------

def entity_point(design, ent):
    """A world point on a face, vertex, sketch point, construction plane or point, for To/From
    Object extents."""
    try:
        native = ent.nativeObject or ent
        if isinstance(native, adsk.fusion.BRepFace):
            return face_point(design, ent)
        if isinstance(native, adsk.fusion.BRepVertex):
            return world_point(design, native.body.parentComponent, native.geometry)
        if isinstance(native, adsk.fusion.SketchPoint):
            return world_point(design, native.parentSketch.parentComponent, native.worldGeometry)
        if isinstance(native, adsk.fusion.ConstructionPlane):
            return world_point(design, native.component, native.geometry.origin)
        if isinstance(native, adsk.fusion.ConstructionPoint):
            return world_point(design, native.component, native.geometry)
    except Exception:
        pass
    return None


def extent(e, design=None):
    t = type(e).__name__
    d = {"type": t}
    if t == "ToEntityExtentDefinition" and design is not None:
        d["to"] = entity_point(design, e.entity)
        d["to_kind"] = type(e.entity).__name__
        try:
            d["to_offset"] = expr(e.offset)
        except Exception:
            pass
    if hasattr(e, "distance"):
        try:
            d["expr"] = e.distance.expression
            d["value"] = mm(e.distance.value)
        except Exception:
            pass
    if hasattr(e, "isPositiveDirection"):
        try:
            d["positive"] = e.isPositiveDirection
        except Exception:
            pass
    return d


def profiles_on_faces(f, profs):
    """The regions of the feature's sketch whose inside point lies on the faces at the start of
    the extrude (with the feature computed), or None when that can't tell."""
    skuids = {p.get("sketch") for p in profs if "sketch" in p}
    if len(skuids) != 1:
        return None
    first = next(p for p in items(f.profile) if isinstance(p, adsk.fusion.Profile))
    sk = first.parentSketch.nativeObject or first.parentSketch
    tl = f.timelineObject
    tl.rollTo(False)
    try:
        faces = [x.nativeObject or x for x in items(f.startFaces)]
        if not faces:
            return None
        planes = [(x, x.geometry) for x in faces if isinstance(x.geometry, adsk.core.Plane)]
        used = []
        for pr in sk.profiles:
            ip = interior_point(pr)
            if ip is None:
                continue
            q = sk.sketchToModelSpace(adsk.core.Point3D.create(ip[0] / CM, ip[1] / CM, 0))
            on = False
            for face, pl in planes:
                n = pl.normal
                v = adsk.core.Vector3D.create(q.x - pl.origin.x, q.y - pl.origin.y, q.z - pl.origin.z)
                dd = v.dotProduct(n)
                p2 = adsk.core.Point3D.create(q.x - n.x * dd, q.y - n.y * dd, q.z - n.z * dd)
                ok, prm = face.evaluator.getParameterAtPoint(p2)
                if ok and face.evaluator.isParameterOnFace(prm):
                    on = True
                    break
            if on:
                used.append(profile_info(pr))
        return used or None
    finally:
        tl.rollTo(True)


def read_extrude(design, f, sketches):
    d = {"operation": ["join", "cut", "intersect", "new", "new_component"][f.operation]}
    d["two_sided"] = f.hasTwoExtents
    d["one"] = extent(f.extentOne, design)
    if f.hasTwoExtents:
        d["two"] = extent(f.extentTwo, design)
    try:
        d["symmetric"] = isinstance(f.extentOne, adsk.fusion.SymmetricExtentDefinition)
        if d["symmetric"]:
            d["one"]["expr"] = f.extentOne.distance.expression
            d["one"]["whole"] = f.extentOne.isFullLength
    except Exception:
        pass
    try:
        if f.taperAngleOne and abs(f.taperAngleOne.value) > 1e-12:
            d["taper"] = f.taperAngleOne.expression
    except Exception:
        pass
    st = f.startExtent
    if isinstance(st, adsk.fusion.OffsetStartDefinition):
        d["start_offset"] = st.offset.expression
    elif isinstance(st, adsk.fusion.FromEntityStartDefinition):
        d["start"] = type(st).__name__
        try:
            d["from"] = entity_point(design, st.entity)
        except Exception:
            # Fusion can't give the entity back for some starts (a face of a base feature).
            d["from"] = None
        try:
            d["from_offset"] = expr(st.offset)
        except Exception:
            pass
    elif not isinstance(st, adsk.fusion.ProfilePlaneStartDefinition):
        d["start"] = type(st).__name__
    profs = []
    try:
        chosen = items(f.profile)
    except Exception:
        # A lost profile reference: Fusion keeps the feature's cached result.
        d["broken"] = (getattr(f, "errorOrWarningMessage", "") or "profile reference lost").split("<")[0].strip()
        chosen = []
    for p in chosen:
        if isinstance(p, adsk.fusion.Profile):
            profs.append(profile_info(p))
        elif isinstance(p, adsk.fusion.BRepFace):
            profs.append({"face": face_point(design, p)})
        else:
            profs.append({"other": type(p).__name__})
    d["profiles"] = profs
    # Fusion's API can report a stale profile list for a feature whose sketch changed later;
    # the faces it made say which regions it really used.
    try:
        used = profiles_on_faces(f, profs) if d["operation"] in ("new", "new_component", "join") else None
        if used is not None:
            d["profiles"] = used
    except Exception as e:
        d["profiles_on_faces_error"] = str(e)
    try:
        d["participants"] = [body_key(b) for b in f.participantBodies]
    except Exception:
        d["participants"] = []
    d["bodies"] = [body_key(b) for b in f.bodies]
    return d


def read_feature(design, f, sketches):
    t = type(f).__name__
    if t == "ExtrudeFeature":
        return read_extrude(design, f, sketches)
    if t == "CombineFeature":
        return {"target": body_key(f.targetBody), "tools": [body_key(b) for b in items(f.toolBodies)],
                "operation": ["join", "cut", "intersect"][f.operation] if f.operation < 3 else str(f.operation),
                "keep_tools": f.isKeepToolBodies, "new_component": f.isNewComponent,
                "bodies": [body_key(b) for b in f.bodies]}
    if t == "ShellFeature":
        faces = items(f.inputEntities)
        d = {"faces": [face_point(design, x) for x in faces if isinstance(x, adsk.fusion.BRepFace)],
             "face_bodies": [body_key(x.body) for x in faces if isinstance(x, adsk.fusion.BRepFace)],
             "bodies_in": [body_key(x) for x in faces if isinstance(x, adsk.fusion.BRepBody)],
             "inside": expr(f.insideThickness) if f.insideThickness else None,
             "outside": expr(f.outsideThickness) if f.outsideThickness else None,
             "tangent_chain": f.isTangentChain, "bodies": [body_key(b) for b in f.bodies]}
        if faces and isinstance(faces[0], adsk.fusion.BRepFace):
            d["body"] = body_key(faces[0].body)
        return d
    if t == "OffsetFacesFeature":
        faces = items(f.inputFaces)
        d = {"faces": [face_point(design, x) for x in faces], "face_bodies": [body_key(x.body) for x in faces], "distance": expr(f.distance),
             "bodies": [body_key(b) for b in f.bodies]}
        if faces:
            d["body"] = body_key(faces[0].body)
        return d
    if t in ("CopyPasteBody", "CutPasteBody"):
        return {"sources": [body_key(b) for b in items(f.sourceBody) if isinstance(b, adsk.fusion.BRepBody)],
                "bodies": [body_key(b) for b in f.bodies],
                "into": f.parentComponent.name}
    if t == "MoveFeature":
        m = f.transform
        return {"bodies_in": [body_key(b) for b in items(f.inputEntities) if isinstance(b, adsk.fusion.BRepBody)],
                "matrix": [round(v, 9) for v in m.asArray()], "bodies": [body_key(b) for b in f.bodies]}
    if t == "ScaleFeature":
        d = {"bodies_in": [body_key(b) for b in items(f.inputEntities) if isinstance(b, adsk.fusion.BRepBody)],
             "uniform": f.isUniform, "bodies": [body_key(b) for b in f.bodies]}
        try:
            d["factor"] = expr(f.scaleFactor)
        except Exception:
            pass
        try:
            d["factors"] = [expr(f.xScale), expr(f.yScale), expr(f.zScale)]
        except Exception:
            pass
        try:
            pt = f.point
            d["origin"] = pt3(pt.geometry) if hasattr(pt, "geometry") else None
        except Exception:
            pass
        return d
    if t == "RemoveFeature":
        e = f.itemToRemove
        return {"bodies_in": [body_key(e)] if isinstance(e, adsk.fusion.BRepBody) else [], "what": type(e).__name__}
    if t == "SplitBodyFeature":
        tool = f.splittingTool
        d = {"bodies_in": [body_key(b) for b in items(f.splitBodies)], "tool": type(tool).__name__,
             "extend": f.isSplittingToolExtended, "bodies": [body_key(b) for b in f.bodies]}
        if isinstance(tool, adsk.fusion.SketchLine):
            d["sketch"] = sketch_uid(tool.parentSketch)
            d["line"] = "C%d" % curve_index(tool.parentSketch, tool)
        elif isinstance(tool, adsk.fusion.ConstructionPlane):
            g = tool.geometry
            m = occ_transform(design, (tool.nativeObject or tool).parent if hasattr(tool, "parent") else design.rootComponent)
            o = g.origin.copy(); o.transformBy(m)
            n = g.normal.copy(); n.transformBy(m)
            d["plane"] = {"origin": pt3(o), "normal": vec(n)}
        elif isinstance(tool, adsk.fusion.BRepFace):
            d["face"] = {"body": body_key(tool.body), "point": face_point(design, tool)}
            try:
                native = tool.nativeObject or tool  # a proxy's geometry is already placed
                g = native.geometry
                if isinstance(g, adsk.core.Plane):
                    m = occ_transform(design, native.body.parentComponent)
                    n = g.normal.copy(); n.transformBy(m)
                    d["plane"] = {"origin": face_point(design, tool), "normal": vec(n)}
            except Exception:
                pass
        elif isinstance(tool, adsk.fusion.BRepBody):
            d["tool_body"] = body_key(tool)
        return d
    if t == "RectangularPatternFeature":
        ents = items(f.inputEntities)
        d = {"pattern": "rectangular", "bodies": [body_key(b) for b in f.bodies],
             "inputs": [body_key(e) if isinstance(e, adsk.fusion.BRepBody) else {"feature": e.name, "type": type(e).__name__} for e in ents],
             "count1": expr(f.quantityOne), "distance1": expr(f.distanceOne),
             "count2": expr(f.quantityTwo), "distance2": expr(f.distanceTwo),
             "spacing": f.patternDistanceType == adsk.fusion.PatternDistanceType.SpacingPatternDistanceType}
        for k, ent in (("dir1", f.directionOneEntity), ("dir2", f.directionTwoEntity)):
            d[k] = direction(design, ent)
        try:
            d["symmetric1"] = f.isSymmetricInDirectionOne
            d["symmetric2"] = f.isSymmetricInDirectionTwo
        except Exception:
            pass
        return d
    if t == "SweepFeature":
        prof = items(f.profile)
        path = f.path
        d = {"operation": ["join", "cut", "intersect", "new", "new_component"][f.operation],
             "profiles": [profile_info(p) for p in prof if isinstance(p, adsk.fusion.Profile)],
             "path": [], "bodies": [body_key(b) for b in f.bodies],
             "participants": [body_key(b) for b in items(f.participantBodies)] if f.participantBodies else []}
        for i in range(path.count):
            e = path.item(i).entity
            if isinstance(e, adsk.fusion.SketchCurve):
                d["path_sketch"] = sketch_uid(e.parentSketch)
                d["path"].append("C%d" % curve_index(e.parentSketch, e))
            else:
                d["path"].append(None)
        try:
            d["orientation"] = f.orientation
        except Exception:
            pass
        return d
    if t in ("FilletFeature", "ChamferFeature"):
        return read_blend(design, f, t)
    if t == "MirrorFeature":
        d = {"inputs": [pattern_input(e) for e in items(f.inputEntities)], "plane": plane_of(design, f.mirrorPlane),
             "combine": f.isCombine, "bodies": [body_key(b) for b in f.bodies]}
        return d
    if t == "CircularPatternFeature":
        d = {"inputs": [pattern_input(e) for e in items(f.inputEntities)], "axis": axis_of(design, f.axis),
             "count": expr(f.quantity), "angle": expr(f.totalAngle), "symmetric": f.isSymmetric,
             "bodies": [body_key(b) for b in f.bodies]}
        try:
            d["suppressed"] = list(f.suppressedElementsIds)
        except Exception:
            pass
        return d
    if t == "RevolveFeature":
        prof = items(f.profile)
        d = {"operation": ["join", "cut", "intersect", "new", "new_component"][f.operation],
             "profiles": [profile_info(p) for p in prof if isinstance(p, adsk.fusion.Profile)],
             "other_profiles": [type(p).__name__ for p in prof if not isinstance(p, adsk.fusion.Profile)],
             "axis": axis_of(design, f.axis), "bodies": [body_key(b) for b in f.bodies]}
        e = f.extentDefinition
        d["extent"] = type(e).__name__
        if isinstance(e, adsk.fusion.AngleExtentDefinition):
            d["angle"] = expr(e.angle)
            d["angle_value"] = math.degrees(e.angle.value)
            d["symmetric"] = e.isSymmetric
        elif isinstance(e, adsk.fusion.TwoSidesAngleExtentDefinition):
            d["angle"] = expr(e.angleOne)
            d["angle2"] = expr(e.angleTwo)
        try:
            d["participants"] = [body_key(b) for b in f.participantBodies]
        except Exception:
            d["participants"] = []
        return d
    if t == "LoftFeature":
        d = {"operation": ["join", "cut", "intersect", "new", "new_component"][f.operation],
             "rails": f.centerLineOrRails.count, "closed": f.isClosed, "solid": f.isSolid, "sections": [],
             "bodies": [body_key(b) for b in f.bodies]}
        for i in range(f.loftSections.count):
            e = f.loftSections.item(i).entity
            if isinstance(e, adsk.fusion.Profile):
                d["sections"].append(profile_info(e))
            elif isinstance(e, adsk.fusion.SketchPoint):
                d["sections"].append({"point_sketch": sketch_uid(e.parentSketch), "point": e.entityToken})
            else:
                d["sections"].append({"other": type(e).__name__})
        try:
            d["participants"] = [body_key(b) for b in f.participantBodies]
        except Exception:
            d["participants"] = []
        return d
    return {"bodies": [body_key(b) for b in getattr(f, "bodies", [])] if hasattr(f, "bodies") else []}


def pattern_input(e):
    """What a pattern or mirror copies: a body by name, a feature by name and component."""
    if isinstance(e, adsk.fusion.BRepBody):
        return body_key(e)
    try:
        return {"feature": e.name, "type": type(e).__name__, "component": e.parentComponent.name}
    except Exception:
        return {"type": type(e).__name__}


def plane_of(design, ent):
    """A construction plane or planar face as a world {origin, normal}."""
    try:
        native = ent.nativeObject or ent
        if isinstance(native, adsk.fusion.ConstructionPlane):
            g = native.geometry
            m = occ_transform(design, native.component)
        elif isinstance(native, adsk.fusion.BRepFace):
            g = native.geometry
            if not isinstance(g, adsk.core.Plane):
                return {"unknown": "curved face"}
            m = occ_transform(design, native.body.parentComponent)
        else:
            return {"unknown": type(ent).__name__}
        o = g.origin.copy(); o.transformBy(m)
        n = g.normal.copy(); n.transformBy(m)
        return {"origin": pt3(o), "normal": vec(n)}
    except Exception as e:
        return {"error": str(e)}


def axis_of(design, ent):
    """A construction axis, sketch line or straight/circular edge, as a world {origin, dir}."""
    try:
        native = ent.nativeObject or ent
        if isinstance(native, adsk.fusion.ConstructionAxis):
            g = native.geometry
            m = occ_transform(design, native.component)
            o, v = g.origin.copy(), g.direction.copy()
        elif isinstance(native, adsk.fusion.SketchLine):
            g = native.worldGeometry
            m = occ_transform(design, native.parentSketch.parentComponent)
            o = g.startPoint.copy()
            v = o.vectorTo(g.endPoint)
        elif isinstance(native, adsk.fusion.BRepEdge):
            g = native.geometry
            m = occ_transform(design, native.body.parentComponent)
            if isinstance(g, adsk.core.Line3D):
                o = g.startPoint.copy(); v = o.vectorTo(g.endPoint)
            elif hasattr(g, "center") and hasattr(g, "normal"):
                o = g.center.copy(); v = g.normal.copy()
            else:
                return {"unknown": type(g).__name__}
        else:
            return {"unknown": type(ent).__name__}
        o.transformBy(m); v.transformBy(m)
        v.normalize()
        return {"origin": pt3(o), "dir": vec(v)}
    except Exception as e:
        return {"error": str(e)}


def edge_point(design, edge):
    """A world point in the middle of an edge."""
    native = edge.nativeObject or edge
    ev = native.evaluator
    ok, a, b = ev.getParameterExtents()
    ok, p = ev.getPointAtParameter((a + b) / 2)
    ends = [v.geometry for v in (native.startVertex, native.endVertex) if v is not None]
    if not ok or any(p.distanceTo(q) < 1e-5 for q in ends):
        p = native.pointOnEdge  # a closed or oddly parametrised edge: Fusion's own point on it
    return world_point(design, native.body.parentComponent, p)


def tangent_chain(edges):
    """The edges plus every edge joined to them smoothly, end to end (Fusion's tangent chain)."""
    out = [e.nativeObject or e for e in edges]
    seen = {(body_key(e.body), e.tempId) for e in out}
    i = 0
    while i < len(out) and len(out) < 2000:
        e = out[i]
        i += 1
        for v in (e.startVertex, e.endVertex):
            t1 = tangent_at(e, v.geometry)
            if t1 is None:
                continue
            for e2 in v.edges:
                k = (body_key(e2.body), e2.tempId)
                if k in seen:
                    continue
                t2 = tangent_at(e2, v.geometry)
                if t2 is not None and abs(t1.dotProduct(t2)) > 0.9995:
                    seen.add(k)
                    out.append(e2)
    return out


def tangent_at(edge, p):
    try:
        ev = edge.evaluator
        ok, prm = ev.getParameterAtPoint(p)
        ok2, t = ev.getTangent(prm)
        if ok and ok2 and t.length > 1e-12:
            t.normalize()
            return t
    except Exception:
        pass
    return None


def read_blend(design, f, t):
    """Fillet or chamfer: each edge set with its size and the edges it rounds (a point on each)."""
    d = {"sets": [], "bodies": [body_key(b) for b in f.bodies]}
    for attr in ("isRollingBallCorner", "isG2", "cornerType"):
        try:
            d[attr] = getattr(f, attr)
        except Exception:
            pass
    for es in items(f.edgeSets):
        s = {"kind": type(es).__name__}
        try:
            chain = es.isTangentChain
        except Exception:
            chain = getattr(f, "isTangentChain", False)
        ents = items(es.edges)
        edges = [e for e in ents if isinstance(e, adsk.fusion.BRepEdge)]
        for e in ents:
            if isinstance(e, adsk.fusion.BRepFace):  # a face picked: all its edges
                edges += [x for x in e.edges if x.tempId not in {y.tempId for y in edges}]
        others = {type(e).__name__ for e in ents if not isinstance(e, (adsk.fusion.BRepEdge, adsk.fusion.BRepFace))}
        if others:
            s["other"] = sorted(others)
        if chain:
            edges = tangent_chain(edges)
        s["edges"] = [edge_point(design, e) for e in edges]
        s["edge_bodies"] = [body_key(e.body) for e in edges]
        for k in ("radius", "distance", "distanceOne", "distanceTwo", "angle", "startRadius", "endRadius"):
            try:
                v = getattr(es, k)
                if v is not None:
                    s[k] = expr(v)
            except Exception:
                pass
        try:
            s["flipped"] = es.isFlipped
        except Exception:
            pass
        d["sets"].append(s)
    return d


def direction(design, ent):
    """A direction picked as an edge, sketch line or construction axis, as a world vector."""
    if ent is None:
        return None
    try:
        if isinstance(ent, adsk.fusion.BRepEdge):
            g = ent.geometry
            comp = (ent.nativeObject or ent).body.parentComponent
            a, b = g.startPoint.copy(), g.endPoint.copy()
        elif isinstance(ent, adsk.fusion.SketchLine):
            g = ent.worldGeometry
            comp = design.rootComponent
            a, b = g.startPoint.copy(), g.endPoint.copy()
            m = occ_transform(design, (ent.nativeObject or ent).parentSketch.parentComponent)
            a.transformBy(m); b.transformBy(m)
            return vec(adsk.core.Vector3D.create(b.x - a.x, b.y - a.y, b.z - a.z))
        elif isinstance(ent, adsk.fusion.ConstructionAxis):
            v = ent.geometry.direction
            return vec(v)
        else:
            return {"unknown": type(ent).__name__}
        m = occ_transform(design, comp)
        a.transformBy(m); b.transformBy(m)
        return vec(adsk.core.Vector3D.create(b.x - a.x, b.y - a.y, b.z - a.z))
    except Exception as e:
        return {"error": str(e)}


def bodies_now(design):
    out = {}
    for comp in design.allComponents:
        for b in comp.bRepBodies:
            try:
                vol = b.volume * 1000.0  # cm3 -> mm3
            except Exception:
                vol = None
            d = {"volume": round(vol, 3) if vol is not None else None, "visible": b.isLightBulbOn}
            try:
                d["center"] = world_point(design, comp, b.physicalProperties.centerOfMass)
                bb = b.boundingBox
                d["bbox"] = [world_point(design, comp, bb.minPoint), world_point(design, comp, bb.maxPoint)]
            except Exception:
                pass
            out[body_key(b)] = d
    return out


JOINTS = ("Joint", "AsBuiltJoint", "RigidGroup")


def placements(design):
    """Every component's (first) placement as a row-major 4x4 in cm, for steps that move them."""
    out = {}
    for o in design.rootComponent.allOccurrences:
        name = o.component.name
        if name not in out:
            out[name] = [round(v, 9) for v in o.transform2.asArray()]
    return out


def extract(design, start=0, stop=None, sketches=None, rollback=True):
    tl = design.timeline
    marker = tl.markerPosition
    stop = tl.count if stop is None else min(stop, tl.count)
    sketches = {} if sketches is None else sketches
    steps = []
    try:
        for i in range(start, stop):
            item = tl.item(i)
            step = {"index": i, "name": item.name, "suppressed": item.isSuppressed}
            try:
                # Inputs are read with the model as it was before this step; an occurrence only
                # exists after it.
                if rollback:
                    kind = type(item.entity).__name__
                    item.rollTo(kind not in ("Occurrence", "Sketch", "ConstructionPlane", "ConstructionAxis", "ConstructionPoint"))
                e = item.entity
                t = type(e).__name__
                step["type"] = t
                if t == "Sketch":
                    s = read_sketch(design, e)
                    s["uid"] = sketch_uid(e)
                    s["component"] = e.parentComponent.name
                    sketches[e.entityToken] = {"uid": s["uid"], "ids": s["ids"]}
                    del s["ids"]
                    step["sketch"] = s
                elif t == "Occurrence":
                    step["component"] = e.component.name
                    if e.isReferencedComponent:
                        step["linked"] = e.documentReference.dataFile.name if e.documentReference else e.component.name
                    step["parent"] = e.assemblyContext.component.name if e.assemblyContext else None
                    step["matrix"] = [round(v, 9) for v in e.transform2.asArray()]
                elif t == "ConstructionPlane":
                    g = e.geometry
                    step["plane"] = {"origin": pt3(g.origin), "normal": vec(g.normal)}
                else:
                    try:
                        step["component"] = e.parentComponent.name
                    except Exception:
                        pass
                    step["feature"] = read_feature(design, e, sketches)
                if t in JOINTS:
                    step["placements_before"] = placements(design)
                step["health"] = getattr(e, "healthState", None) if e is not None else None
                if rollback:
                    item.rollTo(False)
                    step["after"] = bodies_now(design)
                    if t in JOINTS:
                        step["placements_after"] = placements(design)
            except Exception:
                step["error"] = traceback.format_exc().strip().splitlines()[-1]
            steps.append(step)
    finally:
        if marker >= tl.count:
            tl.moveToEnd()
        else:
            tl.markerPosition = marker
    return {"steps": steps, "sketches": sketches}


def parameters(design):
    return [{"name": p.name, "expression": p.expression, "unit": p.unit, "comment": p.comment}
            for p in design.userParameters]


def components(design):
    out = []
    for occ in design.rootComponent.allOccurrences:
        d = {"component": occ.component.name, "occurrence": occ.fullPathName,
             "matrix": [round(v, 9) for v in occ.transform2.asArray()],
             "bodies": [b.name for b in occ.component.bRepBodies],
             "parent": occ.assemblyContext.component.name if occ.assemblyContext else None,
             "visible": occ.isVisible}
        if occ.isReferencedComponent:
            d["linked"] = occ.documentReference.dataFile.name if occ.documentReference else occ.component.name
        else:
            p = occ.assemblyContext
            while p is not None:
                if p.isReferencedComponent:
                    d["inside_linked"] = True
                    break
                p = p.assemblyContext
        out.append(d)
    return out


def export_bodies(bodies, path):
    """Bodies to STEP by way of a scratch document, whatever their visibility. The scratch
    document is closed without saving."""
    app_ = adsk.core.Application.get()
    tbm = adsk.fusion.TemporaryBRepManager.get()
    copies = [(b.name, tbm.copy(b)) for b in bodies]
    was = app_.activeDocument
    doc = app_.documents.add(adsk.core.DocumentTypes.FusionDesignDocumentType)
    try:
        d = adsk.fusion.Design.cast(doc.products.itemByProductType("DesignProductType"))
        d.designType = adsk.fusion.DesignTypes.DirectDesignType
        for name, t in copies:
            d.rootComponent.bRepBodies.add(t).name = name
        d.exportManager.execute(d.exportManager.createSTEPExportOptions(path, d.rootComponent))
    finally:
        doc.close(False)
        if was:
            was.activate()


def export_step(design, index, components, folder):
    """The bodies of these components as they are just after timeline step `index`, one STEP
    file per component: [{component, path, matrix}] with the component's world placement
    (row-major 4x4, millimetres)."""
    import os
    tl = design.timeline
    marker = tl.markerPosition
    out = []
    try:
        tl.item(index).rollTo(False)
        for name in components:
            comp = design.rootComponent if name == design.rootComponent.name else design.allComponents.itemByName(name)
            if comp is None:
                continue
            path = os.path.join(folder, "fusion-step%d-%s.step" % (index, "".join(ch if ch.isalnum() else "_" for ch in name)))
            em = design.exportManager
            # STEP export leaves hidden bodies out: show them for the export only.
            hidden = [b for b in comp.bRepBodies if not b.isLightBulbOn]
            if comp != design.rootComponent:
                # ...and so are bodies of a hidden component, or one inside a hidden one.
                for o in design.rootComponent.allOccurrencesByComponent(comp):
                    while o is not None:
                        if not o.isLightBulbOn:
                            hidden.append(o)
                        o = o.assemblyContext
            if comp != design.rootComponent:
                # A body can also be hidden in one placement of its component only.
                for o in design.rootComponent.allOccurrencesByComponent(comp):
                    hidden += [b for b in o.bRepBodies if not b.isLightBulbOn]
            for b in hidden:
                b.isLightBulbOn = True
            try:
                em.execute(em.createSTEPExportOptions(path, comp))
                # Some bodies stay invisible with every light bulb on; STEP export skips them.
                unseen = [b for b in comp.bRepBodies if not b.isVisible]
            finally:
                for b in hidden:
                    b.isLightBulbOn = False
            m = list(occ_transform(design, comp).asArray())
            for i in (3, 7, 11):
                m[i] *= CM
            m = [round(v, 9) for v in m]
            out.append({"component": name, "path": path, "matrix": m})
            if unseen:
                extra = path[:-5] + "-unseen.step"
                export_bodies(unseen, extra)
                out.append({"component": name, "path": extra, "matrix": m})
    finally:
        if marker >= tl.count:
            tl.moveToEnd()
        else:
            tl.markerPosition = marker
    return out


# --- looks -------------------------------------------------------------------------------------

COLOR_IDS = ("opaque_albedo", "wood_early_color", "generic_diffuse", "metal_f0", "layered_diffuse",
             "glazing_transmittance_color", "surface_albedo")
NAMED = {"black": "#191919", "white": "#f2f2f2", "grey": "#808080", "gray": "#808080", "red": "#b3261e",
         "blue": "#1f4fa3", "green": "#2e7d32", "yellow": "#e0b000", "orange": "#e07000", "brown": "#6b4a2b",
         "silver": "#c0c0c0", "gold": "#d4af37", "clear": "#e8eef2"}


def look(app_):
    """An appearance as {name, color, opacity, metallic, roughness}: its main colour, read by
    the property its kind of appearance keeps it in."""
    found = {}
    for p in app_.appearanceProperties:
        if isinstance(p, adsk.core.ColorProperty):
            try:
                v = p.value
            except Exception:
                v = None
            if v is not None and p.id not in found:
                found[p.id] = (v.red, v.green, v.blue)
    rgb = None
    for k in COLOR_IDS:
        if k in found and not (k == "surface_albedo" and found[k] == (255, 255, 255) and len(found) > 1):
            rgb = found[k]
            break
    name = app_.name
    low = name.lower()
    if rgb is None or rgb == (255, 255, 255) and "white" not in low:
        for word, hexv in NAMED.items():
            if word in low:
                rgb = tuple(int(hexv[i:i + 2], 16) for i in (1, 3, 5))
                break
    if rgb is None:
        rgb = (176, 176, 176)
    d = {"name": name, "color": "#%02x%02x%02x" % rgb}
    if any(w in low for w in ("steel", "aluminum", "aluminium", "metal", "chrome", "brass", "copper", "iron", "titanium", "gold", "silver", "zinc", "nickel")):
        d["metallic"] = 1.0
        d["roughness"] = 0.15 if "polish" in low or "chrome" in low or "mirror" in low else 0.6 if "brushed" in low or "cast" in low else 0.35
    elif any(w in low for w in ("glass", "clear", "acrylic", "transparent", "plexi")) and "matte (black)" not in low:
        d["opacity"] = 0.35
    return d


def looks(design):
    """Each body's appearance and physical material, and faces painted differently."""
    out = {}
    for comp in design.allComponents:
        for b in comp.bRepBodies:
            d = {}
            try:
                d["appearance"] = look(b.appearance)
            except Exception:
                pass
            try:
                m = b.material
                dens = m.materialProperties.itemByName("Density")
                d["material"] = {"name": m.name, "density_g_cm3": round(dens.value / 1000.0, 6) if dens else None}
            except Exception:
                pass
            faces = []
            try:
                base = b.appearance.name
                for f in b.faces:
                    fa = f.appearance
                    if fa is not None and fa.name != base:
                        faces.append({"point": world_point(design, comp, f.pointOnFace), "appearance": look(fa)})
                        if len(faces) >= 200:
                            break
            except Exception:
                pass
            if faces:
                d["faces"] = faces
            out[body_key(b)] = d
    return out
