# -*- coding: utf-8 -*-
"""Copy Template Filters v1.0
Author: Dang Quoc Truong (DQT)

Copies view filters from one view template to other view templates - each
filter with its Visible and Enabled check boxes and its graphic overrides
(lines, patterns, halftone, transparency), exactly as Visibility/Graphics >
Filters shows them in the source template.

Three ways to copy:
  Add & update - add the filters a target lacks; a filter it already has
                 takes the source's settings
  Add only     - add the filters a target lacks; leave the ones it already
                 has as they are
  Replace      - make the target's filter list identical to the source's
                 chosen filters, in the same order (the others are removed)

A view template applies its filters to its views only when it controls
"V/G Overrides Filters"; the tool can switch that on in the targets that
do not. Everything happens in one transaction - one Undo.
"""
__title__ = "Copy\nFilters"
__author__ = "DQT"

import os
import datetime
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

TITLE = "DQT - Copy Template Filters"
HELP_PAGE = "copy_template_filters.html"

MODE_MERGE = "merge"         # Add & update
MODE_ADD = "add"             # Add only
MODE_REPLACE = "replace"     # Replace

DARK = "#5D4E37"
GREY = "#888888"
WARN = "#B26A00"


def _open_help_page(html_filename):
    """Open this tool's page from the shared _Views-Sheets_Help folder in
    the default browser. Returns True on success, False if the caller should
    fall back to the in-app help text (e.g. the folder went missing)."""
    try:
        panel_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        path = os.path.join(panel_dir, "_Views-Sheets_Help", html_filename)
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


# ============================================================================
# READING A TEMPLATE'S FILTERS
# ============================================================================
def pretty_view_type(view):
    """"FloorPlan" -> "Floor Plan", "ThreeD" -> "3D"."""
    try:
        kind = str(view.ViewType)
    except Exception:
        return ""
    if "." in kind:
        kind = kind.rsplit(".", 1)[-1]
    if kind == "ThreeD":
        return "3D"
    out = ""
    for i, ch in enumerate(kind):
        if ch.isupper() and i and not kind[i - 1].isupper():
            out += " "
        out += ch
    return out


def can_take_filters(view):
    """A view template that can carry filters (a schedule template cannot)."""
    try:
        return bool(view.AreGraphicsOverridesAllowed())
    except Exception:
        return True


def view_templates(doc):
    """Every view template that can carry filters, sorted by name."""
    found = []
    for view in DB.FilteredElementCollector(doc).OfClass(DB.View):
        try:
            if view.IsTemplate and can_take_filters(view):
                found.append(view)
        except Exception:
            continue
    return sorted(found, key=lambda v: v.Name.lower())


def ordered_filter_ids(view):
    """The view's filters in Visibility/Graphics order (Revit 2021+ keeps an
    order; older versions only a set)."""
    try:
        return list(view.GetOrderedFilters())
    except Exception:
        pass
    try:
        return list(view.GetFilters())
    except Exception:
        return []


def is_filter_enabled(view, filter_id):
    try:
        return bool(view.GetIsFilterEnabled(filter_id))
    except Exception:
        return True               # before Revit 2021 every filter is enabled


def _valid_color(color):
    try:
        return color is not None and color.IsValid
    except Exception:
        return False


def _valid_id(eid):
    try:
        return eid is not None and _eid_int(eid) > 0
    except Exception:
        return False


def override_summary(ogs):
    """What a filter's graphic overrides change, in a few words."""
    parts = []

    def any_of(names, kind):
        for name in names:
            try:
                value = getattr(ogs, name)
            except Exception:
                continue
            if kind == "color" and _valid_color(value):
                return True
            if kind == "id" and _valid_id(value):
                return True
            if kind == "weight":
                try:
                    if int(value) > 0:
                        return True
                except Exception:
                    pass
        return False

    if (any_of(("ProjectionLineColor",), "color") or any_of(("ProjectionLinePatternId",), "id")
            or any_of(("ProjectionLineWeight",), "weight")):
        parts.append("lines")
    if (any_of(("SurfaceForegroundPatternColor", "SurfaceBackgroundPatternColor",
                "ProjectionFillColor"), "color")
            or any_of(("SurfaceForegroundPatternId", "SurfaceBackgroundPatternId",
                       "ProjectionFillPatternId"), "id")):
        parts.append("patterns")
    if (any_of(("CutLineColor",), "color") or any_of(("CutLinePatternId",), "id")
            or any_of(("CutLineWeight",), "weight")):
        parts.append("cut lines")
    if (any_of(("CutForegroundPatternColor", "CutBackgroundPatternColor", "CutFillColor"), "color")
            or any_of(("CutForegroundPatternId", "CutBackgroundPatternId", "CutFillPatternId"), "id")):
        parts.append("cut patterns")
    try:
        if ogs.Halftone:
            parts.append("halftone")
    except Exception:
        pass
    try:
        if int(ogs.Transparency) > 0:
            parts.append("transparency {}%".format(int(ogs.Transparency)))
    except Exception:
        pass
    return ", ".join(parts) if parts else "no overrides"


