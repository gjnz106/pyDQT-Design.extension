# -*- coding: utf-8 -*-
"""
Rotate Tag to Grid v1.0
Rotate the selected tag(s) so they follow a chosen grid's direction.

Workflow:
  1. Select tag(s) to rotate (Enter / right-click to finish)
  2. Pick which grid's direction to rotate to, from a dropdown
  3. Click Rotate

Only IndependentTag elements are supported (door/wall/MEP/keyed/multi-
category tags, etc.) - Room and Area tags use a different API surface and
are out of scope here. Only straight grids are listed, since a curved
grid has no single direction to rotate a tag to.

A tag must switch to TagOrientation.AnyModelDirection ("Rotate with
component" in the UI) before an arbitrary RotationAngle can be set - this
tool does that automatically for whichever tags need it.

Copyright (c) 2026 Dang Quoc Truong (DQT)
All rights reserved.
"""

__title__ = "Rotate Tag\nto Grid"
__author__ = "Dang Quoc Truong (DQT)"
__doc__ = "Rotate selected tag(s) to match a chosen grid's direction."

import clr
clr.AddReference('RevitAPI')
clr.AddReference('RevitAPIUI')
clr.AddReference('PresentationCore')
clr.AddReference('PresentationFramework')
clr.AddReference('WindowsBase')
clr.AddReference('System')

import math
from Autodesk.Revit.DB import *
from Autodesk.Revit.UI import *
from Autodesk.Revit.UI.Selection import *
import System
from System.Windows import (
    Window, WindowStartupLocation, Thickness,
    HorizontalAlignment, TextWrapping,
    CornerRadius
)
from System.Windows.Controls import (
    StackPanel, Button, TextBlock, ComboBox, ComboBoxItem, CheckBox, Border
)
from System.Windows.Media import SolidColorBrush, Color, Colors, FontFamily

# ─── Revit Context ──────────────────────────────────────────
doc = __revit__.ActiveUIDocument.Document
uidoc = __revit__.ActiveUIDocument
active_view = doc.ActiveView

# ─── DQT Color Palette ─────────────────────────────────────
DQT_PRIMARY   = Color.FromRgb(0xF0, 0xCC, 0x88)   # #F0CC88 Gold
DQT_ACCENT    = Color.FromRgb(0xC8, 0x96, 0x50)   # #C89650 Dark Gold
DQT_BG        = Color.FromRgb(0xFE, 0xF8, 0xE7)   # #FEF8E7 Cream
DQT_DARK      = Color.FromRgb(0x3C, 0x3C, 0x3C)   # #3C3C3C Dark
DQT_WHITE     = Colors.White
DQT_BORDER    = Color.FromRgb(0xDD, 0xDD, 0xDD)   # #DDDDDD
DQT_TEXT_DARK = Color.FromRgb(0x33, 0x33, 0x33)   # #333333


def B(color):
    return SolidColorBrush(color)


# ─── Selection Filter ──────────────────────────────────────
class TagSelectionFilter(ISelectionFilter):
    def AllowElement(self, elem):
        return isinstance(elem, IndependentTag)
    def AllowReference(self, reference, position):
        return False


# ─── Core Logic ─────────────────────────────────────────────
def _eid_int(eid):
    """Get integer value from ElementId - compatible with Revit 2024-2027."""
    try:
        return eid.Value
    except AttributeError:
        return eid.IntegerValue


def natural_sort_key(name):
    """Sort grid names the way a human reads them: purely-numeric names
    ("2", "10") sort in numeric order ahead of everything else, which then
    sorts alphabetically - so "10" doesn't land between "1" and "2"."""
    name = name or ""
    try:
        return (0, float(name), name.lower())
    except ValueError:
        return (1, 0.0, name.lower())


def _straight_only(grids):
    """Keep grids whose centerline is a straight Line - a curved (arc) grid
    has no single direction to hand a tag."""
    items = []
    for g in grids:
        try:
            curve = g.Curve
        except:
            continue
        if not isinstance(curve, Line):
            continue
        items.append(g)
    return items


