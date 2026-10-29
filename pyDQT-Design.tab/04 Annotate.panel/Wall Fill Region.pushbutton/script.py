# -*- coding: utf-8 -*-
"""
Wall Fill Region v3.3 - DQT
Creates filled region(s) in the active plan view covering walls and/or columns,
from the host model AND/OR linked Revit files.

Modes:
- All cut elements in the current view (auto, by cut plane + crop).
- Pick elements one by one in the host model.
- Pick elements one by one in linked models.

Footprints come from the real solid geometry (match the elements exactly, no
overhang). Overlapping footprints are merged into as few regions as possible,
with a per-element fallback so nothing is lost and it never crashes.

Copyright (c) 2026 Dang Quoc Truong (DQT)
All rights reserved.

Author: Dang Quoc Truong (DQT)
License: All rights reserved - pyDQT Suite
"""
try: __import__("dqt_usage").log_use(__file__)  # pyDQT usage log
except Exception: pass

__title__ = "Wall Fill\nRegion"
__author__ = "Dang Quoc Truong (DQT)"
__doc__ = ("Create filled region(s) covering walls/columns - all in view or "
           "picked one by one, from host or linked files.\n"
           "Copyright (c) 2026 Dang Quoc Truong (DQT)")

import clr
clr.AddReference('RevitAPI')
clr.AddReference('RevitAPIUI')

from Autodesk.Revit.DB import (
    FilteredElementCollector, BuiltInCategory,
    Transaction, TransactionGroup, TransactionStatus,
    XYZ, Line, Arc, CurveLoop, LocationCurve, Element,
    FilledRegion, FilledRegionType,
    GeometryCreationUtilities, BooleanOperationsUtils, BooleanOperationsType,
    PlanarFace, Solid, GeometryInstance, Options, ViewDetailLevel,
    ExtrusionAnalyzer, Plane,
    ViewPlan, PlanViewPlane, ElementId, RevitLinkInstance,
    IFailuresPreprocessor, FailureProcessingResult, FailureSeverity,
    GraphicsStyleType
)
from Autodesk.Revit.UI.Selection import ObjectType, ISelectionFilter
from System.Collections.Generic import List

from pyrevit import forms, script

# ============================================================================
# REVIT CONTEXT
# ============================================================================
doc = __revit__.ActiveUIDocument.Document
uidoc = __revit__.ActiveUIDocument
view = doc.ActiveView
output = script.get_output()

Z0 = 0.0
TOL = 1e-9
EPS_Z = 1e-4
MIN_SEG = 0.0052
CROP_MARGIN = 0.1
EXTRUDE_HEIGHT = 10.0

MODE_ALL = "All cut elements in current view"
MODE_HOST = "Pick elements - Host model"
MODE_LINK = "Pick elements - Linked models"

CATEGORY_CHOICES = [
    ("Walls", BuiltInCategory.OST_Walls, "wall"),
    ("Columns (Architectural)", BuiltInCategory.OST_Columns, "column"),
    ("Structural Columns", BuiltInCategory.OST_StructuralColumns, "column"),
]


def _kind_of(elem):
    """Return 'wall' / 'column' for supported elements, else None."""
    try:
        bic = elem.Category.BuiltInCategory
    except Exception:
        return None
    if bic == BuiltInCategory.OST_Walls:
        return "wall"
    if bic in (BuiltInCategory.OST_Columns,
               BuiltInCategory.OST_StructuralColumns):
        return "column"
    return None


# ============================================================================
# SELECTION FILTERS
# ============================================================================
class _HostFilter(ISelectionFilter):
    def AllowElement(self, e):
        return _kind_of(e) is not None

    def AllowReference(self, r, p):
        return False


class _LinkFilter(ISelectionFilter):
    def AllowElement(self, e):
        return True  # allow link instances so Revit drills into them

    def AllowReference(self, r, p):
        try:
            link = doc.GetElement(r.ElementId)
            ld = link.GetLinkDocument()
            le = ld.GetElement(r.LinkedElementId)
            return _kind_of(le) is not None
        except Exception:
            return False


