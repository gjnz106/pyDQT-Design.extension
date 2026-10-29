# -*- coding: utf-8 -*-
"""Fix Off Axis v1.1
Author: Dang Quoc Truong (DQT)

Straightens elements that are a hair off the model axes - what Revit's
"... is slightly off axis and may cause inaccuracies" warnings are about -
by turning each one about its own midpoint onto the nearest axis:

  Walls, Grids, Reference Planes, Model & Detail Lines,
  Room / Area / Space Separation Lines, Beams & Braces,
  Floor / Roof / Ceiling Sketch Lines

A sketch line is one edge of a closed boundary, so it cannot turn on its
own: the edge is put on the axis through its midpoint and its two
neighbouring edges are trimmed / extended to meet it - what Align + Trim
do by hand in Edit Boundary - inside a sketch edit of that floor, roof or
ceiling. The preview gives how far the corners move and how much the area
changes; more than 0.1 % of the area, a curved neighbour or a neighbour
running along the edge is left to fix by hand.

The axis is the model X / Y in plan. A line drawn in an elevation or
section (a detail line, a reference plane) snaps to that view's horizontal
or vertical instead. 45-degree diagonals can be added as targets.

Nothing changes until Fix: Scan lists every element it would turn, how far
it is off and how far its ends would move, and which ones Revit itself
warns about. Pinned elements, elements in a group, part of a stacked wall
or a multi-segment grid, and elements another user owns are listed but
left alone. Each element is turned in its own step, so one that Revit
refuses is rolled back and reported without stopping the rest - and the
whole run is still one Undo.

Replaces the Fix Element Off Axis pulldown (5 buttons), whose Beam and
Line buttons measured the angle against the wrong axis and never turned
anything.

v1.1: Floor / Roof / Ceiling Sketch Lines.
"""
try: __import__("dqt_usage").log_use(__file__)  # pyDQT usage log
except Exception: pass
__title__ = "Fix\nOff Axis"
__author__ = "DQT"

import os
import math
import clr
clr.AddReference('RevitAPI')
clr.AddReference('PresentationFramework')
clr.AddReference('PresentationCore')
clr.AddReference('WindowsBase')

from pyrevit import revit, forms
from pyrevit.forms import WPFWindow
import Autodesk.Revit.DB as DB
from System.Collections.Generic import List
import System.Windows.Controls as WPFControls
from System.Windows import Thickness, TextWrapping, FontWeights
from System.Windows.Media import BrushConverter

TITLE = "DQT - Fix Off Axis"
HELP_PAGE = "fix_off_axis.html"

DARK = "#5D4E37"
GREY = "#888888"
WARN = "#B26A00"
TEXT = "#333333"

MIN_DEVIATION_DEG = 1e-9     # below this a line is on its axis
VERIFY_DEG = 1e-6            # after the fix the element must be this close
MAX_LIMIT_DEG = 5.0          # beyond this a line is drawn at an angle on purpose
FEET_TO_MM = 304.8
SQFT_TO_SQM = 0.09290304
CORNER_TOL = 1e-5            # sketch-line ends this close meet at a corner (feet)
MAX_AREA_CHANGE = 0.001      # a sketch fix may change the area by at most 0.1 %

FIXED = "fixed"
FAILED = "failed"
STILL_OFF = "still off"

# kind key, label, one element's label, built-in category names
KINDS = [
    ("walls", "Walls", "Wall", ("OST_Walls",)),
    ("grids", "Grids", "Grid", ("OST_Grids",)),
    ("refplanes", "Reference Planes", "Reference Plane", ("OST_CLines",)),
    ("lines", "Model & Detail Lines", "Line", ("OST_Lines",)),
    ("separations", "Room / Area / Space Separation Lines", "Separation Line",
     ("OST_RoomSeparationLines", "OST_AreaSchemeLines", "OST_MEPSpaceSeparationLines")),
    ("beams", "Beams & Braces", "Beam / Brace", ("OST_StructuralFraming",)),
    ("sketches", "Floor / Roof / Ceiling Sketch Lines", "Sketch Line", ("OST_SketchLines",)),
]
KIND_LABELS = dict((k[0], k[1]) for k in KINDS)
KIND_SINGULAR = dict((k[0], k[2]) for k in KINDS)
KIND_ORDER = dict((k[0], i) for i, k in enumerate(KINDS))

# Revit's "slightly off axis" warnings (BuiltInFailures.InaccurateFailures)
OFF_AXIS_FAILURES = (
    "InaccurateWall", "InaccurateLine", "InaccurateSketchLine", "InaccurateCurveBasedFamily",
    "InaccurateBeamOrBrace", "InaccurateRefPlane", "InaccurateRoomSeparation",
    "InaccurateSpaceSeparation", "InaccurateAreaLine", "InaccurateGrid", "InaccurateLevel",
    "InaccurateMassingSketchLine")


def _open_help_page(html_filename):
    """Open this tool's page from the shared _Modify_Help folder in the
    default browser. Returns True on success, False if the caller should
    fall back to the in-app help text (e.g. the folder went missing)."""
    try:
        panel_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        path = os.path.join(panel_dir, "_Modify_Help", html_filename)
        if not os.path.isfile(path):
            return False
        os.startfile(path)
        return True
    except Exception:
        return False


def _eid_int(eid):
    """ElementId -> int across Revit 2024-2027 (.Value vs .IntegerValue)."""
    try:
        return eid.Value
    except AttributeError:
        return eid.IntegerValue


def _brush(hex_color):
    return BrushConverter().ConvertFromString(hex_color)


def fmt_deg(value):
    if value < 0.0001:
        return u"< 0.0001°"
    return u"{}°".format(round(value, 4))


def fmt_mm(value):
    if value < 0.01:
        return "< 0.01 mm"
    return "{} mm".format(round(value, 2))