def get_straight_grids(doc, view):
    """Straight grids VISIBLE IN `view`, from this model only, sorted by name.

    Collecting through the view rather than the whole document matters: a
    real project usually carries extra grids the drawing never shows -
    other design options, other building blocks, stray copies pasted in
    from a consultant's model - and those routinely reuse names. A
    document-wide list therefore shows the same name many times over, and
    picking "D" out of it can hand back a different "D" pointing another
    way, which is exactly how a tag ends up rotated but not parallel to
    the grid that was chosen.

    Linked grids are never included either way: they live in the link's
    own document, which this collector does not reach.
    """
    grids = []
    try:
        grids = _straight_only(
            FilteredElementCollector(doc, view.Id).OfClass(Grid))
    except:
        grids = []
    if not grids:
        # This view draws no grids at all - fall back to the whole model so
        # the tool still works instead of showing an empty dropdown.
        try:
            grids = _straight_only(FilteredElementCollector(doc).OfClass(Grid))
        except:
            grids = []
    return sorted(grids, key=lambda g: natural_sort_key(g.Name))


def direction_to_angle(dx, dy):
    """Angle (radians, CCW from +X) of a 2D direction vector."""
    return math.atan2(dy, dx)


def grid_view_angle(grid, view):
    """Angle (radians) of the grid's own line as it appears in `view` -
    projected onto the view's own X/Y axes rather than raw model XYZ, so
    this is correct even in a rotated (e.g. True North) plan, a section,
    or an elevation. Falls back to raw model X/Y if the view exposes no
    RightDirection/UpDirection (defensive; shouldn't normally happen for
    the plan/section/elevation/3D views a tag can live in)."""
    curve = grid.Curve
    p0 = curve.GetEndPoint(0)
    p1 = curve.GetEndPoint(1)
    direction = p1 - p0
    try:
        dx = direction.DotProduct(view.RightDirection)
        dy = direction.DotProduct(view.UpDirection)
    except:
        dx, dy = direction.X, direction.Y
    return direction_to_angle(dx, dy)


def grid_entries(doc, view):
    """[(grid, angle, label)] for the dropdown, one row per distinct
    direction a name can mean.

    Every row is labelled with the angle it will apply, so a name that
    genuinely appears twice at different angles stays distinguishable
    instead of looking like a duplicate. Rows that agree on both name and
    angle are collapsed - they would rotate a tag identically, so showing
    them twice is only noise."""
    entries = []
    seen = set()
    for g in get_straight_grids(doc, view):
        try:
            angle = grid_view_angle(g, view)
        except:
            continue
        degrees = math.degrees(angle)
        key = (g.Name, round(degrees, 1))
        if key in seen:
            continue
        seen.add(key)
        label = u"{}   ({:.1f}°)".format(g.Name, degrees)
        entries.append((g, angle, label))
    return entries


def normalize_upright(angle):
    """Fold an angle (radians) into (-pi/2, pi/2] so tag text reads
    left-to-right instead of upside-down. A grid's direction is only known
    up to a 180-degree flip (which end is the curve's "start" is arbitrary
    and carries no real-world meaning), so this is safe to apply."""
    while angle <= -math.pi / 2:
        angle += math.pi
    while angle > math.pi / 2:
        angle -= math.pi
    return angle


def to_tag_rotation_angle(math_angle):
    """Convert a standard math angle (radians, counter-clockwise-positive
    from the view's right direction - what grid_view_angle() returns) into
    the value IndependentTag.RotationAngle expects.

    Both use the same convention, so this is a pass-through. It stays a
    named function because it is the one place to adjust if that ever
    proves wrong: an earlier build negated here, on the theory that the
    property ran clockwise, after a tag came out not parallel to its grid.
    That was a misdiagnosis - the tag was being rotated to a DIFFERENT,
    same-named grid pulled from elsewhere in the model (see
    get_straight_grids), so the angle being applied was never the chosen
    grid's to begin with. The negation is gone."""
    return math_angle