# ============================================================================
# FAILURE HANDLING
# ============================================================================
class _SwallowErrors(IFailuresPreprocessor):
    def PreprocessFailures(self, fa):
        fa.DeleteAllWarnings()
        for msg in fa.GetFailureMessages():
            if msg.GetSeverity() == FailureSeverity.Error:
                return FailureProcessingResult.ProceedWithRollBack
        return FailureProcessingResult.Continue


# ============================================================================
# GEOMETRY HELPERS
# ============================================================================
def _flat(p):
    return XYZ(p.X, p.Y, Z0)


def _flatten_curve(curve):
    if isinstance(curve, Line):
        return Line.CreateBound(_flat(curve.GetEndPoint(0)),
                                _flat(curve.GetEndPoint(1)))
    if isinstance(curve, Arc):
        return Arc.Create(_flat(curve.GetEndPoint(0)),
                          _flat(curve.GetEndPoint(1)),
                          _flat(curve.Evaluate(0.5, True)))
    pts = curve.Tessellate()
    segs = []
    for i in range(len(pts) - 1):
        a = _flat(pts[i])
        b = _flat(pts[i + 1])
        if a.DistanceTo(b) > TOL:
            segs.append(Line.CreateBound(a, b))
    return segs


def _transform_flatten_loop(loop, transform):
    new_loop = CurveLoop()
    for curve in loop:
        c = curve.CreateTransformed(transform) if transform else curve
        flat = _flatten_curve(c)
        if isinstance(flat, list):
            for seg in flat:
                new_loop.Append(seg)
        else:
            new_loop.Append(flat)
    return new_loop


def _iter_solids(geo):
    if geo is None:
        return
    for g in geo:
        if isinstance(g, Solid):
            if g.Volume > TOL:
                yield g
        elif isinstance(g, GeometryInstance):
            for s in _iter_solids(g.GetInstanceGeometry()):
                yield s


def _solid_footprint_loops(elem, transform):
    opt = Options()
    opt.DetailLevel = ViewDetailLevel.Fine
    opt.ComputeReferences = False
    geo = elem.get_Geometry(opt)
    bottom = []
    top = []
    for solid in _iter_solids(geo):
        for face in solid.Faces:
            if isinstance(face, PlanarFace):
                nz = face.FaceNormal.Z
                if nz < -0.9:
                    bottom.append(face)
                elif nz > 0.9:
                    top.append(face)
    # Prefer a bottom face; if the solid only exposes a top face, use that.
    faces = bottom if bottom else top
    loops = []
    for face in faces:
        try:
            for cl in face.GetEdgesAsCurveLoops():
                loops.append(_transform_flatten_loop(cl, transform))
        except Exception:
            continue
    return loops


def _wall_rect_loops(wall, transform):
    loc = wall.Location
    if not isinstance(loc, LocationCurve):
        return []
    curve = loc.Curve
    if transform is not None:
        curve = curve.CreateTransformed(transform)
    width = None
    try:
        width = wall.Width
    except Exception:
        width = None
    if not width or width <= TOL:
        try:
            width = wall.WallType.Width
        except Exception:
            width = None
    if not width or width <= TOL:
        return []
    half = width / 2.0

    if isinstance(curve, Line):
        p0 = _flat(curve.GetEndPoint(0))
        p1 = _flat(curve.GetEndPoint(1))
        direction = p1 - p0
        if direction.GetLength() <= TOL:
            return []
        direction = direction.Normalize()
        normal = XYZ.BasisZ.CrossProduct(direction).Normalize()
        offset = normal.Multiply(half)
        a, b = p0 + offset, p1 + offset
        c, d = p1 - offset, p0 - offset
        loop = CurveLoop()
        loop.Append(Line.CreateBound(a, b))
        loop.Append(Line.CreateBound(b, c))
        loop.Append(Line.CreateBound(c, d))
        loop.Append(Line.CreateBound(d, a))
        return [loop]

    if isinstance(curve, Arc):
        a0 = _flat(curve.GetEndPoint(0))
        a1 = _flat(curve.GetEndPoint(1))
        amid = _flat(curve.Evaluate(0.5, True))
        try:
            flat_arc = Arc.Create(a0, a1, amid)
            outer = flat_arc.CreateOffset(half, XYZ.BasisZ)
            inner = flat_arc.CreateOffset(-half, XYZ.BasisZ)
        except Exception:
            return []
        loop = CurveLoop()
        loop.Append(outer)
        loop.Append(Line.CreateBound(outer.GetEndPoint(1), inner.GetEndPoint(1)))
        loop.Append(inner.CreateReversed())
        loop.Append(Line.CreateBound(inner.GetEndPoint(0), outer.GetEndPoint(0)))
        return [loop]

    return []