def fmt_sqm(value):
    if value < 0.0001:
        return u"< 0.0001 m\u00b2"
    return u"{} m\u00b2".format(round(value, 4))


# ============================================================================
# GEOMETRY - how far a line is off its nearest axis, and the turn that fixes it
# ============================================================================
def plane_basis(axis):
    """The two in-plane directions (u, v) for turns about `axis`, with
    v = axis x u so that a positive turn takes u toward v. In plan (axis
    vertical) they are model X and Y; in an elevation-like plane u is the
    horizontal and v the vertical of that plane."""
    if abs(axis.Z) > 0.999:
        u = DB.XYZ.BasisX
    else:
        u = axis.CrossProduct(DB.XYZ.BasisZ).Normalize()
    return u, axis.CrossProduct(u)


def correction(direction, axis, step_deg):
    """(turn about `axis` in radians, how far off in degrees) that brings
    `direction` onto the nearest multiple of `step_deg` (90, or 45 with the
    diagonals) in the plane square to `axis`. None when the direction has
    no extent in that plane (a vertical beam seen in plan)."""
    u, v = plane_basis(axis)
    x = direction.DotProduct(u)
    y = direction.DotProduct(v)
    if math.hypot(x, y) < 1e-9:
        return None
    theta = math.degrees(math.atan2(y, x))
    target = round(theta / step_deg) * step_deg
    delta = target - theta
    return math.radians(delta), abs(delta)


def _location_line(elem):
    curve = getattr(elem.Location, "Curve", None)
    return curve if isinstance(curve, DB.Line) else None


def geometry_of(elem, kind):
    """(start, end, turn axis) of the straight line that defines the
    element, or None when it has none (an arc wall, a curved grid)."""
    try:
        if kind in ("walls", "beams"):
            line = _location_line(elem)
            if line is None:
                return None
            return line.GetEndPoint(0), line.GetEndPoint(1), DB.XYZ.BasisZ
        if kind == "grids":
            line = elem.Curve
            if not isinstance(line, DB.Line):
                return None
            return line.GetEndPoint(0), line.GetEndPoint(1), DB.XYZ.BasisZ
        if kind == "refplanes":
            start, end = elem.BubbleEnd, elem.FreeEnd
            seen = end.Subtract(start).CrossProduct(elem.Normal)
            if seen.GetLength() < 1e-9:
                return None
            return start, end, seen.Normalize()
        if kind in ("lines", "separations", "sketches"):
            line = elem.GeometryCurve
            if not isinstance(line, DB.Line):
                return None
            try:
                axis = elem.SketchPlane.GetPlane().Normal
            except Exception:
                axis = DB.XYZ.BasisZ
            return line.GetEndPoint(0), line.GetEndPoint(1), axis
    except Exception:
        return None
    return None


def skip_reason(doc, elem, kind):
    """Why the element is listed but left alone, or None."""
    try:
        if elem.Pinned:
            return "pinned"
    except Exception:
        pass
    try:
        if _eid_int(elem.GroupId) > 0:
            return "in a group - edit the group to fix it"
    except Exception:
        pass
    if kind == "walls":
        try:
            if elem.IsStackedWallMember:
                return "part of a stacked wall - fix the stacked wall"
        except Exception:
            pass
    if kind == "grids":
        try:
            if _eid_int(DB.MultiSegmentGrid.GetMultiSegementGridId(elem)) > 0:
                return "part of a multi-segment grid"
        except Exception:
            pass
    try:
        if doc.IsWorkshared and DB.WorksharingUtils.GetCheckoutStatus(doc, elem.Id) == \
                DB.CheckoutStatus.OwnedByOtherUser:
            return "owned by another user"
    except Exception:
        pass
    return None


# ============================================================================
# SKETCH LINES - one edge of a floor / roof / ceiling boundary
# ============================================================================
def sketch_owners(doc):
    """{sketch-line id int: (Sketch, owning element, ElementId)} for every sketch."""
    found = {}
    for sketch in DB.FilteredElementCollector(doc).OfClass(DB.Sketch):
        try:
            owner = doc.GetElement(sketch.OwnerId)
            for eid in sketch.GetAllElements():
                found[_eid_int(eid)] = (sketch, owner, eid)
        except Exception:
            continue
    return found


def _shape_edited(owner):
    for getter in (lambda o: o.GetSlabShapeEditor(), lambda o: o.SlabShapeEditor):
        try:
            editor = getter(owner)
            if editor is not None:
                return bool(editor.IsEnabled)
        except Exception:
            continue
    return False


def sketch_owner_problem(doc, owner):
    """Why the sketch of this element is left alone, or None."""
    if owner is None:
        return "the sketch has no owner"
    if not isinstance(owner, (DB.Floor, DB.Ceiling, DB.FootPrintRoof)):
        try:
            what = owner.Category.Name
        except Exception:
            what = "element"
        return u"sketch of a {} - edit it by hand".format(what)
    reason = skip_reason(doc, owner, "owner")
    if reason:
        return u"its {} is {}".format(owner.Category.Name.lower().rstrip("s"), reason)
    if _shape_edited(owner):
        return "shape edited (Modify Sub Elements) - edit its boundary by hand"
    return None


class SketchPlan(object):
    """The edge on its axis and its two neighbours trimmed / extended to it:
    changes = [(curve element, new Line)], how far the corners move (feet),
    how much the area changes (square feet)."""

    def __init__(self, changes, move, area_change):
        self.changes = changes
        self.move = move
        self.area_change = area_change


