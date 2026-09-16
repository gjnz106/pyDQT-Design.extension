# -*- coding: utf-8 -*-
"""Auto Dimension Columns
3 modes:
  1. Column Size: dim Left<->Right, Front<->Back
  2. Face to Grid: dim column face to nearest grid
  3. Center to Grid: dim column center to nearest grid

Copyright (c) 2025 Dang Quoc Truong (DQT)
All rights reserved.
"""

__title__ = "Auto Dim\nColumns"
__author__ = "Dang Quoc Truong (DQT)"
__doc__ = "Auto dimension selected columns in the active view."

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
    LocationPoint, Grid, Reference,
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
    GridLength, GridUnitType, FontWeights, CornerRadius,
    SizeToContent, WindowStyle, ResizeMode
)
from System.Windows.Controls import (
    StackPanel, Button, TextBlock, RadioButton, GroupBox,
    Border, CheckBox, Grid as WPFGrid, ColumnDefinition,
    RowDefinition, Orientation, ComboBox, ComboBoxItem,
    TextBox, Separator
)
from System.Windows.Media import (
    SolidColorBrush, Color, Colors, FontFamily
)
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

def B(color):
    return SolidColorBrush(color)


class ColumnSelectionFilter(ISelectionFilter):
    def AllowElement(self, elem):
        return is_column(elem)
    def AllowReference(self, reference, position):
        return False


# =====================================================================
#  HELPERS
# =====================================================================

def _eid_int(eid):
    """Get integer value from ElementId - works on Revit 2024 (.IntegerValue) and 2025+/2026 (.Value)."""
    try:
        return eid.IntegerValue
    except:
        return eid.Value


def _bic_int(bic):
    """Get integer value from BuiltInCategory enum."""
    return int(bic)


COLUMN_CAT_IDS = [
    _bic_int(BuiltInCategory.OST_StructuralColumns),
    _bic_int(BuiltInCategory.OST_Columns)
]


def is_column(elem):
    """Check if element is a column (works across Revit versions)."""
    if not isinstance(elem, FamilyInstance):
        return False
    cat = elem.Category
    if cat is None:
        return False
    return _eid_int(cat.Id) in COLUMN_CAT_IDS

def get_column_center(column):
    loc = column.Location
    if isinstance(loc, LocationPoint):
        return loc.Point
    return None

def get_column_bbox_size(column):
    bbox = column.get_BoundingBox(active_view)
    if bbox is None:
        return 1.0, 1.0
    return bbox.Max.X - bbox.Min.X, bbox.Max.Y - bbox.Min.Y

def get_column_rotation(column):
    try:
        loc = column.Location
        if isinstance(loc, LocationPoint):
            return loc.Rotation
    except:
        pass
    return 0.0


def get_all_dimension_types():
    """Collect all linear dimension types in the document."""
    dim_types = []
    collector = FilteredElementCollector(doc).OfClass(DimensionType)
    for dt in collector:
        try:
            # Only linear dimension types (not angular, radial, etc.)
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
#  COLUMN REFERENCES
# =====================================================================

def get_column_all_refs(column, view):
    """Get all refs: FamilyInstanceReferenceType + geometry fallback."""
    refs = {}
    type_map = {
        'left':      FamilyInstanceReferenceType.Left,
        'right':     FamilyInstanceReferenceType.Right,
        'front':     FamilyInstanceReferenceType.Front,
        'back':      FamilyInstanceReferenceType.Back,
        'center_lr': FamilyInstanceReferenceType.CenterLeftRight,
        'center_fb': FamilyInstanceReferenceType.CenterFrontBack,
    }
    for key, ref_type in type_map.items():
        try:
            ref_list = column.GetReferences(ref_type)
            if ref_list and ref_list.Count > 0:
                refs[key] = ref_list[0]
        except:
            pass

    need_faces = ('left' not in refs or 'right' not in refs or
                  'front' not in refs or 'back' not in refs)
    if need_faces:
        geom_refs = _get_face_refs_from_symbol_geometry(column, view)
        if geom_refs:
            for key in ['left', 'right', 'front', 'back']:
                if key not in refs and key in geom_refs:
                    refs[key] = geom_refs[key]
    return refs


