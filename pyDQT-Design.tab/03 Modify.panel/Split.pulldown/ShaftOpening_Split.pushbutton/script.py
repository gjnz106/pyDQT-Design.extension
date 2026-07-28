# -*- coding: utf-8 -*-
"""
Split Shaft Opening Tool (copy-paste method)

Splits a shaft opening that has several disconnected boundary loops into
separate individual shaft openings.

METHOD (per DQT's spec - mirrors the manual workflow):
  1. Copy-paste the shaft in place once per boundary. Each copy is a faithful
     duplicate: same sketch (ALL boundary loops + any holes + the user's
     Symbolic Lines) and the same instance parameters (Base/Top Constraint,
     offsets).
  2. For each copy, open its boundary sketch with SketchEditScope and delete
     the boundary loops (and the symbolic lines) that do NOT belong to the one
     region that copy should keep. What remains is a single-region shaft that
     still carries its real Symbolic Lines and all parameters.
  3. The original opening is reused as the first region, so nothing has to be
     recreated from scratch.

This preserves the shaft's SYMBOLIC LINES natively (they stay inside each
opening's sketch) instead of redrawing them as detached model lines, and it
keeps holes too.

WARNING: step 2 uses SketchEditScope.Commit() on a shaft opening sketch. On
some Revit builds this operation is unstable and can hard-crash Revit (an
unrecoverable native crash that Python try/except cannot catch). SAVE the model
before running. Everything is wrapped so that the ORIGINAL opening is never
deleted - if a crash happens mid-run, closing without saving loses nothing.

Dang Quoc Truong - DQT (c) 2026
"""

__title__ = "Split\nShaft"
__author__ = "DQT"

from Autodesk.Revit.DB import *
from Autodesk.Revit.UI.Selection import ObjectType
from pyrevit import revit, DB, UI, forms
from System.Collections.Generic import List
import clr
clr.AddReference('System.Core')


def _eid_int(eid):
    """Get integer value of an ElementId across Revit 2024-2027."""
    try:
        return eid.Value
    except:
        return eid.IntegerValue


doc = revit.doc
uidoc = revit.uidoc

_ON_LOOP_TOL = 1e-4   # feet - midpoint-on-boundary tolerance


class _NoOpFailurePreproc(IFailuresPreprocessor):
    """Swallow sketch warnings so SketchEditScope.Commit isn't blocked by a
    dialog. (It cannot stop a native crash - only managed failures.)"""
    def PreprocessFailures(self, failuresAccessor):
        return FailureProcessingResult.Continue


def is_shaft_opening(elem):
    if not isinstance(elem, Opening):
        return False
    try:
        cat = elem.Category
        return cat is not None and cat.Id == Category.GetCategory(
            doc, DB.BuiltInCategory.OST_ShaftOpening).Id
    except:
        return False


def get_sketch(opening):
    """The boundary Sketch element of an opening (or None)."""
    try:
        for sid in opening.GetDependentElements(DB.ElementClassFilter(DB.Sketch)):
            sk = doc.GetElement(sid)
            if isinstance(sk, DB.Sketch):
                return sk
    except:
        pass
    return None


def get_curve_loops_from_sketch(sketch):
    """All boundary loops of a sketch as a list of CurveLoop (geometry)."""
    curve_loops = []
    if sketch is None:
        return curve_loops
    try:
        for curve_array in sketch.Profile:
            loop = CurveLoop()
            for curve in curve_array:
                loop.Append(curve)
            curve_loops.append(loop)
    except:
        pass
    return curve_loops


def get_sketch_curve_elements(opening, sketch):
    """Every CurveElement that belongs to the opening's sketch (both boundary
    lines and symbolic lines), as a list of (element, geometry_curve)."""
    found = {}
    hosts = [opening]
    if sketch is not None:
        hosts.append(sketch)
    for host in hosts:
        try:
            dep = host.GetDependentElements(None)
        except:
            continue
        for did in dep:
            e = doc.GetElement(did)
            if e is None:
                continue
            try:
                c = e.GeometryCurve
            except:
                continue
            if c is None:
                continue
            found[_eid_int(e.Id)] = (e, c)
    return list(found.values())


def _curve_midpoint(curve):
    try:
        return curve.Evaluate(0.5, True)
    except:
        try:
            return curve.GetEndPoint(0)
        except:
            return None


def point_on_loop(point, loop, tol=_ON_LOOP_TOL):
    """True if point lies on (within tol of) any curve of the loop."""
    for curve in loop:
        try:
            if curve.Distance(point) < tol:
                return True
        except:
            pass
    return False


