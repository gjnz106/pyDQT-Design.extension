# -*- coding: utf-8 -*-
"""Align Viewports

Aligns viewports on multiple sheets to a chosen "Main" sheet, so the same
view lands at the exact same position on every sheet - regardless of what
annotation/content each view happens to show.

How it works: two alignment methods are available.
  - Crop Box Center (default): Viewport.SetBoxCenter() positions a
    viewport by its crop-region outline. That outline is affected by
    whatever is currently visible in the view (tags, dimensions, etc.), so
    every view involved is temporarily hidden (elements only, not the crop
    region) before reading/writing box centers, and restored right after.
    This only lines up the BUILDING/content correctly when every sheet's
    viewport already has a matching crop size/extent - if one sheet's crop
    includes extra context (e.g. site boundary) that another sheet's crop
    doesn't, the crop centers will match but the content inside will not.
  - Grid Intersection: a chosen pair of grids (which share the same
    world X/Y on every level) is used as the alignment reference instead.
    For each viewport, the grid intersection's position is mapped from
    model space to sheet space using the fractional position of the point
    within that view's own crop box, so it works correctly even when the
    two views' crops differ in size - the content (not just the crop
    frame) ends up aligned.

Differences from a typical "align viewports" tool:
  - Works with ANY alignable view type on the sheet (plan, section,
    elevation, 3D, drafting, legend, ...), not only floor/ceiling plans -
    each sheet's viewports are grouped by their underlying View.ViewType and
    matched against the Main sheet's viewports of the same type.
  - When a sheet has more than one viewport of the same view type (with
    "Overlap" enabled), viewports are paired by the underlying View's Name
    first (covers a view reused identically across sheets), then by sheet
    position/reading order for whatever is left (covers views that play the
    same role on every sheet but are named per-floor, e.g. "Plan - 1st
    Storey" vs "Plan - 2nd Storey" - the common case, where name matching
    alone would leave everything unmatched).
  - One transaction per non-Main sheet (plus one to hide Main's views up
    front and one to restore them at the end), instead of several
    transactions per sheet - meaningfully fewer transactions on projects
    with many sheets.
  - A sheet that fails (e.g. a view type mismatch) is skipped with a
    warning; it does not abort the rest of the run. Everything still runs
    inside one TransactionGroup, so the whole operation is a single Undo.
  - Optionally aligns the View Title too (Viewport.LabelOffset), so the
    view name/number text lines up the same way the viewports do - not
    just the crop region.

Copyright (c) 2026 Dang Quoc Truong (DQT)
All rights reserved.
"""

__title__ = "Align\nViewports"
__author__ = "Dang Quoc Truong (DQT)"
__doc__ = "Align viewports on multiple sheets to a chosen Main sheet."

import clr
clr.AddReference('RevitAPI')
clr.AddReference('RevitAPIUI')
clr.AddReference('PresentationCore')
clr.AddReference('PresentationFramework')
clr.AddReference('WindowsBase')
clr.AddReference('System')

from Autodesk.Revit.DB import (
    Transaction, TransactionGroup, FilteredElementCollector,
    BuiltInCategory, BuiltInParameter, ElementId, ElementTransformUtils,
    Grid, ViewSheet, ViewType, XYZ
)

# TemporaryViewMode is only needed to switch the temporary hide/isolate mode
# back off. Import it separately and defensively: pulling it in with the
# block above meant that if a Revit release ever moved or dropped it, the
# ImportError killed the whole module and the button did nothing at all -
# an entirely cosmetic cleanup step taking the tool down with it.
try:
    from Autodesk.Revit.DB import TemporaryViewMode
except ImportError:
    TemporaryViewMode = None

from pyrevit import forms

import System
from System.Windows import (
    Window, WindowStartupLocation, Thickness, HorizontalAlignment,
    VerticalAlignment, TextWrapping, FontWeights, CornerRadius,
    SizeToContent, ResizeMode
)
from System.Windows.Controls import (
    StackPanel, Button, TextBlock, RadioButton, Border, CheckBox,
    ComboBox, ComboBoxItem, Orientation, ScrollViewer
)
from System.Windows.Media import SolidColorBrush, Color, Colors, FontFamily

doc = __revit__.ActiveUIDocument.Document
uidoc = __revit__.ActiveUIDocument