def _corner_neighbour(doc, sketch, edge, point):
    """(curve element, index of its end at `point`) - the one other line of
    the sketch that meets the edge there - or a reason string."""
    found = []
    for eid in sketch.GetAllElements():
        if _eid_int(eid) == _eid_int(edge.Id):
            continue
        elem = doc.GetElement(eid)
        curve = getattr(elem, "GeometryCurve", None)
        if curve is None:
            continue
        for index in (0, 1):
            try:
                if curve.GetEndPoint(index).DistanceTo(point) < CORNER_TOL:
                    found.append((elem, index))
            except Exception:
                continue
    if len(found) != 1:
        return "it does not meet exactly one other line at each end"
    if not isinstance(found[0][0].GeometryCurve, DB.Line):
        return "a neighbouring edge is curved - edit it by hand"
    return found[0]


def _cross(a, b):
    return a[0] * b[1] - a[1] * b[0]


def sketch_plan(doc, sketch, edge, delta, axis):
    """SketchPlan that puts `edge` on its axis (turned by `delta` about its
    midpoint) and trims / extends its two neighbours to it - or a reason
    string when that cannot be done safely."""
    line = edge.GeometryCurve
    start, end = line.GetEndPoint(0), line.GetEndPoint(1)
    mid = start.Add(end).Multiply(0.5)
    u, v = plane_basis(axis)

    def flat(p):
        d = p.Subtract(mid)
        return (d.DotProduct(u), d.DotProduct(v))

    def back(q):
        return mid.Add(u.Multiply(q[0])).Add(v.Multiply(q[1]))

    s2, e2 = flat(start), flat(end)
    c, k = math.cos(delta), math.sin(delta)
    t = (c * (e2[0] - s2[0]) - k * (e2[1] - s2[1]), k * (e2[0] - s2[0]) + c * (e2[1] - s2[1]))
    try:
        tolerance = doc.Application.ShortCurveTolerance
    except Exception:
        tolerance = 1.0 / 256

    corners, changes, chain_old, chain_new = [], [], [], []
    for point in (start, end):
        found = _corner_neighbour(doc, sketch, edge, point)
        if not isinstance(found, tuple):
            return found
        elem, index = found
        n_line = elem.GeometryCurve
        far = flat(n_line.GetEndPoint(1 - index))
        near = flat(n_line.GetEndPoint(index))
        w = (near[0] - far[0], near[1] - far[1])
        denom = _cross(t, w)
        if abs(denom) < 1e-9 * math.hypot(*w) * math.hypot(*t):
            return "a neighbouring edge runs along it - edit it by hand"
        # the axis line through the midpoint (lambda * t) meets the neighbour (far + mu * w)
        lam = _cross(far, w) / denom
        corner = (lam * t[0], lam * t[1])
        length = math.hypot(corner[0] - far[0], corner[1] - far[1])
        if length < tolerance or (corner[0] - far[0]) * w[0] + (corner[1] - far[1]) * w[1] <= 0:
            return "a neighbouring edge would become too short - edit it by hand"
        new_ends = [back(far), back(corner)] if index == 1 else [back(corner), back(far)]
        changes.append((elem, DB.Line.CreateBound(new_ends[0], new_ends[1])))
        corners.append(corner)
        chain_old.append((far, near))
        chain_new.append((far, corner))
    new_s, new_e = corners
    if (new_e[0] - new_s[0]) * (e2[0] - s2[0]) + (new_e[1] - new_s[1]) * (e2[1] - s2[1]) <= 0:
        return "the edge would turn back on itself - edit it by hand"
    changes.insert(0, (edge, DB.Line.CreateBound(back(new_s), back(new_e))))
    move = max(math.hypot(new_s[0] - s2[0], new_s[1] - s2[1]), math.hypot(new_e[0] - e2[0], new_e[1] - e2[1]))

    def chain_area(a_far, p, q, b_far):
        pts = [a_far, p, q, b_far]
        return sum(_cross(pts[i], pts[i + 1]) for i in range(3)) / 2.0

    area_change = abs(chain_area(chain_new[0][0], new_s, new_e, chain_new[1][0])
                      - chain_area(chain_old[0][0], s2, e2, chain_old[1][0]))
    return SketchPlan(changes, move, area_change)


def _host_area(owner):
    try:
        return owner.get_Parameter(DB.BuiltInParameter.HOST_AREA_COMPUTED).AsDouble()
    except Exception:
        return None


# ============================================================================
# REVIT'S OFF-AXIS WARNINGS
# ============================================================================
def _off_axis_guids():
    guids = set()
    holder = getattr(DB.BuiltInFailures, "InaccurateFailures", None)
    for name in OFF_AXIS_FAILURES:
        try:
            guids.add(str(getattr(holder, name).Guid).lower())
        except Exception:
            continue
    return guids


def off_axis_warnings(doc):
    """Revit's off-axis warnings: [set of element id ints per warning]."""
    guids = _off_axis_guids()
    found = []
    try:
        warnings = list(doc.GetWarnings())
    except Exception:
        return found
    for warning in warnings:
        try:
            is_off_axis = str(warning.GetFailureDefinitionId().Guid).lower() in guids
        except Exception:
            is_off_axis = False
        if not is_off_axis:
            try:
                is_off_axis = "off axis" in warning.GetDescriptionText().lower()
            except Exception:
                pass
        if is_off_axis:
            try:
                found.append(set(_eid_int(i) for i in warning.GetFailingElements()))
            except Exception:
                found.append(set())
    return found


