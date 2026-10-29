# -*- coding: utf-8 -*-
"""Merge Fill Region - DQT
Merge several filled regions in the active view into a single filled region,
using the first selected region's type.

Boundaries are extruded along the view direction and unioned, then the merged
outline is read back from the solid - so it works in any view orientation
(plan / section / elevation), keeps holes, and cleans up collinear/short edges.
Original regions are deleted only after the merged one is created.

Copyright (c) 2026 by Dang Quoc Truong (DQT)
"""
try: __import__("dqt_usage").log_use(__file__)  # pyDQT usage log
except Exception: pass

__title__ = "Merge Fill\nRegion"
__author__ = "Dang Quoc Truong (DQT)"
__doc__ = ("Merge selected filled regions into one (uses the first region's "
           "type).\nCopyright (c) 2026 by Dang Quoc Truong (DQT)")

from Autodesk.Revit.DB import (
    FilledRegion, CurveLoop, Line, XYZ, Transaction,
    GeometryCreationUtilities, BooleanOperationsUtils, BooleanOperationsType,
    PlanarFace
)
from Autodesk.Revit.UI.Selection import ObjectType, ISelectionFilter
from System.Collections.Generic import List
from pyrevit import forms, script

doc = __revit__.ActiveUIDocument.Document
uidoc = __revit__.ActiveUIDocument
app = __revit__.Application
view = doc.ActiveView
output = script.get_output()

TOL = 1e-7
EXTRUDE_H = 1.0
try:
    MIN_SEG = app.ShortCurveTolerance * 1.5
except Exception:
    MIN_SEG = 0.0052


class _FRFilter(ISelectionFilter):
    def AllowElement(self, e):
        return isinstance(e, FilledRegion)

    def AllowReference(self, r, p):
        return False


def _pick_regions():
    sel = [doc.GetElement(i) for i in uidoc.Selection.GetElementIds()]
    frs = [e for e in sel if isinstance(e, FilledRegion)
           and e.OwnerViewId == view.Id]
    if len(frs) >= 2:
        return frs
    try:
        refs = uidoc.Selection.PickObjects(
            ObjectType.Element, _FRFilter(),
            "Select 2+ filled regions to merge, then Finish")
    except Exception:
        return []
    out = []
    for r in refs:
        e = doc.GetElement(r.ElementId)
        if isinstance(e, FilledRegion):
            out.append(e)
    return out


def _clean_loop(loop):
    """Drop near-duplicate / collinear vertices and reject short segments.
    Loops containing non-line curves (arcs) are returned unchanged."""
    curves = list(loop)
    if not all(isinstance(c, Line) for c in curves):
        return loop
    pts = [c.GetEndPoint(0) for c in curves]

    # remove consecutive near-duplicate points (incl. wrap-around)
    dedup = []
    for p in pts:
        if dedup and dedup[-1].DistanceTo(p) <= MIN_SEG:
            continue
        dedup.append(p)
    while len(dedup) >= 2 and dedup[0].DistanceTo(dedup[-1]) <= MIN_SEG:
        dedup.pop()
    if len(dedup) < 3:
        return None

    # remove collinear vertices, repeat until stable
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


def main():
    regions = _pick_regions()
    if len(regions) < 2:
        forms.alert("Select at least 2 filled regions (in this view) to merge.",
                    title="Merge Fill Region")
        return

    type_id = regions[0].GetTypeId()
    axis = view.ViewDirection

    # Extrude each region's boundary into a thin solid.
    solids = []
    for fr in regions:
        try:
            solids.append(GeometryCreationUtilities.CreateExtrusionGeometry(
                fr.GetBoundaries(), axis, EXTRUDE_H))
        except Exception:
            pass
    if not solids:
        forms.alert("Could not read the filled region boundaries.",
                    title="Merge Fill Region")
        return

    # Union them all.
    combined = solids[0]
    for s in solids[1:]:
        try:
            combined = BooleanOperationsUtils.ExecuteBooleanOperation(
                combined, s, BooleanOperationsType.Union)
        except Exception:
            pass

    # Read the merged outline from the cap facing the region plane.
    base, top = [], []
    for f in combined.Faces:
        if isinstance(f, PlanarFace):
            d = f.FaceNormal.DotProduct(axis)
            if d < -0.9:
                base.append(f)
            elif d > 0.9:
                top.append(f)
    faces = base if base else top

    profile = List[CurveLoop]()
    for f in faces:
        try:
            for cl in f.GetEdgesAsCurveLoops():
                cleaned = _clean_loop(cl)
                if cleaned is not None:
                    profile.Add(cleaned)
        except Exception:
            continue
    if profile.Count == 0:
        forms.alert("Could not build the merged boundary.",
                    title="Merge Fill Region")
        return

    # Create the merged region first; only delete the originals if it works.
    t = Transaction(doc, "DQT - Merge Fill Region")
    t.Start()
    try:
        FilledRegion.Create(doc, type_id, view.Id, profile)
        for fr in regions:
            try:
                doc.Delete(fr.Id)
            except Exception:
                pass
        t.Commit()
    except Exception as ex:
        t.RollBack()
        forms.alert("Could not create the merged filled region:\n{}".format(ex),
                    title="Merge Fill Region")
        return

    msg = "Merged {} filled regions into 1.".format(len(regions))
    output.print_md("**Merge Fill Region** - {}".format(msg))
    forms.alert(msg, title="Merge Fill Region")


if __name__ == "__main__":
    main()