DQT_PRIMARY   = Color.FromRgb(0xF0, 0xCC, 0x88)
DQT_ACCENT    = Color.FromRgb(0xC8, 0x96, 0x50)
DQT_BG        = Color.FromRgb(0xFE, 0xF8, 0xE7)
DQT_DARK      = Color.FromRgb(0x3C, 0x3C, 0x3C)
DQT_WHITE     = Colors.White
DQT_BORDER    = Color.FromRgb(0xDD, 0xDD, 0xDD)
DQT_TEXT_DARK = Color.FromRgb(0x33, 0x33, 0x33)


def B(color):
    return SolidColorBrush(color)


# =====================================================================
#  HELPERS
# =====================================================================

def _eid_int(eid):
    """Get integer value from ElementId - works on Revit 2024 (.IntegerValue)
    and 2025+/2026 (.Value)."""
    try:
        return eid.Value
    except:
        return eid.IntegerValue


def _view_types(*names):
    """A set of ViewType members looked up by name, skipping any this Revit
    build doesn't have.

    These sets are built at module level, so a straight ViewType.Xxx here
    means one renamed or retired enum member raises AttributeError while the
    script is still being imported - the tool then cannot run at all, even
    though the missing member may be one no project ever puts on a sheet.
    Resolving by name degrades to 'that view type is not alignable' instead."""
    found = set()
    for name in names:
        member = getattr(ViewType, name, None)
        if member is not None:
            found.add(member)
    return found


ALIGNABLE_VIEW_TYPES = _view_types(
    "FloorPlan", "CeilingPlan", "EngineeringPlan", "AreaPlan", "Section",
    "Elevation", "Detail", "ThreeD", "DraftingView", "Legend", "Walkthrough",
)

CROPPABLE_VIEW_TYPES = _view_types(
    "FloorPlan", "CeilingPlan", "EngineeringPlan", "AreaPlan", "Section",
    "Elevation", "Detail", "ThreeD", "DraftingView",
)

# Views whose CropBox is a real, world-space-linked box (needed to map a
# model point to a sheet position). Legends/Drafting views aren't tied to
# model space this way, so grid-based alignment can't apply to them.
GRID_ALIGNABLE_VIEW_TYPES = _view_types(
    "FloorPlan", "CeilingPlan", "EngineeringPlan", "AreaPlan", "Section",
    "Elevation", "ThreeD",
)


def get_target_sheets():
    """Sheets pre-selected in the UI if there are >=2, otherwise a picker
    listing every sheet in the document."""
    sheets = []
    try:
        sel_ids = uidoc.Selection.GetElementIds()
        for eid in sel_ids:
            e = doc.GetElement(eid)
            if isinstance(e, ViewSheet):
                sheets.append(e)
    except:
        pass
    if len(sheets) >= 2:
        return sheets

    all_sheets = list(FilteredElementCollector(doc)
                       .OfClass(ViewSheet).WhereElementIsNotElementType())
    if not all_sheets:
        forms.alert("No sheets found in the document.", title=__title__,
                    exitscript=True)

    display = {"{} - {}".format(s.SheetNumber, s.Name): s for s in all_sheets}
    picked = forms.SelectFromList.show(
        sorted(display.keys()), title="Select Sheets to Align (min. 2)",
        multiselect=True, button_name="Select Sheets")
    if not picked:
        return []
    return [display[k] for k in picked]


def get_viewports_by_type(sheet, include_legend):
    """Dict: ViewType -> [Viewport, ...] for the alignable viewports on a sheet."""
    types = ALIGNABLE_VIEW_TYPES if include_legend else (
        ALIGNABLE_VIEW_TYPES - _view_types("Legend"))
    groups = {}
    for vp_id in sheet.GetAllViewports():
        vp = doc.GetElement(vp_id)
        view = doc.GetElement(vp.ViewId)
        if view is None or view.ViewType not in types:
            continue
        groups.setdefault(view.ViewType, []).append(vp)
    return groups


def _sheet_reading_order_key(vp):
    """Sort key for a viewport's CURRENT position on its sheet: top-to-bottom,
    then left-to-right - the standard reading order. Used to pair viewports
    that play 'the same role' on different sheets (e.g. 'the plan on top' /
    'the inset below') when their underlying view Names don't match, which
    is the normal case when each floor's view is named per-floor."""
    try:
        c = vp.GetBoxCenter()
        return (-c.Y, c.X)
    except Exception:
        return (0, 0)