def _get_face_refs_from_symbol_geometry(column, view):
    """
    Get face references using GetSymbolGeometry() which produces
    INSTANCE-qualified refs (elementId:0:INSTANCE:symbolId:N:SURFACE).
    Uses GetInstanceGeometry() for world-space normals to classify faces.
    Picks outermost opposing pair by max distance for overall size.
    """
    opts = Options()
    opts.ComputeReferences = True
    opts.IncludeNonVisibleObjects = False
    opts.View = view
    geom = column.get_Geometry(opts)
    if geom is None:
        return None

    rotation = get_column_rotation(column)
    cos_r = math.cos(rotation)
    sin_r = math.sin(rotation)
    local_x = XYZ(cos_r, sin_r, 0)
    local_y = XYZ(-sin_r, cos_r, 0)

    instance_faces = []  # (normal_world, origin_world)
    symbol_refs = []

    for geom_obj in geom:
        if not isinstance(geom_obj, GeometryInstance):
            continue
        try:
            inst_geom = geom_obj.GetInstanceGeometry()
            sym_geom = geom_obj.GetSymbolGeometry()
            if not inst_geom or not sym_geom:
                continue

            inst_solids = [g for g in inst_geom if isinstance(g, Solid) and g.Volume > 0]
            sym_solids = [g for g in sym_geom if isinstance(g, Solid) and g.Volume > 0]

            for si in range(min(len(inst_solids), len(sym_solids))):
                ifc = inst_solids[si].Faces.Size
                sfc = sym_solids[si].Faces.Size
                if ifc != sfc:
                    continue
                for fi in range(ifc):
                    inst_face = inst_solids[si].Faces.get_Item(fi)
                    sym_face = sym_solids[si].Faces.get_Item(fi)
                    if not isinstance(inst_face, PlanarFace):
                        continue
                    if not isinstance(sym_face, PlanarFace):
                        continue
                    if sym_face.Reference is None:
                        continue
                    n = inst_face.FaceNormal
                    if abs(n.Z) > 0.1:
                        continue
                    try:
                        sr = sym_face.Reference.ConvertToStableRepresentation(doc)
                        if ":INSTANCE:" not in sr:
                            continue
                    except:
                        continue
                    instance_faces.append((n, inst_face.Origin))
                    symbol_refs.append(sym_face.Reference)
        except:
            continue

    if len(symbol_refs) < 2:
        return None

    x_faces = []
    y_faces = []
    for i in range(len(symbol_refs)):
        normal, origin = instance_faces[i]
        ref = symbol_refs[i]
        dx = abs(normal.DotProduct(local_x))
        dy = abs(normal.DotProduct(local_y))
        if dx > dy and dx > 0.7:
            x_faces.append((ref, normal, origin))
        elif dy > dx and dy > 0.7:
            y_faces.append((ref, normal, origin))

    result = {}

    # Left/Right: outermost opposing pair (max distance)
    if len(x_faces) >= 2:
        best_pair, best_dist = None, 0
        for i in range(len(x_faces)):
            for j in range(i + 1, len(x_faces)):
                ri, ni, oi = x_faces[i]
                rj, nj, oj = x_faces[j]
                if ni.DotProduct(nj) > -0.5:
                    continue
                diff = XYZ(oj.X - oi.X, oj.Y - oi.Y, 0)
                dist = abs(diff.DotProduct(ni))
                if dist > best_dist:
                    best_dist = dist
                    best_pair = (i, j)
        if best_pair:
            i, j = best_pair
            ra, na, oa = x_faces[i]
            rb, nb, ob = x_faces[j]
            if na.DotProduct(local_x) < 0:
                result['left'] = ra; result['right'] = rb
            else:
                result['left'] = rb; result['right'] = ra

    # Front/Back: outermost opposing pair
    if len(y_faces) >= 2:
        best_pair, best_dist = None, 0
        for i in range(len(y_faces)):
            for j in range(i + 1, len(y_faces)):
                ri, ni, oi = y_faces[i]
                rj, nj, oj = y_faces[j]
                if ni.DotProduct(nj) > -0.5:
                    continue
                diff = XYZ(oj.X - oi.X, oj.Y - oi.Y, 0)
                dist = abs(diff.DotProduct(ni))
                if dist > best_dist:
                    best_dist = dist
                    best_pair = (i, j)
        if best_pair:
            i, j = best_pair
            ra, na, oa = y_faces[i]
            rb, nb, ob = y_faces[j]
            if na.DotProduct(local_y) < 0:
                result['back'] = ra; result['front'] = rb
            else:
                result['back'] = rb; result['front'] = ra

    return result if result else None