def point_in_loop(point, loop):
    """Even-odd ray cast: is point strictly inside the loop."""
    ray_end = XYZ(point.X + 10000, point.Y, point.Z)
    ray = Line.CreateBound(point, ray_end)
    count = 0
    for curve in loop:
        try:
            if curve.Intersect(ray) == DB.SetComparisonResult.Overlap:
                count += 1
        except:
            pass
    return count % 2 == 1


def check_if_loop_is_inside(inner_loop, outer_loop):
    test_point = None
    for curve in inner_loop:
        test_point = curve.GetEndPoint(0)
        break
    if not test_point:
        return False
    return point_in_loop(test_point, outer_loop)


def analyze_loops(curve_loops):
    """Classify loops (by original index) into main boundaries and holes.
    Returns (main_indices, holes_of) where holes_of[main] = set(hole indices)."""
    n = len(curve_loops)
    areas = []
    for loop in curve_loops:
        pts = []
        for c in loop:
            pts.append(c.GetEndPoint(0))
            pts.append(c.GetEndPoint(1))
        if pts:
            xs = [p.X for p in pts]
            ys = [p.Y for p in pts]
            areas.append((max(xs) - min(xs)) * (max(ys) - min(ys)))
        else:
            areas.append(0.0)

    parent = [-1] * n
    for i in range(n):
        for j in range(n):
            if i == j:
                continue
            if areas[j] > areas[i] and check_if_loop_is_inside(curve_loops[i], curve_loops[j]):
                # smallest containing loop wins as parent
                if parent[i] == -1 or areas[j] < areas[parent[i]]:
                    parent[i] = j

    is_hole = [parent[i] != -1 for i in range(n)]
    main_indices = [i for i in range(n) if not is_hole[i]]
    holes_of = {}
    for i in range(n):
        if is_hole[i]:
            holes_of.setdefault(parent[i], set()).add(i)
    return main_indices, holes_of


def classify_curve(curve, curve_loops):
    """('boundary', loop_index) if the curve is a boundary segment of a loop,
    ('symbolic', loop_index) if it is a free line inside a loop,
    ('other', -1) otherwise."""
    mid = _curve_midpoint(curve)
    if mid is None:
        return ("other", -1)
    for j, loop in enumerate(curve_loops):
        if point_on_loop(mid, loop):
            return ("boundary", j)
    for j, loop in enumerate(curve_loops):
        if point_in_loop(mid, loop):
            return ("symbolic", j)
    return ("other", -1)


def copy_in_place(opening, count):
    """Copy-paste the opening in the same place `count` times. Returns the list
    of copied shaft openings. Runs in one transaction."""
    copies = []
    t = Transaction(doc, "DQT - Copy shaft in place")
    t.Start()
    try:
        for _ in range(count):
            ids = List[ElementId]()
            ids.Add(opening.Id)
            new_ids = ElementTransformUtils.CopyElements(doc, ids, XYZ.Zero)
            for nid in new_ids:
                el = doc.GetElement(nid)
                if is_shaft_opening(el):
                    copies.append(el)
        t.Commit()
    except Exception:
        if t.HasStarted() and not t.HasEnded():
            t.RollBack()
        raise
    return copies


def reduce_opening_to_region(opening, keep_set, curve_loops):
    """Edit the opening's sketch and delete every boundary loop / symbolic line
    that does NOT belong to a loop index in keep_set. Uses SketchEditScope.
    Returns kept_symbolic_count. Raises on managed failure."""
    sketch = get_sketch(opening)
    if sketch is None:
        raise Exception("opening has no sketch")

    delete_ids = []
    kept_symbolic = 0
    for (e, c) in get_sketch_curve_elements(opening, sketch):
        kind, j = classify_curve(c, curve_loops)
        if kind == "boundary":
            if j not in keep_set:
                delete_ids.append(e.Id)
        elif kind == "symbolic":
            if j in keep_set:
                kept_symbolic += 1
            else:
                delete_ids.append(e.Id)
        # "other" -> keep, safe

    if not delete_ids:
        return kept_symbolic

    ses = SketchEditScope(doc, "DQT - Reduce shaft to one region")
    ses.Start(sketch.Id)
    t = Transaction(doc, "DQT - Delete extra loops")
    t.Start()
    try:
        for did in delete_ids:
            try:
                doc.Delete(did)
            except Exception as ex:
                print("    (could not delete curve {}: {})".format(_eid_int(did), ex))
        t.Commit()
    except Exception:
        if t.HasStarted() and not t.HasEnded():
            t.RollBack()
        try:
            ses.Cancel()
        except:
            pass
        raise

    ses.Commit(_NoOpFailurePreproc())   # <-- SketchEditScope commit (crash risk)
    return kept_symbolic


