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
  - DEPENDENT-ELEMENT QUERIES ARE KEPT NARROW. Element.GetDependentElements
    opens a temporary transaction internally; asking the OPENING for its
    dependents with no filter drags in the floors and walls the shaft cuts
    and their regeneration, and that scan is where Revit died. Only the
    sketch is queried, and only for CurveElement.
  - NO DOCUMENT GEOMETRY IS HELD ACROSS A TRANSACTION. The boundary loops are
    converted to plain float polygons once, up front, and every containment
    test afterwards is pure arithmetic. Curve objects from Sketch.Profile are
    wrappers over document-owned native memory; after a copy/commit
    regenerates the document they can point at freed memory, and touching one
    hard-crashes Revit with no catchable exception. This was the actual cause
    of the reported crash, which struck while classifying curves - outside any
    transaction or edit scope.
  - Copies are made and reduced one at a time (see METHOD) so the
    regeneration load per commit stays minimal.
  - The sketch is validated before AND after deleting curves: if a kept loop
    would lose curves, or a removed loop leaves stray curves, the edit scope
    is CANCELLED instead of committed - committing a broken shaft sketch is a
    known native-crash path.
  - Warnings are deleted rather than shown. Copying a shaft in place raises
    "identical instances in the same place" on every copy - the copy is only
    identical for the moment before it is trimmed - so without this the user
    has to click through one dialog per copy.
  - Deleting sketch curves can cascade, so the delete step re-scans and
    retries (_DELETE_PASSES) instead of failing an opening over a single
    curve that survived the first pass. If a pass removes nothing, the
    curves are undeletable rather than stale and the opening is abandoned.
  - ALL OR NOTHING. If a split cannot finish, every copy made for it is
    deleted again and the original is left exactly as it was. A failed run
    must not leave duplicate shafts stacked on the original.
  - SHARED EDGES ARE KEPT. Loops in one shaft can touch, so a curve can
    belong to a removed loop AND a retained one at the same time. Such a
    curve is never deleted - the retained profile still needs it, and Revit
    refuses the delete anyway ("EditModeMgr element modifiable checker").
    A two-tier tolerance catches shared edges: first at the normal tolerance
    during classification, then at a wider fallback tolerance for any curves
    that Revit refused to delete - if the undeletable curve is near a kept
    loop's boundary, it is reclassified as shared rather than treated as a
    failure.
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
import math
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

_ON_LOOP_TOL = 5e-4   # feet - midpoint-on-boundary tolerance
_SHARED_EDGE_FALLBACK_TOL = 5e-3  # feet - re-check failed deletes for shared edges

MAX_BOUNDARIES_PER_SPLIT = 20   # hard cap on boundaries fully split in one run

_DELETE_PASSES = 3   # re-scan/retry passes for curves that survive a delete


def _chunk(seq, size):
    """Yield successive `size`-length slices of seq."""
    for i in range(0, len(seq), size):
        yield seq[i:i + size]


def _default_log_path():
    """A log location that stays the SAME across Revit restarts.

    tempfile.gettempdir() under Revit points at a per-session sandbox
    (...\\Temp\\<guid>\\) whose guid changes every time Revit starts. After a
    crash-and-restart the new session therefore logs to a different file and
    the crashed session's log is effectively unfindable - so anchor the log to
    the user profile instead."""
    for env in ("USERPROFILE", "HOME"):
        base = os.environ.get(env)
        if base:
            try:
                if os.path.isdir(base):
                    return os.path.join(base, "DQT_ShaftSplit.log")
            except:
                pass
    return os.path.join(tempfile.gettempdir(), "DQT_ShaftSplit.log")


_LOG_PATH = _default_log_path()


