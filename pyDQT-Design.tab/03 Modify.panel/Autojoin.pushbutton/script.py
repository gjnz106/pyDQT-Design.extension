# -*- coding: utf-8 -*-
"""Auto Join / Unjoin Elements - Rule-based pair joining with save/load.

Each rule defines a pair: Priority Category <-> Join With Category.
Priority Category will CUT Join With Category.
Switch button swaps cut direction.
Save/Load rule sets for reuse across sessions.
"""

__title__ = "Auto Join\nElements"
__author__ = "DQT Tools"
__doc__ = "Auto join/unjoin elements in current view with rule-based category pairs"

import os
import json

import clr
clr.AddReference('PresentationFramework')
clr.AddReference('PresentationCore')
clr.AddReference('WindowsBase')
clr.AddReference('System')

import System
from System.Windows import (
    Window, Thickness, HorizontalAlignment, VerticalAlignment,
    TextWrapping, WindowStartupLocation, ResizeMode,
    GridLength, GridUnitType, CornerRadius, SizeToContent
)
from System.Windows.Controls import (
    StackPanel, Button, TextBlock, CheckBox, Border,
    ScrollViewer, Orientation, ScrollBarVisibility,
    ComboBox, ComboBoxItem
)
from System.Windows.Media import (
    SolidColorBrush, Colors, BrushConverter, FontFamily
)

# Revit - EXPLICIT imports only (wildcard shadows WPF Grid/Color)
from Autodesk.Revit.DB import (
    FilteredElementCollector,
    BuiltInCategory,
    JoinGeometryUtils
)
from Autodesk.Revit.UI import TaskDialog

from pyrevit import revit, script

# ==========================================================================
# GLOBALS
# ==========================================================================
doc = __revit__.ActiveUIDocument.Document
uidoc = __revit__.ActiveUIDocument
active_view = doc.ActiveView
output = script.get_output()

# Elements selected when the tool was launched (for "selection only" mode)
try:
    preselected_ids = list(uidoc.Selection.GetElementIds())
except:
    preselected_ids = []

SCRIPT_DIR = os.path.dirname(__file__)
SETTINGS_FILE = os.path.join(SCRIPT_DIR, "autojoin_settings.json")

# ==========================================================================
# BRUSH HELPER (IronPython safe - no Color.FromRgb/FromArgb)
# ==========================================================================
_bc = BrushConverter()

def B(h):
    return _bc.ConvertFromString(h)

# ==========================================================================
# DQT THEME COLORS
# ==========================================================================
C_HEADER    = "#F0CC88"      # DQT gold header
C_HEADER_DK = "#D4A843"      # Darker gold for accents
C_BG        = "#FAFAFA"      # Light background
C_TOOLBAR   = "#F0F0F0"      # Gray toolbar
C_BORDER    = "#D0C8B8"      # Warm border
C_ROW_BG    = "#FFFFFF"      # White rows
C_TEXT      = "#333333"      # Primary text
C_TEXT_MID  = "#666666"      # Secondary text
C_TEXT_LT   = "#999999"      # Muted text
C_ACCENT    = "#C49A2A"      # Gold accent for buttons
C_ACC_DARK  = "#8B7520"      # Dark gold
C_JOIN_BG   = "#4CAF50"      # Green for Join
C_UNJOIN_BG = "#E57373"      # Red for Unjoin
C_ADD_BG    = "#C49A2A"      # Gold for Add Rule
C_DEL_ROW   = "#CC6666"      # Red for delete row
C_SAVE_BG   = "#C49A2A"      # Gold for Save
C_DEL_SET   = "#AA5555"      # Dark red for Delete Setting
C_BTN_FG    = "#FFFFFF"      # Button text
C_COPYRIGHT = "#AAAAAA"      # Copyright text

FONT = FontFamily("Segoe UI")


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


# ==========================================================================
# JOINABLE CATEGORIES
# ==========================================================================
CATEGORY_MAP = {
    "Walls":                   BuiltInCategory.OST_Walls,
    "Floors":                  BuiltInCategory.OST_Floors,
    "Structural Columns":      BuiltInCategory.OST_StructuralColumns,
    "Columns":                 BuiltInCategory.OST_Columns,
    "Structural Framing":      BuiltInCategory.OST_StructuralFraming,
    "Roofs":                   BuiltInCategory.OST_Roofs,
    "Ceilings":                BuiltInCategory.OST_Ceilings,
    "Structural Foundations":   BuiltInCategory.OST_StructuralFoundation,
    "Generic Models":          BuiltInCategory.OST_GenericModel,
}
CAT_NAMES = sorted(CATEGORY_MAP.keys())