def match_viewports(main_groups, other_groups, overlap, warnings, sheet_number):
    """Pair up (main_viewport, other_viewport) for every shared view type.
    Single-vs-single pairs are matched directly. When either side has more
    than one viewport of a type, pairing requires 'overlap' and is done in
    two passes: first by the underlying View's Name (covers views reused
    identically across sheets, e.g. a shared Key Plan), then - for whatever
    is left - by matching sheet position/reading order (covers views that
    play the same role on every sheet but are named per-floor, which is the
    common case)."""
    pairs = []
    for vt, main_list in main_groups.items():
        other_list = other_groups.get(vt)
        if not other_list:
            continue

        if len(main_list) == 1 and len(other_list) == 1:
            pairs.append((main_list[0], other_list[0]))
            continue

        if not overlap:
            warnings.append(
                "Sheet {}: multiple {} viewports - enable 'Overlap same-type "
                "viewports' to align them".format(sheet_number, vt.ToString()))
            continue

        other_by_name = {}
        for vp in other_list:
            v = doc.GetElement(vp.ViewId)
            other_by_name.setdefault(v.Name, []).append(vp)

        used_other = set()
        unmatched_main = []
        for vp in main_list:
            v = doc.GetElement(vp.ViewId)
            bucket = other_by_name.get(v.Name, [])
            picked = None
            for cand in bucket:
                if _eid_int(cand.Id) not in used_other:
                    picked = cand
                    break
            if picked is not None:
                pairs.append((vp, picked))
                used_other.add(_eid_int(picked.Id))
            else:
                unmatched_main.append(vp)

        leftover_other = [vp for vp in other_list
                          if _eid_int(vp.Id) not in used_other]
        unmatched_main.sort(key=_sheet_reading_order_key)
        leftover_other.sort(key=_sheet_reading_order_key)
        for m_vp, o_vp in zip(unmatched_main, leftover_other):
            pairs.append((m_vp, o_vp))
        if len(unmatched_main) != len(leftover_other):
            warnings.append(
                "Sheet {}: {} {} viewport(s) left unmatched (count differs "
                "from Main)".format(
                    sheet_number, abs(len(unmatched_main) - len(leftover_other)),
                    vt.ToString()))

    return pairs


def copy_crop_scope(main_view, other_view):
    """Best-effort copy of crop/scope box from main_view to other_view.
    Never raises - a failed copy just leaves other_view's crop untouched."""
    try:
        crop_param = other_view.get_Parameter(BuiltInParameter.VIEWER_CROP_REGION)
        if crop_param and not crop_param.IsReadOnly and not crop_param.AsInteger():
            crop_param.Set(1)
    except:
        pass

    main_scope_id = ElementId.InvalidElementId
    try:
        p = main_view.get_Parameter(BuiltInParameter.VIEWER_VOLUME_OF_INTEREST_CROP)
        if p:
            main_scope_id = p.AsElementId()
    except:
        pass

    if main_scope_id != ElementId.InvalidElementId:
        try:
            other_p = other_view.get_Parameter(
                BuiltInParameter.VIEWER_VOLUME_OF_INTEREST_CROP)
            if other_p and not other_p.IsReadOnly:
                other_p.Set(main_scope_id)
        except:
            pass
    else:
        try:
            shapes = main_view.GetCropRegionShapeManager().GetCropShape()
            if shapes and len(shapes) > 0:
                other_view.GetCropRegionShapeManager().SetCropShape(shapes[0])
        except:
            pass


def grid_intersection_point(grid1, grid2):
    """World-space intersection of two Grid centerlines (2D, X/Y only - Z is
    taken from grid1 since grids are vertical). Returns XYZ, or None if the
    grids are parallel/don't intersect."""
    try:
        c1 = grid1.Curve
        c2 = grid2.Curve
        p1 = c1.GetEndPoint(0)
        d1 = c1.GetEndPoint(1) - p1
        p2 = c2.GetEndPoint(0)
        d2 = c2.GetEndPoint(1) - p2
    except Exception:
        return None

    a11, a12 = d1.X, -d2.X
    a21, a22 = d1.Y, -d2.Y
    b1, b2 = p2.X - p1.X, p2.Y - p1.Y
    det = a11 * a22 - a12 * a21
    if abs(det) < 1e-9:
        return None
    t = (b1 * a22 - a12 * b2) / det
    return XYZ(p1.X + t * d1.X, p1.Y + t * d1.Y, p1.Z)


