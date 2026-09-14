# Text to Element Transfer Tool

A PyRevit tool that transfers values from Text Notes into elements wherever a text note overlaps that element.

## Description

This tool automates assigning information from annotations (Text Notes) to model elements or detail components, based on where they overlap in the view.

## Two versions

### 1. Full Version (`text_to_element_transfer/`)

The full version with a WPF UI:

**Features:**
- Graphical interface in the pyDQT style (yellow-orange theme)
- Pick text notes from the model, or grab every one in the view
- Choose the target category (Walls, Floors, Rooms, Doors, etc.)
- Choose the target parameter (Comments, Mark, Description, etc.) or enter a custom parameter
- Adjustable tolerance for intersection detection
- Preview before transferring
- Detailed result reporting

**Workflow:**
1. Step 1: Select Text Notes
2. Step 2: Select the target elements' Category
3. Step 3: Select the Parameter to write into
4. Step 4: Preview and Transfer

### 2. Quick Version (`text_to_element_transfer_quick/`)

A faster version, no WPF required:

**Features:**
- Works directly with the current selection
- Automatically finds elements overlapping the text notes
- Pick a parameter from a preset list
- Confirms before transferring

**Workflow:**
1. Select text notes (and optionally the target elements)
2. Run the script
3. Select the target parameter
4. Confirm and transfer

## Installation

1. Copy the tool folder into your extension:
```
YourExtension.extension/
└── YourTab.tab/
    └── YourPanel.panel/
        └── TextToElement.pushbutton/
            ├── script.py
            └── bundle.yaml
```

2. Reload PyRevit or restart Revit

## Usage

### Use Case 1: Assign a room name from Text to a Room
1. Create Text Notes with the room name placed inside the Room boundaries
2. Run the tool
3. Select the "Rooms" category
4. Select the "Comments" or "Name" parameter
5. Transfer

### Use Case 2: Assign a detail code from Text to Detail Items
1. Create Text Notes with the detail code placed near the Detail Components
2. Run the tool
3. Select the "Detail Items" category
4. Select the "Mark" parameter
5. Transfer

### Use Case 3: Assign information from Text to Walls
1. Select the Text Notes whose content you want to transfer
2. Run the tool
3. Select the "Walls" category
4. Enter a custom parameter name
5. Transfer

## Technical notes

### Intersection Detection
- The tool uses bounding box intersection to determine whether a text note overlaps an element
- Default tolerance: 0.5 feet
- Only checks intersection in 2D (X, Y) - suited to plan views

### Parameter Support
- Instance parameters (Comments, Mark, etc.)
- Built-in parameters (ALL_MODEL_INSTANCE_COMMENTS, ALL_MODEL_MARK)
- Custom shared parameters
- Does NOT support read-only parameters or type parameters

### Supported categories
- Walls, Floors, Ceilings, Roofs
- Rooms, Areas
- Doors, Windows
- Furniture, Generic Models
- Structural Framing, Columns
- MEP Equipment
- Detail Items, Casework

## API Reference

```python
# Core functions
get_text_content(text_note)              # Get the text content
get_text_note_bounding_box(text_note, view)  # Get the bounding box
boxes_intersect(bb1, bb2, tolerance)     # Check for intersection
set_parameter_value(element, param, value)   # Assign the parameter value
```

## Troubleshooting

**"No intersections found"**
- Make sure the text notes overlap the elements in the current view
- Try increasing the tolerance in the Full version
- Check whether the elements are visible in the view

**"Parameter not found or read-only"**
- The parameter may be read-only
- The parameter may not exist on that element type
- Try a different parameter such as "Comments"

**"No target elements found"**
- Select the correct category
- Make sure the elements are visible in the view
- Some categories may have no elements in the view

## Version History

- v1.0: Initial release
  - Full version with a WPF UI
  - Quick version for a faster workflow
  - Support for 17 categories
  - Support for custom parameters

## Author

DQT - pyDQT Tools