# ==========================================================================
# REVIT HELPERS
# ==========================================================================

def get_elements(view, bic):
    return list(
        FilteredElementCollector(doc, view.Id)
        .OfCategory(bic)
        .WhereElementIsNotElementType()
        .ToElements()
    )

def get_selected_elements(bic):
    """Pre-selected elements of the given category (for selection-only mode)."""
    out = []
    for eid in preselected_ids:
        el = doc.GetElement(eid)
        if el is None:
            continue
        try:
            if el.Category is not None and el.Category.BuiltInCategory == bic:
                out.append(el)
        except:
            pass
    return out

def collect(bic, use_selection):
    if use_selection:
        return get_selected_elements(bic)
    return get_elements(active_view, bic)

def get_bbox(elem):
    bb = elem.get_BoundingBox(active_view)
    if bb is None:
        bb = elem.get_BoundingBox(None)
    return bb

def bbox_overlap(a, b, tol=0.5):
    if a is None or b is None:
        return False
    return not (a.Max.X + tol < b.Min.X or a.Min.X - tol > b.Max.X or
                a.Max.Y + tol < b.Min.Y or a.Min.Y - tol > b.Max.Y or
                a.Max.Z + tol < b.Min.Z or a.Min.Z - tol > b.Max.Z)

def try_join(d, e1, e2):
    try:
        if JoinGeometryUtils.AreElementsJoined(d, e1, e2):
            return False
        JoinGeometryUtils.JoinGeometry(d, e1, e2)
        return True
    except:
        return False

def try_unjoin(d, e1, e2):
    try:
        if JoinGeometryUtils.AreElementsJoined(d, e1, e2):
            JoinGeometryUtils.UnjoinGeometry(d, e1, e2)
            return True
    except:
        pass
    return False

def try_switch(d, hi, lo):
    try:
        if JoinGeometryUtils.AreElementsJoined(d, hi, lo):
            if not JoinGeometryUtils.IsCuttingElementInJoin(d, hi, lo):
                JoinGeometryUtils.SwitchJoinOrder(d, hi, lo)
                return True
    except:
        pass
    return False


# ==========================================================================
# DATA
# ==========================================================================

class JoinRule(object):
    def __init__(self, enabled=True, cat_a="Walls", cat_b="Floors"):
        self.enabled = enabled
        self.cat_a = cat_a
        self.cat_b = cat_b

    def to_dict(self):
        return {"enabled": self.enabled, "cat_a": self.cat_a, "cat_b": self.cat_b}

    @staticmethod
    def from_dict(d):
        return JoinRule(d.get("enabled", True),
                        d.get("cat_a", "Walls"),
                        d.get("cat_b", "Floors"))


# ==========================================================================
# SETTINGS PERSISTENCE
# ==========================================================================

def load_all_settings():
    if not os.path.exists(SETTINGS_FILE):
        return {}
    try:
        with open(SETTINGS_FILE, 'r') as f:
            return json.load(f)
    except:
        return {}

def save_all_settings(data):
    try:
        with open(SETTINGS_FILE, 'w') as f:
            json.dump(data, f, indent=2)
    except Exception as ex:
        TaskDialog.Show("Save Error", str(ex))

def save_preset(name, rules):
    data = load_all_settings()
    data[name] = [r.to_dict() for r in rules]
    save_all_settings(data)

def delete_preset(name):
    data = load_all_settings()
    if name in data:
        del data[name]
        save_all_settings(data)

def load_preset(name):
    data = load_all_settings()
    if name in data:
        return [JoinRule.from_dict(d) for d in data[name]]
    return []


# ==========================================================================
# WPF WINDOW
# ==========================================================================