def world_point_to_sheet(view, viewport, world_pt):
    """Map a world-space point to sheet-space coordinates through viewport.

    Scale-based, NOT fractional-outline-based: the earlier version mapped
    via Viewport.GetBoxOutline(), but that outline INCLUDES the view title
    that hangs below the crop region, so the box it spans is bigger than
    (and vertically offset from) the actual crop rectangle - which threw
    the mapped point off. Instead:
      - the crop region's center in model space maps to GetBoxCenter()
        (the crop rectangle's center on the sheet, title excluded);
      - the point's in-plane offset from that center is measured along the
        view's own axes and divided by the view scale to get the sheet-space
        offset (model_ft / scale = sheet_ft).
    Assumes the viewport is not rotated on the sheet (the common case).
    Returns XYZ (sheet space, Z=0), or None if it can't be computed."""
    try:
        crop = view.CropBox
        scale = view.Scale
        if crop is None or not scale or scale <= 0:
            return None
        tf = crop.Transform
        center_local = XYZ((crop.Min.X + crop.Max.X) / 2.0,
                          (crop.Min.Y + crop.Max.Y) / 2.0,
                          (crop.Min.Z + crop.Max.Z) / 2.0)
        center_world = tf.OfPoint(center_local)
        v = world_pt - center_world
        dx = v.DotProduct(tf.BasisX)
        dy = v.DotProduct(tf.BasisY)
        sc = viewport.GetBoxCenter()
        return XYZ(sc.X + dx / scale, sc.Y + dy / scale, 0)
    except Exception:
        return None


def align_viewport(main_view, main_vp, other_view, other_vp, method, grid_pt):
    """Move other_vp so it aligns with main_vp. 'grid' method aligns by a
    shared grid intersection (works even when crop sizes differ); anything
    that can't use it (wrong view type, point not resolvable) falls back to
    crop-box-center alignment. Returns (ok, reason-if-failed)."""
    if method == 'grid' and grid_pt is not None \
            and main_view.ViewType in GRID_ALIGNABLE_VIEW_TYPES \
            and other_view.ViewType in GRID_ALIGNABLE_VIEW_TYPES:
        main_pt = world_point_to_sheet(main_view, main_vp, grid_pt)
        other_pt = world_point_to_sheet(other_view, other_vp, grid_pt)
        if main_pt is not None and other_pt is not None:
            try:
                delta = main_pt - other_pt
                ElementTransformUtils.MoveElement(doc, other_vp.Id, delta)
                return True, None
            except Exception as ex:
                return False, str(ex)
        # Grid point not resolvable in one of the views (e.g. it falls
        # outside that view's crop) - fall back to crop-box-center below.

    try:
        other_vp.SetBoxCenter(main_vp.GetBoxCenter())
        return True, None
    except Exception as ex:
        return False, str(ex)


def _crop_bottom_left_on_sheet(view, viewport):
    """Bottom-left corner of the crop RECTANGLE on the sheet (title excluded),
    computed from GetBoxCenter() plus the crop's half-size divided by the view
    scale. Returns XYZ, or None. Uses the crop rectangle - not GetBoxOutline -
    so the view title hanging below doesn't distort it."""
    try:
        crop = view.CropBox
        scale = view.Scale
        if crop is None or not scale or scale <= 0:
            return None
        hw = (crop.Max.X - crop.Min.X) / 2.0 / scale
        hh = (crop.Max.Y - crop.Min.Y) / 2.0 / scale
        c = viewport.GetBoxCenter()
        return XYZ(c.X - hw, c.Y - hh, 0)
    except Exception:
        return None


def copy_label_offset(main_view, main_vp, other_view, other_vp):
    """Best-effort alignment of the View Title's ABSOLUTE sheet position to
    match main_vp's, not a raw copy of the offset value.

    LabelOffset is relative to each viewport's OWN crop rectangle, so a
    straight copy places the title at the wrong spot whenever the two
    viewports' crops differ in size - the same class of problem as
    crop-box-center viewport alignment. This compensates for it: other_vp's
    new offset = main_vp's offset, shifted by the difference between the two
    viewports' crop-rectangle reference corners (computed from the crop
    rectangle on the sheet, title excluded), so the title lands at the same
    sheet position Main's title is at. Falls back to a raw offset copy if the
    crop rectangle can't be computed. Never raises; returns (ok, reason)."""
    try:
        main_offset = main_vp.LabelOffset
        main_anchor = _crop_bottom_left_on_sheet(main_view, main_vp)
        other_anchor = _crop_bottom_left_on_sheet(other_view, other_vp)
        if main_anchor is not None and other_anchor is not None:
            new_offset = XYZ(
                main_offset.X + (main_anchor.X - other_anchor.X),
                main_offset.Y + (main_anchor.Y - other_anchor.Y),
                0)
        else:
            new_offset = main_offset
        other_vp.LabelOffset = new_offset
        return True, None
    except Exception as ex:
        return False, str(ex)