class FilterRow(object):
    """One filter of the source template, as shown in the list."""

    def __init__(self, doc, view, filter_id):
        self.id = filter_id
        element = doc.GetElement(filter_id)
        self.name = element.Name if element is not None else "Filter {}".format(_eid_int(filter_id))
        try:
            self.visible = bool(view.GetFilterVisibility(filter_id))
        except Exception:
            self.visible = True
        self.enabled = is_filter_enabled(view, filter_id)
        try:
            self.summary = override_summary(view.GetFilterOverrides(filter_id))
        except Exception:
            self.summary = "-"

    @property
    def detail(self):
        return u"{}  ·  {}  ·  {}".format(
            "Visible" if self.visible else "Hidden",
            "Enabled" if self.enabled else "Disabled", self.summary)


def source_filters(doc, view):
    return [FilterRow(doc, view, fid) for fid in ordered_filter_ids(view)]


# ============================================================================
# "V/G OVERRIDES FILTERS" - whether a template applies its filters at all
# ============================================================================
def _filters_param_id():
    return DB.ElementId(DB.BuiltInParameter.VIS_GRAPHICS_FILTERS)


def controls_filters(template):
    """True when the template includes V/G Overrides Filters - only then do
    its filters reach the views that use it."""
    try:
        target = _eid_int(_filters_param_id())
        return all(_eid_int(p) != target for p in template.GetNonControlledTemplateParameterIds())
    except Exception:
        return True


def make_control_filters(template):
    """Tick V/G Overrides Filters in the template. True when it changed."""
    if controls_filters(template):
        return False
    target = _eid_int(_filters_param_id())
    keep = List[DB.ElementId]()
    for pid in template.GetNonControlledTemplateParameterIds():
        if _eid_int(pid) != target:
            keep.Add(pid)
    template.SetNonControlledTemplateParameterIds(keep)
    return True


# ============================================================================
# COPYING
# ============================================================================
class TargetReport(object):
    def __init__(self, template):
        self.name = template.Name
        self.added = 0
        self.updated = 0
        self.kept = 0
        self.removed = 0
        self.failed = []            # "filter name: reason"
        self.now_controls = False   # V/G Overrides Filters switched on here

    def text(self):
        parts = []
        for count, label in ((self.added, "added"), (self.updated, "updated"),
                             (self.kept, "already there, kept"), (self.removed, "removed")):
            if count:
                parts.append(u"{} {}".format(count, label))
        line = u"• {}: {}".format(self.name, ", ".join(parts) if parts else "nothing to change")
        if self.now_controls:
            line += u" - now controls V/G Filters"
        for failure in self.failed:
            line += u"\n      ✖ " + failure
        return line


def _filter_name(doc, filter_id):
    try:
        return doc.GetElement(filter_id).Name
    except Exception:
        return "Filter {}".format(_eid_int(filter_id))


def _apply_settings(source, target, filter_id):
    """Give the target the source's overrides, Visible and Enabled for one filter."""
    target.SetFilterOverrides(filter_id, source.GetFilterOverrides(filter_id))
    target.SetFilterVisibility(filter_id, source.GetFilterVisibility(filter_id))
    try:
        target.SetIsFilterEnabled(filter_id, is_filter_enabled(source, filter_id))
    except AttributeError:
        pass                      # before Revit 2021