# ============================================================================
# SCAN
# ============================================================================
class Candidate(object):
    """One element the scan found off axis."""

    def __init__(self, doc, elem, kind, geometry, turn, step, warned, skip, sketch=None, owner=None, plan=None):
        self.elem = elem
        self.id = elem.Id
        self.kind = kind
        self.step = step              # 90, or 45 with the diagonals
        self.start, self.end, self.axis = geometry
        self.delta, self.deviation = turn
        self.warned = warned
        self.skip = skip
        self.sketch = sketch          # sketch lines: the sketch, its owner and the fix
        self.owner = owner
        self.plan = plan
        half = self.start.DistanceTo(self.end) / 2.0
        self.move_mm = half * abs(math.sin(self.delta)) * FEET_TO_MM
        if plan is not None:
            self.move_mm = plan.move * FEET_TO_MM
        self.name = describe(doc, owner if owner is not None else elem)
        if owner is not None:
            self.name = u"{} (Id {})".format(self.name, _eid_int(owner.Id))

    @property
    def select_id(self):
        """What Select in Revit picks - a sketch line only exists in its sketch,
        so its floor / roof / ceiling instead."""
        return self.owner.Id if self.owner is not None else self.id

    @property
    def title(self):
        return u"{}  ·  {}".format(KIND_SINGULAR[self.kind], self.name)

    @property
    def detail(self):
        parts = ["Id {}".format(_eid_int(self.id)), u"off {}".format(fmt_deg(self.deviation))]
        if self.skip:
            parts.append(u"skipped: {}".format(self.skip))
        elif self.plan is not None:
            parts.append(u"corners move ≤ {}".format(fmt_mm(self.move_mm)))
            parts.append(u"area change {}".format(fmt_sqm(self.plan.area_change * SQFT_TO_SQM)))
        else:
            parts.append(u"ends move ≤ {}".format(fmt_mm(self.move_mm)))
        if self.warned:
            parts.append("in a Revit off-axis warning")
        return u"  ·  ".join(parts)


def describe(doc, elem):
    """"Basic Wall: Generic - 200mm", "Grid 3", "Lines: Thin Lines"."""
    try:
        if isinstance(elem, (DB.Grid, DB.ReferencePlane)):
            return elem.Name or "(unnamed)"
    except Exception:
        pass
    try:
        if isinstance(elem, DB.CurveElement):
            return elem.LineStyle.Name
    except Exception:
        pass
    try:
        type_elem = doc.GetElement(elem.GetTypeId())
        family = getattr(type_elem, "FamilyName", "") or ""
        return u"{}: {}".format(family, type_elem.Name) if family else type_elem.Name
    except Exception:
        pass
    try:
        return elem.Name
    except Exception:
        return ""


def _categories(kind):
    """The BuiltInCategory members of one kind (a name this Revit lacks is left out)."""
    names = [k[3] for k in KINDS if k[0] == kind][0]
    return [getattr(DB.BuiltInCategory, n) for n in names if hasattr(DB.BuiltInCategory, n)]


SKETCH_OWNER_CATEGORIES = ("OST_Floors", "OST_StructuralFoundation", "OST_Roofs", "OST_Ceilings")


def _sketch_lines(doc, uidoc, scope, owners):
    """Sketch lines in the scope - by their floor / roof / ceiling, since a
    sketch line is only drawn while its sketch is being edited."""
    if scope == "model":
        keep = None
    elif scope == "selection":
        keep = set(_eid_int(i) for i in uidoc.Selection.GetElementIds())
    else:
        keep = set()
        for name in SKETCH_OWNER_CATEGORIES:
            if not hasattr(DB.BuiltInCategory, name):
                continue
            try:
                collector = DB.FilteredElementCollector(doc, doc.ActiveView.Id)
                keep.update(_eid_int(e.Id) for e in collector.OfCategory(
                    getattr(DB.BuiltInCategory, name)).WhereElementIsNotElementType())
            except Exception:
                continue
    found = []
    for line_id in sorted(owners):
        sketch, owner, eid = owners[line_id]
        if keep is not None and line_id not in keep and (owner is None or _eid_int(owner.Id) not in keep):
            continue
        elem = doc.GetElement(eid)
        if isinstance(elem, DB.CurveElement):
            found.append(elem)
    return found


def _elements(doc, uidoc, kind, scope, owners=None):
    """The elements of one kind in the scope: 'model', 'view', 'selection'."""
    if kind == "sketches":
        return _sketch_lines(doc, uidoc, scope, owners or {})
    cats = _categories(kind)
    if scope == "selection":
        wanted = set(int(c) for c in cats)
        found = []
        for eid in uidoc.Selection.GetElementIds():
            elem = doc.GetElement(eid)
            try:
                if elem is not None and _eid_int(elem.Category.Id) in wanted:
                    found.append(elem)
            except Exception:
                continue
        return found
    found = []
    for cat in cats:
        try:
            if scope == "view":
                collector = DB.FilteredElementCollector(doc, doc.ActiveView.Id)
            else:
                collector = DB.FilteredElementCollector(doc)
            found.extend(collector.OfCategory(cat).WhereElementIsNotElementType())
        except Exception:
            continue
    return found