def get_titleblock(sheet):
    """The sheet's single title block, or None if there isn't exactly one."""
    tbs = list(FilteredElementCollector(doc, sheet.Id)
               .OfCategory(BuiltInCategory.OST_TitleBlocks)
               .WhereElementIsNotElementType())
    return tbs[0] if len(tbs) == 1 else None


def handle_titleblock(main_tb, other_tb, snap_origin, match_type):
    if main_tb is None or other_tb is None:
        return
    if match_type:
        try:
            other_tb.Symbol = main_tb.Symbol
        except Exception:
            pass
    if snap_origin:
        zero = XYZ(0, 0, 0)
        try:
            if not main_tb.Location.Point.IsAlmostEqualTo(zero):
                main_tb.Location.Point = zero
        except:
            pass
        try:
            if not other_tb.Location.Point.IsAlmostEqualTo(zero):
                other_tb.Location.Point = zero
        except:
            pass


def can_temporarily_hide():
    """True only when the temporary hide can also be UNDONE.

    Hiding is worth doing only if the matching restore is available: leaving
    every view in temporary-hide with no way back is far worse than the
    slightly less exact crop centre the hide was there to improve."""
    return TemporaryViewMode is not None and hasattr(
        TemporaryViewMode, "TemporaryHideIsolate")


def hide_view_elements(view):
    if not can_temporarily_hide():
        return
    elems = FilteredElementCollector(doc, view.Id) \
        .WhereElementIsNotElementType().ToElementIds()
    view.HideElementsTemporary(elems)


def unhide_view(view):
    if not can_temporarily_hide():
        return
    try:
        view.DisableTemporaryViewMode(TemporaryViewMode.TemporaryHideIsolate)
    except:
        pass


# =====================================================================
#  DIALOG
# =====================================================================