# =====================================================================
#  GRID FUNCTIONS
# =====================================================================

def get_grid_reference(grid):
    try:
        return Reference(grid)
    except:
        return None

def get_signed_distance_to_grid(point, grid):
    curve = grid.Curve
    start = curve.GetEndPoint(0)
    gd = curve.Direction.Normalize()
    perp = XYZ(-gd.Y, gd.X, 0)
    vec = XYZ(point.X - start.X, point.Y - start.Y, 0)
    return vec.DotProduct(perp)

def find_nearest_grids(column_center, view):
    """Returns (h_grids, v_grids) sorted by abs distance."""
    all_grids = FilteredElementCollector(doc, view.Id) \
        .OfClass(Grid).ToElements()
    h, v = [], []
    for g in all_grids:
        try:
            curve = g.Curve
            if not isinstance(curve, Line):
                continue
            d = curve.Direction.Normalize()
            sd = get_signed_distance_to_grid(column_center, g)
            if abs(d.X) > abs(d.Y):
                h.append((g, sd, abs(sd)))
            else:
                v.append((g, sd, abs(sd)))
        except:
            continue
    h.sort(key=lambda x: x[2])
    v.sort(key=lambda x: x[2])
    return h, v


# =====================================================================
#  DIMENSION CREATION HELPER
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
    """Create dimension with optional type override and overlap avoidance."""
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


# =====================================================================
#  MODE 1: COLUMN SIZE
# =====================================================================

def create_dim_column_size(column, view, refs, dim_x, dim_y, offset_mm, dim_type_id):
    created = []
    errs = []
    center = get_column_center(column)
    if not center:
        return created, errs
    col_w, col_d = get_column_bbox_size(column)
    rot = get_column_rotation(column)
    off = offset_mm / 304.8
    cr, sr = math.cos(rot), math.sin(rot)

    if dim_x and 'left' in refs and 'right' in refs:
        ra = ReferenceArray()
        ra.Append(refs['left'])
        ra.Append(refs['right'])
        oy = -(col_d / 2.0 + off)
        p1 = XYZ(center.X - (col_w + off) * cr - oy * sr,
                  center.Y - (col_w + off) * sr + oy * cr, center.Z)
        p2 = XYZ(center.X + (col_w + off) * cr - oy * sr,
                  center.Y + (col_w + off) * sr + oy * cr, center.Z)
        try:
            dim = make_dim(view, Line.CreateBound(p1, p2), ra, dim_type_id, anchor=center)
            if dim:
                created.append(dim.Id)
        except Exception as ex:
            errs.append("SizeX: {}".format(str(ex)))

    if dim_y and 'front' in refs and 'back' in refs:
        ra = ReferenceArray()
        ra.Append(refs['front'])
        ra.Append(refs['back'])
        ox = -(col_w / 2.0 + off)
        p1 = XYZ(center.X + ox * cr - (-(col_d + off)) * sr,
                  center.Y + ox * sr + (-(col_d + off)) * cr, center.Z)
        p2 = XYZ(center.X + ox * cr - (col_d + off) * sr,
                  center.Y + ox * sr + (col_d + off) * cr, center.Z)
        try:
            dim = make_dim(view, Line.CreateBound(p1, p2), ra, dim_type_id, anchor=center)
            if dim:
                created.append(dim.Id)
        except Exception as ex:
            errs.append("SizeY: {}".format(str(ex)))

    return created, errs


# =====================================================================
#  MODE 2: FACE TO GRID
# =====================================================================

