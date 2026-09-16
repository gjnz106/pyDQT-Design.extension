# -*- coding: utf-8 -*-
"""Auto Dimension Beams
Works for beams (Structural Framing) at any angle, in the beam's own frame:
  1. Beam Width  : dim the two side faces (across the beam)
  2. Face to Grid: nearest side face to the nearest grid parallel to the beam
  3. Center to Grid: beam centreline to the nearest parallel grid
  4. End to Grid : each beam end to the nearest grid perpendicular to the beam

Copyright (c) 2025 Dang Quoc Truong (DQT)
All rights reserved.
"""

__title__ = "Auto Dim\nBeams"
__author__ = "Dang Quoc Truong (DQT)"
__doc__ = "Auto dimension selected beams (any angle) in the active plan view."

import os
import clr
clr.AddReference('RevitAPI')
clr.AddReference('RevitAPIUI')
clr.AddReference('PresentationCore')
clr.AddReference('PresentationFramework')
clr.AddReference('WindowsBase')
clr.AddReference('System')

import Autodesk.Revit.DB as DB
from Autodesk.Revit.DB import (
    Transaction, FilteredElementCollector,
    BuiltInCategory, BuiltInParameter,
    Options, Solid, PlanarFace, Line, XYZ,
    ReferenceArray, ElementId, ElementTransformUtils,
    FamilyInstance, FamilyInstanceReferenceType,
    LocationCurve, Grid, Reference,
    GeometryInstance, DimensionType,
    Dimension, IndependentTag, TextNote
)
from Autodesk.Revit.UI.Selection import ObjectType, ISelectionFilter
from pyrevit import revit, forms, script


def _open_help_page(html_filename):
    """Open this tool's page from the shared _Annotate_Help folder in the
    default browser. Returns True on success, False if the caller should
    fall back to the in-app help text (e.g. the folder went missing)."""
    try:
        panel_dir = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        path = os.path.join(panel_dir, "_Annotate_Help", html_filename)
        if not os.path.isfile(path):
            return False
        os.startfile(path)
        return True
    except Exception:
        return False

import System
from System.Collections.Generic import List
from System.Windows import (
    Window, WindowStartupLocation, Thickness,
    HorizontalAlignment, VerticalAlignment, TextWrapping,
    FontWeights, CornerRadius, SizeToContent, ResizeMode
)
from System.Windows.Controls import (
    StackPanel, Button, TextBlock, Border, CheckBox,
    Orientation, ComboBox, ComboBoxItem, TextBox
)
from System.Windows.Media import SolidColorBrush, Color, Colors, FontFamily
import math

doc = __revit__.ActiveUIDocument.Document
uidoc = __revit__.ActiveUIDocument
active_view = doc.ActiveView

DQT_PRIMARY   = Color.FromRgb(0xF0, 0xCC, 0x88)
DQT_ACCENT    = Color.FromRgb(0xC8, 0x96, 0x50)
DQT_BG        = Color.FromRgb(0xFE, 0xF8, 0xE7)
DQT_DARK      = Color.FromRgb(0x3C, 0x3C, 0x3C)
DQT_WHITE     = Colors.White
DQT_BORDER    = Color.FromRgb(0xDD, 0xDD, 0xDD)
DQT_TEXT_DARK = Color.FromRgb(0x33, 0x33, 0x33)

MM = 304.8


def B(color):
    return SolidColorBrush(color)


def _eid_int(eid):
    try:
        return eid.IntegerValue
    except:
        return eid.Value


def _bic_int(bic):
    return int(bic)


BEAM_CAT_ID = _bic_int(BuiltInCategory.OST_StructuralFraming)


def is_beam(elem):
    if not isinstance(elem, FamilyInstance):
        return False
    cat = elem.Category
    if cat is None:
        return False
    return _eid_int(cat.Id) == BEAM_CAT_ID


class BeamSelectionFilter(ISelectionFilter):
    def AllowElement(self, elem):
        return is_beam(elem)

    def AllowReference(self, reference, position):
        return False


# =====================================================================
#  BEAM GEOMETRY (plan frame)
# =====================================================================

