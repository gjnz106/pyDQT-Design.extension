# -*- coding: utf-8 -*-
"""
In-Place Volume Writer v1.0 - DQT
Writes each In-Place family instance's computed volume into a selected
instance parameter - e.g. for rock armour / precast unit BOQ takeoff.

Workflow:
  1. Uses the In-Place elements in the current selection, or every
     In-Place instance in the document if nothing is selected
  2. User searches and selects the target parameter
  3. Tool writes the computed volume (m3) into the selected parameter

Copyright (c) 2026 Dang Quoc Truong (DQT)
All rights reserved.
"""

__title__ = "InPlace\nVolume"
__author__ = "Dang Quoc Truong (DQT)"
__doc__ = "Write each In-Place instance's computed volume into a selected parameter."

import clr
clr.AddReference('RevitAPI')
clr.AddReference('RevitAPIUI')
clr.AddReference('PresentationFramework')
clr.AddReference('PresentationCore')
clr.AddReference('WindowsBase')

import System
from System.IO import MemoryStream
from System.Text import Encoding
from System.Windows.Markup import XamlReader
from System.Windows.Controls import ListBoxItem

import Autodesk.Revit.DB as DB
from Autodesk.Revit.DB import (
    FilteredElementCollector, FamilyInstance, BuiltInParameter,
    Transaction, StorageType, UnitUtils
)

try:
    from Autodesk.Revit.DB import UnitTypeId
    HAS_UNIT_TYPE_ID = True
except Exception:
    HAS_UNIT_TYPE_ID = False

doc = __revit__.ActiveUIDocument.Document
uidoc = __revit__.ActiveUIDocument


# ── Unit conversion: internal ft3 -> m3 ──────────────────────────────────────
def ft3_to_m3(value):
    """Convert Revit internal cubic feet to cubic metres."""
    try:
        if HAS_UNIT_TYPE_ID:
            return UnitUtils.ConvertFromInternalUnits(value, DB.UnitTypeId.CubicMeters)
        from Autodesk.Revit.DB import DisplayUnitType
        return UnitUtils.ConvertFromInternalUnits(value, DisplayUnitType.DUT_CUBIC_METERS)
    except Exception:
        return value * 0.0283168466  # fallback constant


def is_inplace_instance(element):
    """The house pattern for detecting In-Place families in this suite - see
    SelectAllInPlaceElements / InPlaceModel."""
    try:
        return bool(element.Symbol and element.Symbol.Family and
                    element.Symbol.Family.IsInPlace)
    except Exception:
        return False


# ── Collect In-Place instances ───────────────────────────────────────────────
def get_inplace_elements():
    """The In-Place instances in the current selection, or every In-Place
    instance in the document if nothing is selected."""
    selected = []
    try:
        for eid in uidoc.Selection.GetElementIds():
            el = doc.GetElement(eid)
            if isinstance(el, FamilyInstance) and is_inplace_instance(el):
                selected.append(el)
    except Exception:
        pass
    if selected:
        return selected

    try:
        return [el for el in
                FilteredElementCollector(doc).OfClass(FamilyInstance).WhereElementIsNotElementType()
                if is_inplace_instance(el)]
    except Exception:
        return []


# ── Get volume from element ──────────────────────────────────────────────────
def get_volume_m3(element):
    """Revit's own computed volume first (a Floor/Wall/Foundation-style
    in-place family may expose one); otherwise sum GetMaterialVolume() across
    every material on the element - the same API Material Takeoff schedules
    use, and the one that gets a multi-material, multi-solid in-place model
    right; otherwise a raw sum of every solid in the geometry, as a last
    resort for an element with no material paint at all."""
    try:
        p = element.get_Parameter(BuiltInParameter.HOST_VOLUME_COMPUTED)
        if p and p.HasValue and p.AsDouble() > 0:
            return ft3_to_m3(p.AsDouble())
    except Exception:
        pass

    try:
        total = 0.0
        for material_id in element.GetMaterialIds(False):
            try:
                total += element.GetMaterialVolume(material_id)
            except Exception:
                pass
        if total > 0:
            return ft3_to_m3(total)
    except Exception:
        pass

    try:
        opts = DB.Options()
        opts.ComputeReferences = False
        geom = element.get_Geometry(opts)
        total = 0.0
        for obj in geom:
            if isinstance(obj, DB.Solid) and obj.Volume > 0:
                total += obj.Volume
            elif isinstance(obj, DB.GeometryInstance):
                for sub in obj.GetInstanceGeometry():
                    if isinstance(sub, DB.Solid) and sub.Volume > 0:
                        total += sub.Volume
        if total > 0:
            return ft3_to_m3(total)
    except Exception:
        pass
    return None


