# -*- coding: utf-8 -*-
"""
Extension Tab Manager
Manage visibility of Extension/Add-in tabs in Revit
Copyright (c) 2025 by Dang Quoc Truong (DQT)
"""

__author__ = "Dang Quoc Truong (DQT)"
__version__ = "1.0.0"
__copyright__ = "Copyright (c) 2025 by Dang Quoc Truong (DQT)"
tool_name = "Extension Tab Manager"

# System library
from pyrevit.forms import SelectFromList, alert, TemplateListItem
from pyrevit.api import AdWindows
import os
import codecs
from System import DateTime

# Autodesk library

# External library

# General info
uidoc = __revit__.ActiveUIDocument
app = __revit__.Application
doc = uidoc.Document
activeView = doc.ActiveView
date = DateTime.Now.ToString("yyMMdd")
revit_version = int(app.VersionNumber)

userName = app.Username

# User define

def TempMemory(tool_name, bool):
    output = []

    # main dir
    memory_folder = r"C:\MEOS_Temp"
    memory_clear_folder = os.path.join(memory_folder, userName)

    # temp folder
    memory_need_clear_folder = os.path.join(memory_clear_folder, "Temp")
    output.append(memory_need_clear_folder)  # output 0
    memory_file_folder = os.path.join(memory_need_clear_folder, tool_name, date)
    output.append(memory_file_folder)  # output 1
    try:
        os.makedirs(memory_file_folder)
    except:
        pass
    memory_file_name = userName + "_" + tool_name + ".txt"
    memory_file_path = os.path.join(memory_file_folder, memory_file_name)
    output.append(memory_file_path)  # output 2

    # not delete folder
    memory_not_clear_folder = os.path.join(memory_clear_folder, "Not Delete")
    output.append(memory_not_clear_folder)  # output 3
    memory_data_folder = os.path.join(memory_not_clear_folder, tool_name)
    output.append(memory_data_folder)  # output 4
    try:
        os.makedirs(memory_data_folder)
    except:
        pass
    if bool is True:
        memory_data_name = userName + "_" + tool_name + "_" + str(revit_version) + ".txt"
    else:
        memory_data_name = userName + "_" + tool_name + ".txt"
    memory_data_path = os.path.join(memory_data_folder, memory_data_name)
    output.append(memory_data_path)  # output 5
    return output

class MyOption(TemplateListItem):
    @property
    def name(self):
        return self.item

def CheckBoxForListItem(nameLst, activeLst):
    currentLst = []
    for n in nameLst:
        item = MyOption(n)
        if n in activeLst:
            item = MyOption(n, True)
        currentLst.append(item)
    return currentLst


# Starting
def main_task():
    # List of REVIT BUILT-IN tabs - these are skipped and never shown
    ignoreTabNameLst = []
    ignoreTabNameLst1 = ["Architecture", "Structure", "Steel", "Precast", "Systems", "Insert", "Annotate", "Analyze",
                         "Massing & Site", "Collaborate", "View", "Manage", "Add-Ins", "Modify"]
    ignoreTabNameLst2 = ["Arch", "Struc", "MEP", "Anno", "Mass&Site", "Collab", "Fam.Editor"]
    ignoreTabNameLst3 = ["Create", "In-Place Model", "In-Place Mass", "Zone", "Family Editor"]

    # Add your own tabs here to SKIP them (exclude from management)
    ignoreTabNameLst4 = ["pyRevit", "MEOS"]

    ignoreTabNameLst.extend(ignoreTabNameLst1)
    ignoreTabNameLst.extend(ignoreTabNameLst2)
    ignoreTabNameLst.extend(ignoreTabNameLst3)
    ignoreTabNameLst.extend(ignoreTabNameLst4)

    # Collect every EXTENSION/ADD-IN tab (not a Revit built-in tab)
    extensionTabLst = []
    extensionTabNameLst = []
    visibleTabNameLst = []

    for tab in AdWindows.ComponentManager.Ribbon.Tabs:
        # Only keep tabs NOT in the ignore list (i.e. Extension/Add-in tabs)
        if tab.Title not in ignoreTabNameLst:
            extensionTabLst.append(tab)
            extensionTabNameLst.append(tab.Title)
            if tab.IsVisible:
                visibleTabNameLst.append(tab.Title)

    # Bail out if no extension tab was found
    if len(extensionTabNameLst) == 0:
        alert("No Extension/Add-in tabs found!", title="Extension Tab Manager")
        return

    # Build the checkbox list
    currentLst = CheckBoxForListItem(extensionTabNameLst, visibleTabNameLst)

    # Show the dialog with the copyright notice
    dialog_title = "Extension Tab Manager - Select Tabs to Show\nCopyright (c) 2025 by Dang Quoc Truong (DQT)"
    
    selectedTabNameLst = SelectFromList.show(
        currentLst, 
        title=dialog_title,
        multiselect=True, 
        width=500, 
        height=400, 
        button_name="Apply"
    )

    if selectedTabNameLst is not None:
        # Build the list of tabs to hide
        hideTabNameLst = []
        for i in extensionTabNameLst:
            if i not in selectedTabNameLst:
                hideTabNameLst.append(i)

        # Build the storage file path
        memory_data_path = TempMemory(tool_name, True)[5]

        # Apply the change and save the data
        try:
            with codecs.open(memory_data_path, "w", encoding="utf-8") as textfile:
                # Write the copyright notice into the file
                textfile.write("# Extension Tab Manager\n")
                textfile.write("# Copyright (c) 2025 by Dang Quoc Truong (DQT)\n")
                textfile.write("# Hidden Tabs:\n")

                for tab in extensionTabLst:
                    if tab.Title in hideTabNameLst:
                        tab.IsVisible = False
                        textfile.write(tab.Title)
                        textfile.write("\n")
                    else:
                        tab.IsVisible = True

            # Notify the result
            visible_count = len(selectedTabNameLst)
            hidden_count = len(hideTabNameLst)
            alert("Extension Tab Manager completed!\n\nVisible: {}\nHidden: {}\n\nCopyright (c) 2025 by Dang Quoc Truong (DQT)".format(
                visible_count, hidden_count), 
                title="Extension Tab Manager")
        except Exception as e:
            alert("Error: {}".format(str(e)), title="Error")


if __name__ == "__main__":
    main_task()