def _projected_footprint_loops(elem, transform):
    """Vertical projection (plan silhouette) of the element's solids using
    ExtrusionAnalyzer. Gives the true plan outline even for rotated or slanted
    columns, unlike the bottom face (offset for slanted) or the axis-aligned
    bounding box."""
    opt = Options()
    opt.DetailLevel = ViewDetailLevel.Fine
    opt.ComputeReferences = False
    geo = elem.get_Geometry(opt)
    plane = Plane.CreateByNormalAndOrigin(XYZ.BasisZ, XYZ.Zero)
    loops = []
    for solid in _iter_solids(geo):
        try:
            ea = ExtrusionAnalyzer.Create(solid, plane, XYZ.BasisZ)
            face = ea.GetExtrusionBase()
            for cl in face.GetEdgesAsCurveLoops():
                loops.append(_transform_flatten_loop(cl, transform))
        except Exception:
            continue
    return loops


def _bbox_rect_loops(elem, transform):
    """Last-resort footprint: an axis-aligned rectangle from the element's
    bounding box (used for columns whose solid face is too complex)."""
    try:
        bb = elem.get_BoundingBox(None)
    except Exception:
        bb = None
    if bb is None:
        return []
    xs, ys = [], []
    for ix in (bb.Min.X, bb.Max.X):
        for iy in (bb.Min.Y, bb.Max.Y):
            for iz in (bb.Min.Z, bb.Max.Z):
                p = XYZ(ix, iy, iz)
                if transform is not None:
                    p = transform.OfPoint(p)
                xs.append(p.X)
                ys.append(p.Y)
    minx, maxx = min(xs), max(xs)
    miny, maxy = min(ys), max(ys)
    if (maxx - minx) < MIN_SEG or (maxy - miny) < MIN_SEG:
        return []
    a = XYZ(minx, miny, Z0)
    b = XYZ(maxx, miny, Z0)
    c = XYZ(maxx, maxy, Z0)
    d = XYZ(minx, maxy, Z0)
    loop = CurveLoop()
    loop.Append(Line.CreateBound(a, b))
    loop.Append(Line.CreateBound(b, c))
    loop.Append(Line.CreateBound(c, d))
    loop.Append(Line.CreateBound(d, a))
    return [loop]


def _footprint_loops(elem, kind, transform):
    loops = _solid_footprint_loops(elem, transform)
    if loops:
        return loops
    if kind == "wall":
        return _wall_rect_loops(elem, transform)
    return []


def _loop_ok(loop):
    try:
        if loop.IsOpen():
            return False
    except Exception:
        pass
    n = 0
    for c in loop:
        try:
            if c.Length < MIN_SEG:
                return False
        except Exception:
            return False
        n += 1
    return n >= 3