class AlignViewportsDialog(Window):
    def __init__(self, sheet_keys, grid_items):
        self.sheet_keys = sheet_keys
        self.grid_items = grid_items  # [(name, ElementId), ...]
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

    def _rb(self, t, checked=False, group="MainSheet"):
        r = RadioButton(); r.Content = t; r.GroupName = group
        r.IsChecked = System.Nullable[System.Boolean](checked)
        r.Margin = Thickness(4, 3, 0, 3); r.FontSize = 11.5
        r.Foreground = B(DQT_TEXT_DARK); return r

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
        self.Title = "Align Viewports - DQT"
        self.Width = 440; self.SizeToContent = SizeToContent.Height
        self.WindowStartupLocation = WindowStartupLocation.CenterScreen
        self.ResizeMode = ResizeMode.NoResize; self.Background = B(DQT_BG)

        m = StackPanel()

        # Header
        hd = Border(); hd.Background = B(DQT_DARK)
        hd.Padding = Thickness(16, 12, 16, 12)
        hp = StackPanel()
        t = TextBlock(); t.Text = "Align Viewports"
        t.FontSize = 16; t.FontWeight = FontWeights.Bold
        t.Foreground = B(DQT_PRIMARY); t.FontFamily = FontFamily("Segoe UI")
        hp.Children.Add(t)
        s = TextBlock(); s.Text = "Dang Quoc Truong (DQT)"
        s.FontSize = 10; s.Foreground = B(DQT_ACCENT)
        s.Margin = Thickness(0, 2, 0, 0); hp.Children.Add(s)
        hd.Child = hp; m.Children.Add(hd)

        # Main sheet picker
        self.radios = []
        rp = StackPanel()
        for i, key in enumerate(self.sheet_keys):
            rb = self._rb(key, checked=(i == 0))
            self.radios.append(rb)
            rp.Children.Add(rb)
        sv = ScrollViewer()
        sv.MaxHeight = 160
        sv.Content = rp
        m.Children.Add(self._cd([
            self._st("Main Sheet ({} selected)".format(len(self.sheet_keys))),
            sv]))

        # Alignment method
        self.rb_method_crop = self._rb(
            "Crop Box Center (default)", checked=True, group="AlignMethod")
        self.rb_method_grid = self._rb(
            "Grid Intersection", checked=False, group="AlignMethod")
        self.rb_method_crop.Checked += self._method_changed
        self.rb_method_grid.Checked += self._method_changed

        self.cmb_grid1 = ComboBox(); self.cmb_grid1.Width = 130
        self.cmb_grid1.FontSize = 11.5; self.cmb_grid1.Height = 26
        self.cmb_grid2 = ComboBox(); self.cmb_grid2.Width = 130
        self.cmb_grid2.FontSize = 11.5; self.cmb_grid2.Height = 26
        for combo in (self.cmb_grid1, self.cmb_grid2):
            for name, eid in self.grid_items:
                item = ComboBoxItem(); item.Content = name; item.Tag = eid
                combo.Items.Add(item)
            combo.IsEnabled = False
        if len(self.grid_items) >= 1:
            self.cmb_grid1.SelectedIndex = 0
        if len(self.grid_items) >= 2:
            self.cmb_grid2.SelectedIndex = 1

        if not self.grid_items:
            self.rb_method_grid.IsEnabled = False

        g1row = StackPanel(); g1row.Orientation = Orientation.Horizontal
        g1row.Margin = Thickness(20, 2, 0, 0)
        g1lbl = TextBlock(); g1lbl.Text = "Grid 1: "; g1lbl.Width = 45
        g1lbl.FontSize = 11.5; g1lbl.Foreground = B(DQT_TEXT_DARK)
        g1lbl.VerticalAlignment = VerticalAlignment.Center
        g1row.Children.Add(g1lbl); g1row.Children.Add(self.cmb_grid1)

        g2row = StackPanel(); g2row.Orientation = Orientation.Horizontal
        g2row.Margin = Thickness(20, 4, 0, 4)
        g2lbl = TextBlock(); g2lbl.Text = "Grid 2: "; g2lbl.Width = 45
        g2lbl.FontSize = 11.5; g2lbl.Foreground = B(DQT_TEXT_DARK)
        g2lbl.VerticalAlignment = VerticalAlignment.Center
        g2row.Children.Add(g2lbl); g2row.Children.Add(self.cmb_grid2)

        m.Children.Add(self._cd([
            self._st("Alignment Method"),
            self.rb_method_crop, self.rb_method_grid,
            self._nt("Use Grid Intersection when sheets' crops have "
                     "different size/extent (e.g. one view shows extra "
                     "site context) - Crop Box Center alone will line up "
                     "the crop frames but not the building inside them."),
            g1row, g2row]))

        # Options
        self.chk_overlap = self._cb(
            "Overlap same-type viewports (match by name, then position)", False)
        self.chk_crop = self._cb("Apply same Crop / Scope Box", False)
        self.chk_title = self._cb("Align View Titles (label position)", False)
        self.chk_legend = self._cb("Include Legends", False)
        self.chk_tb_type = self._cb("Match TitleBlock Type", False)
        self.chk_tb_zero = self._cb("Snap TitleBlocks to origin (0,0,0)", True)
        m.Children.Add(self._cd([
            self._st("Options"),
            self.chk_overlap,
            self._nt("Needed when a sheet has more than one viewport of the "
                     "same view type."),
            self.chk_crop, self.chk_title,
            self._nt("Works best together with 'Apply same Crop / Scope Box' "
                     "- Revit rejects a title position that lands outside a "
                     "differently-sized crop box."),
            self.chk_legend, self.chk_tb_type, self.chk_tb_zero]))

        # Buttons
        bp = StackPanel(); bp.Orientation = Orientation.Horizontal
        bp.HorizontalAlignment = HorizontalAlignment.Right
        bp.Margin = Thickness(16, 14, 16, 14)
        bc = Button(); bc.Content = "Cancel"; bc.Width = 90; bc.Height = 32
        bc.FontSize = 12; bc.Margin = Thickness(0, 0, 8, 0)
        bc.Background = B(DQT_WHITE); bc.Foreground = B(DQT_TEXT_DARK)
        bc.Click += self._cancel; bp.Children.Add(bc)
        br = Button(); br.Content = "Align Viewports"
        br.Width = 140; br.Height = 32; br.FontSize = 12
        br.FontWeight = FontWeights.SemiBold
        br.Background = B(DQT_ACCENT); br.Foreground = B(DQT_WHITE)
        br.Click += self._run; bp.Children.Add(br)
        m.Children.Add(bp)

        # Footer
        f = Border(); f.Background = B(DQT_DARK)
        f.Padding = Thickness(16, 6, 16, 6)
        ft = TextBlock()
        ft.Text = "Copyright (c) 2026 Dang Quoc Truong (DQT)"
        ft.FontSize = 9; ft.Foreground = B(DQT_ACCENT)
        ft.HorizontalAlignment = HorizontalAlignment.Center
        f.Child = ft; m.Children.Add(f)
        self.Content = m

    def _cancel(self, s, e):
        self.result = None; self.Close()

    def _method_changed(self, s, e):
        use_grid = self.rb_method_grid.IsChecked == True
        self.cmb_grid1.IsEnabled = use_grid
        self.cmb_grid2.IsEnabled = use_grid

    def _run(self, s, e):
        main_key = None
        for rb in self.radios:
            if rb.IsChecked:
                main_key = str(rb.Content)
                break
        if main_key is None:
            forms.alert("Select a Main sheet.", title=__title__)
            return

        align_method = 'grid' if self.rb_method_grid.IsChecked == True else 'crop'
        grid1_id = None
        grid2_id = None
        if align_method == 'grid':
            i1 = self.cmb_grid1.SelectedItem
            i2 = self.cmb_grid2.SelectedItem
            if i1 is None or i2 is None:
                forms.alert("Select two grids for Grid Intersection alignment.",
                            title=__title__)
                return
            grid1_id, grid2_id = i1.Tag, i2.Tag
            if _eid_int(grid1_id) == _eid_int(grid2_id):
                forms.alert("Grid 1 and Grid 2 must be different.", title=__title__)
                return

        self.result = {
            'main': main_key,
            'align_method': align_method,
            'grid1_id': grid1_id,
            'grid2_id': grid2_id,
            'overlap': self.chk_overlap.IsChecked == True,
            'crop': self.chk_crop.IsChecked == True,
            'title': self.chk_title.IsChecked == True,
            'legend': self.chk_legend.IsChecked == True,
            'tb_type': self.chk_tb_type.IsChecked == True,
            'tb_zero': self.chk_tb_zero.IsChecked == True,
        }
        self.Close()