def get_beam_geom(beam):
    """Plan frame of a straight beam: end points, midpoint, axis dir (bd) and
    in-plane perpendicular (bp). None for non-straight beams."""
    loc = beam.Location
    if not isinstance(loc, LocationCurve):
        return None
    curve = loc.Curve
    if not isinstance(curve, Line):
        return None
    p0 = curve.GetEndPoint(0)
    p1 = curve.GetEndPoint(1)
    dx, dy = p1.X - p0.X, p1.Y - p0.Y
    ln = math.sqrt(dx * dx + dy * dy)
    if ln < 1e-9:
        return None
    bd = XYZ(dx / ln, dy / ln, 0)
    bp = XYZ(-bd.Y, bd.X, 0)
    z = p0.Z
    mid = XYZ((p0.X + p1.X) / 2.0, (p0.Y + p1.Y) / 2.0, z)
    return {'p0': XYZ(p0.X, p0.Y, z), 'p1': XYZ(p1.X, p1.Y, z),
            'mid': mid, 'bd': bd, 'bp': bp, 'len': ln, 'z': z}


def get_all_dimension_types():
    dim_types = []
    for dt in FilteredElementCollector(doc).OfClass(DimensionType):
        try:
            if dt.StyleType == DB.DimensionStyleType.Linear:
                name = dt.get_Parameter(
                    BuiltInParameter.SYMBOL_NAME_PARAM).AsString()
                if name:
                    dim_types.append((name, dt.Id))
        except:
            continue
    dim_types.sort(key=lambda x: x[0])
    return dim_types


# =====================================================================
#  BEAM REFERENCES
# =====================================================================

def _collect_line_ref(geo, best):
    """Recurse geometry, keeping the longest Line that carries a Reference
    (the beam's centreline). best = [reference, length]."""
    if geo is None:
        return
    for g in geo:
        try:
            if isinstance(g, Line) and g.Reference is not None:
                L = g.Length
                if best[0] is None or L > best[1]:
                    best[0] = g.Reference
                    best[1] = L
            elif isinstance(g, GeometryInstance):
                _collect_line_ref(g.GetInstanceGeometry(), best)
        except:
            continue


def _get_beam_centerline_ref(beam):
    """Dimensionable reference to the beam centreline, from its non-visible
    reference line (works when CenterLeftRight isn't exposed by the family)."""
    try:
        opts = Options()
        opts.ComputeReferences = True
        opts.IncludeNonVisibleObjects = True
        geo = beam.get_Geometry(opts)
        best = [None, 0.0]
        _collect_line_ref(geo, best)
        return best[0]
    except:
        return None


def get_beam_refs(beam, view, geom):
    """Return references keyed: 'left'/'right' (side faces, normal along bp),
    'start'/'end' (end faces, normal along bd), 'center' (centreline). Plus
    'width' and 'length' sizes (ft)."""
    refs = {}
    sizes = {'width': 0.0, 'length': geom['len']}

    # Centreline reference (along the length). Many framing families don't
    # expose CenterLeftRight, so fall back to the beam's non-visible reference
    # line (the actual centreline curve carries a dimensionable reference).
    try:
        cl = beam.GetReferences(FamilyInstanceReferenceType.CenterLeftRight)
        if cl and cl.Count > 0:
            refs['center'] = cl[0]
    except:
        pass
    if 'center' not in refs:
        cref = _get_beam_centerline_ref(beam)
        if cref is not None:
            refs['center'] = cref

    # Side/end faces: prefer the instance-qualified geometry references (these
    # are the ones that reliably dimension in the view - same approach as the
    # column tool). The FamilyInstanceReferenceType lookups are only a fallback,
    # because for framing families Left/Right often return references that don't
    # dimension in a plan view.
    face_refs, face_sizes = _get_beam_face_refs(beam, view, geom)
    for key in ('left', 'right', 'start', 'end'):
        if key in face_refs:
            refs[key] = face_refs[key]

    tmap = {
        'left':  FamilyInstanceReferenceType.Left,
        'right': FamilyInstanceReferenceType.Right,
        'start': FamilyInstanceReferenceType.Front,
        'end':   FamilyInstanceReferenceType.Back,
    }
    for key, rt in tmap.items():
        if key in refs:
            continue
        try:
            rl = beam.GetReferences(rt)
            if rl and rl.Count > 0:
                refs[key] = rl[0]
        except:
            pass

    if face_sizes.get('width'):
        sizes['width'] = face_sizes['width']
    return refs, sizes