def _clean_loop(loop):
    """Merge collinear vertices and drop sub-tolerance segments from a line
    loop so the merged outline (with tiny mitred edges at wall joins) becomes a
    valid filled-region boundary. Loops with arcs are returned unchanged."""
    try:
        curves = list(loop)
    except Exception:
        return loop
    if not curves or not all(isinstance(c, Line) for c in curves):
        return loop
    pts = [c.GetEndPoint(0) for c in curves]
    dedup = []
    for p in pts:
        if dedup and dedup[-1].DistanceTo(p) <= MIN_SEG:
            continue
        dedup.append(p)
    while len(dedup) >= 2 and dedup[0].DistanceTo(dedup[-1]) <= MIN_SEG:
        dedup.pop()
    if len(dedup) < 3:
        return None
    changed = True
    while changed and len(dedup) >= 3:
        changed = False
        m = len(dedup)
        keep = []
        for i in range(m):
            prev = dedup[(i - 1) % m]
            cur = dedup[i]
            nxt = dedup[(i + 1) % m]
            v1 = cur - prev
            v2 = nxt - cur
            if v1.GetLength() > TOL and v2.GetLength() > TOL and \
                    v1.Normalize().IsAlmostEqualTo(v2.Normalize()):
                changed = True
                continue
            keep.append(cur)
        dedup = keep
    if len(dedup) < 3:
        return None
    cl = CurveLoop()
    m = len(dedup)
    for i in range(m):
        a = dedup[i]
        b = dedup[(i + 1) % m]
        if a.DistanceTo(b) <= MIN_SEG:
            return None
        cl.Append(Line.CreateBound(a, b))
    return cl


def _filtered(fn, elem, transform):
    try:
        return [lp for lp in fn(elem, transform) if _loop_ok(lp)]
    except Exception:
        return []


def _good_loops(elem, kind, transform):
    if kind == "wall":
        # Real solid footprint (incl. mitred joins) -> clean rectangle.
        loops = _filtered(_solid_footprint_loops, elem, transform)
        if loops:
            return loops
        loops = _filtered(_wall_rect_loops, elem, transform)
        if loops:
            return loops
    else:
        # Columns: vertical projection first - aligns with rotated/slanted
        # columns (the bottom face is offset for slanted ones). Then the solid
        # bottom face as a secondary.
        loops = _filtered(_projected_footprint_loops, elem, transform)
        if loops:
            return loops
        loops = _filtered(_solid_footprint_loops, elem, transform)
        if loops:
            return loops
    # Last resort: axis-aligned bounding-box rectangle.
    return _filtered(_bbox_rect_loops, elem, transform)


def _loop_to_solid(loop):
    loops = List[CurveLoop]()
    loops.Add(loop)
    return GeometryCreationUtilities.CreateExtrusionGeometry(
        loops, XYZ.BasisZ, EXTRUDE_HEIGHT)


def _merge_groups(items):
    groups = []
    for solid, loop in items:
        placed = False
        for g in groups:
            try:
                g[0] = BooleanOperationsUtils.ExecuteBooleanOperation(
                    g[0], solid, BooleanOperationsType.Union)
                g[1].append(loop)
                placed = True
                break
            except Exception:
                continue
        if not placed:
            groups.append([solid, [loop]])
    return groups


def _bottom_loops(solid):
    loops = []
    for face in solid.Faces:
        if isinstance(face, PlanarFace) and face.FaceNormal.Z < -0.9:
            for cl in face.GetEdgesAsCurveLoops():
                loops.append(cl)
    return loops


# ============================================================================
# VIEW HELPERS
# ============================================================================
def _cut_plane_z():
    try:
        vr = view.GetViewRange()
        lvl_id = vr.GetLevelId(PlanViewPlane.CutPlane)
        offset = vr.GetOffset(PlanViewPlane.CutPlane)
        if lvl_id is not None and lvl_id != ElementId.InvalidElementId:
            base_z = doc.GetElement(lvl_id).Elevation
        else:
            gen = view.GenLevel
            if gen is None:
                return None
            base_z = gen.Elevation
        return base_z + offset
    except Exception:
        return None