def rotate_tag_to_angle(doc, tag, angle):
    """Rotate one IndependentTag so it reads at `angle` (radians,
    CCW-positive from +X - ordinary math convention), switching it to
    free-rotation orientation first if needed. Returns (ok, error)."""
    try:
        if tag.TagOrientation != TagOrientation.AnyModelDirection:
            tag.TagOrientation = TagOrientation.AnyModelDirection
            doc.Regenerate()
        tag.RotationAngle = to_tag_rotation_angle(angle)
        return True, None
    except Exception as ex:
        return False, str(ex)


# ─── WPF UI ─────────────────────────────────────────────────
class RotateTagWindow(Window):
    def __init__(self, entries, tag_count):
        self.Title = "Rotate Tag to Grid"
        self.Width = 400
        self.Height = 350
        self.WindowStartupLocation = WindowStartupLocation.CenterScreen
        self.ResizeMode = System.Windows.ResizeMode.NoResize
        self.Background = B(DQT_BG)
        self.result = None
        self.entries = entries
        self.tag_count = tag_count
        self._build_ui()

    # ── UI Builders ──
    def _text(self, text, size=12, bold=False, color=None):
        tb = TextBlock()
        tb.Text = text
        tb.FontSize = size
        tb.FontFamily = FontFamily("Segoe UI")
        tb.Foreground = B(color or DQT_TEXT_DARK)
        if bold:
            tb.FontWeight = System.Windows.FontWeights.Bold
        tb.TextWrapping = TextWrapping.Wrap
        return tb

    def _button(self, text, handler, primary=False):
        btn = Button()
        btn.Content = text
        btn.Height = 34
        btn.FontSize = 12
        btn.FontFamily = FontFamily("Segoe UI")
        btn.FontWeight = System.Windows.FontWeights.SemiBold
        btn.Margin = Thickness(0, 3, 0, 3)
        btn.Cursor = System.Windows.Input.Cursors.Hand
        btn.BorderThickness = Thickness(1)
        if primary:
            btn.Background = B(DQT_PRIMARY)
            btn.Foreground = B(DQT_TEXT_DARK)
            btn.BorderBrush = B(DQT_ACCENT)
        else:
            btn.Background = B(DQT_WHITE)
            btn.Foreground = B(DQT_TEXT_DARK)
            btn.BorderBrush = B(DQT_BORDER)
        btn.Click += handler
        return btn

    def _build_ui(self):
        root = StackPanel()

        # Header
        header = Border()
        header.Background = B(DQT_PRIMARY)
        header.CornerRadius = CornerRadius(0, 0, 6, 6)
        header.Padding = Thickness(16, 10, 16, 10)
        header.Margin = Thickness(0, 0, 0, 8)
        head_panel = StackPanel()
        head_panel.Children.Add(self._text("ROTATE TAG TO GRID", 17, bold=True))
        head_panel.Children.Add(self._text(
            "{} tag(s) selected".format(self.tag_count), 10, color=DQT_DARK))
        header.Child = head_panel
        root.Children.Add(header)

        # Body
        body = StackPanel()
        body.Margin = Thickness(14, 0, 14, 0)

        body.Children.Add(self._text("Rotate to the direction of grid:", 12,
                                      bold=True, color=DQT_ACCENT))
        self.combo = ComboBox()
        self.combo.Margin = Thickness(0, 4, 0, 10)
        self.combo.Height = 28
        for _grid, _angle, label in self.entries:
            item = ComboBoxItem()
            item.Content = label
            self.combo.Items.Add(item)
        if self.combo.Items.Count > 0:
            self.combo.SelectedIndex = 0
        body.Children.Add(self.combo)

        self.chk_upright = CheckBox()
        self.chk_upright.Content = "Keep tag text upright (avoid upside-down)"
        self.chk_upright.IsChecked = True
        self.chk_upright.Foreground = B(DQT_TEXT_DARK)
        self.chk_upright.FontSize = 11.5
        self.chk_upright.Margin = Thickness(0, 0, 0, 10)
        body.Children.Add(self.chk_upright)

        hint_border = Border()
        hint_border.Background = B(DQT_WHITE)
        hint_border.BorderBrush = B(DQT_PRIMARY)
        hint_border.BorderThickness = Thickness(1)
        hint_border.CornerRadius = CornerRadius(4)
        hint_border.Padding = Thickness(10, 8, 10, 8)
        hint_border.Margin = Thickness(0, 0, 0, 10)
        hint_border.Child = self._text(
            "Only straight grids drawn in the current view are listed - "
            "each with the angle it will apply. Tags switch to 'Rotate "
            "with component' orientation to allow free rotation.",
            10.5, color=DQT_DARK)
        body.Children.Add(hint_border)

        btn_panel = StackPanel()
        btn_panel.Children.Add(self._button("Rotate", self._on_run, primary=True))
        btn_panel.Children.Add(self._button("Cancel", self._on_cancel))
        body.Children.Add(btn_panel)

        root.Children.Add(body)

        # Footer
        footer = Border()
        footer.Background = B(DQT_PRIMARY)
        footer.CornerRadius = CornerRadius(6, 6, 0, 0)
        footer.Padding = Thickness(14, 6, 14, 6)
        footer.Margin = Thickness(0, 8, 0, 0)
        footer.Child = self._text("Copyright 2026 Dang Quoc Truong (DQT)", 9.5,
                                   bold=True)
        footer.Child.HorizontalAlignment = HorizontalAlignment.Center
        root.Children.Add(footer)

        self.Content = root

    # ── Events ──
    def _on_run(self, sender, args):
        idx = self.combo.SelectedIndex
        if idx < 0:
            return
        self.result = {
            "grid_index": idx,
            "keep_upright": bool(self.chk_upright.IsChecked),
        }
        self.Close()

    def _on_cancel(self, sender, args):
        self.result = None
        self.Close()


