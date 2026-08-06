# -*- coding: utf-8 -*-
"""
Sheet Manager - Batch Rename Dialog

Brings Sheet Manager's rename up to parity with the other rename tools in the
suite (ViewManager_Advanced, ViewTemplate, Line Style Edit, ...): find/replace,
prefix, suffix, case change and a live before/after preview.

Sheet-specific additions over the view-based dialogs:
  - sheets carry TWO editable fields (Number and Name), so the rules are
    applied to a user-chosen target instead of guessing.
  - sheet numbers must be unique in Revit, so the preview flags collisions
    before they reach Apply.

This dialog only mutates the SheetItemModel rows and the change tracker; it
never writes to Revit. The existing Apply button on the main window remains
the single point that commits to the document.

Copyright (c) Dang Quoc Truong (DQT)
"""

from System.Windows import (Window, MessageBox, MessageBoxButton, MessageBoxImage,
                            Thickness, GridLength, GridUnitType, FontWeights,
                            HorizontalAlignment, VerticalAlignment, TextWrapping,
                            CornerRadius)
from System.Windows.Controls import (Grid, StackPanel, TextBlock, TextBox, Button,
                                     ComboBox, Border, DataGrid, DataGridTextColumn,
                                     DataGridLength, DataGridLengthUnitType,
                                     DataGridHeadersVisibility, RowDefinition,
                                     Orientation)
from System.Windows.Data import Binding
from System.Windows.Input import Cursors
from System.Windows.Media import BrushConverter
from System.Collections.ObjectModel import ObservableCollection
import System


# --- DQT palette (cream track, matching the Sheet Manager main window) ------
HEADER_BG = "#F0CC88"
MAIN_BG = "#FEF8E7"
PANEL_BG = "#FFFFFF"
ACCENT = "#D4B87A"
DARK_ACCENT = "#5D4E37"
TEXT = "#333333"
BORDER = "#E0E0E0"
ROW_ALT = "#FAF3E0"
# Sheet Manager's existing alert red (the Delete button uses it) rather than a
# new hex -- the palette has no dedicated warning token.
WARN = "#FF6B6B"

FOOTER_TEXT = "Dang Quoc Truong - DQT (c) 2026"

# Which field(s) the rename rules apply to.
TARGET_NUMBER = "Sheet Number"
TARGET_NAME = "Sheet Name"
TARGET_BOTH = "Both"

CASE_NONE = "No Change"
CASE_UPPER = "UPPERCASE"
CASE_LOWER = "lowercase"
CASE_TITLE = "Title Case"

PREVIEW_LIMIT = 200


def brush(hex_string):
    """SolidColorBrush from hex. BrushConverter is the IronPython-safe route."""
    return BrushConverter().ConvertFromString(hex_string)


class RenamePreviewItem(object):
    """Row model for the preview grid. Plain attributes bind fine in IronPython."""

    def __init__(self, old_number, new_number, old_name, new_name, note):
        self.old_number = old_number
        self.new_number = new_number
        self.old_name = old_name
        self.new_name = new_name
        self.note = note