def _view_plane_z():
    """Elevation of the active plan view's plane, so the filled region loops
    are coplanar with the view (a region flattened to an arbitrary Z=0 may not
    show when the view's level is far from 0)."""
    try:
        gl = view.GenLevel
        if gl is not None:
            return gl.Elevation
    except Exception:
        pass
    z = _cut_plane_z()
    return z if z is not None else 0.0


def _is_cut(elem, cut_z, transform):
    if cut_z is None:
        return True
    bb = elem.get_BoundingBox(None)
    if bb is None:
        return True
    if transform is None:
        zmin, zmax = bb.Min.Z, bb.Max.Z
    else:
        zs = []
        for ix in (bb.Min.X, bb.Max.X):
            for iy in (bb.Min.Y, bb.Max.Y):
                for iz in (bb.Min.Z, bb.Max.Z):
                    zs.append(transform.OfPoint(XYZ(ix, iy, iz)).Z)
        zmin, zmax = min(zs), max(zs)
    return (zmin - EPS_Z) <= cut_z <= (zmax + EPS_Z)


def _crop_xy_bbox():
    if not view.CropBoxActive:
        return None
    cb = view.CropBox
    t = cb.Transform
    xs, ys = [], []
    for ix in (cb.Min.X, cb.Max.X):
        for iy in (cb.Min.Y, cb.Max.Y):
            for iz in (cb.Min.Z, cb.Max.Z):
                p = t.OfPoint(XYZ(ix, iy, iz))
                xs.append(p.X)
                ys.append(p.Y)
    return (min(xs), min(ys), max(xs), max(ys))


def _loops_in_crop(loops, crop):
    if crop is None:
        return True
    minx, miny, maxx, maxy = crop
    for loop in loops:
        for c in loop:
            p = c.GetEndPoint(0)
            if (minx - CROP_MARGIN) <= p.X <= (maxx + CROP_MARGIN) and \
               (miny - CROP_MARGIN) <= p.Y <= (maxy + CROP_MARGIN):
                return True
    return False


def _name(e):
    return Element.Name.GetValue(e)


# ============================================================================
# REGION CREATION
# ============================================================================
def _create_region(frt, loops, line_style_id=None):
    coll = List[CurveLoop]()
    for lp in loops:
        if _loop_ok(lp):
            coll.Add(lp)
    if coll.Count == 0:
        return False
    t = Transaction(doc, "DQT - Fill Region")
    t.Start()
    opts = t.GetFailureHandlingOptions()
    opts.SetFailuresPreprocessor(_SwallowErrors())
    opts.SetClearAfterRollback(True)
    t.SetFailureHandlingOptions(opts)
    try:
        region = FilledRegion.Create(doc, frt.Id, view.Id, coll)
        if line_style_id is not None and region is not None:
            try:
                region.SetLineStyleId(line_style_id)
            except Exception:
                pass
        return t.Commit() == TransactionStatus.Committed
    except Exception:
        if t.HasStarted() and not t.HasEnded():
            t.RollBack()
        return False