def _log(msg):
    """Crash-proof black-box log: open/append/close per line so nothing is
    buffered - after a hard native crash the LAST line in the file names the
    exact operation that died."""
    try:
        f = open(_LOG_PATH, "a")
        try:
            f.write("{} | {}\n".format(
                datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"), msg))
        finally:
            f.close()
    except:
        pass


class _NoOpFailurePreproc(IFailuresPreprocessor):
    """Delete warnings instead of letting Revit put them on screen.

    Copying a shaft in place legitimately produces "There are identical
    instances in the same place" on every single copy - the copy is identical
    only for the moment before it is trimmed down. Returning Continue without
    deleting them means Revit shows that dialog once per copy and the user has
    to click through all of them. Deleting the warnings here suppresses the
    dialog; it cannot suppress errors, and it cannot stop a native crash."""
    def PreprocessFailures(self, failuresAccessor):
        try:
            failuresAccessor.DeleteAllWarnings()
        except:
            pass
        return FailureProcessingResult.Continue


def _set_silent_failures(t):
    """Attach the warning-deleting preprocessor to a started transaction."""
    try:
        opts = t.GetFailureHandlingOptions()
        opts.SetFailuresPreprocessor(_NoOpFailurePreproc())
        opts.SetClearAfterRollback(True)
        t.SetFailureHandlingOptions(opts)
    except:
        pass


def _regen_checkpoint(tag):
    """Force any deferred host re-cutting to happen HERE, inside a logged
    transaction, rather than later outside the tool's control.

    A shaft opening re-cuts its hosts lazily; after the sketch commits, that
    work can still be pending and is then done by whatever touches the model
    next (a view redraw, a save, the next click). If that is where things
    break, doing it here makes it observable in the log instead of appearing
    as a crash long after the tool reported success."""
    _log("regen {}: start".format(tag))
    t = Transaction(doc, "DQT - Regenerate after split")
    t.Start()
    try:
        doc.Regenerate()
        t.Commit()
        _log("regen {}: OK".format(tag))
    except Exception as ex:
        if t.HasStarted() and not t.HasEnded():
            t.RollBack()
        _log("regen {}: FAILED {}".format(tag, ex))
        raise


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
    """Every CurveElement of the opening's sketch (boundary lines and symbolic
    lines), as a list of (element, (x, y)) where the tuple is the curve's
    MIDPOINT captured immediately as plain floats.

    Only the SKETCH is queried, and only for CurveElement.
    Element.GetDependentElements opens a temporary transaction internally to
    work out the dependency closure. Calling it on the OPENING with a null
    filter pulls in everything that depends on the shaft - including the
    floors and walls it cuts, and their regeneration - which is where Revit
    died during the scan. The sketch owns every curve we care about, so
    asking it directly is both correct and vastly cheaper.

    The Curve wrapper itself is deliberately NOT returned - see
    loops_to_polygons() for why document geometry must never be held."""
    if sketch is None:
        return []
    try:
        dep = sketch.GetDependentElements(DB.ElementClassFilter(DB.CurveElement))
    except Exception as ex:
        _log("    (sketch dependent query failed: {})".format(ex))
        return []

    found = {}
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
        mid = _curve_midpoint(c)
        if mid is None:
            continue
        eps = _curve_endpoints(c)
        found[_eid_int(e.Id)] = (e, mid, eps)
    return list(found.values())


def _curve_midpoint(curve):
    """Midpoint of a curve as a plain (x, y) tuple, read immediately."""
    try:
        p = curve.Evaluate(0.5, True)
    except:
        try:
            p = curve.GetEndPoint(0)
        except:
            return None
    try:
        return (p.X, p.Y)
    except:
        return None


def _curve_endpoints(curve):
    """Both endpoints of a curve as ((x0,y0), (x1,y1)), read immediately.

    Endpoint matching is more reliable than midpoint matching for boundary
    detection: endpoints are exact polygon vertices (shared between adjacent
    curves in a loop), whereas the midpoint of an arc can land far from the
    chord-based polygon approximation and miss the tolerance."""
    try:
        p0 = curve.GetEndPoint(0)
        p1 = curve.GetEndPoint(1)
        return ((p0.X, p0.Y), (p1.X, p1.Y))
    except:
        return None


def loops_to_polygons(curve_loops):
    """Snapshot loop geometry as plain float polygons, ONCE, up front.

    Curve objects handed out by Sketch.Profile are managed wrappers over
    geometry owned by the document. Every copy/commit this tool performs
    modifies and regenerates the document, after which those wrappers can
    point at freed native memory - touching one then hard-crashes Revit with
    no managed exception to catch. Converting to numbers here means every
    later containment test is pure Python and can never touch stale geometry.

    Returns a list of dicts: {"pts": [(x, y), ...] closed ring,
    "curve_count": how many curves the loop had}."""
    polys = []
    for loop in curve_loops:
        pts = []
        curve_count = 0
        for c in loop:
            curve_count += 1
            try:
                tess = c.Tessellate()
            except:
                try:
                    tess = [c.GetEndPoint(0), c.GetEndPoint(1)]
                except:
                    tess = []
            for p in tess:
                try:
                    xy = (p.X, p.Y)
                except:
                    continue
                if not pts or pts[-1] != xy:
                    pts.append(xy)
        if pts and pts[0] != pts[-1]:
            pts.append(pts[0])       # close the ring
        polys.append({"pts": pts, "curve_count": curve_count})
    return polys


def _dist_point_segment(px, py, ax, ay, bx, by):
    """Shortest distance from (px, py) to segment (ax, ay)-(bx, by)."""
    dx = bx - ax
    dy = by - ay
    if dx == 0.0 and dy == 0.0:
        return math.hypot(px - ax, py - ay)
    t = ((px - ax) * dx + (py - ay) * dy) / (dx * dx + dy * dy)
    if t < 0.0:
        t = 0.0
    elif t > 1.0:
        t = 1.0
    return math.hypot(px - (ax + t * dx), py - (ay + t * dy))


def point_on_poly(xy, poly, tol=_ON_LOOP_TOL):
    """True if the point lies on (within tol of) the polygon outline."""
    px, py = xy
    pts = poly["pts"]
    for i in range(len(pts) - 1):
        ax, ay = pts[i]
        bx, by = pts[i + 1]
        if _dist_point_segment(px, py, ax, ay, bx, by) < tol:
            return True
    return False


def point_in_poly(xy, poly):
    """Even-odd ray cast in pure arithmetic - no Revit geometry involved, so
    no long construction lines and nothing that can dereference the model."""
    px, py = xy
    pts = poly["pts"]
    inside = False
    for i in range(len(pts) - 1):
        x1, y1 = pts[i]
        x2, y2 = pts[i + 1]
        if (y1 > py) != (y2 > py):
            x_cross = x1 + (py - y1) * (x2 - x1) / (y2 - y1)
            if px < x_cross:
                inside = not inside
    return inside


def analyze_loops(polys):
    """Classify loops (by original index) into main boundaries and holes.
    Returns (main_indices, holes_of) where holes_of[main] = set(hole indices)."""
    n = len(polys)
    areas = []
    for poly in polys:
        pts = poly["pts"]
        if pts:
            xs = [p[0] for p in pts]
            ys = [p[1] for p in pts]
            areas.append((max(xs) - min(xs)) * (max(ys) - min(ys)))
        else:
            areas.append(0.0)

    parent = [-1] * n
    for i in range(n):
        inner_pts = polys[i]["pts"]
        if not inner_pts:
            continue
        test_point = inner_pts[0]
        for j in range(n):
            if i == j:
                continue
            if areas[j] > areas[i] and point_in_poly(test_point, polys[j]):
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


def loops_at(xy, polys, endpoints=None):
    """(on, inside): EVERY loop whose outline the point lies on, and every
    loop that strictly contains it.

    When endpoints are provided, boundary detection uses BOTH endpoints of
    the curve: if both endpoints lie on a polygon's boundary, the curve is
    classified as a boundary curve of that loop.  This is far more reliable
    than midpoint matching because endpoints are exact polygon vertices
    (shared between adjacent curves in a loop), whereas the midpoint of an
    arc sits between the chord-based polygon segments and can miss the
    tolerance entirely.  Midpoint matching is still used as a fallback for
    any curve where endpoint matching finds nothing (e.g. symbolic lines
    whose endpoints are inside a loop, not on its boundary).

    Both lists matter because loops in one shaft can touch or share an edge.
    A curve on a shared edge belongs to two loops at once - attributing it to
    just the first match (as this used to) makes the tool try to delete a
    curve the remaining profile still needs, and Revit refuses with
    "EditModeMgr element modifiable checker"."""
    if xy is None:
        return [], []
    on = []
    inside = []

    # Primary: endpoint-based boundary detection (exact for all curve types)
    if endpoints is not None:
        ep0, ep1 = endpoints
        for j, poly in enumerate(polys):
            if point_on_poly(ep0, poly) and point_on_poly(ep1, poly):
                on.append(j)
        if on:
            return on, []

    # Fallback: midpoint matching (for symbolic lines, or if no endpoints)
    for j, poly in enumerate(polys):
        if point_on_poly(xy, poly):
            on.append(j)
        elif point_in_poly(xy, poly):
            inside.append(j)
    return on, inside


def _is_removable(on, inside, keep_set):
    """A curve may be deleted only when nothing we are keeping needs it."""
    if on:
        return not any(j in keep_set for j in on)
    if inside:
        return not any(j in keep_set for j in inside)
    return False   # belongs to no loop - leave it alone


def _near_any_kept_boundary(xy, polys, keep_set, tol):
    """True if xy is within tol of ANY kept loop's polygon boundary.

    Used as a fallback after doc.Delete() fails: Revit knows which curves are
    on shared edges even when the polygon approximation misses it.  Re-checking
    with a looser tolerance catches shared-edge curves whose midpoints sit
    between two polygon segments and land just outside the normal tolerance."""
    if xy is None:
        return False
    for j in keep_set:
        if j < len(polys) and point_on_poly(xy, polys[j], tol):
            return True
    return False


def copy_one_in_place(opening):
    """Copy-paste the opening in the same place once, in its own transaction.
    Returns the copied shaft opening."""
    copies = []
    _log("copy: start (src {})".format(_eid_int(opening.Id)))
    t = Transaction(doc, "DQT - Copy shaft in place")
    t.Start()
    _set_silent_failures(t)   # the copy IS an identical instance - no dialog
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


def _expected_loop_counts(polys):
    """Number of boundary curves each loop index should have."""
    expected = {}
    for j, poly in enumerate(polys):
        expected[j] = poly["curve_count"]
    return expected


def _describe_element(eid):
    """Identify an element for the log - used on curves Revit refuses to
    delete, so the next log says what they actually are."""
    try:
        e = doc.GetElement(eid)
    except Exception as ex:
        return "GetElement failed: {}".format(ex)
    if e is None:
        return "already gone"
    bits = []
    try:
        bits.append(e.GetType().Name)
    except:
        pass
    try:
        if e.Category is not None:
            bits.append("cat=" + e.Category.Name)
    except:
        pass
    try:
        bits.append("style=" + e.LineStyle.Name)
    except:
        pass
    try:
        bits.append("ownerView=" + str(_eid_int(e.OwnerViewId)))
    except:
        pass
    try:
        bits.append("pinned=" + str(e.Pinned))
    except:
        pass
    return " / ".join(bits) if bits else "?"


def _stragglers(opening, sketch, keep_set, polys):
    """Curve elements still present that nothing we are keeping needs.

    Returns (boundary_ids, symbolic_ids) where boundary_ids are curves on a
    removed loop's polygon edge and symbolic_ids are curves inside a removed
    region.  The distinction matters: boundary stragglers would leave open
    profiles (fatal), while symbolic stragglers are just extra lines that do
    not affect boundary validity."""
    boundary = []
    symbolic = []
    for (e, xy, eps) in get_sketch_curve_elements(opening, sketch):
        on, inside = loops_at(xy, polys, eps)
        if _is_removable(on, inside, keep_set):
            if on:
                boundary.append((e.Id, xy))
            else:
                symbolic.append((e.Id, xy))
    return boundary, symbolic


def _count_boundary_elements(opening, sketch, polys):
    """Count boundary curve ELEMENTS currently present in the sketch, per
    loop index. A curve on a shared edge counts for every loop it lies on."""
    counts = {}
    for (e, xy, eps) in get_sketch_curve_elements(opening, sketch):
        on, _inside = loops_at(xy, polys, eps)
        for j in on:
            counts[j] = counts.get(j, 0) + 1
    return counts


def reduce_opening_to_region(opening, keep_set, polys):
    """Edit the opening's sketch and delete every boundary loop / symbolic line
    that does NOT belong to a loop index in keep_set. Uses SketchEditScope.
    Returns kept_symbolic_count. Raises on managed failure.

    The sketch is validated before and after deleting: a broken sketch (a
    kept loop missing curves, or a removed loop leaving strays) is never
    committed - the edit scope is cancelled instead, because committing an
    invalid shaft sketch can hard-crash Revit."""
    oid = _eid_int(opening.Id)
    _log("reduce {}: begin (keep {} loop(s))".format(oid, len(keep_set)))
    _log("reduce {}: resolving sketch...".format(oid))
    sketch = get_sketch(opening)
    if sketch is None:
        raise Exception("opening has no sketch")
    _log("reduce {}: sketch {} resolved, scanning curve elements...".format(
        oid, _eid_int(sketch.Id)))

    expected = _expected_loop_counts(polys)

    _log("reduce {}: querying sketch curve elements...".format(oid))
    curve_elements = get_sketch_curve_elements(opening, sketch)
    _log("reduce {}: scanned {} curve element(s), classifying...".format(
        oid, len(curve_elements)))

    delete_ids = []
    kept_symbolic = 0
    found_boundary = {}
    shared = 0
    unmatched = 0
    for (e, xy, eps) in curve_elements:
        on, inside = loops_at(xy, polys, eps)
        for j in on:
            found_boundary[j] = found_boundary.get(j, 0) + 1
        if _is_removable(on, inside, keep_set):
            delete_ids.append(e.Id)
        elif not on and not inside:
            unmatched += 1
        else:
            if on and len(on) > 1:
                shared += 1
            elif inside and any(j in keep_set for j in inside):
                kept_symbolic += 1
    _log("reduce {}: classification: {} to delete, {} shared-edge, {} kept "
         "symbolic, {} unmatched".format(
             oid, len(delete_ids), shared, kept_symbolic, unmatched))
    if shared:
        _log("reduce {}: {} curve(s) lie on a shared edge and are kept "
             "because a retained loop still needs them".format(oid, shared))

    # Pre-check: every loop that we plan to REMOVE must be fully identified
    # so we know exactly which curves to delete.  For loops we are KEEPING, a
    # shortfall is logged as a warning but is not fatal - we are not touching
    # their curves, and a tolerance miss on a kept loop cannot cause us to
    # accidentally delete its curves (a curve always matches its own loop
    # first, since the polygon was built from the same geometry).
    precheck_ok = True
    for j in range(len(polys)):
        fb = found_boundary.get(j, 0)
        ex = expected.get(j, 0)
        if fb < ex:
            in_keep = j in keep_set
            _log("reduce {}: loop {} has {}/{} boundary curves identified "
                 "({}){}" .format(oid, j, fb, ex,
                                  "kept" if in_keep else "TO REMOVE",
                                  " [WARNING]" if in_keep else " [ERROR]"))
            if not in_keep:
                precheck_ok = False
    if not precheck_ok:
        raise Exception(
            "one or more REMOVED loops have unidentified boundary curves - "
            "classification mismatch, skipping this opening instead of "
            "committing a broken sketch (see log for per-loop details)")

    if not delete_ids:
        _log("reduce {}: nothing to delete, done".format(oid))
        return kept_symbolic

    _log("reduce {}: {} curve(s) to delete, SketchEditScope.Start...".format(
        oid, len(delete_ids)))
    ses = SketchEditScope(doc, "DQT - Reduce shaft opening")
    ses.Start(sketch.Id)
    _log("reduce {}: scope started, delete transaction...".format(oid))

    # Deleting sketch curves can cascade: removing one curve may make Revit
    # drop or regenerate others, so a single pass over an id list captured
    # beforehand can leave stragglers behind (and a stale id then fails to
    # delete). Re-scan after each pass and delete whatever still belongs to a
    # removed loop, rather than failing the whole opening over one leftover.
    previous_left = None
    for attempt in range(1, _DELETE_PASSES + 1):
        t = Transaction(doc, "DQT - Delete extra loops")
        t.Start()
        _set_silent_failures(t)
        try:
            for did in delete_ids:
                try:
                    doc.Delete(did)
                except Exception as ex:
                    _log("    delete failed for {} [{}]: {}".format(
                        _eid_int(did), _describe_element(did), ex))
            _log("reduce {}: pass {} deletes done, committing...".format(
                oid, attempt))
            t.Commit()
            _log("reduce {}: pass {} delete transaction OK".format(oid, attempt))
        except Exception:
            if t.HasStarted() and not t.HasEnded():
                t.RollBack()
            try:
                ses.Cancel()
            except:
                pass
            raise

        bnd_left, sym_left = _stragglers(opening, sketch, keep_set, polys)
        delete_ids = [eid for eid, _ in bnd_left] + [eid for eid, _ in sym_left]
        if not delete_ids:
            break
        # Retrying only helps when the pass actually removed something; if the
        # same curves survive again they are undeletable, not stale.
        if previous_left is not None and len(delete_ids) >= previous_left:
            _log("reduce {}: {} curve(s) cannot be deleted in this edit scope "
                 "({} boundary, {} symbolic)".format(
                     oid, len(delete_ids), len(bnd_left), len(sym_left)))
            break
        previous_left = len(delete_ids)
        _log("reduce {}: pass {} left {} straggler(s), retrying...".format(
            oid, attempt, len(delete_ids)))

    # Post-check: kept loops must still be complete and removed loops fully
    # gone. Otherwise cancel the scope - never commit a broken sketch.
    counts_after = _count_boundary_elements(opening, sketch, polys)
    bad = None
    for j in keep_set:
        if counts_after.get(j, 0) < expected.get(j, 0):
            bad = "kept loop {} lost boundary curves ({} of {} left)".format(
                j, counts_after.get(j, 0), expected.get(j, 0))
            break
    if bad is None:
        bnd_left, sym_left = _stragglers(opening, sketch, keep_set, polys)

        # Re-check boundary stragglers with a LARGER tolerance: if a curve
        # that doc.Delete() refused is actually near a kept loop's boundary,
        # Revit is telling us it is a shared-edge curve that the polygon
        # approximation missed at the normal tolerance.
        true_bnd = []
        for (eid, xy) in bnd_left:
            if _near_any_kept_boundary(xy, polys, keep_set,
                                       _SHARED_EDGE_FALLBACK_TOL):
                _log("reduce {}: straggler {} reclassified as shared-edge "
                     "(near kept boundary at fallback tolerance)".format(
                         oid, _eid_int(eid)))
            else:
                true_bnd.append(eid)
                _log("reduce {}: true straggler {} [{}]".format(
                    oid, _eid_int(eid), _describe_element(eid)))

        if true_bnd:
            bad = "{} boundary curve(s) from removed loops could not be " \
                  "deleted".format(len(true_bnd))
        elif sym_left:
            # Symbolic lines inside removed regions do not affect boundary
            # profiles - log them but allow the commit to proceed.
            _log("reduce {}: {} symbolic line(s) from removed regions could "
                 "not be deleted (harmless, proceeding with commit)".format(
                     oid, len(sym_left)))
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


def _delete_copies(ids):
    """Remove copies left behind by a split that could not finish.

    Without this a failed split leaves every copy it had already made sitting
    on top of the original - full duplicates in the same place, which is both
    wrong and the source of a pile of "identical instances" warnings that
    accumulate with each retry."""
    if not ids:
        return
    _log("cleanup: removing {} copy(ies) left by the failed split".format(
        len(ids)))
    t = Transaction(doc, "DQT - Remove failed split copies")
    t.Start()
    _set_silent_failures(t)
    try:
        for eid in ids:
            try:
                doc.Delete(eid)
            except Exception as ex:
                _log("    cleanup delete failed for {}: {}".format(
                    _eid_int(eid), ex))
        t.Commit()
        _log("cleanup: done")
    except Exception as ex:
        if t.HasStarted() and not t.HasEnded():
            t.RollBack()
        _log("cleanup: FAILED {}".format(ex))


def _split_interleaved(opening, keep_sets, polys):
    """Shared engine: produce one opening per keep_set. Copies are made ONE AT
    A TIME and each copy is reduced immediately, so at most two full-sketch
    shafts overlap during any commit. The ORIGINAL is reduced last (to
    keep_sets[0]) - it stays complete as the copy source.

    All or nothing: if any step fails, every copy made so far is deleted and
    the original is left exactly as it was, so a failed run leaves no
    duplicates behind and can simply be retried."""
    original_id = opening.Id
    result_openings = []
    created_ids = []
    symbolic_kept = 0

    try:
        for keep_set in keep_sets[1:]:
            src = doc.GetElement(original_id)
            cp = copy_one_in_place(src)
            created_ids.append(cp.Id)
            print("  Copy {} -> reducing to {} loop(s)...".format(
                _eid_int(cp.Id), len(keep_set)))
            kept = reduce_opening_to_region(cp, keep_set, polys)
            symbolic_kept += kept
            result_openings.append(cp)
            print("    -> kept {} symbolic line(s)".format(kept))

        src = doc.GetElement(original_id)
        print("  Reducing ORIGINAL {} to {} loop(s)...".format(
            _eid_int(original_id), len(keep_sets[0])))
        kept = reduce_opening_to_region(src, keep_sets[0], polys)
        symbolic_kept += kept
        result_openings.insert(0, src)
        print("    -> kept {} symbolic line(s)".format(kept))
    except Exception:
        _delete_copies(created_ids)
        raise

    return result_openings, symbolic_kept


def _split_into_singles(opening, main_indices, holes_of, polys):
    """One opening per main boundary - fully split, each keeps its real
    Symbolic Lines."""
    keep_sets = [_group_keep_set([i], holes_of) for i in main_indices]
    result_openings, symbolic_kept = _split_interleaved(
        opening, keep_sets, polys)
    return result_openings, symbolic_kept, False


def _split_into_groups(opening, main_indices, holes_of, polys):
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
        opening, keep_sets, polys)
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

    # Snapshot the geometry to plain numbers NOW, before anything modifies the
    # document. Nothing below this line touches document geometry again.
    polys = loops_to_polygons(curve_loops)
    curve_loops = None

    main_indices, holes_of = analyze_loops(polys)
    print("\n  Loops: {} total, {} main boundary(ies), {} hole(s)".format(
        len(polys), len(main_indices), len(polys) - len(main_indices)))
    _log("opening {}: {} loop(s), {} main, {} hole(s)".format(
        _eid_int(opening.Id), len(polys), len(main_indices),
        len(polys) - len(main_indices)))

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
        return _split_into_groups(opening, main_indices, holes_of, polys)
    return _split_into_singles(opening, main_indices, holes_of, polys)


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
        print(" operation that died - please send it to DQT. The path above is")
        print(" the same file every session, so it survives a restart.)")
        print("=" * 60)
        _log("=" * 50)
        _log("RUN start - {} opening(s) selected - doc: {}".format(
            len(selected_ids), getattr(doc, "Title", "?")))

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
                # Settle the host geometry now, while we can still log it.
                _regen_checkpoint("after opening {}".format(_eid_int(oid)))
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
        msg += ("\n\nSAVE NOW, before clicking anything else in the model."
                "\n\nLog: {}".format(_LOG_PATH))
        _log("showing summary dialog...")
        forms.alert(msg, title="Split Shaft Opening Summary")
        _log("summary dialog closed - script finished cleanly")

    except Exception as e:
        import traceback
        print("\n=== MAIN ERROR ===")
        print(traceback.format_exc())
        forms.alert("Error: {}".format(e), exitscript=True)


if __name__ == "__main__":
    main()