# ── Collect writable instance parameters ─────────────────────────────────────
def get_writable_params(elements):
    """Sorted list of writable instance parameter names found on the elements."""
    param_names = set()
    for el in elements[:20]:  # sample first 20 for speed
        for p in el.Parameters:
            if p.IsReadOnly:
                continue
            if p.StorageType not in (StorageType.Double, StorageType.String):
                continue
            name = p.Definition.Name
            if name:
                param_names.add(name)
    return sorted(param_names)


# ═══════════════════════════════════════════════════════════════════════════
# XAML UI
# ═══════════════════════════════════════════════════════════════════════════
XAML_TEMPLATE = """
<Window
    xmlns="http://schemas.microsoft.com/winfx/2006/xaml/presentation"
    xmlns:x="http://schemas.microsoft.com/winfx/2006/xaml"
    Title="In-Place Volume Writer"
    Width="420"
    SizeToContent="Height"
    MinHeight="480" MaxHeight="720"
    WindowStartupLocation="CenterScreen"
    ResizeMode="NoResize"
    FontFamily="Segoe UI">
  <Window.Resources>
    <Style x:Key="PrimaryBtn" TargetType="Button">
      <Setter Property="Background" Value="%%BTN_BG%%"/>
      <Setter Property="Foreground" Value="White"/>
      <Setter Property="FontSize" Value="13"/>
      <Setter Property="FontWeight" Value="SemiBold"/>
      <Setter Property="Height" Value="38"/>
      <Setter Property="BorderThickness" Value="0"/>
      <Setter Property="Cursor" Value="Hand"/>
      <Setter Property="Template">
        <Setter.Value>
          <ControlTemplate TargetType="Button">
            <Border Background="{TemplateBinding Background}"
                    CornerRadius="5"
                    Padding="12,0">
              <ContentPresenter HorizontalAlignment="Center" VerticalAlignment="Center"/>
            </Border>
          </ControlTemplate>
        </Setter.Value>
      </Setter>
    </Style>
    <Style x:Key="SecondaryBtn" TargetType="Button" BasedOn="{StaticResource PrimaryBtn}">
      <Setter Property="Background" Value="#9E9E9E"/>
    </Style>
  </Window.Resources>

  <Border Background="White">
    <Grid>
      <Grid.RowDefinitions>
        <RowDefinition Height="Auto"/>
        <RowDefinition Height="*"/>
        <RowDefinition Height="Auto"/>
      </Grid.RowDefinitions>

      <!-- HEADER -->
      <Border Grid.Row="0" Background="%%HEADER_BG%%" CornerRadius="0">
        <Grid Margin="16,14,16,14">
          <Grid.ColumnDefinitions>
            <ColumnDefinition Width="*"/>
            <ColumnDefinition Width="Auto"/>
          </Grid.ColumnDefinitions>
          <StackPanel Grid.Column="0" VerticalAlignment="Center">
            <TextBlock Text="In-Place Volume Writer" FontSize="16"
                       FontWeight="Bold" Foreground="%%DARK_ACCENT%%"/>
            <TextBlock x:Name="ScopeText" Text="Write computed volume to a parameter"
                       FontSize="11" Foreground="%%DARK_ACCENT%%" Opacity="0.75" Margin="0,2,0,0"/>
          </StackPanel>
          <Border Grid.Column="1" Background="%%ACCENT%%" CornerRadius="4"
                  Padding="10,4" VerticalAlignment="Center">
            <TextBlock x:Name="ElementCount" Text="0 element(s)"
                       FontSize="11" FontWeight="SemiBold" Foreground="%%DARK_ACCENT%%"/>
          </Border>
        </Grid>
      </Border>

      <!-- BODY -->
      <StackPanel Grid.Row="1" Margin="16,16,16,12">

        <!-- Step 1 label -->
        <Border Background="#F5F5F5" CornerRadius="4" Padding="10,6" Margin="0,0,0,10">
          <TextBlock FontSize="12" Foreground="#555555">
            <Run FontWeight="Bold" Foreground="%%DARK_ACCENT%%">Step 1 — </Run>
            <Run>Search and select the target parameter</Run>
          </TextBlock>
        </Border>

        <!-- Search box -->
        <Grid Margin="0,0,0,8">
          <Grid.ColumnDefinitions>
            <ColumnDefinition Width="Auto"/>
            <ColumnDefinition Width="*"/>
          </Grid.ColumnDefinitions>
          <TextBlock Grid.Column="0" Text="🔍  " FontSize="14"
                     VerticalAlignment="Center" Margin="0,0,4,0"/>
          <TextBox x:Name="SearchBox" Grid.Column="1"
                   Height="32" Padding="8,4"
                   FontSize="12" BorderBrush="%%ACCENT%%"
                   BorderThickness="1.5" VerticalContentAlignment="Center"/>
        </Grid>

        <!-- Parameter list -->
        <Border BorderBrush="#E0E0E0" BorderThickness="1" CornerRadius="4" Height="220">
          <ListBox x:Name="ParamList"
                   BorderThickness="0"
                   ScrollViewer.HorizontalScrollBarVisibility="Disabled"
                   FontSize="12" Padding="2">
            <ListBox.ItemContainerStyle>
              <Style TargetType="ListBoxItem">
                <Setter Property="Padding" Value="10,7"/>
                <Setter Property="HorizontalContentAlignment" Value="Stretch"/>
                <Style.Triggers>
                  <Trigger Property="IsSelected" Value="True">
                    <Setter Property="Background" Value="%%HEADER_BG%%"/>
                    <Setter Property="Foreground" Value="%%DARK_ACCENT%%"/>
                    <Setter Property="FontWeight" Value="SemiBold"/>
                  </Trigger>
                </Style.Triggers>
              </Style>
            </ListBox.ItemContainerStyle>
          </ListBox>
        </Border>

        <!-- Selected label -->
        <Border Background="#FFF8E8" CornerRadius="4" Padding="10,7" Margin="0,8,0,0"
                BorderBrush="%%ACCENT%%" BorderThickness="1">
          <StackPanel Orientation="Horizontal">
            <TextBlock Text="Selected: " FontSize="11" Foreground="#666666"/>
            <TextBlock x:Name="SelectedLabel" Text="(none)"
                       FontSize="11" FontWeight="SemiBold" Foreground="%%DARK_ACCENT%%"/>
          </StackPanel>
        </Border>

        <!-- Step 2 label -->
        <Border Background="#F5F5F5" CornerRadius="4" Padding="10,6" Margin="0,12,0,8">
          <TextBlock FontSize="12" Foreground="#555555">
            <Run FontWeight="Bold" Foreground="%%DARK_ACCENT%%">Step 2 — </Run>
            <Run>Click Write Volume to apply</Run>
          </TextBlock>
        </Border>

        <!-- Status result - always visible after run -->
        <Border x:Name="StatusBorder" CornerRadius="5"
                Padding="12,10" Margin="0,0,0,4" Visibility="Collapsed">
          <StackPanel>
            <TextBlock x:Name="StatusTitle" FontSize="13" FontWeight="Bold"
                       Margin="0,0,0,4"/>
            <TextBlock x:Name="StatusText" FontSize="11"
                       Foreground="#333333" TextWrapping="Wrap" LineHeight="18"/>
          </StackPanel>
        </Border>

      </StackPanel>

      <!-- FOOTER -->
      <Border Grid.Row="2" BorderBrush="#E0E0E0" BorderThickness="0,1,0,0" Padding="16,10">
        <Grid>
          <Grid.ColumnDefinitions>
            <ColumnDefinition Width="*"/>
            <ColumnDefinition Width="Auto"/>
            <ColumnDefinition Width="8"/>
            <ColumnDefinition Width="Auto"/>
          </Grid.ColumnDefinitions>
          <TextBlock Grid.Column="0" Text="Dang Quoc Truong - DQT (c) 2026"
                     FontSize="10" Foreground="#AAAAAA" VerticalAlignment="Center"/>
          <Button x:Name="RunButton" Grid.Column="1"
                  Content="▶  Write Volume" Width="140"
                  Style="{StaticResource PrimaryBtn}"/>
          <Button x:Name="CloseButton" Grid.Column="3"
                  Content="Close" Width="80"
                  Style="{StaticResource SecondaryBtn}"/>
        </Grid>
      </Border>

    </Grid>
  </Border>
</Window>
""".replace("%%HEADER_BG%%", "#F0CC88") \
   .replace("%%ACCENT%%",    "#D4B87A") \
   .replace("%%DARK_ACCENT%%","#5D4E37") \
   .replace("%%BTN_BG%%",    "#5D4E37")


