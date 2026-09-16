# -*- coding: utf-8 -*-
"""Level Elevation Impact Check

Answers "if I change this Level's elevation, which elements move with it?"
BEFORE you actually do it - useful when you want to raise/lower a level but
need to know (or compensate for) everything that would follow.

How it works: the change is SIMULATED inside a transaction that is always
rolled back - nothing in the model is modified. Each model element's
bounding box (Z) is recorded, the level's elevation is bumped by the
requested amount, the document is regenerated so Revit's own constraint
solver runs, and each element's Z is measured again. Elements are then
classified by how their Z changed:
  - "Moves with level": the whole element shifted by the full amount (its
    elevation is driven by this level - e.g. a floor, a wall based here, a
    column based here, furniture hosted here).
  - "Partially affected": only part of it moved (e.g. a wall whose BASE is
    on this level but TOP is on another - it gets taller/shorter, not just
    moved).
  - Everything else is unaffected.

Because it uses Revit's real regeneration rather than reimplementing the
hosting rules, it catches indirect dependencies too. Results are printed as
a per-category breakdown and the affected elements are selected in the model
so you can see exactly what would move.

Copyright (c) 2026 Dang Quoc Truong (DQT)
All rights reserved.
"""

__title__ = "Level\nImpact"
__author__ = "Dang Quoc Truong (DQT)"
__doc__ = ("Check which elements would change elevation if a Level's "
           "elevation is changed (simulated, non-destructive).")

import os
import clr
clr.AddReference('RevitAPI')
clr.AddReference('RevitAPIUI')
clr.AddReference('PresentationCore')
clr.AddReference('PresentationFramework')
clr.AddReference('WindowsBase')
clr.AddReference('System')

from Autodesk.Revit.DB import (
    Transaction, FilteredElementCollector, Level, ElementId, CategoryType
)
from pyrevit import forms, script


def _open_help_page(html_filename):
    """Open this tool's page from the shared _Modify_Help folder in the
    default browser. Returns True on success, False if the caller should
    fall back to the in-app help text (e.g. the folder went missing)."""
    try:
        panel_dir = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        path = os.path.join(panel_dir, "_Modify_Help", html_filename)
        if not os.path.isfile(path):
            return False
        os.startfile(path)
        return True
    except Exception:
        return False