def _get_beam_face_refs(beam, view, geom):
    """Dimensionable face references classified in the beam's frame:
    normal ~ bp -> side (left/right, width); normal ~ bd -> end (start/end).

    Classifies the SYMBOL faces (which stay intact and keep their references
    even when the beam is cut at a join) using their normals transformed to
    world by the instance transform - so it is robust to join-trimming, unlike
    pairing instance faces to symbol faces by index."""
    opts = Options()
    opts.ComputeReferences = True
    opts.IncludeNonVisibleObjects = False
    opts.View = view
    geo = beam.get_Geometry(opts)
    if geo is None:
        return {}, {}

    bd = geom['bd']
    bp = geom['bp']

    side_faces = []   # normal ~ bp
    end_faces = []    # normal ~ bd
    for gobj in geo:
        if not isinstance(gobj, GeometryInstance):
            continue
        try:
            xf = gobj.Transform
            sg = gobj.GetSymbolGeometry()
            if not sg:
                continue
            for solid in sg:
                if not isinstance(solid, Solid) or solid.Volume <= 0:
                    continue
                for fi in range(solid.Faces.Size):
                    face = solid.Faces.get_Item(fi)
                    if not isinstance(face, PlanarFace):
                        continue
                    if face.Reference is None:
                        continue
                    wn = xf.OfVector(face.FaceNormal)
                    ln = wn.GetLength()
                    if ln < 1e-9:
                        continue
                    wn = XYZ(wn.X / ln, wn.Y / ln, wn.Z / ln)
                    if abs(wn.Z) > 0.1:         # skip top/bottom faces
                        continue
                    try:
                        sr = face.Reference.ConvertToStableRepresentation(doc)
                        if ":INSTANCE:" not in sr:
                            continue
                    except:
                        continue
                    wo = xf.OfPoint(face.Origin)
                    dp = abs(wn.DotProduct(bp))
                    dd = abs(wn.DotProduct(bd))
                    if dp > dd and dp > 0.7:
                        side_faces.append((face.Reference, wn, wo))
                    elif dd > dp and dd > 0.7:
                        end_faces.append((face.Reference, wn, wo))
        except:
            continue

    result = {}
    sizes = {}

    result_pair = _outermost_pair(side_faces, bp)
    if result_pair:
        (rl, rr), dist = result_pair
        result['left'] = rl
        result['right'] = rr
        sizes['width'] = dist

    epair = _outermost_pair(end_faces, bd)
    if epair:
        (rs, re), _ = epair
        result['start'] = rs
        result['end'] = re

    return result, sizes


def _outermost_pair(faces, axis):
    """Pick the outermost opposing pair along 'axis'. Returns
    ((neg_side_ref, pos_side_ref), distance) or None."""
    if len(faces) < 2:
        return None
    best = None
    best_dist = 0.0
    for i in range(len(faces)):
        for j in range(i + 1, len(faces)):
            ri, ni, oi = faces[i]
            rj, nj, oj = faces[j]
            if ni.DotProduct(nj) > -0.5:      # must oppose
                continue
            diff = XYZ(oj.X - oi.X, oj.Y - oi.Y, 0)
            dist = abs(diff.DotProduct(ni))
            if dist > best_dist:
                best_dist = dist
                best = (i, j)
    if not best:
        return None
    i, j = best
    ri, ni, oi = faces[i]
    rj, nj, oj = faces[j]
    if ni.DotProduct(axis) < 0:
        return (ri, rj), best_dist
    return (rj, ri), best_dist


# =====================================================================
#  GRIDS
# =====================================================================

def get_grid_reference(grid):
    try:
        return Reference(grid)
    except:
        return None


def signed_dist_to_grid(point, grid):
    curve = grid.Curve
    start = curve.GetEndPoint(0)
    gd = curve.Direction.Normalize()
    perp = XYZ(-gd.Y, gd.X, 0)
    vec = XYZ(point.X - start.X, point.Y - start.Y, 0)
    return vec.DotProduct(perp)