# ═══════════════════════════════════════════════════════════════════════════
# Main dialog class
# ═══════════════════════════════════════════════════════════════════════════
class InPlaceVolumeDialog(object):

    def __init__(self, elements, used_selection):
        self.elements = elements
        self.used_selection = used_selection
        self.all_params = get_writable_params(self.elements)
        self.selected_param = None

        stream = MemoryStream(Encoding.UTF8.GetBytes(XAML_TEMPLATE))
        self.window = XamlReader.Load(stream)

        # Controls
        self._count_lbl = self.window.FindName("ElementCount")
        self._scope_lbl = self.window.FindName("ScopeText")
        self._search_box = self.window.FindName("SearchBox")
        self._param_list = self.window.FindName("ParamList")
        self._sel_label = self.window.FindName("SelectedLabel")
        self._status_bdr = self.window.FindName("StatusBorder")
        self._status_title = self.window.FindName("StatusTitle")
        self._status_txt = self.window.FindName("StatusText")
        self._run_btn = self.window.FindName("RunButton")
        self._close_btn = self.window.FindName("CloseButton")

        # Init
        self._count_lbl.Text = "{0} element(s)".format(len(self.elements))
        self._scope_lbl.Text = ("Using the In-Place elements in your selection"
                                if used_selection else
                                "Using every In-Place instance in the document")
        self._populate_list(self.all_params)

        # Events
        self._search_box.TextChanged += self._on_search
        self._param_list.MouseDoubleClick += self._on_list_double_click
        self._param_list.SelectionChanged += self._on_selection_changed
        self._run_btn.Click += self._on_run
        self._close_btn.Click += self._on_close

    # ── List helpers ──────────────────────────────────────────────────────
    def _populate_list(self, names):
        self._param_list.Items.Clear()
        for n in names:
            item = ListBoxItem()
            item.Content = n
            self._param_list.Items.Add(item)
        if self._param_list.Items.Count > 0:
            self._param_list.SelectedIndex = 0

    def _on_search(self, sender, e):
        query = self._search_box.Text.strip().lower()
        filtered = [n for n in self.all_params if query in n.lower()] if query else self.all_params
        self._populate_list(filtered)

    def _on_selection_changed(self, sender, e):
        item = self._param_list.SelectedItem
        if item:
            self.selected_param = item.Content
            self._sel_label.Text = self.selected_param
        else:
            self.selected_param = None
            self._sel_label.Text = "(none)"

    def _on_list_double_click(self, sender, e):
        self._on_run(None, None)

    # ── Status helper ─────────────────────────────────────────────────────
    def _show_status(self, title, detail, success=True):
        conv = System.Windows.Media.BrushConverter()
        if success:
            self._status_bdr.Background = conv.ConvertFromString("#E8F5E9")
            self._status_bdr.SetValue(
                System.Windows.Controls.Border.BorderBrushProperty,
                conv.ConvertFromString("#A5D6A7"))
            self._status_bdr.SetValue(
                System.Windows.Controls.Border.BorderThicknessProperty,
                System.Windows.Thickness(1))
            self._status_title.Foreground = conv.ConvertFromString("#1B5E20")
        else:
            self._status_bdr.Background = conv.ConvertFromString("#FFF3E0")
            self._status_bdr.SetValue(
                System.Windows.Controls.Border.BorderBrushProperty,
                conv.ConvertFromString("#FFCC80"))
            self._status_bdr.SetValue(
                System.Windows.Controls.Border.BorderThicknessProperty,
                System.Windows.Thickness(1))
            self._status_title.Foreground = conv.ConvertFromString("#E65100")

        self._status_title.Text = title
        self._status_txt.Text = detail
        self._status_bdr.Visibility = System.Windows.Visibility.Visible
        self.window.SizeToContent = System.Windows.SizeToContent.Height

    # ── Run logic ─────────────────────────────────────────────────────────
    def _on_run(self, sender, e):
        if not self.selected_param:
            self._show_status(
                "⚠  No parameter selected",
                "Please select a target parameter from the list above.",
                success=False)
            return
        if not self.elements:
            self._show_status(
                "⚠  No In-Place elements found",
                "No In-Place family instances were found.",
                success=False)
            return

        target_param_name = self.selected_param
        updated = 0
        skipped_no_vol = 0
        skipped_no_param = 0
        skipped_readonly = 0
        errors = 0

        with Transaction(doc, "DQT - Write In-Place Volume") as t:
            t.Start()
            for el in self.elements:
                try:
                    vol_m3 = get_volume_m3(el)
                    if vol_m3 is None or vol_m3 <= 0:
                        skipped_no_vol += 1
                        continue

                    p = el.LookupParameter(target_param_name)
                    if p is None:
                        skipped_no_param += 1
                        continue
                    if p.IsReadOnly:
                        skipped_readonly += 1
                        continue

                    if p.StorageType == StorageType.Double:
                        try:
                            is_volume_spec = False
                            try:
                                spec_id = p.Definition.GetSpecTypeId()
                                if HAS_UNIT_TYPE_ID:
                                    is_volume_spec = (spec_id == DB.SpecTypeId.Volume)
                            except Exception:
                                pass

                            if is_volume_spec:
                                try:
                                    if HAS_UNIT_TYPE_ID:
                                        internal_val = UnitUtils.ConvertToInternalUnits(
                                            vol_m3, DB.UnitTypeId.CubicMeters)
                                    else:
                                        from Autodesk.Revit.DB import DisplayUnitType
                                        internal_val = UnitUtils.ConvertToInternalUnits(
                                            vol_m3, DisplayUnitType.DUT_CUBIC_METERS)
                                    p.Set(internal_val)
                                except Exception:
                                    p.Set(vol_m3)
                            else:
                                p.Set(vol_m3)
                        except Exception:
                            p.Set(vol_m3)

                    elif p.StorageType == StorageType.String:
                        p.Set(str(round(vol_m3, 4)))

                    else:
                        skipped_no_param += 1
                        continue

                    updated += 1

                except Exception:
                    errors += 1

            t.Commit()

        success = updated > 0
        if success:
            title = "✅  Completed — {0} of {1} element(s) updated".format(
                updated, len(self.elements))
        else:
            title = "⚠  No elements were updated"

        detail_lines = []
        if skipped_no_vol > 0:
            detail_lines.append("• {0} skipped — volume = 0 or unavailable".format(skipped_no_vol))
        if skipped_no_param > 0:
            detail_lines.append(
                "• {0} skipped — parameter \"{1}\" not found on element".format(
                    skipped_no_param, target_param_name))
        if skipped_readonly > 0:
            detail_lines.append("• {0} skipped — parameter is read-only".format(skipped_readonly))
        if errors > 0:
            detail_lines.append("• {0} error(s) encountered during write".format(errors))
        if not detail_lines:
            detail_lines.append("All elements processed successfully.")

        self._show_status(title, "\n".join(detail_lines), success=success)

    def _on_close(self, sender, e):
        self.window.Close()

    def show(self):
        self.window.ShowDialog()


# ═══════════════════════════════════════════════════════════════════════════
# Entry point
# ═══════════════════════════════════════════════════════════════════════════
def run():
    selected_ids = []
    try:
        selected_ids = list(uidoc.Selection.GetElementIds())
    except Exception:
        pass
    used_selection = bool(selected_ids) and any(
        isinstance(doc.GetElement(eid), FamilyInstance) and
        is_inplace_instance(doc.GetElement(eid))
        for eid in selected_ids)

    elements = get_inplace_elements()
    if not elements:
        from pyrevit import forms
        forms.alert(
            "No In-Place family instances were found (in your selection or "
            "in the document).",
            title="In-Place Volume Writer",
            warn_icon=True)
        return

    dialog = InPlaceVolumeDialog(elements, used_selection)
    dialog.show()


run()