def copy_into(doc, source, target, filter_ids, mode, control_filters):
    """Copy the chosen filters from source to one target template; runs inside
    the caller's transaction. Returns its TargetReport."""
    report = TargetReport(target)
    wanted = set(_eid_int(f) for f in filter_ids)
    chosen = [fid for fid in ordered_filter_ids(source) if _eid_int(fid) in wanted]
    if mode == MODE_REPLACE:
        for fid in ordered_filter_ids(target):
            try:
                target.RemoveFilter(fid)
                if _eid_int(fid) not in wanted:
                    report.removed += 1
            except Exception as error:
                report.failed.append("{}: could not be removed ({})".format(_filter_name(doc, fid), error))
    present = set(_eid_int(fid) for fid in ordered_filter_ids(target))
    for fid in chosen:
        name = _filter_name(doc, fid)
        try:
            if _eid_int(fid) in present:
                if mode == MODE_ADD:
                    report.kept += 1
                    continue
                _apply_settings(source, target, fid)
                report.updated += 1
            else:
                target.AddFilter(fid)
                _apply_settings(source, target, fid)
                report.added += 1
        except Exception as error:
            report.failed.append("{}: {}".format(name, error))
    if control_filters:
        try:
            report.now_controls = make_control_filters(target)
        except Exception as error:
            report.failed.append("V/G Overrides Filters could not be switched on ({})".format(error))
    return report


def copy_filters(doc, source, targets, filter_ids, mode, control_filters):
    """Copy to every target in ONE transaction (one Undo). [TargetReport]."""
    reports = []
    with revit.Transaction("DQT - Copy Template Filters", doc=doc):
        for target in targets:
            reports.append(copy_into(doc, source, target, filter_ids, mode, control_filters))
    return reports


def removed_by_replace(source, targets, filter_ids):
    """How many filters Replace would take out of the targets - for the
    confirmation before it runs."""
    keep = set(_eid_int(f) for f in filter_ids)
    total = 0
    for target in targets:
        total += sum(1 for fid in ordered_filter_ids(target) if _eid_int(fid) not in keep)
    return total