def scan(doc, uidoc, kinds, scope, max_deg, warned_only, step_deg):
    """[Candidate] - fixable ones first (most off first), then the ones
    left alone. With warned_only, only elements in Revit's off-axis warnings
    are considered; a warned element off by more than max_deg is listed as
    skipped (it may be drawn at that angle on purpose)."""
    warned = set()
    for ids in off_axis_warnings(doc):
        warned |= ids
    owners = sketch_owners(doc) if "sketches" in kinds else {}
    found = []
    for kind in kinds:
        for elem in _elements(doc, uidoc, kind, scope, owners):
            is_warned = _eid_int(elem.Id) in warned
            if warned_only and not is_warned:
                continue
            geometry = geometry_of(elem, kind)
            if geometry is None:
                continue
            start, end, axis = geometry
            turn = correction(end.Subtract(start), axis, step_deg)
            if turn is None or turn[1] <= MIN_DEVIATION_DEG:
                continue
            skip = None
            if turn[1] > max_deg:
                if not is_warned:
                    continue          # drawn at an angle on purpose
                skip = u"off by more than the {} limit".format(fmt_deg(max_deg))
            if kind == "sketches":
                sketch, owner, _ = owners[_eid_int(elem.Id)]
                problem = sketch_owner_problem(doc, owner)
                if problem and not is_warned:
                    continue          # stairs, railings, filled regions ... unless Revit warns
                plan = None
                if not (skip or problem):
                    plan = sketch_plan(doc, sketch, elem, turn[0], axis)
                    if not isinstance(plan, SketchPlan):
                        problem, plan = plan, None
                    elif _host_area(owner) and plan.area_change > MAX_AREA_CHANGE * _host_area(owner):
                        problem = u"the area would change by more than 0.1 % - edit it by hand"
                found.append(Candidate(doc, elem, kind, geometry, turn, step_deg, is_warned, skip or problem,
                                       sketch=sketch, owner=owner, plan=plan))
                continue
            skip = skip or skip_reason(doc, elem, kind)
            found.append(Candidate(doc, elem, kind, geometry, turn, step_deg, is_warned, skip))
    found.sort(key=lambda c: (c.skip is not None, KIND_ORDER[c.kind], -c.deviation))
    return found


# ============================================================================
# FIX
# ============================================================================
class _RollBackOnError(DB.IFailuresPreprocessor):
    """Lets warnings through; an error rolls the one element back."""

    def __init__(self):
        self.errors = []

    def PreprocessFailures(self, accessor):
        for failure in accessor.GetFailureMessages():
            try:
                if failure.GetSeverity() == DB.FailureSeverity.Warning:
                    continue
                self.errors.append(failure.GetDescriptionText())
            except Exception:
                self.errors.append("Revit reported an error")
        if self.errors:
            return DB.FailureProcessingResult.ProceedWithRollBack
        return DB.FailureProcessingResult.Continue


def _verify(doc, cand):
    """(status, message) once the change is committed: the element must now
    be on its axis."""
    elem = doc.GetElement(cand.id)
    if elem is None:
        return FAILED, "Revit replaced the line - check its floor / roof / ceiling"
    geometry = geometry_of(elem, cand.kind)
    if geometry is not None:
        start, end, axis = geometry
        after = correction(end.Subtract(start), axis, cand.step)
        if after is not None and after[1] > VERIFY_DEG:
            return STILL_OFF, u"still off by {}".format(fmt_deg(after[1]))
    return FIXED, ""


def fix_sketch_edge(doc, cand):
    """Put one sketch edge on its axis inside a sketch edit of its owner,
    its two neighbours trimmed / extended to it. (status, message)."""
    pre = _RollBackOnError()
    scope = DB.SketchEditScope(doc, TITLE)
    try:
        scope.Start(cand.sketch.Id)
    except Exception as error:
        return FAILED, u"Revit would not open the sketch for editing: {}".format(error)
    tx = DB.Transaction(doc, TITLE)
    try:
        tx.Start()
        # from the sketch as it is now - an edge fixed before may have moved this one's corners
        line = cand.elem.GeometryCurve
        turn = correction(line.GetEndPoint(1).Subtract(line.GetEndPoint(0)), cand.axis, cand.step)
        plan = sketch_plan(doc, cand.sketch, cand.elem, turn[0], cand.axis) if turn else "already on its axis"
        if not isinstance(plan, SketchPlan):
            raise ValueError(plan)
        for elem, new_line in plan.changes:
            elem.SetGeometryCurve(new_line, True)
        tx.Commit()
        scope.Commit(pre)
    except Exception as error:
        if tx.HasStarted() and not tx.HasEnded():
            tx.RollBack()
        if scope.IsActive:
            scope.Cancel()
        return FAILED, "; ".join(pre.errors) or str(error)
    if pre.errors:
        return FAILED, "; ".join(pre.errors)
    return _verify(doc, cand)


def fix_one(doc, cand):
    """Turn one element onto its axis in its own transaction. (status, message)."""
    if cand.kind == "sketches":
        return fix_sketch_edge(doc, cand)
    pre = _RollBackOnError()
    tx = DB.Transaction(doc, TITLE)
    options = tx.GetFailureHandlingOptions()
    options.SetFailuresPreprocessor(pre)
    options.SetClearAfterRollback(True)
    tx.SetFailureHandlingOptions(options)
    tx.Start()
    try:
        pivot = cand.start.Add(cand.end).Multiply(0.5)
        DB.ElementTransformUtils.RotateElement(
            doc, cand.id, DB.Line.CreateBound(pivot, pivot.Add(cand.axis)), cand.delta)
        status = tx.Commit()
    except Exception as error:
        if tx.HasStarted() and not tx.HasEnded():
            tx.RollBack()
        return FAILED, str(error)
    if status != DB.TransactionStatus.Committed:
        return FAILED, "; ".join(pre.errors) or "Revit rolled the change back"
    return _verify(doc, cand)


def fix_all(doc, candidates):
    """Fix every candidate; one Undo for the whole run. [(cand, status, msg)]."""
    results = []
    group = DB.TransactionGroup(doc, TITLE)
    group.Start()
    try:
        for cand in candidates:
            status, message = fix_one(doc, cand)
            results.append((cand, status, message))
        group.Assimilate()
    except Exception:
        group.RollBack()
        raise
    return results