class SheetBatchRenameDialog(Window):
    """Batch rename dialog for sheets, with live preview."""

    def __init__(self, sheet_models, doc, all_models=None):
        """
        :param sheet_models: SheetItemModel rows the user selected (renamed)
        :param doc: Revit document
        :param all_models: every row in the grid, used for uniqueness checks
                           against sheets the user did NOT select.
        """
        self.sheet_models = list(sheet_models)
        self.doc = doc
        self.all_models = list(all_models) if all_models else list(sheet_models)
        self.preview_items = ObservableCollection[object]()
        self.result_applied = False
        self._building = True

        self.Title = "Batch Rename Sheets - Dang Quoc Truong (DQT)"
        self.Width = 900
        self.Height = 650
        # CenterScreen, not CenterOwner: no Owner is set on this dialog, matching
        # the other Sheet Manager dialogs.
        self.WindowStartupLocation = System.Windows.WindowStartupLocation.CenterScreen
        self.Background = brush(MAIN_BG)

        self._build_ui()
        self._building = False
        self._update_preview()

    # ------------------------------------------------------------------ UI --
    def _build_ui(self):
        root = Grid()
        root.RowDefinitions.Add(RowDefinition(Height=GridLength.Auto))   # header
        root.RowDefinitions.Add(RowDefinition(Height=GridLength.Auto))   # options
        root.RowDefinitions.Add(RowDefinition(Height=GridLength(1, GridUnitType.Star)))  # preview
        root.RowDefinitions.Add(RowDefinition(Height=GridLength.Auto))   # buttons
        root.RowDefinitions.Add(RowDefinition(Height=GridLength.Auto))   # footer

        header = self._create_header()
        Grid.SetRow(header, 0)
        root.Children.Add(header)

        options = self._create_options()
        Grid.SetRow(options, 1)
        root.Children.Add(options)

        preview = self._create_preview()
        Grid.SetRow(preview, 2)
        root.Children.Add(preview)

        buttons = self._create_buttons()
        Grid.SetRow(buttons, 3)
        root.Children.Add(buttons)

        footer = self._create_footer()
        Grid.SetRow(footer, 4)
        root.Children.Add(footer)

        self.Content = root

    def _create_header(self):
        border = Border()
        border.Background = brush(HEADER_BG)
        border.BorderBrush = brush(ACCENT)
        border.BorderThickness = Thickness(0, 0, 0, 2)
        border.Padding = Thickness(16, 12, 16, 12)

        stack = StackPanel()

        title = TextBlock()
        title.Text = "Batch Rename Sheets"
        title.FontSize = 18
        title.FontWeight = FontWeights.Bold
        title.Foreground = brush(DARK_ACCENT)

        subtitle = TextBlock()
        subtitle.Text = "{0} sheet(s) selected".format(len(self.sheet_models))
        subtitle.FontSize = 12
        subtitle.Foreground = brush(DARK_ACCENT)
        subtitle.Margin = Thickness(0, 2, 0, 0)

        stack.Children.Add(title)
        stack.Children.Add(subtitle)
        border.Child = stack
        return border

    def _labelled_row(self, label_text, control, label_width=95, control_width=210):
        """One 'Label: [control]' pair as a horizontal StackPanel."""
        panel = StackPanel()
        panel.Orientation = Orientation.Horizontal
        panel.Margin = Thickness(0, 0, 20, 8)

        lbl = TextBlock()
        lbl.Text = label_text
        lbl.Width = label_width
        lbl.Foreground = brush(DARK_ACCENT)
        lbl.FontWeight = FontWeights.SemiBold
        lbl.VerticalAlignment = VerticalAlignment.Center

        control.Width = control_width
        control.Height = 24

        panel.Children.Add(lbl)
        panel.Children.Add(control)
        return panel

    def _create_options(self):
        border = Border()
        border.Background = brush(PANEL_BG)
        border.BorderBrush = brush(ACCENT)
        border.BorderThickness = Thickness(1)
        border.CornerRadius = CornerRadius(4)
        border.Padding = Thickness(14)
        border.Margin = Thickness(16, 14, 16, 8)

        outer = StackPanel()

        # --- row 1: target + case ---
        row1 = StackPanel()
        row1.Orientation = Orientation.Horizontal

        self.target_combo = ComboBox()
        self.target_combo.Items.Add(TARGET_NUMBER)
        self.target_combo.Items.Add(TARGET_NAME)
        self.target_combo.Items.Add(TARGET_BOTH)
        self.target_combo.SelectedIndex = 2  # Both -- matches the old behaviour
        self.target_combo.SelectionChanged += self._on_option_changed

        self.case_combo = ComboBox()
        self.case_combo.Items.Add(CASE_NONE)
        self.case_combo.Items.Add(CASE_UPPER)
        self.case_combo.Items.Add(CASE_LOWER)
        self.case_combo.Items.Add(CASE_TITLE)
        self.case_combo.SelectedIndex = 0
        self.case_combo.SelectionChanged += self._on_option_changed

        row1.Children.Add(self._labelled_row("Apply to:", self.target_combo))
        row1.Children.Add(self._labelled_row("Change Case:", self.case_combo, 95, 160))

        # --- row 2: find / replace ---
        row2 = StackPanel()
        row2.Orientation = Orientation.Horizontal

        self.find_box = TextBox()
        self.find_box.TextChanged += self._on_option_changed

        self.replace_box = TextBox()
        self.replace_box.TextChanged += self._on_option_changed

        row2.Children.Add(self._labelled_row("Find:", self.find_box))
        row2.Children.Add(self._labelled_row("Replace with:", self.replace_box))

        # --- row 3: prefix / suffix ---
        row3 = StackPanel()
        row3.Orientation = Orientation.Horizontal

        self.prefix_box = TextBox()
        self.prefix_box.TextChanged += self._on_option_changed

        self.suffix_box = TextBox()
        self.suffix_box.TextChanged += self._on_option_changed

        row3.Children.Add(self._labelled_row("Add Prefix:", self.prefix_box))
        row3.Children.Add(self._labelled_row("Add Suffix:", self.suffix_box))

        hint = TextBlock()
        hint.Text = ("Rules apply in order: find/replace, then prefix, then suffix, "
                     "then case. Changes are staged - press Apply on the main "
                     "window to write them to the model.")
        hint.TextWrapping = TextWrapping.Wrap
        hint.FontSize = 11
        hint.Foreground = brush(TEXT)
        hint.Margin = Thickness(0, 4, 0, 0)

        outer.Children.Add(row1)
        outer.Children.Add(row2)
        outer.Children.Add(row3)
        outer.Children.Add(hint)

        border.Child = outer
        return border

    def _add_column(self, header, binding_path, star):
        col = DataGridTextColumn()
        col.Header = header
        col.Binding = Binding(binding_path)
        col.Width = DataGridLength(star, DataGridLengthUnitType.Star)
        self.preview_grid.Columns.Add(col)

    def _create_preview(self):
        border = Border()
        border.Background = brush(PANEL_BG)
        border.BorderBrush = brush(ACCENT)
        border.BorderThickness = Thickness(1)
        border.CornerRadius = CornerRadius(4)
        border.Padding = Thickness(10)
        border.Margin = Thickness(16, 0, 16, 8)

        grid = Grid()
        grid.RowDefinitions.Add(RowDefinition(Height=GridLength.Auto))
        grid.RowDefinitions.Add(RowDefinition(Height=GridLength(1, GridUnitType.Star)))

        self.preview_label = TextBlock()
        self.preview_label.Text = "Preview"
        self.preview_label.FontWeight = FontWeights.Bold
        self.preview_label.Foreground = brush(DARK_ACCENT)
        self.preview_label.Margin = Thickness(0, 0, 0, 8)
        Grid.SetRow(self.preview_label, 0)

        # Read-only on purpose: editable DataGrid columns crash IronPython.
        self.preview_grid = DataGrid()
        self.preview_grid.IsReadOnly = True
        self.preview_grid.AutoGenerateColumns = False
        self.preview_grid.HeadersVisibility = DataGridHeadersVisibility.Column
        self.preview_grid.ItemsSource = self.preview_items
        self.preview_grid.Background = brush(PANEL_BG)
        self.preview_grid.RowBackground = brush(PANEL_BG)
        self.preview_grid.AlternatingRowBackground = brush(ROW_ALT)
        self.preview_grid.BorderBrush = brush(BORDER)
        self.preview_grid.BorderThickness = Thickness(1)
        self.preview_grid.HorizontalGridLinesBrush = brush(BORDER)
        self.preview_grid.VerticalGridLinesBrush = brush(BORDER)
        Grid.SetRow(self.preview_grid, 1)

        self._add_column("Number", "old_number", 1)
        self._add_column("New Number", "new_number", 1)
        self._add_column("Name", "old_name", 1.4)
        self._add_column("New Name", "new_name", 1.4)
        self._add_column("Note", "note", 1)

        grid.Children.Add(self.preview_label)
        grid.Children.Add(self.preview_grid)
        border.Child = grid
        return border

    def _create_button(self, text, is_primary):
        btn = Button()
        btn.Content = text
        btn.Width = 130
        btn.Height = 32
        btn.Margin = Thickness(0, 0, 10, 0)
        btn.Foreground = brush(DARK_ACCENT)
        btn.BorderBrush = brush(ACCENT)
        btn.BorderThickness = Thickness(1)
        btn.Background = brush(HEADER_BG if is_primary else PANEL_BG)
        btn.FontWeight = FontWeights.SemiBold
        btn.Cursor = Cursors.Hand
        return btn

    def _create_buttons(self):
        panel = StackPanel()
        panel.Orientation = Orientation.Horizontal
        panel.HorizontalAlignment = HorizontalAlignment.Right
        panel.Margin = Thickness(16, 0, 16, 10)

        self.apply_btn = self._create_button("Stage Rename", True)
        self.apply_btn.Click += self._on_apply

        cancel_btn = self._create_button("Cancel", False)
        cancel_btn.Margin = Thickness(0)
        cancel_btn.Click += self._on_cancel

        panel.Children.Add(self.apply_btn)
        panel.Children.Add(cancel_btn)
        return panel

    def _create_footer(self):
        border = Border()
        border.Background = brush(MAIN_BG)
        border.BorderBrush = brush(BORDER)
        border.BorderThickness = Thickness(0, 1, 0, 0)
        border.Padding = Thickness(12, 6, 16, 6)

        txt = TextBlock()
        txt.Text = FOOTER_TEXT
        txt.FontSize = 11
        txt.Foreground = brush(DARK_ACCENT)
        txt.HorizontalAlignment = HorizontalAlignment.Right

        border.Child = txt
        return border

    # --------------------------------------------------------------- rules --
    def _selected_text(self, combo, fallback):
        return str(combo.SelectedItem) if combo.SelectedItem else fallback

    def _apply_case(self, value):
        case_option = self._selected_text(self.case_combo, CASE_NONE)
        if case_option == CASE_UPPER:
            return value.upper()
        if case_option == CASE_LOWER:
            return value.lower()
        if case_option == CASE_TITLE:
            return value.title()
        return value

    def _transform(self, value):
        """Apply every enabled rule to one string, in a documented order."""
        new_value = value

        find_text = self.find_box.Text
        if find_text:
            new_value = new_value.replace(find_text, self.replace_box.Text)

        if self.prefix_box.Text:
            new_value = self.prefix_box.Text + new_value

        if self.suffix_box.Text:
            new_value = new_value + self.suffix_box.Text

        return self._apply_case(new_value)

    def _compute(self, model):
        """Return (new_number, new_name) for one sheet under the current rules."""
        target = self._selected_text(self.target_combo, TARGET_BOTH)

        new_number = model.sheet_number
        new_name = model.sheet_name

        if target in (TARGET_NUMBER, TARGET_BOTH):
            new_number = self._transform(new_number)
        if target in (TARGET_NAME, TARGET_BOTH):
            new_name = self._transform(new_name)

        return new_number, new_name

    def _compute_all(self):
        """Compute results for every selected sheet and flag number collisions.

        Revit requires unique sheet numbers. A batch rule can easily collapse
        two numbers onto one, which would fail at Apply time with an opaque
        error -- so surface it here instead.

        :return: list of (model, new_number, new_name, note)
        """
        results = []
        for model in self.sheet_models:
            new_number, new_name = self._compute(model)
            results.append([model, new_number, new_name, ""])

        # Numbers held by sheets that are NOT being renamed stay occupied.
        renaming_ids = set(id(m) for m in self.sheet_models)
        taken = {}
        for model in self.all_models:
            if id(model) not in renaming_ids:
                taken[model.sheet_number] = True

        seen = {}
        for row in results:
            new_number = row[1]
            if not new_number:
                row[3] = "Empty number"
            elif new_number in taken:
                row[3] = "Conflicts with existing sheet"
            elif new_number in seen:
                row[3] = "Duplicate in selection"
            else:
                seen[new_number] = True
                if new_number != row[0].sheet_number or row[2] != row[0].sheet_name:
                    row[3] = "Changed"
                else:
                    row[3] = "No change"

        return results

    # -------------------------------------------------------------- events --
    def _on_option_changed(self, sender, args):
        if self._building:
            return
        self._update_preview()

    def _update_preview(self):
        results = self._compute_all()

        self.preview_items.Clear()
        for model, new_number, new_name, note in results[:PREVIEW_LIMIT]:
            self.preview_items.Add(RenamePreviewItem(
                model.sheet_number, new_number,
                model.sheet_name, new_name, note))

        problems = len([r for r in results if r[3] not in ("Changed", "No change")])
        changed = len([r for r in results if r[3] == "Changed"])

        label = "Preview - {0} of {1} will change".format(changed, len(results))
        if problems:
            label += "  |  {0} blocked".format(problems)
        if len(results) > PREVIEW_LIMIT:
            label += "  (showing first {0})".format(PREVIEW_LIMIT)

        self.preview_label.Text = label
        self.preview_label.Foreground = brush(WARN if problems else DARK_ACCENT)

    def _on_apply(self, sender, args):
        results = self._compute_all()

        blocked = [r for r in results if r[3] not in ("Changed", "No change")]
        if blocked:
            MessageBox.Show(
                "{0} sheet(s) would end up with a duplicate or empty number.\n\n"
                "Sheet numbers must be unique in Revit. Adjust the rules so "
                "every number stays unique, then try again.".format(len(blocked)),
                "Cannot Rename", MessageBoxButton.OK, MessageBoxImage.Warning)
            return

        changed = [r for r in results if r[3] == "Changed"]
        if not changed:
            MessageBox.Show("The current rules do not change any sheet.",
                            "Nothing to Rename",
                            MessageBoxButton.OK, MessageBoxImage.Information)
            return

        # Stage onto the row models only. Writing to Revit stays the job of the
        # main window's Apply button, which is the tool's existing contract.
        for model, new_number, new_name, _note in changed:
            model.sheet_number = new_number
            model.sheet_name = new_name

        self.result_applied = True
        self.staged_count = len(changed)
        self.DialogResult = True
        self.Close()

    def _on_cancel(self, sender, args):
        self.DialogResult = False
        self.Close()