class AutoJoinWindow(Window):

    MODE_JOIN = "join"
    MODE_UNJOIN = "unjoin"

    def __init__(self):
        self.rules = [JoinRule(True, "Floors", "Walls")]
        self.result_rules = []
        self.mode = None
        self.use_selection = False
        self._setup()
        self._build()
        self._load_presets_combo()

    def _setup(self):
        self.Title = "Auto Join - DQT"
        self.Width = 600
        self.Height = 420
        self.WindowStartupLocation = WindowStartupLocation.CenterScreen
        self.ResizeMode = ResizeMode.CanResize
        self.MinWidth = 520
        self.MinHeight = 340
        self.Background = B(C_BG)

    def _build(self):
        root = StackPanel()
        root.Children.Add(self._header())
        root.Children.Add(self._setting_bar())
        root.Children.Add(self._lbl_section())
        root.Children.Add(self._rule_area())
        root.Children.Add(self._bottom_bar())
        root.Children.Add(self._copyright())
        self.Content = root

    # ------------------------------------------------------------------
    # HEADER - DQT gold style
    # ------------------------------------------------------------------
    def _header(self):
        bd = Border()
        bd.Background = B(C_HEADER)
        bd.Padding = Thickness(20, 10, 20, 10)

        sp = StackPanel()
        sp.Orientation = Orientation.Horizontal
        sp.VerticalAlignment = VerticalAlignment.Center

        # Title
        title_sp = StackPanel()

        t = TextBlock()
        t.Text = "Auto Join"
        t.FontSize = 18
        t.FontWeight = System.Windows.FontWeights.Bold
        t.Foreground = B(C_TEXT)
        t.FontFamily = FONT
        title_sp.Children.Add(t)

        sub = TextBlock()
        sub.Text = "View: " + active_view.Name
        sub.FontSize = 10
        sub.Foreground = B(C_TEXT_MID)
        sub.FontFamily = FONT
        title_sp.Children.Add(sub)

        sp.Children.Add(title_sp)

        bd.Child = sp
        return bd

    # ------------------------------------------------------------------
    # SETTING BAR
    # ------------------------------------------------------------------
    def _setting_bar(self):
        sp = StackPanel()
        sp.Orientation = Orientation.Horizontal
        sp.Margin = Thickness(16, 10, 16, 4)

        self._preset_cb = ComboBox()
        self._preset_cb.Width = 240
        self._preset_cb.Height = 30
        self._preset_cb.FontSize = 12
        self._preset_cb.FontFamily = FONT
        self._preset_cb.IsEditable = True
        self._preset_cb.SelectionChanged += self._on_preset_selected
        sp.Children.Add(self._preset_cb)

        sp.Children.Add(self._spacer(8))

        btn_save = self._btn("Save Setting", 95, C_SAVE_BG, C_BTN_FG)
        btn_save.Click += self._on_save
        sp.Children.Add(btn_save)

        sp.Children.Add(self._spacer(6))

        btn_del = self._btn("Delete Setting", 105, C_DEL_SET, C_BTN_FG)
        btn_del.Click += self._on_delete_setting
        sp.Children.Add(btn_del)

        sp.Children.Add(self._spacer(6))

        btn_help = self._btn("? Help", 60, C_TEXT_MID, C_BTN_FG)
        btn_help.Click += self._on_help
        sp.Children.Add(btn_help)

        return sp

    # ------------------------------------------------------------------
    # SECTION LABEL
    # ------------------------------------------------------------------
    def _lbl_section(self):
        lbl = TextBlock()
        lbl.Text = "Select Rule Join"
        lbl.FontSize = 11
        lbl.Foreground = B(C_TEXT_MID)
        lbl.FontFamily = FONT
        lbl.Margin = Thickness(16, 6, 16, 4)
        return lbl

    # ------------------------------------------------------------------
    # RULE AREA
    # ------------------------------------------------------------------
    def _rule_area(self):
        outer = Border()
        outer.BorderBrush = B(C_BORDER)
        outer.BorderThickness = Thickness(1)
        outer.CornerRadius = CornerRadius(4)
        outer.Background = B(C_ROW_BG)
        outer.Padding = Thickness(6, 4, 6, 4)
        outer.Margin = Thickness(16, 0, 16, 0)

        inner = StackPanel()

        # Column headers
        hdr = StackPanel()
        hdr.Orientation = Orientation.Horizontal
        hdr.Margin = Thickness(2, 2, 2, 4)

        for text, w in [("Check", 42), ("Priority Category", 155),
                         ("Switch", 55), ("Join With Category", 155),
                         ("Delete", 50)]:
            tb = TextBlock()
            tb.Text = text
            tb.Width = w
            tb.FontSize = 10
            tb.Foreground = B(C_TEXT_MID)
            tb.FontFamily = FONT
            tb.FontWeight = System.Windows.FontWeights.SemiBold
            hdr.Children.Add(tb)
        inner.Children.Add(hdr)

        sep = Border()
        sep.Height = 1
        sep.Background = B(C_BORDER)
        sep.Margin = Thickness(0, 0, 0, 4)
        inner.Children.Add(sep)

        sv = ScrollViewer()
        sv.VerticalScrollBarVisibility = ScrollBarVisibility.Auto
        sv.MinHeight = 60
        sv.MaxHeight = 280

        self._rules_panel = StackPanel()
        self._refresh_rules()
        sv.Content = self._rules_panel
        inner.Children.Add(sv)

        outer.Child = inner
        return outer

    def _refresh_rules(self):
        self._rules_panel.Children.Clear()
        for i, rule in enumerate(self.rules):
            self._rules_panel.Children.Add(self._rule_row(rule, i))

    def _rule_row(self, rule, idx):
        row = StackPanel()
        row.Orientation = Orientation.Horizontal
        row.Margin = Thickness(2, 3, 2, 3)

        # Checkbox
        cb = CheckBox()
        cb.IsChecked = rule.enabled
        cb.Width = 42
        cb.VerticalAlignment = VerticalAlignment.Center
        cb.HorizontalAlignment = HorizontalAlignment.Center
        cb.Tag = idx
        cb.Checked += self._on_check
        cb.Unchecked += self._on_check
        row.Children.Add(cb)

        # Priority Category
        cmb_a = self._cat_combo(rule.cat_a, 150)
        cmb_a.Tag = ("a", idx)
        cmb_a.SelectionChanged += self._on_cat_change
        row.Children.Add(cmb_a)

        row.Children.Add(self._spacer(5))

        # Switch
        sw = Button()
        sw.Content = u"\u21C6"
        sw.Width = 40
        sw.Height = 28
        sw.FontSize = 16
        sw.Background = B(C_TOOLBAR)
        sw.BorderBrush = B(C_BORDER)
        sw.BorderThickness = Thickness(1)
        sw.Cursor = System.Windows.Input.Cursors.Hand
        sw.FontFamily = FONT
        sw.VerticalAlignment = VerticalAlignment.Center
        sw.Tag = idx
        sw.Click += self._on_switch
        sw.ToolTip = "Swap priority direction"
        row.Children.Add(sw)

        row.Children.Add(self._spacer(5))

        # Join With Category
        cmb_b = self._cat_combo(rule.cat_b, 150)
        cmb_b.Tag = ("b", idx)
        cmb_b.SelectionChanged += self._on_cat_change
        row.Children.Add(cmb_b)

        row.Children.Add(self._spacer(8))

        # Delete row
        bd = Button()
        bd.Content = u"\u2716"
        bd.Width = 30
        bd.Height = 28
        bd.FontSize = 11
        bd.Background = SolidColorBrush(Colors.Transparent)
        bd.Foreground = B(C_DEL_ROW)
        bd.BorderBrush = B(C_BORDER)
        bd.BorderThickness = Thickness(1)
        bd.Cursor = System.Windows.Input.Cursors.Hand
        bd.VerticalAlignment = VerticalAlignment.Center
        bd.Tag = idx
        bd.Click += self._on_delete_rule
        bd.ToolTip = "Delete this rule"
        row.Children.Add(bd)

        return row

    def _cat_combo(self, selected, width):
        cmb = ComboBox()
        cmb.Width = width
        cmb.Height = 28
        cmb.FontSize = 12
        cmb.FontFamily = FONT
        for name in CAT_NAMES:
            item = ComboBoxItem()
            item.Content = name
            cmb.Items.Add(item)
            if name == selected:
                cmb.SelectedItem = item
        return cmb

    # ------------------------------------------------------------------
    # BOTTOM BAR (Add Rule + Unjoin + Join)
    # ------------------------------------------------------------------
    def _bottom_bar(self):
        sp = StackPanel()
        sp.Orientation = Orientation.Horizontal
        sp.HorizontalAlignment = HorizontalAlignment.Right
        sp.Margin = Thickness(16, 12, 16, 4)

        self.cb_selection = CheckBox()
        self.cb_selection.Content = "Selected elements only ({})".format(
            len(preselected_ids))
        self.cb_selection.VerticalAlignment = VerticalAlignment.Center
        self.cb_selection.Foreground = B("#444444")
        self.cb_selection.FontFamily = FONT
        self.cb_selection.FontSize = 12
        self.cb_selection.IsChecked = (len(preselected_ids) > 0)
        sp.Children.Add(self.cb_selection)
        sp.Children.Add(self._spacer(14))

        btn_add = self._btn("Add Rule", 100, C_ADD_BG, C_BTN_FG)
        btn_add.Click += self._on_add_rule
        sp.Children.Add(btn_add)

        sp.Children.Add(self._spacer(10))

        btn_unjoin = self._btn("Unjoin", 90, C_UNJOIN_BG, C_BTN_FG)
        btn_unjoin.FontWeight = System.Windows.FontWeights.Bold
        btn_unjoin.Click += self._on_unjoin
        sp.Children.Add(btn_unjoin)

        sp.Children.Add(self._spacer(8))

        btn_join = self._btn("Join", 90, C_JOIN_BG, C_BTN_FG)
        btn_join.FontWeight = System.Windows.FontWeights.Bold
        btn_join.Click += self._on_join
        sp.Children.Add(btn_join)

        return sp

    # ------------------------------------------------------------------
    # COPYRIGHT FOOTER
    # ------------------------------------------------------------------
    def _copyright(self):
        sp = StackPanel()
        sp.Orientation = Orientation.Horizontal
        sp.HorizontalAlignment = HorizontalAlignment.Center
        sp.Margin = Thickness(0, 4, 0, 8)

        cr = TextBlock()
        cr.Text = "Copyright by Dang Quoc Truong (DQT)"
        cr.FontSize = 9.5
        cr.Foreground = B(C_COPYRIGHT)
        cr.FontFamily = FONT
        sp.Children.Add(cr)

        return sp

    # ------------------------------------------------------------------
    # HELPERS
    # ------------------------------------------------------------------
    def _btn(self, text, width, bg, fg):
        b = Button()
        b.Content = text
        b.Width = width
        b.Height = 30
        b.FontSize = 12
        b.Background = B(bg)
        b.Foreground = B(fg)
        b.BorderThickness = Thickness(0)
        b.Cursor = System.Windows.Input.Cursors.Hand
        b.FontFamily = FONT
        return b

    def _spacer(self, w):
        s = Border()
        s.Width = w
        return s

    def _load_presets_combo(self):
        self._preset_cb.Items.Clear()
        data = load_all_settings()
        for name in sorted(data.keys()):
            item = ComboBoxItem()
            item.Content = name
            self._preset_cb.Items.Add(item)

    def _get_combo_text(self, combo):
        if combo.SelectedItem is not None:
            return combo.SelectedItem.Content
        if combo.IsEditable and combo.Text:
            return combo.Text
        return ""

    # ------------------------------------------------------------------
    # HANDLERS
    # ------------------------------------------------------------------
    def _on_check(self, s, e):
        idx = s.Tag
        if idx < len(self.rules):
            self.rules[idx].enabled = bool(s.IsChecked)

    def _on_cat_change(self, s, e):
        tag = s.Tag
        if tag is None:
            return
        side, idx = tag
        if idx >= len(self.rules):
            return
        sel = s.SelectedItem
        if sel is None:
            return
        name = sel.Content
        if side == "a":
            self.rules[idx].cat_a = name
        else:
            self.rules[idx].cat_b = name

    def _on_switch(self, s, e):
        idx = s.Tag
        if idx < len(self.rules):
            r = self.rules[idx]
            r.cat_a, r.cat_b = r.cat_b, r.cat_a
            self._refresh_rules()

    def _on_delete_rule(self, s, e):
        idx = s.Tag
        if idx < len(self.rules):
            self.rules.pop(idx)
            self._refresh_rules()

    def _on_add_rule(self, s, e):
        a, b = "Walls", "Floors"
        used = set()
        for r in self.rules:
            used.add((r.cat_a, r.cat_b))
        for na in CAT_NAMES:
            found = False
            for nb in CAT_NAMES:
                if na != nb and (na, nb) not in used:
                    a, b = na, nb
                    found = True
                    break
            if found:
                break
        self.rules.append(JoinRule(True, a, b))
        self._refresh_rules()

    def _on_preset_selected(self, s, e):
        name = self._get_combo_text(self._preset_cb)
        if not name:
            return
        loaded = load_preset(name)
        if loaded:
            self.rules = loaded
            self._refresh_rules()

    def _on_save(self, s, e):
        name = ""
        if self._preset_cb.IsEditable and self._preset_cb.Text:
            name = self._preset_cb.Text.strip()
        elif self._preset_cb.SelectedItem is not None:
            name = self._preset_cb.SelectedItem.Content.strip()

        if not name:
            TaskDialog.Show("Save", "Please type a setting name in the dropdown.")
            return

        save_preset(name, self.rules)
        self._load_presets_combo()

        for i in range(self._preset_cb.Items.Count):
            if self._preset_cb.Items[i].Content == name:
                self._preset_cb.SelectedIndex = i
                break

        TaskDialog.Show("Saved", "Setting '{}' saved.".format(name))

    def _on_delete_setting(self, s, e):
        name = self._get_combo_text(self._preset_cb)
        if not name:
            TaskDialog.Show("Delete", "Select a setting to delete.")
            return
        delete_preset(name)
        self._load_presets_combo()
        self._preset_cb.Text = ""
        TaskDialog.Show("Deleted", "Setting '{}' deleted.".format(name))

    def _on_help(self, s, e):
        if _open_help_page("autojoin.html"):
            return
        TaskDialog.Show(
            "Auto Join - DQT",
            "Each rule pairs a Priority Category with a Join With Category; "
            "the Priority Category cuts the other one.\n\n"
            "- Add Rule adds another category pair.\n"
            "- The Switch button on a rule swaps which side cuts which.\n"
            "- 'Selected elements only' restricts Join/Unjoin to the current "
            "Revit selection instead of the whole active view.\n"
            "- Join applies every enabled rule; Unjoin removes those joins.\n"
            "- Save Setting / Delete Setting keep named rule sets for reuse "
            "across sessions.")

    def _validate_selection(self):
        if bool(self.cb_selection.IsChecked) and not preselected_ids:
            TaskDialog.Show("Auto Join",
                            "'Selected elements only' is on, but you haven't "
                            "selected any elements.\nSelect elements before "
                            "running, or untick that checkbox.")
            return False
        return True

    def _on_join(self, s, e):
        self.result_rules = [r for r in self.rules if r.enabled]
        if not self.result_rules:
            TaskDialog.Show("Auto Join",
                            "No enabled rules. Add and enable at least one.")
            return
        if not self._validate_selection():
            return
        self.use_selection = bool(self.cb_selection.IsChecked)
        self.mode = self.MODE_JOIN
        self.DialogResult = True
        self.Close()

    def _on_unjoin(self, s, e):
        self.result_rules = [r for r in self.rules if r.enabled]
        if not self.result_rules:
            TaskDialog.Show("Auto Unjoin",
                            "No enabled rules. Add and enable at least one.")
            return
        if not self._validate_selection():
            return
        self.use_selection = bool(self.cb_selection.IsChecked)
        self.mode = self.MODE_UNJOIN
        self.DialogResult = True
        self.Close()