# ============================================================================
# WINDOW
# ============================================================================
MAIN_XAML = """
<Window xmlns="http://schemas.microsoft.com/winfx/2006/xaml/presentation"
        xmlns:x="http://schemas.microsoft.com/winfx/2006/xaml"
        Title="Copy Template Filters - DQT" Height="760" Width="1100"
        MinHeight="560" MinWidth="860"
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
            <RowDefinition Height="Auto"/>
        </Grid.RowDefinitions>

        <!-- Header -->
        <Border Grid.Row="0" Background="#F0CC88" BorderBrush="#D4B87A" BorderThickness="0,0,0,2"
                CornerRadius="5" Padding="14,10" Margin="0,0,0,10">
            <Grid>
                <StackPanel>
                    <TextBlock Text="Copy Template Filters" FontSize="17" FontWeight="Bold" Foreground="#5D4E37"/>
                    <TextBlock Text="Copy view filters - with Visible, Enabled and graphic overrides - from one view template to others"
                               FontSize="10" Foreground="#5D4E37" Margin="0,2,0,0"/>
                </StackPanel>
                <Button x:Name="btnHelp" Content="? Help" Style="{StaticResource PlainButton}"
                        HorizontalAlignment="Right" VerticalAlignment="Center" Margin="0"/>
            </Grid>
        </Border>

        <!-- Source | Targets -->
        <Grid Grid.Row="1">
            <Grid.ColumnDefinitions>
                <ColumnDefinition Width="*"/>
                <ColumnDefinition Width="12"/>
                <ColumnDefinition Width="*"/>
            </Grid.ColumnDefinitions>

            <Border Grid.Column="0" BorderBrush="#D4B87A" BorderThickness="1" CornerRadius="4" Padding="10">
                <Grid>
                    <Grid.RowDefinitions>
                        <RowDefinition Height="Auto"/>
                        <RowDefinition Height="Auto"/>
                        <RowDefinition Height="Auto"/>
                        <RowDefinition Height="*"/>
                    </Grid.RowDefinitions>
                    <TextBlock Grid.Row="0" Text="1. SOURCE VIEW TEMPLATE" FontSize="11" FontWeight="Bold" Foreground="#5D4E37" Margin="0,0,0,6"/>
                    <ComboBox Grid.Row="1" x:Name="cmbSource" Padding="6,4" Margin="0,0,0,10"/>
                    <Grid Grid.Row="2" Margin="0,0,0,6">
                        <TextBlock x:Name="txtSourceCount" Text="FILTERS TO COPY" FontSize="11" FontWeight="Bold" Foreground="#5D4E37" VerticalAlignment="Center"/>
                        <StackPanel Orientation="Horizontal" HorizontalAlignment="Right">
                            <Button x:Name="btnFiltersAll" Content="All" Style="{StaticResource PlainButton}"/>
                            <Button x:Name="btnFiltersNone" Content="None" Style="{StaticResource PlainButton}" Margin="0"/>
                        </StackPanel>
                    </Grid>
                    <Border Grid.Row="3" BorderBrush="#E0E0E0" BorderThickness="1">
                        <ScrollViewer VerticalScrollBarVisibility="Auto">
                            <StackPanel x:Name="pnlFilters" Margin="8,6"/>
                        </ScrollViewer>
                    </Border>
                </Grid>
            </Border>

            <Border Grid.Column="2" BorderBrush="#D4B87A" BorderThickness="1" CornerRadius="4" Padding="10">
                <Grid>
                    <Grid.RowDefinitions>
                        <RowDefinition Height="Auto"/>
                        <RowDefinition Height="Auto"/>
                        <RowDefinition Height="Auto"/>
                        <RowDefinition Height="*"/>
                    </Grid.RowDefinitions>
                    <TextBlock Grid.Row="0" Text="2. TARGET VIEW TEMPLATES" FontSize="11" FontWeight="Bold" Foreground="#5D4E37" Margin="0,0,0,6"/>
                    <TextBox Grid.Row="1" x:Name="txtSearch" Padding="6,4" Margin="0,0,0,10" ToolTip="Filter the list by name or view type"/>
                    <Grid Grid.Row="2" Margin="0,0,0,6">
                        <TextBlock x:Name="txtTargetCount" Text="0 ticked" FontSize="11" FontWeight="Bold" Foreground="#5D4E37" VerticalAlignment="Center"/>
                        <StackPanel Orientation="Horizontal" HorizontalAlignment="Right">
                            <Button x:Name="btnTargetsAll" Content="All shown" Style="{StaticResource PlainButton}"/>
                            <Button x:Name="btnTargetsNone" Content="None" Style="{StaticResource PlainButton}" Margin="0"/>
                        </StackPanel>
                    </Grid>
                    <Border Grid.Row="3" BorderBrush="#E0E0E0" BorderThickness="1">
                        <ScrollViewer VerticalScrollBarVisibility="Auto">
                            <StackPanel x:Name="pnlTargets" Margin="8,6"/>
                        </ScrollViewer>
                    </Border>
                </Grid>
            </Border>
        </Grid>

        <!-- How -->
        <Border Grid.Row="2" BorderBrush="#D4B87A" BorderThickness="1" CornerRadius="4" Padding="10" Margin="0,10,0,0">
            <StackPanel>
                <TextBlock Text="3. HOW TO COPY" FontSize="11" FontWeight="Bold" Foreground="#5D4E37" Margin="0,0,0,6"/>
                <RadioButton x:Name="rbMerge" GroupName="mode" IsChecked="True" Margin="0,2"
                             Content="Add &amp; update - add the filters a target lacks; a filter it already has takes the source's settings"/>
                <RadioButton x:Name="rbAdd" GroupName="mode" Margin="0,2"
                             Content="Add only - add the filters a target lacks; leave the ones it already has as they are"/>
                <RadioButton x:Name="rbReplace" GroupName="mode" Margin="0,2"
                             Content="Replace - make the target's filters identical to the ticked ones, same order (its other filters are removed)"/>
                <CheckBox x:Name="chkControl" IsChecked="True" Margin="0,8,0,0"
                          Content="Switch on &quot;V/G Overrides Filters&quot; in targets that do not control it (otherwise their filters do not reach their views)"/>
            </StackPanel>
        </Border>

        <!-- Status + actions -->
        <Grid Grid.Row="3" Margin="0,10,0,0">
            <TextBlock x:Name="txtStatus" FontSize="11" Foreground="#888888" VerticalAlignment="Center" TextWrapping="Wrap" Margin="0,0,220,0"/>
            <StackPanel Orientation="Horizontal" HorizontalAlignment="Right">
                <Button x:Name="btnCopy" Content="Copy Filters" Style="{StaticResource DqtButton}"/>
                <Button x:Name="btnClose" Content="Close" Style="{StaticResource PlainButton}" Margin="6,0,0,0"/>
            </StackPanel>
        </Grid>

        <!-- Footer -->
        <Border Grid.Row="4" Background="#F0CC88" CornerRadius="3" Padding="8,5" Margin="0,10,0,0">
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
    head.Foreground = _brush("#333333")
    head.TextWrapping = TextWrapping.Wrap
    panel.Children.Add(head)
    sub = WPFControls.TextBlock()
    sub.Text = detail
    sub.FontSize = 10
    sub.Foreground = _brush(detail_color)
    sub.TextWrapping = TextWrapping.Wrap
    panel.Children.Add(sub)
    return panel


class CopyFiltersWindow(WPFWindow):
    def __init__(self, doc):
        WPFWindow.__init__(self, MAIN_XAML, literal_string=True)
        self.doc = doc
        self.templates = view_templates(doc)
        self.filter_boxes = []        # (FilterRow, CheckBox)
        self.target_boxes = []        # (template, CheckBox)
        self.ticked_targets = set()   # template id ints - survive searching

        for template in self.templates:
            self.cmbSource.Items.Add(u"{}  ·  {}".format(template.Name, pretty_view_type(template)))
        self.cmbSource.SelectionChanged += self.on_source_changed
        self.txtSearch.TextChanged += self.on_search
        self.btnFiltersAll.Click += lambda s, e: self._tick_filters(True)
        self.btnFiltersNone.Click += lambda s, e: self._tick_filters(False)
        self.btnTargetsAll.Click += lambda s, e: self._tick_targets(True)
        self.btnTargetsNone.Click += lambda s, e: self._tick_targets(False)
        self.btnCopy.Click += self.on_copy
        self.btnClose.Click += lambda s, e: self.Close()
        self.btnHelp.Click += self.on_help

        if self.templates:
            self.cmbSource.SelectedIndex = 0
        else:
            self.txtStatus.Text = "This model has no view templates that can carry filters."

    # ---- source ----
    @property
    def source(self):
        index = self.cmbSource.SelectedIndex
        return self.templates[index] if 0 <= index < len(self.templates) else None

    def on_source_changed(self, sender, args):
        # read the old target boxes BEFORE dropping the new source's tick,
        # or re-reading them would tick it again
        self._remember_ticks()
        self.target_boxes = []
        if self.source is not None:
            self.ticked_targets.discard(_eid_int(self.source.Id))
        self._fill_filters()
        self._fill_targets()

    def _fill_filters(self):
        self.pnlFilters.Children.Clear()
        self.filter_boxes = []
        source = self.source
        rows = source_filters(self.doc, source) if source is not None else []
        for row in rows:
            box = WPFControls.CheckBox()
            box.IsChecked = True
            box.Margin = Thickness(0, 3, 0, 3)
            box.Content = _row_content(row.name, row.detail)
            box.Click += self._update_counts
            self.pnlFilters.Children.Add(box)
            self.filter_boxes.append((row, box))
        if not rows:
            note = WPFControls.TextBlock()
            note.Text = "This template has no filters."
            note.Foreground = _brush(GREY)
            self.pnlFilters.Children.Add(note)
        self._update_counts()

    def _tick_filters(self, value):
        for _, box in self.filter_boxes:
            box.IsChecked = value
        self._update_counts()

    def chosen_filter_ids(self):
        return [row.id for row, box in self.filter_boxes if box.IsChecked == True]

    # ---- targets ----
    def on_search(self, sender, args):
        self._fill_targets()

    def _fill_targets(self):
        self._remember_ticks()
        self.pnlTargets.Children.Clear()
        self.target_boxes = []
        source = self.source
        text = (self.txtSearch.Text or "").strip().lower()
        for template in self.templates:
            if source is not None and _eid_int(template.Id) == _eid_int(source.Id):
                continue
            kind = pretty_view_type(template)
            if text and text not in template.Name.lower() and text not in kind.lower():
                continue
            count = len(ordered_filter_ids(template))
            controls = controls_filters(template)
            detail = u"{}  ·  {} filter(s)".format(kind, count)
            if not controls:
                detail += u"  ·  does not control V/G Filters"
            box = WPFControls.CheckBox()
            box.IsChecked = _eid_int(template.Id) in self.ticked_targets
            box.Margin = Thickness(0, 3, 0, 3)
            box.Content = _row_content(template.Name, detail, GREY if controls else WARN)
            box.Click += self._update_counts
            self.pnlTargets.Children.Add(box)
            self.target_boxes.append((template, box))
        self._update_counts()

    def _remember_ticks(self):
        for template, box in self.target_boxes:
            key = _eid_int(template.Id)
            if box.IsChecked == True:
                self.ticked_targets.add(key)
            else:
                self.ticked_targets.discard(key)

    def _tick_targets(self, value):
        for _, box in self.target_boxes:
            box.IsChecked = value
        self._update_counts()

    def chosen_targets(self):
        self._remember_ticks()
        source_key = _eid_int(self.source.Id) if self.source is not None else None
        return [t for t in self.templates
                if _eid_int(t.Id) in self.ticked_targets and _eid_int(t.Id) != source_key]

    def _update_counts(self, *args):
        chosen = len(self.chosen_filter_ids())
        self.txtSourceCount.Text = "FILTERS TO COPY ({} of {} ticked)".format(chosen, len(self.filter_boxes))
        self.txtTargetCount.Text = "{} ticked".format(len(self.chosen_targets()))

    # ---- copy ----
    def mode(self):
        if self.rbReplace.IsChecked == True:
            return MODE_REPLACE
        if self.rbAdd.IsChecked == True:
            return MODE_ADD
        return MODE_MERGE

    def on_copy(self, sender, args):
        source = self.source
        filter_ids = self.chosen_filter_ids()
        targets = self.chosen_targets()
        mode = self.mode()
        if source is None:
            return
        if not filter_ids and mode != MODE_REPLACE:
            forms.alert("Tick at least one filter to copy.", title=TITLE)
            return
        if not targets:
            forms.alert("Tick at least one target view template.", title=TITLE)
            return
        if mode == MODE_REPLACE:
            removing = removed_by_replace(source, targets, filter_ids)
            question = ("Replace makes the filters of {} template(s) identical to the {} "
                        "ticked filter(s) of \"{}\".".format(len(targets), len(filter_ids), source.Name))
            if removing:
                question += "\n\n{} filter(s) not in that list will be removed from them.".format(removing)
            question += "\n\nContinue? (Undo brings everything back.)"
            if not forms.alert(question, title=TITLE, yes=True, no=True):
                return
        try:
            reports = copy_filters(self.doc, source, targets, filter_ids, mode,
                                   self.chkControl.IsChecked == True)
        except Exception as error:
            forms.alert("The filters could not be copied - nothing was changed.\n\n{}".format(error),
                        title=TITLE)
            return
        self._fill_targets()
        failures = sum(len(r.failed) for r in reports)
        self.txtStatus.Text = u"Copied from \"{}\" to {} template(s) at {}{}.".format(
            source.Name, len(reports), datetime.datetime.now().strftime("%H:%M"),
            " - {} problem(s), see the summary".format(failures) if failures else "")
        forms.alert(u"Filters copied from \"{}\":\n\n{}".format(
            source.Name, "\n".join(r.text() for r in reports)), title=TITLE)

    def on_help(self, sender, args):
        if _open_help_page(HELP_PAGE):
            return
        forms.alert(
            "Copy Template Filters\n\n"
            "1. Pick the SOURCE view template - its filters are listed with "
            "Visible / Hidden, Enabled / Disabled and what their overrides "
            "change. Tick the ones to copy.\n"
            "2. Tick the TARGET view templates (search narrows the list; "
            "ticks are kept while you search).\n"
            "3. Choose how: Add & update, Add only, or Replace (asks first).\n"
            "   Keep 'Switch on V/G Overrides Filters' ticked so the copied "
            "filters reach the views that use those templates.\n"
            "4. Copy Filters - one Undo. A summary lists, per template, what "
            "was added, updated, kept or removed.",
            title=TITLE)


# ============================================================================
# MAIN
# ============================================================================
if __name__ == "__main__":
    try:
        if not revit.doc or revit.doc.IsFamilyDocument:
            forms.alert("Open a project first.", title=TITLE)
        else:
            CopyFiltersWindow(revit.doc).ShowDialog()
    except Exception as ex:
        forms.alert("Error: {}".format(ex), title=TITLE)