def summary_text(results, before, after):
    fixed = [r for r in results if r[1] == FIXED]
    problems = [r for r in results if r[1] != FIXED]
    lines = ["Turned onto the axis: {} element(s).".format(len(fixed))]
    if problems:
        lines.append("")
        lines.append("Not fixed: {}".format(len(problems)))
        for cand, status, message in problems[:12]:
            lines.append(u"  • {} (Id {}): {}".format(KIND_LABELS[cand.kind], _eid_int(cand.id), message))
        if len(problems) > 12:
            lines.append("  ... and {} more".format(len(problems) - 12))
    lines.append("")
    lines.append(u"Revit off-axis warnings: {} → {}".format(before, after))
    if after:
        lines.append("What is left is either not ticked, left alone (pinned, in a group, "
                     "a sketch edge next to a curve ...) or something this tool does not turn: "
                     "levels, other line-based families, and sketches of stairs, railings, "
                     "openings or filled regions.")
    lines.append("")
    lines.append("One Undo reverts the whole run.")
    return u"\n".join(lines)


# ============================================================================
# WINDOW
# ============================================================================
MAIN_XAML = """
<Window xmlns="http://schemas.microsoft.com/winfx/2006/xaml/presentation"
        xmlns:x="http://schemas.microsoft.com/winfx/2006/xaml"
        Title="Fix Off Axis - DQT" Height="760" Width="1120"
        MinHeight="560" MinWidth="880"
        WindowStartupLocation="CenterScreen" Background="#FFFFFF">
    <Window.Resources>
        <Style x:Key="DqtButton" TargetType="Button">
            <Setter Property="Background" Value="#F0CC88"/>
            <Setter Property="Foreground" Value="#5D4E37"/>
            <Setter Property="BorderBrush" Value="#D4B87A"/>
            <Setter Property="BorderThickness" Value="1"/>
            <Setter Property="Padding" Value="14,6"/>
            <Setter Property="Margin" Value="4,0,0,0"/>
            <Setter Property="FontWeight" Value="SemiBold"/>
            <Setter Property="Cursor" Value="Hand"/>
        </Style>
        <Style x:Key="PlainButton" TargetType="Button">
            <Setter Property="Background" Value="White"/>
            <Setter Property="Foreground" Value="#5D4E37"/>
            <Setter Property="BorderBrush" Value="#D4B87A"/>
            <Setter Property="BorderThickness" Value="1"/>
            <Setter Property="Padding" Value="10,4"/>
            <Setter Property="Margin" Value="0,0,6,0"/>
            <Setter Property="Cursor" Value="Hand"/>
        </Style>
    </Window.Resources>
    <Grid Margin="12">
        <Grid.RowDefinitions>
            <RowDefinition Height="Auto"/>
            <RowDefinition Height="*"/>
            <RowDefinition Height="Auto"/>
            <RowDefinition Height="Auto"/>
        </Grid.RowDefinitions>

        <!-- Header -->
        <Border Grid.Row="0" Background="#F0CC88" BorderBrush="#D4B87A" BorderThickness="0,0,0,2"
                CornerRadius="5" Padding="14,10" Margin="0,0,0,10">
            <Grid>
                <StackPanel>
                    <TextBlock Text="Fix Off Axis" FontSize="17" FontWeight="Bold" Foreground="#5D4E37"/>
                    <TextBlock Text="Turn elements that are a hair off the model axes back onto them - what Revit's 'slightly off axis' warnings are about"
                               FontSize="10" Foreground="#5D4E37" Margin="0,2,0,0"/>
                </StackPanel>
                <Button x:Name="btnHelp" Content="? Help" Style="{StaticResource PlainButton}"
                        HorizontalAlignment="Right" VerticalAlignment="Center" Margin="0"/>
            </Grid>
        </Border>

        <Grid Grid.Row="1">
            <Grid.ColumnDefinitions>
                <ColumnDefinition Width="330"/>
                <ColumnDefinition Width="12"/>
                <ColumnDefinition Width="*"/>
            </Grid.ColumnDefinitions>

            <!-- Options -->
            <Border Grid.Column="0" BorderBrush="#D4B87A" BorderThickness="1" CornerRadius="4" Padding="12">
                <ScrollViewer VerticalScrollBarVisibility="Auto">
                    <StackPanel>
                        <TextBlock Text="1. ELEMENTS" FontSize="11" FontWeight="Bold" Foreground="#5D4E37" Margin="0,0,0,6"/>
                        <CheckBox x:Name="chkWalls" Content="Walls" IsChecked="True" Margin="0,2"/>
                        <CheckBox x:Name="chkGrids" Content="Grids" IsChecked="True" Margin="0,2"/>
                        <CheckBox x:Name="chkRefPlanes" Content="Reference Planes" IsChecked="True" Margin="0,2"/>
                        <CheckBox x:Name="chkLines" Content="Model &amp; Detail Lines" IsChecked="True" Margin="0,2"/>
                        <CheckBox x:Name="chkSeparations" Content="Room / Area / Space Separation Lines" IsChecked="True" Margin="0,2"/>
                        <CheckBox x:Name="chkBeams" Content="Beams &amp; Braces" IsChecked="True" Margin="0,2"/>
                        <CheckBox x:Name="chkSketches" Content="Floor / Roof / Ceiling Sketch Lines" IsChecked="True" Margin="0,2"
                                  ToolTip="The edge is put on the axis and its two neighbours are trimmed / extended to it"/>

                        <TextBlock Text="2. WHERE" FontSize="11" FontWeight="Bold" Foreground="#5D4E37" Margin="0,14,0,6"/>
                        <RadioButton x:Name="rbModel" GroupName="scope" Content="Whole model" IsChecked="True" Margin="0,2"/>
                        <RadioButton x:Name="rbView" GroupName="scope" Content="Visible in the active view" Margin="0,2"/>
                        <RadioButton x:Name="rbSelection" GroupName="scope" Content="Current selection" Margin="0,2"/>

                        <TextBlock Text="3. RULES" FontSize="11" FontWeight="Bold" Foreground="#5D4E37" Margin="0,14,0,6"/>
                        <CheckBox x:Name="chkWarnedOnly" IsChecked="True" Margin="0,2"
                                  Content="Only elements in Revit's off-axis warnings"/>
                        <StackPanel Orientation="Horizontal" Margin="0,6,0,2">
                            <TextBlock Text="Off by at most" VerticalAlignment="Center" Foreground="#333333"/>
                            <TextBox x:Name="txtMaxAngle" Text="1.0" Width="52" Margin="6,0" Padding="3,2"/>
                            <TextBlock Text="degrees" VerticalAlignment="Center" Foreground="#333333"/>
                        </StackPanel>
                        <CheckBox x:Name="chk45" Margin="0,4,0,2" Content="Also snap to 45-degree diagonals"/>

                        <Button x:Name="btnScan" Content="Scan" Style="{StaticResource DqtButton}"
                                HorizontalAlignment="Left" Margin="0,16,0,0"/>
                        <TextBlock x:Name="txtWarnings" FontSize="10" Foreground="#888888" TextWrapping="Wrap" Margin="0,10,0,0"/>
                    </StackPanel>
                </ScrollViewer>
            </Border>

            <!-- Results -->
            <Border Grid.Column="2" BorderBrush="#D4B87A" BorderThickness="1" CornerRadius="4" Padding="10">
                <Grid>
                    <Grid.RowDefinitions>
                        <RowDefinition Height="Auto"/>
                        <RowDefinition Height="*"/>
                    </Grid.RowDefinitions>
                    <Grid Grid.Row="0" Margin="0,0,0,6">
                        <TextBlock x:Name="txtResultCount" Text="4. FOUND - press Scan" FontSize="11" FontWeight="Bold"
                                   Foreground="#5D4E37" VerticalAlignment="Center"/>
                        <StackPanel Orientation="Horizontal" HorizontalAlignment="Right">
                            <Button x:Name="btnAll" Content="All" Style="{StaticResource PlainButton}"/>
                            <Button x:Name="btnNone" Content="None" Style="{StaticResource PlainButton}"/>
                            <Button x:Name="btnSelect" Content="Select in Revit" Style="{StaticResource PlainButton}" Margin="0"/>
                        </StackPanel>
                    </Grid>
                    <Border Grid.Row="1" BorderBrush="#E0E0E0" BorderThickness="1">
                        <ScrollViewer VerticalScrollBarVisibility="Auto">
                            <StackPanel x:Name="pnlResults" Margin="8,6"/>
                        </ScrollViewer>
                    </Border>
                </Grid>
            </Border>
        </Grid>

        <!-- Status + actions -->
        <Grid Grid.Row="2" Margin="0,10,0,0">
            <TextBlock x:Name="txtStatus" FontSize="11" Foreground="#888888" VerticalAlignment="Center"
                       TextWrapping="Wrap" Margin="0,0,240,0"/>
            <StackPanel Orientation="Horizontal" HorizontalAlignment="Right">
                <Button x:Name="btnFix" Content="Fix Ticked" Style="{StaticResource DqtButton}"/>
                <Button x:Name="btnClose" Content="Close" Style="{StaticResource PlainButton}" Margin="6,0,0,0"/>
            </StackPanel>
        </Grid>

        <!-- Footer -->
        <Border Grid.Row="3" Background="#F0CC88" CornerRadius="3" Padding="8,5" Margin="0,10,0,0">
            <TextBlock Text="Dang Quoc Truong - DQT (c) 2026" FontSize="10" FontWeight="SemiBold"
                       HorizontalAlignment="Center" Foreground="#5D4E37"/>
        </Border>
    </Grid>
</Window>
"""


