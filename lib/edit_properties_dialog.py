# -*- coding: utf-8 -*-
"""
Edit Properties Dialog for DQT Manager Tools
Flexible configuration-driven dialog for editing element properties

Copyright (c) 2025 Copyright by Dang Quoc Truong (DQT)
All rights reserved.
"""

import clr
clr.AddReference('System')
clr.AddReference('PresentationCore')
clr.AddReference('PresentationFramework')
clr.AddReference('WindowsBase')

from System.Windows import Window, Thickness, WindowStartupLocation, MessageBox, MessageBoxButton, MessageBoxImage
from System.Windows.Controls import (Label, TextBox, ComboBox, CheckBox, Button, 
                                     Border, StackPanel, ScrollViewer, Grid, RowDefinition, ColumnDefinition)
from System.Windows.Media import SolidColorBrush, Brushes
from System.Windows import HorizontalAlignment, VerticalAlignment, FontWeights
from System.Collections.ObjectModel import ObservableCollection
import System

from Autodesk.Revit.DB import Transaction

from config import Colors, Settings, PropertyType


# ============================================================================
# EDIT PROPERTIES DIALOG
# ============================================================================

class EditPropertiesDialog(Window):
    """Flexible dialog for editing element properties
    
    Configuration format:
    {
        'properties': [
            {
                'name': 'PropertyName',
                'label': 'Display Label',
                'type': 'textbox',  # or 'combobox', 'checkbox', 'number', 'readonly'
                'get_current': lambda elem: ...,  # Function to get current value
                'set_value': lambda elem, val: ...,  # Function to set new value
                'get_options': function,  # For combobox (optional)
                'options': [...],  # Static options for combobox (optional)
                'validation': lambda val: ...,  # Validation function (optional)
                'hint': 'Tooltip text'  # Tooltip (optional)
            }
        ]
    }
    """
    
    def __init__(self, doc, selected_items, config, parent_window=None):
        self.doc = doc
        self.selected_items = selected_items
        self.config = config
        self.parent_window = parent_window
        
        # Window properties
        self.Title = "Edit Properties - By DQT"
        self.Width = 500
        self.Height = 600
        self.WindowStartupLocation = WindowStartupLocation.CenterScreen
        self.Background = SolidColorBrush(Colors.DIALOG_BACKGROUND)
        
        # Property controls dictionary
        self.property_controls = {}
        
        # Create UI
        self.create_ui()
        
        # Load current values
        self.load_current_values()
    
    def create_ui(self):
        """Create the user interface"""
        main_grid = Grid()
        
        # Define rows
        main_grid.RowDefinitions.Add(RowDefinition())
        main_grid.RowDefinitions.Add(RowDefinition())
        main_grid.RowDefinitions.Add(RowDefinition())
        
        main_grid.RowDefinitions[0].Height = System.Windows.GridLength(60)
        main_grid.RowDefinitions[1].Height = System.Windows.GridLength(1, System.Windows.GridUnitType.Star)
        main_grid.RowDefinitions[2].Height = System.Windows.GridLength(60)
        
        # Header
        header = self.create_header()
        Grid.SetRow(header, 0)
        main_grid.Children.Add(header)
        
        # Content (scrollable)
        content = self.create_content()
        Grid.SetRow(content, 1)
        main_grid.Children.Add(content)
        
        # Footer (buttons)
        footer = self.create_footer()
        Grid.SetRow(footer, 2)
        main_grid.Children.Add(footer)
        
        self.Content = main_grid
    
    def create_header(self):
        """Create header section"""
        border = Border()
        border.Background = SolidColorBrush(Colors.HEADER_BACKGROUND)
        border.BorderBrush = SolidColorBrush(Colors.HEADER_BORDER)
        border.BorderThickness = Thickness(0, 0, 0, 2)
        
        stack = StackPanel()
        stack.VerticalAlignment = VerticalAlignment.Center
        stack.Margin = Thickness(20, 0, 20, 0)
        
        # Title
        title_label = Label()
        title_label.Content = "EDIT PROPERTIES"
        title_label.FontSize = 18
        title_label.FontWeight = FontWeights.Bold
        title_label.Foreground = SolidColorBrush(Colors.HEADER_TEXT)
        stack.Children.Add(title_label)
        
        # Subtitle
        subtitle_label = Label()
        subtitle_label.Content = "Editing {} item(s)".format(len(self.selected_items))
        subtitle_label.FontSize = 11
        subtitle_label.Foreground = SolidColorBrush(Colors.HEADER_SUBTITLE)
        subtitle_label.Margin = Thickness(0, -5, 0, 0)
        stack.Children.Add(subtitle_label)
        
        border.Child = stack
        return border
    
    def create_content(self):
        """Create scrollable content section with property editors"""
        scroll = ScrollViewer()
        scroll.VerticalScrollBarVisibility = System.Windows.Controls.ScrollBarVisibility.Auto
        scroll.Margin = Thickness(10)
        
        stack = StackPanel()
        stack.Margin = Thickness(10)
        
        # Get properties from config
        properties = self.config.get('properties', [])
        
        if not properties:
            # No properties configured
            label = Label()
            label.Content = "No properties configured for editing."
            label.FontSize = 12
            label.Foreground = Brushes.Gray
            stack.Children.Add(label)
        else:
            # Create editor for each property
            for prop_config in properties:
                editor = self.create_property_editor(prop_config)
                if editor:
                    stack.Children.Add(editor)
                    
                    # Add separator
                    separator = Border()
                    separator.Height = 1
                    separator.Background = SolidColorBrush(Colors.GRID_BORDER)
                    separator.Margin = Thickness(0, 10, 0, 10)
                    stack.Children.Add(separator)
        
        scroll.Content = stack
        return scroll
    
    def create_property_editor(self, prop_config):
        """Create editor control for a property based on its type"""
        prop_type = prop_config.get('type', PropertyType.TEXTBOX)
        prop_name = prop_config.get('name')
        prop_label = prop_config.get('label', prop_name)
        prop_hint = prop_config.get('hint', '')
        
        # Container
        container = StackPanel()
        container.Margin = Thickness(0, 5, 0, 5)
        
        # Label
        label = Label()
        label.Content = prop_label
        label.FontWeight = FontWeights.Bold
        label.FontSize = 12
        container.Children.Add(label)
        
        # Hint
        if prop_hint:
            hint_label = Label()
            hint_label.Content = prop_hint
            hint_label.FontSize = 10
            hint_label.Foreground = Brushes.Gray
            hint_label.Margin = Thickness(0, -5, 0, 5)
            container.Children.Add(hint_label)
        
        # Create appropriate control based on type
        control = None
        
        if prop_type == PropertyType.TEXTBOX:
            control = TextBox()
            control.Height = 30
            control.VerticalContentAlignment = VerticalAlignment.Center
            control.Padding = Thickness(5)
            
        elif prop_type == PropertyType.NUMBER:
            control = TextBox()
            control.Height = 30
            control.VerticalContentAlignment = VerticalAlignment.Center
            control.Padding = Thickness(5)
            
        elif prop_type == PropertyType.COMBOBOX:
            control = ComboBox()
            control.Height = 30
            
            # Get options
            options = prop_config.get('options', [])
            if not options and 'get_options' in prop_config:
                try:
                    options = prop_config['get_options']()
                except:
                    options = []
            
            # Populate combobox
            for option in options:
                control.Items.Add(option)
            
        elif prop_type == PropertyType.CHECKBOX:
            control = CheckBox()
            control.Content = "Enabled"
            control.VerticalAlignment = VerticalAlignment.Center
            
        elif prop_type == PropertyType.READONLY:
            control = TextBox()
            control.Height = 30
            control.VerticalContentAlignment = VerticalAlignment.Center
            control.Padding = Thickness(5)
            control.IsReadOnly = True
            control.Background = SolidColorBrush(Colors.GRID_ALTERNATING)
        
        if control:
            container.Children.Add(control)
            self.property_controls[prop_name] = {
                'control': control,
                'config': prop_config
            }
        
        return container
    
    def create_footer(self):
        """Create footer with Apply and Cancel buttons"""
        border = Border()
        border.Background = SolidColorBrush(Colors.FOOTER_BACKGROUND)
        border.BorderBrush = SolidColorBrush(Colors.FOOTER_BORDER)
        border.BorderThickness = Thickness(0, 2, 0, 0)
        border.Padding = Thickness(10)
        
        stack = StackPanel()
        stack.Orientation = System.Windows.Controls.Orientation.Horizontal
        stack.HorizontalAlignment = HorizontalAlignment.Center
        
        # Apply button
        btn_apply = Button()
        btn_apply.Content = "Apply"
        btn_apply.Width = 100
        btn_apply.Height = 35
        btn_apply.Margin = Thickness(5)
        btn_apply.Background = SolidColorBrush(Colors.BTN_APPLY)
        btn_apply.Foreground = Brushes.White
        btn_apply.FontWeight = FontWeights.Bold
        btn_apply.BorderThickness = Thickness(0)
        btn_apply.Click += self.on_apply_click
        stack.Children.Add(btn_apply)
        
        # Cancel button
        btn_cancel = Button()
        btn_cancel.Content = "Cancel"
        btn_cancel.Width = 100
        btn_cancel.Height = 35
        btn_cancel.Margin = Thickness(5)
        btn_cancel.Background = SolidColorBrush(Colors.BTN_CANCEL)
        btn_cancel.Foreground = Brushes.White
        btn_cancel.FontWeight = FontWeights.Bold
        btn_cancel.BorderThickness = Thickness(0)
        btn_cancel.Click += self.on_cancel_click
        stack.Children.Add(btn_cancel)
        
        border.Child = stack
        return border
    
    def load_current_values(self):
        """Load current values from first selected item"""
        if not self.selected_items:
            return
        
        # Use first item to get current values
        first_item = self.selected_items[0]
        
        for prop_name, prop_data in self.property_controls.items():
            control = prop_data['control']
            config = prop_data['config']
            
            try:
                # Get current value using configured function
                get_current = config.get('get_current')
                if get_current:
                    current_value = get_current(first_item.Element)
                    
                    # Set control value based on type
                    if isinstance(control, TextBox):
                        control.Text = str(current_value) if current_value is not None else ""
                    elif isinstance(control, ComboBox):
                        # Try to select matching item
                        for i in range(control.Items.Count):
                            if str(control.Items[i]) == str(current_value):
                                control.SelectedIndex = i
                                break
                    elif isinstance(control, CheckBox):
                        control.IsChecked = bool(current_value)
            except Exception as ex:
                print("Error loading value for {}: {}".format(prop_name, str(ex)))
    def get_control_value(self, control, prop_config):
        """Get value from control"""
        prop_type = prop_config.get('type', PropertyType.TEXTBOX)

        if isinstance(control, TextBox):
            return control.Text
        elif isinstance(control, ComboBox):
            if control.SelectedItem:
                return str(control.SelectedItem)
            return None
        elif isinstance(control, CheckBox):
            return control.IsChecked == True

        return None
    
    def validate_values(self):
        """Validate all property values before applying"""
        errors = []
        
        for prop_name, prop_data in self.property_controls.items():
            control = prop_data['control']
            config = prop_data['config']
            
            # Skip readonly properties
            if config.get('type') == PropertyType.READONLY:
                continue
            
            # Get value
            value = self.get_control_value(control, config)
            
            # Validate if validation function provided
            validation_func = config.get('validation')
            if validation_func:
                try:
                    if not validation_func(value):
                        prop_label = config.get('label', prop_name)
                        errors.append("Invalid value for: {}".format(prop_label))
                except Exception as ex:
                    prop_label = config.get('label', prop_name)
                    errors.append("Validation error for {}: {}".format(prop_label, str(ex)))
        
        return errors
    
    def apply_changes(self):
        """Apply changes to all selected items"""
        # Validate first
        errors = self.validate_values()
        if errors:
            error_msg = "Validation errors:\n\n" + "\n".join(errors)
            MessageBox.Show(error_msg, "Validation Error", MessageBoxButton.OK, MessageBoxImage.Warning)
            return False
        
        t = Transaction(self.doc, "Edit Properties")
        t.Start()
        
        try:
            success_count = 0
            error_count = 0
            
            for item in self.selected_items:
                try:
                    # Apply each property
                    for prop_name, prop_data in self.property_controls.items():
                        control = prop_data['control']
                        config = prop_data['config']
                        
                        # Skip readonly properties
                        if config.get('type') == PropertyType.READONLY:
                            continue
                        
                        # Get value
                        value = self.get_control_value(control, config)
                        
                        # Set value using configured function
                        set_value = config.get('set_value')
                        if set_value:
                            set_value(item.Element, value)
                    
                    success_count += 1
                except Exception as ex:
                    print("Error applying to {}: {}".format(item.Name, str(ex)))
                    error_count += 1
            
            t.Commit()
            
            # Show result
            msg = "Successfully updated {} item(s)!".format(success_count)
            if error_count > 0:
                msg += "\nFailed: {}".format(error_count)
            
            MessageBox.Show(msg, "Result", MessageBoxButton.OK, MessageBoxImage.Information)
            
            # Refresh parent window if available
            if self.parent_window and hasattr(self.parent_window, 'load_items'):
                self.parent_window.load_items()
                if hasattr(self.parent_window, 'calculate_usage'):
                    self.parent_window.calculate_usage()
                if hasattr(self.parent_window, 'update_stats'):
                    self.parent_window.update_stats()
            
            return True
            
        except Exception as ex:
            t.RollBack()
            MessageBox.Show(
                "Error applying changes: {}".format(str(ex)),
                "Error",
                MessageBoxButton.OK,
                MessageBoxImage.Error
            )
            return False
    
    def on_apply_click(self, sender, args):
        """Handle Apply button click"""
        if self.apply_changes():
            self.Close()
    
    def on_cancel_click(self, sender, args):
        """Handle Cancel button click"""
        self.Close()


