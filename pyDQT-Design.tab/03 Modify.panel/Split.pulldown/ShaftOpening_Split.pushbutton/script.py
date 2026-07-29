# -*- coding: utf-8 -*-
"""
Split Shaft Opening Tool (copy-paste method)

Splits a shaft opening that has several disconnected boundary loops into
separate individual shaft openings.

METHOD (per DQT's spec - mirrors the manual workflow):
  1. Copy-paste the shaft in place ONE COPY AT A TIME. Each copy is a faithful
     duplicate: same sketch (ALL boundary loops + any holes + the user's
     Symbolic Lines) and the same instance parameters (Base/Top Constraint,
     offsets).
  2. Immediately reduce that copy - open its boundary sketch with
     SketchEditScope and delete the boundary loops (and the symbolic lines)
     that do NOT belong to the region it should keep, then commit - BEFORE
     making the next copy. At most two full-sketch shafts ever overlap, so
     every regeneration stays small. (Making all N copies first and reducing
     them afterwards stacks N full shafts cutting the same geometry during
     every commit - a native-crash path on large shafts.)
  3. The original opening is reduced LAST: it stays complete as the copy
     source, and if anything fails mid-run the original is still untouched.

This preserves the shaft's SYMBOLIC LINES natively (they stay inside each
opening's sketch) instead of redrawing them as detached model lines, and it
keeps holes too.

WARNING: step 2 uses SketchEditScope.Commit() on a shaft opening sketch. On
some Revit builds this operation is unstable and can hard-crash Revit (an
unrecoverable native crash that Python try/except cannot catch). SAVE the model
before running. Everything is wrapped so that the ORIGINAL opening is never
deleted - if a crash happens mid-run, closing without saving loses nothing.

CRASH SAFEGUARDS:
  - Copies are made and reduced one at a time (see METHOD) so the
    regeneration load per commit stays minimal.
  - The sketch is validated before AND after deleting curves: if a kept loop
    would lose curves, or a removed loop leaves stray curves, the edit scope
    is CANCELLED instead of committed - committing a broken shaft sketch is a
    known native-crash path.
  - Failure handling is kept EXACTLY as in the run that succeeded on this
    machine: no failures preprocessor on plain transactions, and a plain
    Continue preprocessor only on the sketch commit.
  - The UI selection is cleared before any editing, and every element is
    re-fetched by Id right before use; stale references are never reused
    across regenerations.
  - Openings that belong to a model group are skipped (editing a grouped
    sketch is a known crash path - ungroup first).
  - BLACK BOX: every step is appended (open/write/close, unbuffered) to
    %TEMP%/DQT_ShaftSplit.log. After a hard crash the LAST line of that file
    names the exact operation that died.

SAFETY LIMIT: each SketchEditScope.Commit() carries some crash risk, so every
RUN gets a budget of MAX_BOUNDARIES_PER_SPLIT sketch commits, SHARED by all
selected openings (each resulting opening costs exactly one commit). Selecting
several openings can no longer multiply past the cap - once the budget is
spent the remaining openings are deferred untouched to a later run.

If one shaft has more boundaries than the limit (e.g. 100), it is divided into
intermediate multi-boundary openings of at most MAX_BOUNDARIES_PER_SPLIT
boundaries each (e.g. 100 -> 5 openings of 20), which costs only 5 commits.
Re-run this tool on those intermediate openings to finish splitting them
(pass 2). SAVE - ideally close and reopen - between passes: sketch edits stay
in memory and stacking passes in one session is itself a crash factor.

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
import datetime
import os
import tempfile


def _eid_int(eid):
    """Get integer value of an ElementId across Revit 2024-2027."""
    try:
        return eid.Value
    except:
        return eid.IntegerValue


doc = revit.doc
uidoc = revit.uidoc

_ON_LOOP_TOL = 1e-4   # feet - midpoint-on-boundary tolerance

MAX_BOUNDARIES_PER_SPLIT = 20   # hard cap on boundaries fully split in one run


def _chunk(seq, size):
    """Yield successive `size`-length slices of seq."""
    for i in range(0, len(seq), size):
        yield seq[i:i + size]


_LOG_PATH = os.path.join(tempfile.gettempdir(), "DQT_ShaftSplit.log")


def _log(msg):
    """Crash-proof black-box log: open/append/close per line so nothing is
    buffered - after a hard native crash the LAST line in the file names the
    exact operation that died."""
    try:
        f = open(_LOG_PATH, "a")
        try:
            f.write("{} | {}\n".format(
                datetime.datetime.now().strftime("%H:%M:%S"), msg))
        finally:
            f.close()
    except:
        pass


class _NoOpFailurePreproc(IFailuresPreprocessor):
    """Swallow sketch warnings so SketchEditScope.Commit isn't blocked by a
    dialog. Kept EXACTLY as in the run that succeeded on this machine (no
    DeleteAllWarnings, no per-transaction attachment) - every extra failure-
    handling callback is one more native round-trip that can differ from the
    proven-good configuration."""
    def PreprocessFailures(self, failuresAccessor):
        return FailureProcessingResult.Continue


def _in_group(elem):
    """True if the element belongs to a model group (sketch editing of grouped
    elements is a known crash path)."""
    try:
        return elem.GroupId != ElementId.InvalidElementId
    except:
        return False


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


def copy_one_in_place(opening):
    """Copy-paste the opening in the same place once, in its own transaction.
    Returns the copied shaft opening."""
    copies = []
    _log("copy: start (src {})".format(_eid_int(opening.Id)))
    t = Transaction(doc, "DQT - Copy shaft in place")
    t.Start()
    try:
        ids = List[ElementId]()
        ids.Add(opening.Id)
        new_ids = ElementTransformUtils.CopyElements(doc, ids, XYZ.Zero)
        for nid in new_ids:
            el = doc.GetElement(nid)
            if is_shaft_opening(el):
                copies.append(el)
        _log("copy: CopyElements OK, committing transaction...")
        t.Commit()
        _log("copy: commit OK -> {}".format(
            ", ".join(str(_eid_int(c.Id)) for c in copies)))
    except Exception:
        if t.HasStarted() and not t.HasEnded():
            t.RollBack()
        raise
    if len(copies) != 1:
        raise Exception("copy-paste did not produce a shaft copy "
                        "(got {})".format(len(copies)))
    return copies[0]


def _expected_loop_counts(curve_loops):
    """Number of boundary curves each loop index should have."""
    expected = {}
    for j, loop in enumerate(curve_loops):
        expected[j] = sum(1 for _ in loop)
    return expected


def _count_boundary_elements(opening, sketch, curve_loops):
    """Count boundary curve ELEMENTS currently present in the sketch, per
    loop index."""
    counts = {}
    for (e, c) in get_sketch_curve_elements(opening, sketch):
        kind, j = classify_curve(c, curve_loops)
        if kind == "boundary":
            counts[j] = counts.get(j, 0) + 1
    return counts


def reduce_opening_to_region(opening, keep_set, curve_loops):
    """Edit the opening's sketch and delete every boundary loop / symbolic line
    that does NOT belong to a loop index in keep_set. Uses SketchEditScope.
    Returns kept_symbolic_count. Raises on managed failure.

    The sketch is validated before and after deleting: a broken sketch (a
    kept loop missing curves, or a removed loop leaving strays) is never
    committed - the edit scope is cancelled instead, because committing an
    invalid shaft sketch can hard-crash Revit."""
    oid = _eid_int(opening.Id)
    _log("reduce {}: begin (keep {} loop(s))".format(oid, len(keep_set)))
    sketch = get_sketch(opening)
    if sketch is None:
        raise Exception("opening has no sketch")

    expected = _expected_loop_counts(curve_loops)

    delete_ids = []
    kept_symbolic = 0
    found_boundary = {}
    for (e, c) in get_sketch_curve_elements(opening, sketch):
        kind, j = classify_curve(c, curve_loops)
        if kind == "boundary":
            found_boundary[j] = found_boundary.get(j, 0) + 1
            if j not in keep_set:
                delete_ids.append(e.Id)
        elif kind == "symbolic":
            if j in keep_set:
                kept_symbolic += 1
            else:
                delete_ids.append(e.Id)
        # "other" -> keep, safe

    # Pre-check: every loop must be fully re-identified in THIS sketch. A
    # shortfall means classification drifted (tolerance) and deleting would
    # leave a broken sketch - skip this opening instead of risking the commit.
    for j in range(len(curve_loops)):
        if found_boundary.get(j, 0) < expected.get(j, 0):
            raise Exception(
                "loop {}: only {} of {} boundary curves identified in the "
                "sketch - classification mismatch, skipping this opening "
                "instead of committing a broken sketch".format(
                    j, found_boundary.get(j, 0), expected.get(j, 0)))

    if not delete_ids:
        _log("reduce {}: nothing to delete, done".format(oid))
        return kept_symbolic

    _log("reduce {}: {} curve(s) to delete, SketchEditScope.Start...".format(
        oid, len(delete_ids)))
    ses = SketchEditScope(doc, "DQT - Reduce shaft opening")
    ses.Start(sketch.Id)
    _log("reduce {}: scope started, delete transaction...".format(oid))
    t = Transaction(doc, "DQT - Delete extra loops")
    t.Start()
    try:
        for did in delete_ids:
            try:
                doc.Delete(did)
            except Exception as ex:
                print("    (could not delete curve {}: {})".format(_eid_int(did), ex))
        _log("reduce {}: deletes done, committing transaction...".format(oid))
        t.Commit()
        _log("reduce {}: delete transaction OK".format(oid))
    except Exception:
        if t.HasStarted() and not t.HasEnded():
            t.RollBack()
        try:
            ses.Cancel()
        except:
            pass
        raise

    # Post-check: kept loops must still be complete and removed loops fully
    # gone. Otherwise cancel the scope - never commit a broken sketch.
    counts_after = _count_boundary_elements(opening, sketch, curve_loops)
    bad = None
    for j in keep_set:
        if counts_after.get(j, 0) < expected.get(j, 0):
            bad = "kept loop {} lost boundary curves ({} of {} left)".format(
                j, counts_after.get(j, 0), expected.get(j, 0))
            break
    if bad is None:
        for j in range(len(curve_loops)):
            if j not in keep_set and counts_after.get(j, 0) > 0:
                bad = "removed loop {} still has {} boundary curve(s)".format(
                    j, counts_after.get(j, 0))
                break
    if bad is not None:
        try:
            ses.Cancel()
        except:
            pass
        raise Exception("sketch invalid after deleting ({}) - edit scope "
                        "cancelled instead of committed".format(bad))

    _log("reduce {}: post-check OK, SketchEditScope.Commit >>>".format(oid))
    try:
        ses.Commit(_NoOpFailurePreproc())   # <-- SketchEditScope commit (crash risk)
    except Exception:
        try:
            ses.Cancel()
        except:
            pass
        raise
    _log("reduce {}: SketchEditScope.Commit OK".format(oid))
    return kept_symbolic


def _group_keep_set(group, holes_of):
    """keep_set for a batch of main-loop indices: the loops plus their holes."""
    keep_set = set(group)
    for main_idx in group:
        keep_set |= holes_of.get(main_idx, set())
    return keep_set


def _split_interleaved(opening, keep_sets, curve_loops):
    """Shared engine: produce one opening per keep_set. Copies are made ONE AT
    A TIME and each copy is reduced immediately, so at most two full-sketch
    shafts overlap during any commit. The ORIGINAL is reduced last (to
    keep_sets[0]) - it stays complete as the copy source, and a mid-run
    failure leaves it untouched."""
    original_id = opening.Id
    result_openings = []
    symbolic_kept = 0

    for keep_set in keep_sets[1:]:
        src = doc.GetElement(original_id)
        cp = copy_one_in_place(src)
        print("  Copy {} -> reducing to {} loop(s)...".format(
            _eid_int(cp.Id), len(keep_set)))
        kept = reduce_opening_to_region(cp, keep_set, curve_loops)
        symbolic_kept += kept
        result_openings.append(cp)
        print("    -> kept {} symbolic line(s)".format(kept))

    src = doc.GetElement(original_id)
    print("  Reducing ORIGINAL {} to {} loop(s)...".format(
        _eid_int(original_id), len(keep_sets[0])))
    kept = reduce_opening_to_region(src, keep_sets[0], curve_loops)
    symbolic_kept += kept
    result_openings.insert(0, src)
    print("    -> kept {} symbolic line(s)".format(kept))

    return result_openings, symbolic_kept


def _split_into_singles(opening, main_indices, holes_of, curve_loops):
    """One opening per main boundary - fully split, each keeps its real
    Symbolic Lines."""
    keep_sets = [_group_keep_set([i], holes_of) for i in main_indices]
    result_openings, symbolic_kept = _split_interleaved(
        opening, keep_sets, curve_loops)
    return result_openings, symbolic_kept, False


def _split_into_groups(opening, main_indices, holes_of, curve_loops):
    """Too many boundaries for one safe run: instead of fully splitting all of
    them, produce one intermediate opening per batch of up to
    MAX_BOUNDARIES_PER_SPLIT boundaries (still multi-boundary). The caller
    must re-run Split on each of these to finish (pass 2, now under the
    limit)."""
    groups = list(_chunk(main_indices, MAX_BOUNDARIES_PER_SPLIT))
    print("  {} boundaries > limit of {} - grouping into {} intermediate "
          "opening(s) of up to {} boundaries each (run Split again on each "
          "to finish)...".format(
              len(main_indices), MAX_BOUNDARIES_PER_SPLIT, len(groups),
              MAX_BOUNDARIES_PER_SPLIT))
    keep_sets = [_group_keep_set(g, holes_of) for g in groups]
    result_openings, symbolic_kept = _split_interleaved(
        opening, keep_sets, curve_loops)
    return result_openings, symbolic_kept, True


class _Deferred(object):
    """Sentinel: the opening was left untouched because the run's commit
    budget could not cover it (distinct from 'skipped, nothing to do')."""
    pass


DEFERRED = _Deferred()


def _planned_cost(main_count):
    """How many SketchEditScope commits processing this opening will cost.

    Each resulting opening costs exactly one sketch commit, so a full split of
    N boundaries costs N, while a grouping pass costs only the number of
    groups. This is the number that must be budgeted per RUN - it is the
    commit count, not the boundary count, that drives the crash risk."""
    if main_count > MAX_BOUNDARIES_PER_SPLIT:
        # grouping pass: ceil(N / MAX) groups
        return (main_count + MAX_BOUNDARIES_PER_SPLIT - 1) // MAX_BOUNDARIES_PER_SPLIT
    return main_count


def split_shaft(opening, budget):
    """Split one multi-boundary shaft opening, spending at most `budget`
    sketch commits. Returns (result_openings, symbolic_kept, is_batch) or
    None if nothing was done (skipped, or too expensive for the remaining
    budget - see _planned_cost).

    is_batch is True when the boundary count exceeded
    MAX_BOUNDARIES_PER_SPLIT and result_openings are intermediate,
    still-multi-boundary groups that need a second Split pass rather than
    fully-split single regions."""
    if _in_group(opening):
        print("  SKIPPED: this opening belongs to a model group - editing a "
              "grouped sketch can crash Revit. Ungroup it first.")
        return None

    sketch = get_sketch(opening)
    curve_loops = get_curve_loops_from_sketch(sketch)
    if len(curve_loops) <= 1:
        print("  Shaft opening has only one boundary - skipping")
        return None

    main_indices, holes_of = analyze_loops(curve_loops)
    print("\n  Loops: {} total, {} main boundary(ies), {} hole(s)".format(
        len(curve_loops), len(main_indices), len(curve_loops) - len(main_indices)))
    _log("opening {}: {} loop(s), {} main, {} hole(s)".format(
        _eid_int(opening.Id), len(curve_loops), len(main_indices),
        len(curve_loops) - len(main_indices)))

    if len(main_indices) <= 1:
        print("  Only one main boundary - nothing to split")
        return None

    cost = _planned_cost(len(main_indices))
    if cost > budget:
        print("  DEFERRED: this opening needs {} sketch commit(s) but only {} "
              "left in this run's budget of {}.\n"
              "  Save the model, then run Split again and select this opening "
              "on its own.".format(cost, budget, MAX_BOUNDARIES_PER_SPLIT))
        _log("opening {}: deferred (cost {} > budget {})".format(
            _eid_int(opening.Id), cost, budget))
        return DEFERRED

    if len(main_indices) > MAX_BOUNDARIES_PER_SPLIT:
        return _split_into_groups(opening, main_indices, holes_of, curve_loops)
    return _split_into_singles(opening, main_indices, holes_of, curve_loops)


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
            "SAFETY LIMIT: {} sketch commits per run, SHARED by everything you "
            "select. Openings that do not fit are deferred to a later run, and "
            "a shaft with more boundaries than that is first divided into "
            "intermediate openings - re-run Split on those to finish.\n\n"
            "SAVE (ideally close and reopen) between passes - stacking passes "
            "in one session is itself a crash factor.\n\n"
            "Openings inside a model group are skipped - ungroup them first.\n\n"
            "Click OK, then select the shaft opening(s) to split.".format(
                MAX_BOUNDARIES_PER_SPLIT),
            title="Split Shaft Opening Tool",
            ok=True, cancel=True)
        if not proceed:
            return

        selected_ids = []
        try:
            refs = uidoc.Selection.PickObjects(
                ObjectType.Element,
                "Select shaft openings to split (ESC / Finish when done)")
        except:
            return

        for ref in refs:
            el = doc.GetElement(ref.ElementId)
            if is_shaft_opening(el):
                selected_ids.append(ref.ElementId)
            else:
                print("Skipping non-shaft element (ID {})".format(_eid_int(ref.ElementId)))

        if not selected_ids:
            forms.alert("No shaft openings selected.", exitscript=True)

        # Clear the UI selection before editing - deleting / sketch-editing
        # elements that sit in the active selection set is one more native
        # code path we don't need during the run.
        try:
            uidoc.Selection.SetElementIds(List[ElementId]())
        except:
            pass

        print("\n" + "=" * 60)
        print("SPLIT SHAFT OPENING (copy-paste method) - {} opening(s)".format(
            len(selected_ids)))
        print("Black-box log: {}".format(_LOG_PATH))
        print("(if Revit crashes, the LAST line of that file names the exact")
        print(" operation that died - please send it to DQT)")
        print("=" * 60)
        _log("=" * 50)
        _log("RUN start - {} opening(s) selected".format(len(selected_ids)))

        total_result = 0
        total_symbolic = 0
        successful = 0
        failed = 0
        batch_ids = []
        deferred = 0
        # Budget is per RUN, shared by every selected opening - it is the
        # total number of sketch commits that matters, not the count per
        # opening.
        budget = MAX_BOUNDARIES_PER_SPLIT

        for idx, oid in enumerate(selected_ids):
            print("\n" + "-" * 60)
            print("Processing shaft opening {}/{} (ID {})".format(
                idx + 1, len(selected_ids), _eid_int(oid)))
            print("-" * 60)
            _log("opening {} ({}/{}): processing... (budget left {})".format(
                _eid_int(oid), idx + 1, len(selected_ids), budget))
            print("  Run budget left: {} of {} sketch commit(s)".format(
                budget, MAX_BOUNDARIES_PER_SPLIT))
            # Re-fetch fresh by Id - never reuse a wrapper that survived the
            # previous opening's commits/regenerations.
            opening = doc.GetElement(oid)
            if opening is None or not is_shaft_opening(opening):
                print("  Opening no longer valid - skipping")
                _log("opening {}: no longer valid, skipped".format(_eid_int(oid)))
                continue
            try:
                result = split_shaft(opening, budget)
                if result is DEFERRED:
                    deferred += 1
                elif result:
                    result_openings, symbolic_kept, is_batch = result
                    # Each resulting opening cost exactly one sketch commit.
                    budget -= len(result_openings)
                    total_result += len(result_openings)
                    total_symbolic += symbolic_kept
                    successful += 1
                    if is_batch:
                        batch_ids.extend(_eid_int(op.Id) for op in result_openings)
                        print("GROUPED (pass 1 of 2): {} intermediate opening(s), "
                              "{} symbolic line(s) kept. Re-run Split on these "
                              "to finish.".format(len(result_openings), symbolic_kept))
                    else:
                        print("SUCCESS: {} region opening(s), {} symbolic line(s) kept".format(
                            len(result_openings), symbolic_kept))
                _log("opening {}: DONE".format(_eid_int(oid)))
            except Exception as e:
                failed += 1
                import traceback
                print("FAILED: {}".format(e))
                print(traceback.format_exc())
                _log("opening {}: FAILED (managed): {}".format(_eid_int(oid), e))
                continue

        _log("RUN end - {} ok, {} failed".format(successful, failed))
        print("\n" + "=" * 60)
        print("SUMMARY")
        print("=" * 60)
        print("Shaft openings processed : {}".format(len(selected_ids)))
        print("Successful splits        : {}".format(successful))
        print("Failed splits            : {}".format(failed))
        print("Resulting shaft openings : {}".format(total_result))
        print("Symbolic lines preserved : {}".format(total_symbolic))
        print("Sketch commits used      : {} of {}".format(
            MAX_BOUNDARIES_PER_SPLIT - budget, MAX_BOUNDARIES_PER_SPLIT))
        if deferred:
            print("Deferred (budget)        : {} opening(s)".format(deferred))
        if batch_ids:
            print("Grouped, need 2nd pass   : {} opening(s) -> IDs {}".format(
                len(batch_ids), ", ".join(str(i) for i in batch_ids)))
        print("=" * 60)

        msg = (
            "Split Shaft Opening Complete!\n\n"
            "Processed: {} shaft opening(s)\n"
            "Successful: {}\n"
            "Failed: {}\n"
            "Resulting shaft openings: {}\n"
            "Symbolic lines preserved (native): {}"
        ).format(len(selected_ids), successful, failed, total_result, total_symbolic)
        if deferred:
            msg += (
                "\n\nDeferred: {} opening(s) were left untouched because this "
                "run's budget of {} sketch commits was already spent."
            ).format(deferred, MAX_BOUNDARIES_PER_SPLIT)
        if batch_ids:
            msg += (
                "\n\nSafety limit reached (max {} sketch commits/run): {} of "
                "the resulting openings still have several boundaries grouped "
                "together.\nSelect them and run Split Shaft again to finish "
                "(pass 2)."
            ).format(MAX_BOUNDARIES_PER_SPLIT, len(batch_ids))
        if batch_ids or deferred:
            msg += ("\n\nIMPORTANT: SAVE the model (and ideally close and "
                    "reopen it) before the next pass. Sketch edits from this "
                    "run stay in memory and running another pass on top of "
                    "them in the same session is what has been crashing.")
        forms.alert(msg, title="Split Shaft Opening Summary")

    except Exception as e:
        import traceback
        print("\n=== MAIN ERROR ===")
        print(traceback.format_exc())
        forms.alert("Error: {}".format(e), exitscript=True)


if __name__ == "__main__":
    main()