def _row_content(title, detail, detail_color=GREY):
    panel = WPFControls.StackPanel()
    head = WPFControls.TextBlock()
    head.Text = title
    head.FontWeight = FontWeights.SemiBold
    head.Foreground = _brush(TEXT)
    head.TextWrapping = TextWrapping.Wrap
    panel.Children.Add(head)
    sub = WPFControls.TextBlock()
    sub.Text = detail
    sub.FontSize = 10
    sub.Foreground = _brush(detail_color)
    sub.TextWrapping = TextWrapping.Wrap
    panel.Children.Add(sub)
    return panel


class FixOffAxisWindow(WPFWindow):
    def __init__(self, doc, uidoc):
        WPFWindow.__init__(self, MAIN_XAML, literal_string=True)
        self.doc = doc
        self.uidoc = uidoc
        self.rows = []                # (Candidate, CheckBox)
        self.kind_boxes = [("walls", self.chkWalls), ("grids", self.chkGrids),
                           ("refplanes", self.chkRefPlanes), ("lines", self.chkLines),
                           ("separations", self.chkSeparations), ("beams", self.chkBeams),
                           ("sketches", self.chkSketches)]
        self.btnScan.Click += self.on_scan
        self.btnAll.Click += lambda s, e: self._tick(True)
        self.btnNone.Click += lambda s, e: self._tick(False)
        self.btnSelect.Click += self.on_select
        self.btnFix.Click += self.on_fix
        self.btnClose.Click += lambda s, e: self.Close()
        self.btnHelp.Click += self.on_help
        self._show_warning_count()

    # ---- options ----
    def kinds(self):
        return [key for key, box in self.kind_boxes if box.IsChecked == True]

    def scope(self):
        if self.rbSelection.IsChecked == True:
            return "selection"
        if self.rbView.IsChecked == True:
            return "view"
        return "model"

    def max_angle(self):
        """The angle limit in degrees, or None when the box is not a valid number."""
        try:
            value = float((self.txtMaxAngle.Text or "").strip().replace(",", "."))
        except ValueError:
            return None
        if not 0 < value <= MAX_LIMIT_DEG:
            return None
        return value

    def step(self):
        return 45.0 if self.chk45.IsChecked == True else 90.0

    def _show_warning_count(self):
        count = len(off_axis_warnings(self.doc))
        self.txtWarnings.Text = "Revit off-axis warnings in this model: {}".format(count)
        return count

    # ---- scan ----
    def on_scan(self, sender=None, args=None):
        kinds = self.kinds()
        if not kinds:
            forms.alert("Tick at least one kind of element.", title=TITLE)
            return
        limit = self.max_angle()
        if limit is None:
            forms.alert("Enter an angle greater than 0 and at most {} degrees.".format(int(MAX_LIMIT_DEG)),
                        title=TITLE)
            return
        scope = self.scope()
        if scope == "selection" and not list(self.uidoc.Selection.GetElementIds()):
            forms.alert("Nothing is selected in Revit. Select the elements first, or pick "
                        "Whole model / Visible in the active view.", title=TITLE)
            return
        found = scan(self.doc, self.uidoc, kinds, scope, limit,
                     self.chkWarnedOnly.IsChecked == True, self.step())
        self._fill(found)
        warned = self._show_warning_count()
        fixable = len([c for c in found if not c.skip])
        if found:
            self.txtStatus.Text = "{} element(s) to fix, {} left alone. Untick any you want kept as they are.".format(
                fixable, len(found) - fixable)
        elif self.chkWarnedOnly.IsChecked == True:
            self.txtStatus.Text = ("Revit reports nothing off axis here ({} warning(s) in the model). Untick "
                                   "'Only elements in Revit's off-axis warnings' to look for every element "
                                   "off by up to {} degrees.".format(warned, limit))
        else:
            self.txtStatus.Text = "Nothing found off axis by up to {} degrees.".format(limit)

    def _fill(self, found):
        self.pnlResults.Children.Clear()
        self.rows = []
        for cand in found:
            box = WPFControls.CheckBox()
            box.Margin = Thickness(0, 3, 0, 3)
            box.Content = _row_content(cand.title, cand.detail, WARN if cand.skip else GREY)
            box.IsChecked = cand.skip is None
            box.IsEnabled = cand.skip is None
            box.Click += self._update_count
            self.pnlResults.Children.Add(box)
            self.rows.append((cand, box))
        if not found:
            note = WPFControls.TextBlock()
            note.Text = "Nothing to fix."
            note.Foreground = _brush(GREY)
            self.pnlResults.Children.Add(note)
        self._update_count()

    def ticked(self):
        return [cand for cand, box in self.rows if box.IsChecked == True and not cand.skip]

    def _tick(self, value):
        for cand, box in self.rows:
            if not cand.skip:
                box.IsChecked = value
        self._update_count()

    def _update_count(self, *args):
        self.txtResultCount.Text = "4. FOUND {}  -  {} ticked to fix".format(len(self.rows), len(self.ticked()))

    # ---- actions ----
    def on_select(self, sender, args):
        cands = self.ticked() or [cand for cand, _ in self.rows]
        if not cands:
            forms.alert("Scan first - there is nothing to select.", title=TITLE)
            return
        ids = List[DB.ElementId]()
        seen = set()
        for cand in cands:
            if _eid_int(cand.select_id) not in seen:
                seen.add(_eid_int(cand.select_id))
                ids.Add(cand.select_id)
        self.Close()
        self.uidoc.Selection.SetElementIds(ids)
        try:
            self.uidoc.ShowElements(ids)
        except Exception:
            pass

    def on_fix(self, sender, args):
        cands = self.ticked()
        if not cands:
            forms.alert("Scan, then tick at least one element to fix.", title=TITLE)
            return
        worst = max(c.deviation for c in cands)
        move = max(c.move_mm for c in cands)
        question = (u"Turn {} element(s) onto the nearest axis?\n\nEach one turns about its own midpoint "
                    u"by at most {}; its ends move at most {}. Joined elements follow; a sketch "
                    u"edge takes its two neighbouring edges with it.\n\n"
                    u"One Undo reverts the whole run.".format(len(cands), fmt_deg(worst), fmt_mm(move)))
        if not forms.alert(question, title=TITLE, yes=True, no=True):
            return
        before = len(off_axis_warnings(self.doc))
        try:
            results = fix_all(self.doc, cands)
        except Exception as error:
            forms.alert("The fix could not run - nothing was changed.\n\n{}".format(error), title=TITLE)
            return
        after = len(off_axis_warnings(self.doc))
        self.on_scan()
        fixed = len([r for r in results if r[1] == FIXED])
        self.txtStatus.Text = u"Fixed {} of {}. Revit off-axis warnings: {} → {}.".format(
            fixed, len(results), before, after)
        forms.alert(summary_text(results, before, after), title=TITLE)

    def on_help(self, sender, args):
        if _open_help_page(HELP_PAGE):
            return
        forms.alert(
            "Fix Off Axis\n\n"
            "1. Tick the kinds of element to check and where to look.\n"
            "2. Keep 'Only elements in Revit's off-axis warnings' ticked to fix exactly what "
            "Revit warns about; untick it to find every element off by up to the angle limit.\n"
            "3. Scan - the list shows how far each element is off and how far its ends would "
            "move. Pinned elements, group members and elements owned by another user are "
            "listed but left alone.\n"
            "4. Fix Ticked - each element turns about its own midpoint onto the nearest axis; "
            "a floor / roof / ceiling sketch edge takes its two neighbouring edges with it. "
            "One Undo reverts the whole run.",
            title=TITLE)


# ============================================================================
# MAIN
# ============================================================================
if __name__ == "__main__":
    try:
        if not revit.doc or revit.doc.IsFamilyDocument:
            forms.alert("Open a project first.", title=TITLE)
        else:
            FixOffAxisWindow(revit.doc, revit.uidoc).ShowDialog()
    except Exception as ex:
        forms.alert("Error: {}".format(ex), title=TITLE)