def create_dim_face_to_grid(column, view, refs, dim_x, dim_y, offset_mm, dim_type_id):
    """Face to 1 nearest grid."""
    created = []
    errs = []
    center = get_column_center(column)
    if not center:
        return created, errs
    col_w, col_d = get_column_bbox_size(column)
    off = offset_mm / 304.8
    h_grids, v_grids = find_nearest_grids(center, view)

    if dim_x and ('left' in refs or 'right' in refs) and v_grids:
        nearest = v_grids[0]
        gr = get_grid_reference(nearest[0])
        if gr:
            items = [(nearest[1], gr)]
            if 'left' in refs:
                items.append((-col_w / 2.0, refs['left']))
            if 'right' in refs:
                items.append((col_w / 2.0, refs['right']))
            items.sort(key=lambda x: x[0])
            if len(items) >= 2:
                ra = ReferenceArray()
                for _, ref in items:
                    ra.Append(ref)
                y_pos = center.Y - col_d / 2.0 - off
                try:
                    dim = make_dim(view, Line.CreateBound(
                        XYZ(center.X - 15.0, y_pos, center.Z),
                        XYZ(center.X + 15.0, y_pos, center.Z)), ra, dim_type_id, anchor=center)
                    if dim:
                        created.append(dim.Id)
                except Exception as ex:
                    errs.append("FaceGridX: {}".format(str(ex)))

    if dim_y and ('front' in refs or 'back' in refs) and h_grids:
        nearest = h_grids[0]
        gr = get_grid_reference(nearest[0])
        if gr:
            items = [(nearest[1], gr)]
            if 'back' in refs:
                items.append((-col_d / 2.0, refs['back']))
            if 'front' in refs:
                items.append((col_d / 2.0, refs['front']))
            items.sort(key=lambda x: x[0])
            if len(items) >= 2:
                ra = ReferenceArray()
                for _, ref in items:
                    ra.Append(ref)
                x_pos = center.X - col_w / 2.0 - off
                try:
                    dim = make_dim(view, Line.CreateBound(
                        XYZ(x_pos, center.Y - 15.0, center.Z),
                        XYZ(x_pos, center.Y + 15.0, center.Z)), ra, dim_type_id, anchor=center)
                    if dim:
                        created.append(dim.Id)
                except Exception as ex:
                    errs.append("FaceGridY: {}".format(str(ex)))

    return created, errs


# =====================================================================
#  MODE 3: CENTER TO GRID
# =====================================================================

def create_dim_center_to_grid(column, view, refs, dim_x, dim_y, offset_mm, dim_type_id):
    """Center ref to 1 nearest grid."""
    created = []
    errs = []
    center = get_column_center(column)
    if not center:
        return created, errs
    col_w, col_d = get_column_bbox_size(column)
    off = offset_mm / 304.8
    h_grids, v_grids = find_nearest_grids(center, view)

    if dim_x and 'center_lr' in refs and v_grids:
        g, sd, ad = v_grids[0]
        if ad > 0.05:
            gr = get_grid_reference(g)
            if gr:
                ra = ReferenceArray()
                ra.Append(refs['center_lr'])
                ra.Append(gr)
                y_pos = center.Y - col_d / 2.0 - off * 2.0
                try:
                    dim = make_dim(view, Line.CreateBound(
                        XYZ(center.X - 15.0, y_pos, center.Z),
                        XYZ(center.X + 15.0, y_pos, center.Z)), ra, dim_type_id, anchor=center)
                    if dim:
                        created.append(dim.Id)
                except Exception as ex:
                    errs.append("CtrGridX: {}".format(str(ex)))

    if dim_y and 'center_fb' in refs and h_grids:
        g, sd, ad = h_grids[0]
        if ad > 0.05:
            gr = get_grid_reference(g)
            if gr:
                ra = ReferenceArray()
                ra.Append(refs['center_fb'])
                ra.Append(gr)
                x_pos = center.X - col_w / 2.0 - off * 2.0
                try:
                    dim = make_dim(view, Line.CreateBound(
                        XYZ(x_pos, center.Y - 15.0, center.Z),
                        XYZ(x_pos, center.Y + 15.0, center.Z)), ra, dim_type_id, anchor=center)
                    if dim:
                        created.append(dim.Id)
                except Exception as ex:
                    errs.append("CtrGridY: {}".format(str(ex)))

    return created, errs