# ─── Pick Helper ────────────────────────────────────────────
def pick_tags():
    try:
        refs = uidoc.Selection.PickObjects(
            ObjectType.Element, TagSelectionFilter(),
            "Select tag(s) to rotate (Enter / right-click to finish)")
        return [doc.GetElement(r.ElementId) for r in refs]
    except:
        return []


# ─── Main ───────────────────────────────────────────────────
def run():
    tags = pick_tags()
    if not tags:
        return

    entries = grid_entries(doc, active_view)
    if not entries:
        TaskDialog.Show("Rotate Tag to Grid",
                         "No straight grids found in this project.")
        return

    window = RotateTagWindow(entries, len(tags))
    window.ShowDialog()
    if window.result is None:
        return

    grid, angle, _label = entries[window.result["grid_index"]]
    if window.result["keep_upright"]:
        angle = normalize_upright(angle)

    ok = 0
    failed = []

    t = Transaction(doc, "DQT - Rotate Tag to Grid")
    t.Start()
    try:
        for tag in tags:
            success, err = rotate_tag_to_angle(doc, tag, angle)
            if success:
                ok += 1
            else:
                failed.append((tag, err))
        t.Commit()
    except Exception as e:
        t.RollBack()
        TaskDialog.Show("Rotate Tag to Grid", "Error: {}".format(str(e)))
        return

    msg = u"Rotated {} of {} tag(s) to match '{}' ({:.1f}°).".format(
        ok, len(tags), grid.Name, math.degrees(angle))
    if failed:
        lines = ["  ID {} - {}".format(_eid_int(tg.Id), err) for tg, err in failed[:10]]
        more = "" if len(failed) <= 10 else "\n  ... and {} more".format(len(failed) - 10)
        msg += "\n\nCould not rotate:\n{}{}".format("\n".join(lines), more)
    TaskDialog.Show("Rotate Tag to Grid", msg)


run()