# =====================================================================
#  MAIN
# =====================================================================

def main():
    sheets = get_target_sheets()
    if not sheets:
        return
    if len(sheets) < 2:
        forms.alert("Select at least 2 sheets.", title=__title__, exitscript=True)

    sheet_map = {"{} - {}".format(s.SheetNumber, s.Name): s for s in sheets}

    all_grids = [g for g in FilteredElementCollector(doc)
                .OfClass(Grid).WhereElementIsNotElementType() if g.Name]
    # Multiple independent grid systems (e.g. separate blocks/towers) can
    # legally reuse the same names ("A", "1", ...) - disambiguate with the
    # ElementId whenever a name isn't unique, so the picker never shows
    # indistinguishable duplicate entries.
    name_counts = {}
    for g in all_grids:
        name_counts[g.Name] = name_counts.get(g.Name, 0) + 1
    grid_items = sorted(
        [(g.Name if name_counts[g.Name] == 1
          else "{} (ID {})".format(g.Name, _eid_int(g.Id)), g.Id)
         for g in all_grids],
        key=lambda x: x[0])

    dlg = AlignViewportsDialog(sorted(sheet_map.keys()), grid_items)
    dlg.ShowDialog()
    if dlg.result is None:
        return
    opts = dlg.result

    grid_pt = None
    if opts['align_method'] == 'grid':
        g1 = doc.GetElement(opts['grid1_id'])
        g2 = doc.GetElement(opts['grid2_id'])
        grid_pt = grid_intersection_point(g1, g2) if (g1 and g2) else None
        if grid_pt is None:
            forms.alert(
                "Could not compute an intersection for the selected grids "
                "(they may be parallel). Pick two grids that actually "
                "cross.", title=__title__, exitscript=True)

    main_sheet = sheet_map[opts['main']]
    other_sheets = [s for k, s in sheet_map.items() if k != opts['main']]

    main_groups = get_viewports_by_type(main_sheet, opts['legend'])
    if not main_groups:
        forms.alert("Main sheet has no alignable viewports.", title=__title__,
                    exitscript=True)
    main_tb = get_titleblock(main_sheet)

    warnings = []
    aligned_count = 0
    titles_aligned = 0
    processed_sheets = 0
    error_sheets = []

    tg = TransactionGroup(doc, __title__)
    tg.Start()
    try:
        main_view_ids = set()
        for vps in main_groups.values():
            for vp in vps:
                main_view_ids.add(vp.ViewId)

        t0 = Transaction(doc, "DQT - Prepare Main Sheet Views")
        t0.Start()
        for vid in main_view_ids:
            try:
                hide_view_elements(doc.GetElement(vid))
            except Exception as ex:
                warnings.append("Could not hide elements in a Main view: {}".format(ex))
        t0.Commit()

        for other_sheet in other_sheets:
            other_groups = get_viewports_by_type(other_sheet, opts['legend'])
            pairs = match_viewports(main_groups, other_groups, opts['overlap'],
                                    warnings, other_sheet.SheetNumber)

            if not pairs:
                warnings.append("Sheet {}: no matching viewports to align"
                                .format(other_sheet.SheetNumber))
                continue

            t = Transaction(doc, "DQT - Align Viewports - {}".format(
                other_sheet.SheetNumber))
            t.Start()
            try:
                other_view_ids = set(vp.ViewId for _, vp in pairs)
                for vid in other_view_ids:
                    hide_view_elements(doc.GetElement(vid))

                sheet_ok = 0
                for main_vp, other_vp in pairs:
                    main_view = doc.GetElement(main_vp.ViewId)
                    other_view = doc.GetElement(other_vp.ViewId)

                    if opts['crop'] and main_view.ViewType in CROPPABLE_VIEW_TYPES:
                        copy_crop_scope(main_view, other_view)

                    ok, reason = align_viewport(
                        main_view, main_vp, other_view, other_vp,
                        opts['align_method'], grid_pt)
                    if ok:
                        sheet_ok += 1
                    else:
                        warnings.append("Sheet {}: could not align '{}' - {}"
                                        .format(other_sheet.SheetNumber,
                                                other_view.Name, reason))
                        continue

                    if opts['title']:
                        ok, reason = copy_label_offset(
                            main_view, main_vp, other_view, other_vp)
                        if ok:
                            titles_aligned += 1
                        else:
                            warnings.append(
                                "Sheet {}: could not align title of '{}' "
                                "(often fixed by also enabling 'Apply same "
                                "Crop / Scope Box') - {}".format(
                                    other_sheet.SheetNumber, other_view.Name,
                                    reason))

                for vid in other_view_ids:
                    unhide_view(doc.GetElement(vid))

                if opts['tb_type'] or opts['tb_zero']:
                    other_tb = get_titleblock(other_sheet)
                    handle_titleblock(main_tb, other_tb, opts['tb_zero'],
                                      opts['tb_type'])

                t.Commit()
                aligned_count += sheet_ok
                processed_sheets += 1
            except Exception as ex:
                t.RollBack()
                error_sheets.append((other_sheet.SheetNumber, str(ex)))

        t2 = Transaction(doc, "DQT - Restore Main Sheet Views")
        t2.Start()
        for vid in main_view_ids:
            unhide_view(doc.GetElement(vid))
        t2.Commit()

        tg.Assimilate()

    except Exception:
        tg.RollBack()
        import traceback
        print(traceback.format_exc())
        forms.alert("Align Viewports failed - all changes rolled back.\n\n{}"
                    .format(traceback.format_exc()), title=__title__)
        return

    print("\n" + "=" * 60)
    print("ALIGN VIEWPORTS - Main: {}".format(opts['main']))
    print("=" * 60)
    print("Sheets processed: {}/{}".format(processed_sheets, len(other_sheets)))
    print("Viewports aligned: {}".format(aligned_count))
    if opts['title']:
        print("View titles aligned: {}".format(titles_aligned))
    if error_sheets:
        print("Sheets with errors: {}".format(len(error_sheets)))
        for num, err in error_sheets:
            print("  {} : {}".format(num, err))
    if warnings:
        print("Warnings: {}".format(len(warnings)))
        for w in warnings:
            print("  {}".format(w))
    print("=" * 60)

    summary = (
        "Align Viewports Complete!\n\n"
        "Main sheet: {}\n"
        "Sheets processed: {}/{}\n"
        "Viewports aligned: {}"
    ).format(opts['main'], processed_sheets, len(other_sheets), aligned_count)
    if opts['title']:
        summary += "\nView titles aligned: {}".format(titles_aligned)
    if error_sheets:
        summary += "\n\n{} sheet(s) failed - see the pyRevit output for details."\
            .format(len(error_sheets))
    if warnings:
        summary += "\n\n{} warning(s) - see the pyRevit output for details."\
            .format(len(warnings))

    forms.alert(summary, title=__title__)


if __name__ == "__main__":
    main()