def _build_and_create(frt, all_loops, counts, skipped, announce=True,
                      line_style_id=None):
    """Shared pipeline: merge footprints and create the region(s).
    Returns the number of filled regions created."""
    if not all_loops:
        forms.alert("No usable walls/columns were found for this selection.",
                    title="Wall Fill Region")
        return 0
    items = []
    for loop in all_loops:
        try:
            items.append((_loop_to_solid(loop), loop))
        except Exception:
            skipped += 1
    groups = _merge_groups(items)

    # Collect the merged outline of every group, cleaned so the tiny mitred
    # edges at wall joins do not make the boundary invalid (which would force
    # the per-element fallback and produce many fragmented regions).
    group_loops = []
    for solid, orig_loops in groups:
        bl = []
        for lp in _bottom_loops(solid):
            c = _clean_loop(lp)
            if c is not None and _loop_ok(c):
                bl.append(c)
        group_loops.append((bl, orig_loops))

    created = 0
    tg = TransactionGroup(doc, "DQT - Wall Fill Region")
    tg.Start()

    # Preferred: ONE filled region holding all (non-overlapping) outlines.
    combined = []
    for bl, _orig in group_loops:
        combined.extend(bl)
    if combined and _create_region(frt, combined, line_style_id):
        created = 1
    else:
        # Fallback: one region per group, then per original loop, so nothing
        # is lost if a combined boundary is rejected.
        for bl, orig_loops in group_loops:
            if bl and _create_region(frt, bl, line_style_id):
                created += 1
            else:
                for lp in orig_loops:
                    if _create_region(frt, [lp], line_style_id):
                        created += 1
    tg.Assimilate()

    if created == 0:
        forms.alert("Could not create any filled region from the selection.",
                    title="Wall Fill Region")
        return 0
    detail = ", ".join("{}: {}".format(k, v) for k, v in counts.items())
    msg = "Created {} filled region(s) covering {}.".format(created, detail)
    if skipped:
        msg += "\n{} footprint(s) skipped.".format(skipped)
    output.print_md("**Wall Fill Region** - {}".format(msg))
    if announce:
        forms.alert(msg, title="Wall Fill Region")
    return created


# ============================================================================
# COLLECTORS
# ============================================================================
def _pick_filled_region_type():
    frts = list(FilteredElementCollector(doc).OfClass(FilledRegionType))
    if not frts:
        forms.alert("No Filled Region Type found in this project.\n"
                    "Create one (Annotate > Region) and try again.",
                    title="Wall Fill Region")
        return None
    name_map = {}
    for frt_ in frts:
        name_map[_name(frt_)] = frt_
    chosen = forms.SelectFromList.show(
        sorted(name_map.keys()), title="Wall Fill Region - region type",
        multiselect=False, button_name="Next")
    return name_map.get(chosen) if chosen else None


DEFAULT_LINE = "<Default (region type)>"


def _pick_line_style():
    """Let the user pick a boundary line style. Returns its ElementId, or None
    to keep the region type's default."""
    styles = {}
    try:
        lines_cat = doc.Settings.Categories.get_Item(BuiltInCategory.OST_Lines)
        for sub in lines_cat.SubCategories:
            try:
                gs = sub.GetGraphicsStyle(GraphicsStyleType.Projection)
                if gs is not None:
                    styles[sub.Name] = gs.Id
            except Exception:
                pass
    except Exception:
        pass
    if not styles:
        return None
    chosen = forms.SelectFromList.show(
        [DEFAULT_LINE] + sorted(styles.keys()),
        title="Wall Fill Region - boundary line style",
        multiselect=False, button_name="Create")
    if not chosen or chosen == DEFAULT_LINE:
        return None
    return styles.get(chosen)


def _collect_all():
    """All cut walls/columns in the view, from chosen sources/categories."""
    sources = [("Host model", None, None)]
    seen = {"Host model": 1}
    links = (FilteredElementCollector(doc, view.Id)
             .OfCategory(BuiltInCategory.OST_RvtLinks)
             .WhereElementIsNotElementType().ToElements())
    for li in links:
        if not isinstance(li, RevitLinkInstance) or li.GetLinkDocument() is None:
            continue
        base = "LINK: " + _name(li)
        label = base
        if label in seen:
            seen[base] += 1
            label = "{} ({})".format(base, seen[base])
        else:
            seen[base] = 1
        sources.append((label, li, li.GetTotalTransform()))

    picked_src = forms.SelectFromList.show(
        [s[0] for s in sources], title="Wall Fill Region - source model(s)",
        multiselect=True, button_name="Next")
    if not picked_src:
        return None
    chosen_sources = [s for s in sources if s[0] in picked_src]

    picked = forms.SelectFromList.show(
        [c[0] for c in CATEGORY_CHOICES],
        title="Wall Fill Region - elements to cover",
        multiselect=True, button_name="Next")
    if not picked:
        return None
    chosen_cats = [c for c in CATEGORY_CHOICES if c[0] in picked]

    cut_z = _cut_plane_z()
    crop = _crop_xy_bbox()
    all_loops = []
    counts = {}
    skipped = 0
    for label, li, transform in chosen_sources:
        src_doc = li.GetLinkDocument() if li is not None else doc
        for clabel, bic, kind in chosen_cats:
            if li is None:
                elems = (FilteredElementCollector(doc, view.Id)
                         .OfCategory(bic).WhereElementIsNotElementType()
                         .ToElements())
            else:
                elems = (FilteredElementCollector(src_doc)
                         .OfCategory(bic).WhereElementIsNotElementType()
                         .ToElements())
            for elem in elems:
                if not _is_cut(elem, cut_z, transform):
                    continue
                good = _good_loops(elem, kind, transform)
                if not good or not _loops_in_crop(good, crop):
                    if good:
                        skipped += 1
                    continue
                all_loops.extend(good)
                counts[clabel] = counts.get(clabel, 0) + 1
    return all_loops, counts, skipped