def find_beam_grids(geom, view):
    """Grids split relative to the beam axis: parallel (measured across, along
    bp) and perpendicular (measured along the beam, along bd)."""
    bd = geom['bd']
    par, perp = [], []
    for g in FilteredElementCollector(doc, view.Id).OfClass(Grid).ToElements():
        try:
            curve = g.Curve
            if not isinstance(curve, Line):
                continue
            gd = curve.Direction.Normalize()
            dot = abs(gd.X * bd.X + gd.Y * bd.Y)
            sd = signed_dist_to_grid(geom['mid'], g)
            if dot > 0.9:
                par.append((g, sd, abs(sd)))
            elif dot < 0.1:
                perp.append(g)
        except:
            continue
    par.sort(key=lambda x: x[2])
    return par, perp


# =====================================================================
#  DIMENSION CREATION
# =====================================================================

# Occupied annotation bounding boxes (set in main() when overlap-avoidance is
# on, else None). New dimensions are nudged clear of these and then added.
_OCCUPIED = None


def _bbox_overlap(a, b, pad=0.0):
    return not (a.Max.X + pad < b.Min.X or a.Min.X - pad > b.Max.X or
                a.Max.Y + pad < b.Min.Y or a.Min.Y - pad > b.Max.Y)


def collect_anno_boxes(view):
    """View bounding boxes of existing dimensions, tags and text notes (grids
    are excluded - they span the whole view)."""
    boxes = []
    for cls in (Dimension, IndependentTag, TextNote):
        try:
            for e in FilteredElementCollector(doc, view.Id).OfClass(cls):
                try:
                    bb = e.get_BoundingBox(view)
                    if bb is not None:
                        boxes.append(bb)
                except:
                    pass
        except:
            pass
    return boxes


def _avoid_overlap(dim, line, anchor, view):
    """Nudge a freshly created dimension perpendicular to its line, away from
    the member (anchor), until it clears everything already placed, then record
    it so later dimensions stack instead of piling up."""
    if _OCCUPIED is None:
        return
    try:
        d = line.Direction
        perp = XYZ(-d.Y, d.X, 0)
        mid = line.Evaluate(0.5, True)
        if (mid.X - anchor.X) * perp.X + (mid.Y - anchor.Y) * perp.Y < 0:
            perp = XYZ(-perp.X, -perp.Y, 0)
        step = 1.2   # ft (~365 mm) per nudge
        for _ in range(15):
            bb = dim.get_BoundingBox(view)
            if bb is None:
                break
            if not any(_bbox_overlap(bb, ob) for ob in _OCCUPIED):
                break
            ElementTransformUtils.MoveElement(
                doc, dim.Id, XYZ(perp.X * step, perp.Y * step, 0))
            doc.Regenerate()
        bb = dim.get_BoundingBox(view)
        if bb is not None:
            _OCCUPIED.append(bb)
    except:
        pass


def make_dim(view, line, ref_array, dim_type_id=None, anchor=None):
    dim = None
    if dim_type_id:
        dt = doc.GetElement(dim_type_id)
        if dt:
            dim = doc.Create.NewDimension(view, line, ref_array, dt)
    if dim is None:
        dim = doc.Create.NewDimension(view, line, ref_array)
    if dim is not None and anchor is not None:
        _avoid_overlap(dim, line, anchor, view)
    return dim


def _line(base, direction, half):
    p1 = XYZ(base.X - direction.X * half, base.Y - direction.Y * half, base.Z)
    p2 = XYZ(base.X + direction.X * half, base.Y + direction.Y * half, base.Z)
    return Line.CreateBound(p1, p2)


def create_dim_width(beam, view, refs, geom, sizes, off, dt_id):
    created, errs = [], []
    if 'left' not in refs or 'right' not in refs:
        return created, ["Width: side face refs not found ({})".format(
            list(refs.keys()))]
    bp = geom['bp']
    width = sizes.get('width', 0.0)
    half = (width / 2.0 if width > 0 else 1.0) + off
    base = geom['mid']
    ra = ReferenceArray()
    ra.Append(refs['left'])
    ra.Append(refs['right'])
    try:
        d = make_dim(view, _line(base, bp, half), ra, dt_id, anchor=base)
        if d:
            created.append(d.Id)
    except Exception as ex:
        errs.append("Width: {}".format(str(ex)))
    return created, errs