import System
from System.Collections.Generic import List
from System.Windows import (
    Window, WindowStartupLocation, Thickness, HorizontalAlignment,
    VerticalAlignment, TextWrapping, FontWeights, CornerRadius,
    SizeToContent, ResizeMode
)
from System.Windows.Controls import (
    StackPanel, Button, TextBlock, RadioButton, Border, ComboBox,
    ComboBoxItem, TextBox, Orientation
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

MM_PER_FT = 304.8


def B(color):
    return SolidColorBrush(color)


def _eid_int(eid):
    try:
        return eid.Value
    except:
        return eid.IntegerValue


def get_levels():
    """All levels, sorted by elevation (low to high)."""
    lvls = list(FilteredElementCollector(doc).OfClass(Level)
                .WhereElementIsNotElementType())
    lvls.sort(key=lambda l: l.Elevation)
    return lvls


def collect_model_elements(scope_ids):
    """Model elements (with a bounding box) to test. If scope_ids is given,
    only those are tested; otherwise every model-category element."""
    result = []
    if scope_ids:
        source = [doc.GetElement(eid) for eid in scope_ids]
    else:
        source = list(FilteredElementCollector(doc)
                      .WhereElementIsNotElementType())
    for el in source:
        if el is None:
            continue
        cat = el.Category
        if cat is None or cat.CategoryType != CategoryType.Model:
            continue
        if isinstance(el, Level):
            continue
        try:
            bbox = el.get_BoundingBox(None)
        except:
            bbox = None
        if bbox is None:
            continue
        result.append((el.Id, el, bbox.Min.Z, bbox.Max.Z))
    return result


def simulate_impact(level, delta_ft, elems):
    """Bump the level by delta_ft inside a rolled-back transaction and measure
    which elements' Z changed. Returns (full_ids, partial_ids). Nothing is
    committed - the model is untouched."""
    # ~0.3 mm tolerance: below this a Z change is regeneration noise.
    tol = 0.3 / MM_PER_FT
    full_ids = []
    partial_ids = []

    t = Transaction(doc, "DQT - Level Impact (simulated, will roll back)")
    t.Start()
    try:
        level.Elevation = level.Elevation + delta_ft
        doc.Regenerate()

        for eid, el, before_min, before_max in elems:
            try:
                bbox = el.get_BoundingBox(None)
            except:
                bbox = None
            if bbox is None:
                continue
            dz_min = bbox.Min.Z - before_min
            dz_max = bbox.Max.Z - before_max

            moved_min = abs(dz_min - delta_ft) < tol
            moved_max = abs(dz_max - delta_ft) < tol
            changed = abs(dz_min) > tol or abs(dz_max) > tol

            if moved_min and moved_max:
                full_ids.append(eid)
            elif changed:
                partial_ids.append(eid)
    finally:
        # Never keep the change - this is analysis only.
        t.RollBack()

    return full_ids, partial_ids


def category_breakdown(full_ids, partial_ids):
    """dict: category name -> [full_count, partial_count]."""
    rows = {}
    for eid in full_ids:
        el = doc.GetElement(eid)
        name = el.Category.Name if (el and el.Category) else "?"
        rows.setdefault(name, [0, 0])[0] += 1
    for eid in partial_ids:
        el = doc.GetElement(eid)
        name = el.Category.Name if (el and el.Category) else "?"
        rows.setdefault(name, [0, 0])[1] += 1
    return rows


# =====================================================================
#  DIALOG
# =====================================================================

class LevelImpactDialog(Window):
    def __init__(self, levels, active_level_id, selection_count):
        self.levels = levels
        self.active_level_id = active_level_id
        self.selection_count = selection_count
        self.result = None
        self._build()

    def _st(self, t):
        b = TextBlock(); b.Text = t; b.FontSize = 12
        b.FontWeight = FontWeights.SemiBold; b.Foreground = B(DQT_ACCENT)
        b.Margin = Thickness(0, 0, 0, 6); return b

    def _nt(self, t):
        b = TextBlock(); b.Text = t; b.FontSize = 10
        b.Foreground = B(Color.FromRgb(0x99, 0x99, 0x99))
        b.TextWrapping = TextWrapping.Wrap
        b.Margin = Thickness(2, 2, 0, 2); return b

    def _rb(self, t, checked, group, enabled=True):
        r = RadioButton(); r.Content = t; r.GroupName = group
        r.IsChecked = System.Nullable[System.Boolean](checked)
        r.IsEnabled = enabled
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
        self.Title = "Level Impact - DQT"
        self.Width = 440; self.SizeToContent = SizeToContent.Height
        self.WindowStartupLocation = WindowStartupLocation.CenterScreen
        self.ResizeMode = ResizeMode.NoResize; self.Background = B(DQT_BG)

        m = StackPanel()

        # Header
        hd = Border(); hd.Background = B(DQT_DARK)
        hd.Padding = Thickness(16, 12, 16, 12)
        hp = StackPanel()
        t = TextBlock(); t.Text = "Level Elevation Impact"
        t.FontSize = 16; t.FontWeight = FontWeights.Bold
        t.Foreground = B(DQT_PRIMARY); t.FontFamily = FontFamily("Segoe UI")
        hp.Children.Add(t)
        s = TextBlock(); s.Text = "Dang Quoc Truong (DQT)"
        s.FontSize = 10; s.Foreground = B(DQT_ACCENT)
        s.Margin = Thickness(0, 2, 0, 0); hp.Children.Add(s)
        hd.Child = hp; m.Children.Add(hd)

        # Level picker
        self.cmb_level = ComboBox(); self.cmb_level.FontSize = 11.5
        self.cmb_level.Height = 28
        sel_idx = 0
        for i, lvl in enumerate(self.levels):
            item = ComboBoxItem()
            item.Content = "{}   ({:.0f} mm)".format(
                lvl.Name, lvl.Elevation * MM_PER_FT)
            item.Tag = lvl.Id
            self.cmb_level.Items.Add(item)
            if self.active_level_id is not None \
                    and _eid_int(lvl.Id) == _eid_int(self.active_level_id):
                sel_idx = i
        self.cmb_level.SelectedIndex = sel_idx
        self.cmb_level.SelectionChanged += self._level_changed

        self.lbl_cur = self._nt("")
        m.Children.Add(self._cd([self._st("Level"), self.cmb_level, self.lbl_cur]))

        # Amount
        op = StackPanel(); op.Orientation = Orientation.Horizontal
        ol = TextBlock(); ol.Text = "Change elevation by (mm, + = up): "
        ol.FontSize = 12; ol.Foreground = B(DQT_TEXT_DARK)
        ol.VerticalAlignment = VerticalAlignment.Center; op.Children.Add(ol)
        self.txt_delta = TextBox(); self.txt_delta.Text = "50"
        self.txt_delta.Width = 70; self.txt_delta.FontSize = 12
        self.txt_delta.Padding = Thickness(4, 2, 4, 2); op.Children.Add(self.txt_delta)
        m.Children.Add(self._cd([self._st("Elevation Change"), op]))

        # Scope
        has_sel = self.selection_count > 0
        self.rb_scope_all = self._rb(
            "Entire model", checked=(not has_sel), group="Scope")
        self.rb_scope_sel = self._rb(
            "Current selection ({} element(s))".format(self.selection_count),
            checked=has_sel, group="Scope", enabled=has_sel)
        m.Children.Add(self._cd([
            self._st("Scope"), self.rb_scope_all, self.rb_scope_sel,
            self._nt("Nothing is modified - the change is simulated and rolled "
                     "back. Affected elements are selected afterwards so you "
                     "can review them.")]))

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
        br = Button(); br.Content = "Check Impact"
        br.Width = 130; br.Height = 32; br.FontSize = 12
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

        self._level_changed(None, None)

    def _current_level(self):
        item = self.cmb_level.SelectedItem
        if item is None:
            return None
        return doc.GetElement(item.Tag)

    def _level_changed(self, s, e):
        lvl = self._current_level()
        if lvl is not None:
            self.lbl_cur.Text = "Current elevation: {:.0f} mm".format(
                lvl.Elevation * MM_PER_FT)

    def _cancel(self, s, e):
        self.result = None; self.Close()

    def _on_help(self, s, e):
        if _open_help_page("level_impact.html"):
            return
        forms.alert(
            "Answers 'if I change this Level's elevation, which elements "
            "move with it?' BEFORE you actually do it.\n\n"
            "The change is simulated inside a transaction that is always "
            "rolled back - nothing in the model is modified.\n\n"
            "- Pick the Level and the elevation change (mm, + = up).\n"
            "- Scope: check the entire model, or just the current selection.\n"
            "- Check Impact reports 'Moves with level' (fully driven by "
            "this level) vs 'Partially affected' (e.g. only one end "
            "constrained to it), per category, and selects the affected "
            "elements in the model.",
            title="Level Impact")

    def _run(self, s, e):
        lvl = self._current_level()
        if lvl is None:
            forms.alert("Select a level.", title=__title__)
            return
        try:
            delta_mm = float(self.txt_delta.Text)
        except:
            forms.alert("Enter a valid number of millimetres.", title=__title__)
            return
        if abs(delta_mm) < 1e-6:
            forms.alert("Elevation change is 0 - nothing to check.", title=__title__)
            return

        self.result = {
            'level_id': lvl.Id,
            'delta_mm': delta_mm,
            'use_selection': self.rb_scope_sel.IsChecked == True,
        }
        self.Close()


# =====================================================================
#  MAIN
# =====================================================================

def main():
    levels = get_levels()
    if not levels:
        forms.alert("No levels found in the document.", title=__title__,
                    exitscript=True)

    # Pre-run selection (so 'Current selection' can scope the check).
    pre_sel = [eid for eid in uidoc.Selection.GetElementIds()]
    active_level_id = None
    try:
        av = doc.ActiveView
        if av is not None and av.GenLevel is not None:
            active_level_id = av.GenLevel.Id
    except:
        pass

    dlg = LevelImpactDialog(levels, active_level_id, len(pre_sel))
    dlg.ShowDialog()
    if dlg.result is None:
        return
    opts = dlg.result

    level = doc.GetElement(opts['level_id'])
    delta_ft = opts['delta_mm'] / MM_PER_FT

    scope_ids = pre_sel if opts['use_selection'] else None
    elems = collect_model_elements(scope_ids)
    if not elems:
        forms.alert("No model elements to check in the chosen scope.",
                    title=__title__, exitscript=True)

    full_ids, partial_ids = simulate_impact(level, delta_ft, elems)
    rows = category_breakdown(full_ids, partial_ids)

    output = script.get_output()
    output.print_md("# Level Elevation Impact - {}".format(level.Name))
    output.print_md(
        "Simulated change: **{:+.0f} mm** &nbsp;|&nbsp; "
        "Elements checked: **{}** &nbsp;|&nbsp; "
        "Scope: **{}**".format(
            opts['delta_mm'], len(elems),
            "current selection" if scope_ids else "entire model"))
    output.print_md(
        "_Nothing was modified - the change was simulated and rolled back._")

    if rows:
        data = []
        for name in sorted(rows.keys()):
            full_c, part_c = rows[name]
            data.append([name, full_c, part_c, full_c + part_c])
        data.sort(key=lambda r: r[3], reverse=True)
        data.append(["TOTAL", len(full_ids), len(partial_ids),
                     len(full_ids) + len(partial_ids)])
        output.print_table(
            table_data=data,
            columns=["Category", "Moves with level", "Partially affected",
                     "Total affected"])
    else:
        output.print_md("### No elements would change elevation.")

    output.print_md(
        "**Moves with level** = the whole element follows the level "
        "(elevation driven by it). \n"
        "**Partially affected** = only part moves (e.g. a wall based on this "
        "level but topped on another - it stretches).")

    # Select the affected elements so the user can see them in the model.
    affected = List[ElementId]()
    for eid in full_ids:
        affected.Add(eid)
    for eid in partial_ids:
        affected.Add(eid)
    try:
        uidoc.Selection.SetElementIds(affected)
    except:
        pass

    total = len(full_ids) + len(partial_ids)
    summary = (
        "Level Impact Check - {}\n\n"
        "Simulated change: {:+.0f} mm\n"
        "Elements checked: {}\n\n"
        "Moves with level: {}\n"
        "Partially affected: {}\n"
        "Total affected: {}\n\n"
        "The affected elements are now selected in the model, and a full "
        "breakdown is in the output window.\n\n"
        "(Nothing was changed - this was a simulation.)"
    ).format(level.Name, opts['delta_mm'], len(elems),
             len(full_ids), len(partial_ids), total)
    forms.alert(summary, title=__title__)


if __name__ == "__main__":
    main()