def split_shaft(opening):
    """Split one multi-boundary shaft opening. Returns
    (result_openings, symbolic_kept) or None if nothing to split."""
    sketch = get_sketch(opening)
    curve_loops = get_curve_loops_from_sketch(sketch)
    if len(curve_loops) <= 1:
        print("  Shaft opening has only one boundary - skipping")
        return None

    main_indices, holes_of = analyze_loops(curve_loops)
    print("\n  Loops: {} total, {} main boundary(ies), {} hole(s)".format(
        len(curve_loops), len(main_indices), len(curve_loops) - len(main_indices)))

    if len(main_indices) <= 1:
        print("  Only one main boundary - nothing to split")
        return None

    # One opening per main boundary; reuse the original for the first region.
    print("  Copy-pasting {} in-place copy(ies)...".format(len(main_indices) - 1))
    copies = copy_in_place(opening, len(main_indices) - 1)
    if len(copies) != len(main_indices) - 1:
        raise Exception("expected {} copies, got {}".format(
            len(main_indices) - 1, len(copies)))

    openings = [opening] + copies

    result_openings = []
    symbolic_kept = 0
    for op, main_idx in zip(openings, main_indices):
        keep_set = set([main_idx]) | holes_of.get(main_idx, set())
        print("  Reducing opening {} to region (loop {}, {} hole(s))...".format(
            _eid_int(op.Id), main_idx, len(holes_of.get(main_idx, set()))))
        kept = reduce_opening_to_region(op, keep_set, curve_loops)
        symbolic_kept += kept
        result_openings.append(op)
        print("    -> kept {} symbolic line(s)".format(kept))

    return (result_openings, symbolic_kept)


def main():
    try:
        proceed = forms.alert(
            "Split a shaft opening that has several disconnected boundaries into "
            "separate shaft openings.\n\n"
            "Method: copy the shaft in place, then trim each copy down to one "
            "region - so every result keeps its real Symbolic Lines, holes and "
            "parameters.\n\n"
            "IMPORTANT: this edits the shaft sketch (SketchEditScope), which can "
            "crash Revit on some builds. SAVE your model first.\n\n"
            "Click OK, then select the shaft opening(s) to split.",
            title="Split Shaft Opening Tool",
            ok=True, cancel=True)
        if not proceed:
            return

        selected_openings = []
        try:
            refs = uidoc.Selection.PickObjects(
                ObjectType.Element,
                "Select shaft openings to split (ESC / Finish when done)")
        except:
            return

        for ref in refs:
            el = doc.GetElement(ref.ElementId)
            if is_shaft_opening(el):
                selected_openings.append(el)
            else:
                print("Skipping non-shaft element (ID {})".format(_eid_int(ref.ElementId)))

        if not selected_openings:
            forms.alert("No shaft openings selected.", exitscript=True)

        print("\n" + "=" * 60)
        print("SPLIT SHAFT OPENING (copy-paste method) - {} opening(s)".format(
            len(selected_openings)))
        print("=" * 60)

        total_result = 0
        total_symbolic = 0
        successful = 0
        failed = 0

        for idx, opening in enumerate(selected_openings):
            print("\n" + "-" * 60)
            print("Processing shaft opening {}/{} (ID {})".format(
                idx + 1, len(selected_openings), _eid_int(opening.Id)))
            print("-" * 60)
            try:
                result = split_shaft(opening)
                if result:
                    result_openings, symbolic_kept = result
                    total_result += len(result_openings)
                    total_symbolic += symbolic_kept
                    successful += 1
                    print("SUCCESS: {} region opening(s), {} symbolic line(s) kept".format(
                        len(result_openings), symbolic_kept))
            except Exception as e:
                failed += 1
                import traceback
                print("FAILED: {}".format(e))
                print(traceback.format_exc())
                continue

        print("\n" + "=" * 60)
        print("SUMMARY")
        print("=" * 60)
        print("Shaft openings processed : {}".format(len(selected_openings)))
        print("Successful splits        : {}".format(successful))
        print("Failed splits            : {}".format(failed))
        print("Resulting shaft openings : {}".format(total_result))
        print("Symbolic lines preserved : {}".format(total_symbolic))
        print("=" * 60)

        msg = (
            "Split Shaft Opening Complete!\n\n"
            "Processed: {} shaft opening(s)\n"
            "Successful: {}\n"
            "Failed: {}\n"
            "Resulting shaft openings: {}\n"
            "Symbolic lines preserved (native): {}"
        ).format(len(selected_openings), successful, failed, total_result, total_symbolic)
        forms.alert(msg, title="Split Shaft Opening Summary")

    except Exception as e:
        import traceback
        print("\n=== MAIN ERROR ===")
        print(traceback.format_exc())
        forms.alert("Error: {}".format(e), exitscript=True)


if __name__ == "__main__":
    main()