def _dim_to_parallel(beam, view, ref, geom, off, off_extra, dt_id, tag):
    """Dim a beam reference (centre or a side face) to the nearest parallel
    grid, measured across the beam (along bp). off = dim-line margin,
    off_extra = shift along the beam so stacked dims don't overlap."""
    created, errs = [], []
    bd, bp, mid = geom['bd'], geom['bp'], geom['mid']
    par, _ = find_beam_grids(view=view, geom=geom)
    if not par:
        return created, ["no parallel grid"]
    g, sd, ad = par[0]
    if ad < 0.05:
        return created, []
    gr = get_grid_reference(g)
    if not gr:
        return created, ["no grid ref"]
    ra = ReferenceArray()
    ra.Append(ref)
    ra.Append(gr)
    # Line along bp, centred between beam and grid, shifted along the beam so
    # the different dimensions don't stack on top of each other.
    cen = XYZ(mid.X + bp.X * (sd / 2.0) + bd.X * off_extra,
              mid.Y + bp.Y * (sd / 2.0) + bd.Y * off_extra, mid.Z)
    half = ad / 2.0 + off + 1.0
    try:
        d = make_dim(view, _line(cen, bp, half), ra, dt_id, anchor=mid)
        if d:
            created.append(d.Id)
    except Exception as ex:
        errs.append("{}: {}".format(tag, str(ex)))
    return created, errs


def create_dim_center_to_grid(beam, view, refs, geom, off, dt_id):
    if 'center' not in refs:
        # No dimensionable centreline on this family - fall back to the nearest
        # side face so a grid dimension is still produced.
        created, errs = create_dim_face_to_grid(beam, view, refs, geom,
                                                off, dt_id)
        return created, errs + ["centre ref unavailable - used nearest face"]
    return _dim_to_parallel(beam, view, refs['center'], geom,
                            off, off * 2.0, dt_id, "CtrGrid")


def create_dim_face_to_grid(beam, view, refs, geom, off, dt_id):
    par, _ = find_beam_grids(view=view, geom=geom)
    if not par:
        return [], ["no parallel grid"]
    g, sd, ad = par[0]
    # side face nearest the grid: +bp side if grid is on +bp side
    face_key = 'right' if sd > 0 else 'left'
    if face_key not in refs:
        face_key = 'left' if face_key == 'right' else 'right'
    if face_key not in refs:
        return [], ["no side face ref"]
    return _dim_to_parallel(beam, view, refs[face_key], geom,
                            off, off * 3.5, dt_id, "FaceGrid")


def create_dim_end_to_grid(beam, view, refs, geom, off, dt_id):
    created, errs = [], []
    bd, bp = geom['bd'], geom['bp']
    _, perp = find_beam_grids(view=view, geom=geom)
    if not perp:
        return created, ["no perpendicular grid"]
    for key, endpt in (('start', geom['p0']), ('end', geom['p1'])):
        if key not in refs:
            continue
        best, best_d = None, 1e18
        for g in perp:
            d = abs(signed_dist_to_grid(endpt, g))
            if d < best_d:
                best_d = d
                best = g
        if best is None or best_d < 0.05:
            continue
        gr = get_grid_reference(best)
        if not gr:
            continue
        ra = ReferenceArray()
        ra.Append(refs[key])
        ra.Append(gr)
        base = XYZ(endpt.X + bp.X * off, endpt.Y + bp.Y * off, endpt.Z)
        half = best_d + 2.0
        try:
            d = make_dim(view, _line(base, bd, half), ra, dt_id, anchor=endpt)
            if d:
                created.append(d.Id)
        except Exception as ex:
            errs.append("End {}: {}".format(key, str(ex)))
    return created, errs


# =====================================================================
#  WPF DIALOG
# =====================================================================