def _collect_picked(linked):
    """Walls/columns picked one by one (host or linked)."""
    otype = ObjectType.LinkedElement if linked else ObjectType.Element
    filt = _LinkFilter() if linked else _HostFilter()
    src = "linked models" if linked else "the host model"
    try:
        refs = uidoc.Selection.PickObjects(
            otype, filt,
            "Pick walls/columns in {} - click Finish when done".format(src))
    except Exception:
        return None  # user cancelled
    if not refs:
        return None

    all_loops = []
    counts = {}
    skipped = 0
    for r in refs:
        try:
            if linked:
                link = doc.GetElement(r.ElementId)
                ld = link.GetLinkDocument()
                elem = ld.GetElement(r.LinkedElementId)
                transform = link.GetTotalTransform()
            else:
                elem = doc.GetElement(r.ElementId)
                transform = None
        except Exception:
            skipped += 1
            continue
        kind = _kind_of(elem)
        if kind is None:
            skipped += 1
            continue
        good = _good_loops(elem, kind, transform)
        if good:
            all_loops.extend(good)
            clabel = "Walls" if kind == "wall" else "Columns"
            counts[clabel] = counts.get(clabel, 0) + 1
        else:
            skipped += 1
    return all_loops, counts, skipped


# ============================================================================
# MAIN
# ============================================================================
def main():
    if not isinstance(view, ViewPlan):
        forms.alert("This tool works in plan views only (floor / structural / "
                    "ceiling / area plan).\nOpen a plan view and try again.",
                    title="Wall Fill Region")
        return

    # Place the filled-region loops on the view's own plane so they are visible
    # (a region flattened to Z=0 may not show when the level is far from 0).
    global Z0
    Z0 = _view_plane_z()

    mode = forms.SelectFromList.show(
        [MODE_ALL, MODE_HOST, MODE_LINK], title="Wall Fill Region - mode",
        multiselect=False, button_name="Next")
    if not mode:
        return

    frt = _pick_filled_region_type()
    if frt is None:
        return

    line_style_id = _pick_line_style()

    if mode == MODE_ALL:
        result = _collect_all()
        if not result:
            return
        all_loops, counts, skipped = result
        _build_and_create(frt, all_loops, counts, skipped,
                          line_style_id=line_style_id)
        return

    # Pick modes: keep picking batches until the user stops.
    linked = (mode == MODE_LINK)
    total = 0
    while True:
        result = _collect_picked(linked)
        if not result:
            break
        all_loops, counts, skipped = result
        total += _build_and_create(frt, all_loops, counts, skipped,
                                   announce=False,
                                   line_style_id=line_style_id) or 0
        if not forms.alert(
                "Created {} filled region(s) so far.\n"
                "Pick more elements?".format(total),
                title="Wall Fill Region", yes=True, no=True):
            break


if __name__ == "__main__":
    main()