# ==========================================================================
# JOIN LOGIC
# ==========================================================================

def run_auto_join(rules, use_selection=False):
    output.print_md("# Auto Join Elements")
    output.print_md("**View:** " + active_view.Name)
    if use_selection:
        output.print_md("**Scope:** Selected elements only ({})".format(
            len(preselected_ids)))
    output.print_md("---")

    t_joined = 0
    t_switched = 0
    t_failed = 0
    t_already = 0

    with revit.Transaction("Auto Join Elements"):
        for ri, rule in enumerate(rules):
            bic_a = CATEGORY_MAP.get(rule.cat_a)
            bic_b = CATEGORY_MAP.get(rule.cat_b)
            if bic_a is None or bic_b is None:
                output.print_md("*Rule {}: Invalid category, skipped.*".format(ri + 1))
                continue

            elems_a = collect(bic_a, use_selection)
            elems_b = collect(bic_b, use_selection)

            if not elems_a or not elems_b:
                output.print_md("**Rule {}:** {} -> {} | No elements found".format(
                    ri + 1, rule.cat_a, rule.cat_b))
                continue

            rj, rs, rf, ra = 0, 0, 0, 0

            for ea in elems_a:
                ba = get_bbox(ea)
                if ba is None:
                    continue
                for eb in elems_b:
                    bb = get_bbox(eb)
                    if bb is None:
                        continue
                    if bbox_overlap(ba, bb):
                        already = False
                        try:
                            already = JoinGeometryUtils.AreElementsJoined(
                                doc, ea, eb)
                        except:
                            pass

                        if not already:
                            if try_join(doc, ea, eb):
                                rj += 1
                            else:
                                rf += 1
                        else:
                            ra += 1

                        if try_switch(doc, ea, eb):
                            rs += 1

            output.print_md(
                "**Rule {}:** {} -> {} | "
                "{} joined, {} switched, {} already, {} failed".format(
                    ri + 1, rule.cat_a, rule.cat_b, rj, rs, ra, rf))

            t_joined += rj
            t_switched += rs
            t_failed += rf
            t_already += ra

    output.print_md("---")
    output.print_md("## Summary")
    output.print_md("- **New joins:** {}".format(t_joined))
    output.print_md("- **Priority switches:** {}".format(t_switched))
    output.print_md("- **Already joined:** {}".format(t_already))
    if t_failed > 0:
        output.print_md("- **Failed:** {}".format(t_failed))
    output.print_md("**Done!**")