class AutoDimBeamDialog(Window):
    def __init__(self, count, dim_types):
        self.count = count
        self.dim_types = dim_types
        self.result = None
        self._build()

    def _st(self, t):
        b = TextBlock(); b.Text = t; b.FontSize = 12
        b.FontWeight = FontWeights.SemiBold; b.Foreground = B(DQT_ACCENT)
        b.Margin = Thickness(0, 0, 0, 6); return b

    def _cb(self, t, c=True):
        x = CheckBox(); x.Content = t
        x.IsChecked = System.Nullable[System.Boolean](c)
        x.Margin = Thickness(4, 3, 0, 3); x.FontSize = 11.5
        x.Foreground = B(DQT_TEXT_DARK); return x

    def _nt(self, t):
        b = TextBlock(); b.Text = t; b.FontSize = 10
        b.Foreground = B(Color.FromRgb(0x99, 0x99, 0x99))
        b.TextWrapping = TextWrapping.Wrap
        b.Margin = Thickness(24, 0, 0, 2); return b

    def _cd(self, ch):
        b = Border(); b.Background = B(DQT_WHITE)
        b.BorderBrush = B(DQT_BORDER); b.BorderThickness = Thickness(1)
        b.CornerRadius = CornerRadius(4)
        b.Margin = Thickness(16, 10, 16, 0)
        b.Padding = Thickness(12, 10, 12, 10)
        p = StackPanel()
        for c in ch:
            p.Children.Add(c)
        b.Child = p; return b

    def _build(self):
        self.Title = "Auto Dimension Beams - DQT"
        self.Width = 440; self.SizeToContent = SizeToContent.Height
        self.WindowStartupLocation = WindowStartupLocation.CenterScreen
        self.ResizeMode = ResizeMode.NoResize; self.Background = B(DQT_BG)

        m = StackPanel()

        hd = Border(); hd.Background = B(DQT_DARK)
        hd.Padding = Thickness(16, 12, 16, 12)
        hp = StackPanel()
        t = TextBlock(); t.Text = "Auto Dimension Beams"
        t.FontSize = 16; t.FontWeight = FontWeights.Bold
        t.Foreground = B(DQT_PRIMARY); t.FontFamily = FontFamily("Segoe UI")
        hp.Children.Add(t)
        s = TextBlock(); s.Text = "Dang Quoc Truong (DQT)"
        s.FontSize = 10; s.Foreground = B(DQT_ACCENT)
        s.Margin = Thickness(0, 2, 0, 0); hp.Children.Add(s)
        hd.Child = hp; m.Children.Add(hd)

        sb = Border(); sb.Background = B(DQT_WHITE)
        sb.BorderBrush = B(DQT_BORDER); sb.BorderThickness = Thickness(1)
        sb.CornerRadius = CornerRadius(4)
        sb.Margin = Thickness(16, 12, 16, 0)
        sb.Padding = Thickness(12, 8, 12, 8)
        st2 = TextBlock()
        st2.Text = "{} beam(s) selected".format(self.count)
        st2.FontSize = 13; st2.FontWeight = FontWeights.SemiBold
        st2.Foreground = B(DQT_TEXT_DARK); sb.Child = st2
        m.Children.Add(sb)

        dt_label = self._st("Dimension Type")
        self.cmb_dim_type = ComboBox()
        self.cmb_dim_type.FontSize = 11.5
        self.cmb_dim_type.Height = 28
        default_item = ComboBoxItem()
        default_item.Content = "<Default>"
        default_item.Tag = None
        self.cmb_dim_type.Items.Add(default_item)
        self.cmb_dim_type.SelectedIndex = 0
        for name, eid in self.dim_types:
            item = ComboBoxItem()
            item.Content = name
            item.Tag = eid
            self.cmb_dim_type.Items.Add(item)
        m.Children.Add(self._cd([dt_label, self.cmb_dim_type]))

        self.chk_w = self._cb("Beam width (side to side)", True)
        m.Children.Add(self._cd([self._st("1. Beam Size"), self.chk_w]))

        self.chk_f = self._cb("Side face to nearest parallel grid", False)
        m.Children.Add(self._cd([self._st("2. Face to Grid"),
            self.chk_f, self._nt("Beam side face to the grid running along it")]))

        self.chk_c = self._cb("Centreline to nearest parallel grid", False)
        m.Children.Add(self._cd([self._st("3. Center to Grid"),
            self.chk_c, self._nt("Beam centreline to the grid running along it")]))

        self.chk_e = self._cb("Each end to nearest perpendicular grid", False)
        m.Children.Add(self._cd([self._st("4. End to Grid"),
            self.chk_e, self._nt("Beam ends to the grids crossing it")]))

        op = StackPanel(); op.Orientation = Orientation.Horizontal
        ol = TextBlock(); ol.Text = "Dim line offset (mm): "
        ol.FontSize = 12; ol.Foreground = B(DQT_TEXT_DARK)
        ol.VerticalAlignment = VerticalAlignment.Center; op.Children.Add(ol)
        self.txt_off = TextBox(); self.txt_off.Text = "500"
        self.txt_off.Width = 80; self.txt_off.FontSize = 12
        self.txt_off.Padding = Thickness(4, 2, 4, 2); op.Children.Add(self.txt_off)
        self.chk_avoid = self._cb(
            "Avoid overlapping existing dimensions / tags / text", True)
        m.Children.Add(self._cd([op, self.chk_avoid]))

        bp = StackPanel(); bp.Orientation = Orientation.Horizontal
        bp.HorizontalAlignment = HorizontalAlignment.Right
        bp.Margin = Thickness(16, 14, 16, 14)
        bh = Button(); bh.Content = "? Help"; bh.Width = 70; bh.Height = 32
        bh.FontSize = 12; bh.Margin = Thickness(0, 0, 8, 0)
        bh.Background = B(DQT_WHITE); bh.Foreground = B(DQT_TEXT_DARK)
        bh.Click += self._on_help; bp.Children.Add(bh)
        bc = Button(); bc.Content = "Cancel"; bc.Width = 90; bc.Height = 32
        bc.FontSize = 12; bc.Margin = Thickness(0, 0, 8, 0)
        bc.Background = B(DQT_WHITE); bc.Foreground = B(DQT_TEXT_DARK)
        bc.Click += self._cancel; bp.Children.Add(bc)
        br = Button(); br.Content = "Create Dimensions"
        br.Width = 140; br.Height = 32; br.FontSize = 12
        br.FontWeight = FontWeights.SemiBold
        br.Background = B(DQT_ACCENT); br.Foreground = B(DQT_WHITE)
        br.Click += self._run; bp.Children.Add(br)
        m.Children.Add(bp)

        f = Border(); f.Background = B(DQT_DARK)
        f.Padding = Thickness(16, 6, 16, 6)
        ft = TextBlock()
        ft.Text = "Copyright (c) 2025 Dang Quoc Truong (DQT)"
        ft.FontSize = 9; ft.Foreground = B(DQT_ACCENT)
        ft.HorizontalAlignment = HorizontalAlignment.Center
        f.Child = ft; m.Children.Add(f)
        self.Content = m

    def _cancel(self, s, e):
        self.result = None; self.Close()

    def _on_help(self, s, e):
        if _open_help_page("dim_beam.html"):
            return
        forms.alert(
            "Auto-dimensions the selected beams (any angle) in the active "
            "plan view.\n\n"
            "Tick one or more dimension modes:\n"
            "1. Beam Size - width (side to side).\n"
            "2. Face to Grid - beam side face to the nearest parallel grid.\n"
            "3. Center to Grid - beam centreline to the nearest parallel grid.\n"
            "4. End to Grid - each beam end to the nearest perpendicular grid.\n\n"
            "Set the dimension line offset (mm) and optionally 'Avoid "
            "overlapping existing dimensions / tags / text', then Create "
            "Dimensions.",
            title="Auto Dimension Beams - DQT")

    def _run(self, s, e):
        try:
            off = float(self.txt_off.Text)
        except:
            off = 500.0
        dim_type_id = None
        sel_item = self.cmb_dim_type.SelectedItem
        if sel_item and sel_item.Tag is not None:
            dim_type_id = sel_item.Tag
        self.result = {
            'w': self.chk_w.IsChecked == True,
            'f': self.chk_f.IsChecked == True,
            'c': self.chk_c.IsChecked == True,
            'e': self.chk_e.IsChecked == True,
            'avoid': self.chk_avoid.IsChecked == True,
            'off': off,
            'dim_type_id': dim_type_id,
        }
        self.Close()