# =====================================================================
#  WPF DIALOG
# =====================================================================

class AutoDimColumnDialog(Window):
    def __init__(self, count, dim_types):
        self.count = count
        self.dim_types = dim_types  # [(name, ElementId), ...]
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
        self.Title = "Auto Dimension Columns - DQT"
        self.Width = 440; self.SizeToContent = SizeToContent.Height
        self.WindowStartupLocation = WindowStartupLocation.CenterScreen
        self.ResizeMode = ResizeMode.NoResize; self.Background = B(DQT_BG)

        m = StackPanel()

        # Header
        hd = Border(); hd.Background = B(DQT_DARK)
        hd.Padding = Thickness(16, 12, 16, 12)
        hp = StackPanel()
        t = TextBlock(); t.Text = "Auto Dimension Columns"
        t.FontSize = 16; t.FontWeight = FontWeights.Bold
        t.Foreground = B(DQT_PRIMARY); t.FontFamily = FontFamily("Segoe UI")
        hp.Children.Add(t)
        s = TextBlock(); s.Text = "Dang Quoc Truong (DQT)"
        s.FontSize = 10; s.Foreground = B(DQT_ACCENT)
        s.Margin = Thickness(0, 2, 0, 0); hp.Children.Add(s)
        hd.Child = hp; m.Children.Add(hd)

        # Summary
        sb = Border(); sb.Background = B(DQT_WHITE)
        sb.BorderBrush = B(DQT_BORDER); sb.BorderThickness = Thickness(1)
        sb.CornerRadius = CornerRadius(4)
        sb.Margin = Thickness(16, 12, 16, 0)
        sb.Padding = Thickness(12, 8, 12, 8)
        st2 = TextBlock()
        st2.Text = "{} column(s) selected".format(self.count)
        st2.FontSize = 13; st2.FontWeight = FontWeights.SemiBold
        st2.Foreground = B(DQT_TEXT_DARK); sb.Child = st2
        m.Children.Add(sb)

        # Dimension Type Picker
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

        # 1. Column Size
        self.chk_sx = self._cb("Dimension Width (Left - Right)", True)
        self.chk_sy = self._cb("Dimension Depth (Front - Back)", True)
        m.Children.Add(self._cd([self._st("1. Column Size"), self.chk_sx, self.chk_sy]))

        # 2. Face to Grid
        self.chk_fx = self._cb("X-direction (face to vertical grid)", False)
        self.chk_fy = self._cb("Y-direction (face to horizontal grid)", False)
        m.Children.Add(self._cd([self._st("2. Face to Nearest Grid"),
            self.chk_fx, self.chk_fy,
            self._nt("Column face to 1 nearest grid line")]))

        # 3. Center to Grid
        self.chk_cx = self._cb("X-direction (center to vertical grid)", False)
        self.chk_cy = self._cb("Y-direction (center to horizontal grid)", False)
        m.Children.Add(self._cd([self._st("3. Center to Nearest Grid"),
            self.chk_cx, self.chk_cy,
            self._nt("Column center to 1 nearest grid line")]))

        # Offset
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

        # Buttons
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

        # Footer
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
        if _open_help_page("dim_column.html"):
            return
        forms.alert(
            "Auto-dimensions the selected columns in the active plan view.\n\n"
            "Tick one or more dimension modes:\n"
            "1. Column Size - Width (Left-Right) and/or Depth (Front-Back).\n"
            "2. Face to Nearest Grid - X-direction (face to vertical grid) "
            "and/or Y-direction (face to horizontal grid).\n"
            "3. Center to Nearest Grid - same, from the column centerline.\n\n"
            "Set the dimension line offset (mm) and optionally 'Avoid "
            "overlapping existing dimensions / tags / text', then Create "
            "Dimensions.",
            title="Auto Dimension Columns - DQT")

    def _run(self, s, e):
        try:
            off = float(self.txt_off.Text)
        except:
            off = 500.0

        # Get selected dim type
        dim_type_id = None
        sel_item = self.cmb_dim_type.SelectedItem
        if sel_item and sel_item.Tag is not None:
            dim_type_id = sel_item.Tag

        self.result = {
            'sx': self.chk_sx.IsChecked == True,
            'sy': self.chk_sy.IsChecked == True,
            'fx': self.chk_fx.IsChecked == True,
            'fy': self.chk_fy.IsChecked == True,
            'cx': self.chk_cx.IsChecked == True,
            'cy': self.chk_cy.IsChecked == True,
            'avoid': self.chk_avoid.IsChecked == True,
            'off': off,
            'dim_type_id': dim_type_id
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
        forms.alert("Plan views only.", title="Auto Dimension Columns - DQT")
        return

    columns = []
    sel = uidoc.Selection.GetElementIds()
    if sel.Count > 0:
        for eid in sel:
            e = doc.GetElement(eid)
            if isinstance(e, FamilyInstance) and is_column(e):
                    columns.append(e)
    if not columns:
        try:
            picks = uidoc.Selection.PickObjects(
                ObjectType.Element, ColumnSelectionFilter(),
                "Select columns to dimension")
            for p in picks:
                e = doc.GetElement(p.ElementId)
                if e:
                    columns.append(e)
        except:
            return
    if not columns:
        forms.alert("No columns selected.", title="Auto Dimension Columns - DQT")
        return

    # Collect dimension types
    dim_types = get_all_dimension_types()

    dlg = AutoDimColumnDialog(len(columns), dim_types)
    dlg.ShowDialog()
    if dlg.result is None:
        return

    r = dlg.result
    if not any([r['sx'], r['sy'], r['fx'], r['fy'], r['cx'], r['cy']]):
        forms.alert("Select at least one option.", title="Auto Dimension Columns - DQT")
        return

    dt_id = r['dim_type_id']
    total = 0
    failed = []
    errors = []

    global _OCCUPIED
    _OCCUPIED = collect_anno_boxes(view) if r['avoid'] else None

    txn = Transaction(doc, "DQT - Auto Dimension Columns")
    txn.Start()

    try:
        for col in columns:
            cid = _eid_int(col.Id)
            cd = 0
            try:
                refs = get_column_all_refs(col, view)
                if not refs:
                    errors.append("Col {}: no refs found".format(cid))
                    failed.append(cid)
                    continue
                if r['sx'] or r['sy']:
                    dims, dim_errs = create_dim_column_size(
                        col, view, refs, r['sx'], r['sy'], r['off'], dt_id)
                    cd += len(dims)
                    errors.extend(["Col {} {}".format(cid, e) for e in dim_errs])
                if r['fx'] or r['fy']:
                    dims, dim_errs = create_dim_face_to_grid(
                        col, view, refs, r['fx'], r['fy'], r['off'], dt_id)
                    cd += len(dims)
                    errors.extend(["Col {} {}".format(cid, e) for e in dim_errs])
                if r['cx'] or r['cy']:
                    dims, dim_errs = create_dim_center_to_grid(
                        col, view, refs, r['cx'], r['cy'], r['off'], dt_id)
                    cd += len(dims)
                    errors.extend(["Col {} {}".format(cid, e) for e in dim_errs])
                total += cd
                if cd == 0:
                    errors.append("Col {}: refs={}, dims created=0".format(
                        cid, list(refs.keys())))
                    failed.append(cid)
            except Exception as ex:
                errors.append("Col {} error: {}".format(cid, str(ex)))
                failed.append(cid)
        txn.Commit()
    except Exception as ex:
        txn.RollBack()
        forms.alert("Transaction failed: {}".format(str(ex)),
                     title="Auto Dimension Columns - DQT")
        return

    ok = len(columns) - len(failed)
    msg = "Created {} dimension(s) for {} of {} column(s).".format(total, ok, len(columns))
    if failed:
        msg += "\n\n{} failed (ID: {})".format(len(failed),
            ", ".join(str(x) for x in failed[:10]))
    if errors:
        msg += "\n\nDetails:\n" + "\n".join(errors[:5])
    forms.alert(msg, title="Auto Dimension Columns - DQT")


if __name__ == '__main__':
    main()
else:
    main()