# ==========================================================================
# UNJOIN LOGIC
# ==========================================================================

def run_auto_unjoin(rules, use_selection=False):
    output.print_md("# Auto Unjoin Elements")
    output.print_md("**View:** " + active_view.Name)
    if use_selection:
        output.print_md("**Scope:** Selected elements only ({})".format(
            len(preselected_ids)))
    output.print_md("---")

    t_unjoined = 0
    t_not_joined = 0

    with revit.Transaction("Auto Unjoin Elements"):
        for ri, rule in enumerate(rules):
            bic_a = CATEGORY_MAP.get(rule.cat_a)
            bic_b = CATEGORY_MAP.get(rule.cat_b)
            if bic_a is None or bic_b is None:
                output.print_md("*Rule {}: Invalid category, skipped.*".format(ri + 1))
                continue

            elems_a = collect(bic_a, use_selection)
            elems_b = collect(bic_b, use_selection)

            if not elems_a or not elems_b:
                output.print_md("**Rule {}:** {} x {} | No elements found".format(
                    ri + 1, rule.cat_a, rule.cat_b))
                continue

            ru, rn = 0, 0

            for ea in elems_a:
                for eb in elems_b:
                    if try_unjoin(doc, ea, eb):
                        ru += 1
                    else:
                        rn += 1

            output.print_md(
                "**Rule {}:** {} x {} | {} unjoined, {} not joined".format(
                    ri + 1, rule.cat_a, rule.cat_b, ru, rn))

            t_unjoined += ru
            t_not_joined += rn

    output.print_md("---")
    output.print_md("## Summary")
    output.print_md("- **Total unjoined:** {}".format(t_unjoined))
    output.print_md("- **Not joined (skipped):** {}".format(t_not_joined))
    output.print_md("**Done!**")


# ==========================================================================
# ENTRY
# ==========================================================================

def main():
    if active_view is None:
        TaskDialog.Show("Auto Join", "No active view found.")
        return

    win = AutoJoinWindow()
    result = win.ShowDialog()

    if result and win.result_rules:
        if win.mode == AutoJoinWindow.MODE_JOIN:
            run_auto_join(win.result_rules, win.use_selection)
        elif win.mode == AutoJoinWindow.MODE_UNJOIN:
            run_auto_unjoin(win.result_rules, win.use_selection)
    else:
        output.print_md("*Cancelled.*")

main()