# ============================================================================
# USAGE EXAMPLE
# ============================================================================

"""
Example configuration for Text Note Type Manager:

edit_config = {
    'properties': [
        {
            'name': 'Font',
            'label': 'Text Font',
            'type': 'combobox',
            'get_current': lambda e: e.get_Parameter(BuiltInParameter.TEXT_FONT).AsString(),
            'set_value': lambda e, v: e.get_Parameter(BuiltInParameter.TEXT_FONT).Set(v),
            'get_options': get_available_fonts,
            'hint': 'Select font family'
        },
        {
            'name': 'TextSize',
            'label': 'Text Size (mm)',
            'type': 'number',
            'get_current': lambda e: convert_to_mm(e.get_Parameter(BuiltInParameter.TEXT_SIZE).AsDouble()),
            'set_value': lambda e, v: e.get_Parameter(BuiltInParameter.TEXT_SIZE).Set(convert_to_feet(float(v))),
            'validation': lambda v: v.replace('.','').isdigit() and float(v) > 0,
            'hint': 'Enter size in millimeters'
        },
        {
            'name': 'Bold',
            'label': 'Bold',
            'type': 'checkbox',
            'get_current': lambda e: e.get_Parameter(BuiltInParameter.TEXT_STYLE_BOLD).AsInteger() == 1,
            'set_value': lambda e, v: e.get_Parameter(BuiltInParameter.TEXT_STYLE_BOLD).Set(1 if v else 0)
        }
    ]
}

# Usage:
dialog = EditPropertiesDialog(doc, selected_items, edit_config, parent_window=self)
dialog.ShowDialog()
"""