# =====================================================================
#  MAIN
# =====================================================================

def main():
    view = active_view
    if view.ViewType not in [
        DB.ViewType.FloorPlan, DB.ViewType.CeilingPlan,
        DB.ViewType.EngineeringPlan, DB.ViewType.AreaPlan
    ]:
        forms.alert("Plan views only.", title="Auto Dimension Beams - DQT")
        return

    beams = []
    sel = uidoc.Selection.GetElementIds()
    if sel.Count > 0:
        for eid in sel:
            e = doc.GetElement(eid)
            if is_beam(e):
                beams.append(e)
    if not beams:
        try:
            picks = uidoc.Selection.PickObjects(
                ObjectType.Element, BeamSelectionFilter(),
                "Select beams to dimension")
            for p in picks:
                e = doc.GetElement(p.ElementId)
                if e:
                    beams.append(e)
        except:
            return
    if not beams:
        forms.alert("No beams selected.", title="Auto Dimension Beams - DQT")
        return

    dim_types = get_all_dimension_types()
    dlg = AutoDimBeamDialog(len(beams), dim_types)
    dlg.ShowDialog()
    if dlg.result is None:
        return

    r = dlg.result
    if not any([r['w'], r['f'], r['c'], r['e']]):
        forms.alert("Select at least one option.",
                    title="Auto Dimension Beams - DQT")
        return

    dt_id = r['dim_type_id']
    off = r['off'] / MM
    total = 0
    failed = []
    errors = []

    global _OCCUPIED
    _OCCUPIED = collect_anno_boxes(view) if r['avoid'] else None

    txn = Transaction(doc, "DQT - Auto Dimension Beams")
    txn.Start()
    try:
        for beam in beams:
            bid = _eid_int(beam.Id)
            cd = 0
            try:
                geom = get_beam_geom(beam)
                if geom is None:
                    errors.append("Beam {}: not a straight beam".format(bid))
                    failed.append(bid)
                    continue
                refs, sizes = get_beam_refs(beam, view, geom)
                if r['w']:
                    dims, es = create_dim_width(
                        beam, view, refs, geom, sizes, off, dt_id)
                    cd += len(dims)
                    errors.extend(["Beam {} {}".format(bid, x) for x in es])
                if r['f']:
                    dims, es = create_dim_face_to_grid(
                        beam, view, refs, geom, off, dt_id)
                    cd += len(dims)
                    errors.extend(["Beam {} {}".format(bid, x) for x in es])
                if r['c']:
                    dims, es = create_dim_center_to_grid(
                        beam, view, refs, geom, off, dt_id)
                    cd += len(dims)
                    errors.extend(["Beam {} {}".format(bid, x) for x in es])
                if r['e']:
                    dims, es = create_dim_end_to_grid(
                        beam, view, refs, geom, off, dt_id)
                    cd += len(dims)
                    errors.extend(["Beam {} {}".format(bid, x) for x in es])
                total += cd
                if cd == 0:
                    errors.append("Beam {}: refs={}, dims=0".format(
                        bid, list(refs.keys())))
                    failed.append(bid)
            except Exception as ex:
                errors.append("Beam {} error: {}".format(bid, str(ex)))
                failed.append(bid)
        txn.Commit()
    except Exception as ex:
        txn.RollBack()
        forms.alert("Transaction failed: {}".format(str(ex)),
                    title="Auto Dimension Beams - DQT")
        return

    ok = len(beams) - len(failed)
    msg = "Created {} dimension(s) for {} of {} beam(s).".format(
        total, ok, len(beams))
    if failed:
        msg += "\n\n{} failed (ID: {})".format(
            len(failed), ", ".join(str(x) for x in failed[:10]))
    if errors:
        msg += "\n\nDetails:\n" + "\n".join(errors[:5])
    forms.alert(msg, title="Auto Dimension Beams - DQT")


if __name__ == '__main__':
    main()
else:
    main()
