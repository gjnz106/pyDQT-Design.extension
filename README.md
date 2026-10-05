# pyDQT-Design

**pyRevit extension for Autodesk Revit — BIM Designer tools.**

> 62 tools · 7 panels · Revit 2024 – 2027

pyDQT-Design adds a single **pyDQT-Design** tab to the Revit ribbon, organised into
panels that follow the modelling workflow from interface setup and element selection,
through modelling and annotation, to view/sheet management and data exchange.

For model auditing/QA-QC, IFC-SG compliance and cleanup tools, see the companion
extension **[pyDQT-Manage](https://github.com/gjnz106/pyDQT-Manage.extension)**.

Author: **Dang Quoc Truong (DQT)**

---

## Requirements

- **Autodesk Revit** 2024, 2025, 2026 or 2027
- **pyRevit** 4.8 or newer (tested on 6.1+), with the **IronPython** engine (default)
- **Windows** 10 / 11 (administrator rights for the first install)
- Internet access to download the pyRevit installer

## Installation

### 1. Install pyRevit

1. Download the latest installer from
   [github.com/pyrevitlabs/pyRevit/releases](https://github.com/pyrevitlabs/pyRevit/releases)
   (`pyRevit_x.x.x_signed.exe`).
2. Close every running Revit session.
3. Run the installer with the default options. pyRevit installs to
   `%AppData%\pyRevit-Master` and attaches to all detected Revit versions.
4. Start Revit — a **pyRevit** tab should appear on the ribbon. If prompted to load the
   add-in, choose **Always Load**.

### 2. Install pyDQT-Design

1. Get the `pyDQT-Design.zip` package.
2. Create this folder if it does not exist:
   ```
   %AppData%\pyRevit\Extensions\pyDQT-Design
   ```
   (Paste `%AppData%\pyRevit\Extensions` into the File Explorer address bar to jump there.)
3. Extract the ZIP so the final structure is exactly:
   ```
   ...\pyRevit\Extensions\pyDQT-Design\pyDQT-Design.extension\pyDQT-Design.tab\...
   ```
   > The `pyDQT-Design.extension` folder name (with the `.extension` suffix) is required
   > by pyRevit — **do not rename it**. If you end up with
   > `pyDQT-Design.extension\pyDQT-Design.extension`, move the inner folder up one level.

### 3. Register the extension path

1. In Revit, open the **pyRevit** tab → **Settings**.
2. Under **Custom Extension Directories**, click **Add folder** and select the **parent
   folder that contains `pyDQT-Design.extension`** (the `pyDQT-Design` folder) — not
   `pyDQT-Design.extension` itself, and not `pyDQT-Design.tab`.
3. Click **Save Settings & Reload**. The **pyDQT-Design** tab appears with 7 panels.

**To update later:** delete the old `pyDQT-Design.extension` folder, extract the new ZIP
to the same location, and press the pyRevit **Reload** button.

## Ribbon overview

| Panel | Purpose |
|-------|---------|
| **UI** | Revit interface utilities: manage ribbon tabs, shorten ribbon names, set the canvas background theme. |
| **Select** | Find and select elements quickly — by category, family, type, material or link. |
| **Modify** | Geometry editing: split walls/floors/columns/ceilings/shafts, convert CAD to model elements, coping, align grids and levels. |
| **Annotate** | Dimensioning, tagging and annotation management between views and models. |
| **Views-Sheets** | View and sheet management: batch operations, templates, numbering, section boxes. |
| **Data** | Schedules, Excel export/import, parameter transfer, BCF coordination data. |
| **Settings** | Element colour overrides and fill-pattern naming. |

## Tools

### UI
- **BG Theme** — Set the model-view background colour from a themed picker (presets, RGB sliders, HEX, live preview). `SHIFT + Click` quick-cycles Black → Gray → White.
- **Check Update** — Check for and install pyDQT-Design updates.
- **Ribbon Names** — Shorten or restore Revit ribbon tab names.
- **Tab Manager** — Toggle the visibility of unwanted ribbon tabs.

### Select
- **Material Select** — Material statistics with element finder.
- **Quick Select** — Hierarchical Category → Family → Type → Instance browser; search, zoom and select (including hidden elements).
- **Select Linked** — Unhide linked elements.
- **Select** (drop-down): Deselect Grouped Elements · Select In-Place Elements · Select By Category · On Sheets (DWGs / Title Blocks) · Select Similar by Category / Family / Type (in Model or in View).

### Modify
- **Auto Coping** — Automated beam/member coping at intersections.
- **AutoJoin** — Auto-join elements with rule-based category pairs (save/load settings).
- **CAD to Floor** — Create Floors or Parts (DirectShape) from linked/imported DWG geometry.
- **CAD to Wall** — Detect parallel CAD line pairs, compute centerlines, auto-create matching wall types.
- **Toposolid to Void** — Create a void Generic Model with exactly the shape of a toposolid (Cut with Voids When Loaded on), to cut other elements with Cut Geometry.
- **Gridline** (drop-down): Align Gridline · Convert Gridline (swap 3D ↔ 2D extents).
- **Level** (drop-down): Align Level · Convert Level (swap 3D ↔ 2D extents, bubble control) · Level Impact (elevation-change compensation) · Rehost Level.
- **Revise Base** — Auto-adjust Base Offset when changing Base Constraint to keep position.
- **Room to Area** — Create Areas from selected Rooms with matching boundaries.
- **Split** (drop-down): Ceiling Split · Column Split · Floor Split · Shaft Opening Split · Wall Split.
- **Wall Cut Profile** — Create wall openings from intersecting linked elements.

### Annotate
- **Copy Annotation** — Copy annotations (dimensions, tags, text, detail items/lines) between matching views in two open documents.
- **Dimension** (drop-down): Dim Beam · Dim Column · Dim Wall · Snap Dimension (round to nearest gridline).
- **Merge Fill Region** — Merge adjacent/overlapping filled regions.
- **Renumber by spline** — Renumber doors/rooms by proximity along a spline.
- **Tag Checker** — Check whether elements in the current view are fully tagged.
- **Wall Fill Region** — Generate filled regions from wall profiles.

### Views-Sheets
- **Align Viewports** — Align viewports across sheets.
- **Linked Element Box** — Section box around linked elements.
- **Sheet re-number** — Batch sheet renumbering.
- **Sheet Manager** — Advanced sheet management.
- **View Manager** — Advanced view management with summary cards.
- **View Template** — Standalone view-template manager.

### Data
- **BCF Reader** — Read BCF files from IFC Delta Viewer and navigate issues in Revit.
- **Contains Manager** — Find elements in Rooms/Areas/Spaces and assign parameter values.
- **Foundation Volume** — Write built-in volume into a selected parameter on structural foundations.
- **Room Data Collector** — Aggregate parameter values from elements into spatial elements (reverse of Contains Manager).
- **Schedule Export/Import Pro** — Export/import schedules to Excel, reading directly from elements.
- **Schedule Copy** — Advanced schedule duplicator with template options.
- **Text to Element** — Transfer Text Note values to intersecting elements.
- **Transfer Para** — Transfer values between parameters on the same elements (all storage types).

### Settings
- **Color Splasher** — Auto-color elements by parameter value (gradient/random, legend, view filters).
- **Hatching** — Manage and rename fill patterns.

## Troubleshooting

| Problem | Solution |
|---------|----------|
| pyDQT-Design tab does not appear after reload | The extension path is wrong. It must point to the **parent folder** containing `pyDQT-Design.extension` — not `pyDQT-Design.extension` itself, and not `pyDQT-Design.tab`. |
| Tab appears but some panels are empty/missing | The ZIP was extracted with a nested `pyDQT-Design.extension\pyDQT-Design.extension` folder, or extraction was incomplete. Delete and re-extract, then Reload. |
| `Save Settings` fails: *pyRevit.addin … being used by another process* | Another Revit session or antivirus is locking the manifest. Close all Revit instances, run `pyrevit attach master default --installed`, then restart Revit. |
| Errors in the pyRevit output window during Reload | Screenshot the full error (it names the file) and send it to DQT. The tab still loads the remaining tools. |
| A tool crashes with an IronPython error | Confirm the active engine is IronPython (pyRevit Settings → Engines). Screenshot the traceback and send it to DQT. |
| Buttons show without icons | Extraction was incomplete or icon files were blocked. Right-click the ZIP → Properties → **Unblock** before extracting, then extract again. |

## License

See the [LICENSE](LICENSE) file.

---

**Made for the AEC community** · pyDQT-Design by Dang Quoc Truong (DQT)
