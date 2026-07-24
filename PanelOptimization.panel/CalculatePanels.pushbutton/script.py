# -*- coding: utf-8 -*-
"""
Places largest possible wall panels on walls defined in walls.csv,
considering openings from wall_openings.csv.

All user inputs are collected in a single Revit-style parameter grid dialog.
"""

from __future__ import print_function
import os
from datetime import datetime

# --- .NET UI imports ---
import clr
clr.AddReference('System.Windows.Forms')
clr.AddReference('System.Drawing')



from System.Windows.Forms import (
    Application, FolderBrowserDialog, DialogResult, Form,
    Label, RadioButton, Button, FormBorderStyle, FormStartPosition,
    MessageBox, MessageBoxButtons, MessageBoxIcon, TextBox,
    Panel, ScrollableControl, AnchorStyles, DockStyle, Padding,
    FlatStyle, CheckBox, TabControl, TabPage
)


from System.Drawing import (
    Point, Size, Color, Font, FontStyle, ContentAlignment, GraphicsUnit,
    SolidBrush, Pen, StringFormat, StringAlignment, RectangleF, PointF
)
from System import IntPtr
import System.Windows.Forms as _WF
IWin32Window = _WF.IWin32Window
from System.Drawing import Graphics as _Graphics
from System.Windows.Forms import Screen as _Screen


# =============================================================================
# DPI / SCREEN-SCALE HELPERS
# =============================================================================
def _get_dpi_scale():
    """
    Return a float scale factor relative to 96 DPI (Windows standard).
    On a 96-DPI (100 %) screen  -> 1.0
    On a 120-DPI (125 %) screen -> 1.25
    On a 144-DPI (150 %) screen -> 1.5
    Falls back to 1.0 on any error (IronPython / Revit environment).
    """
    try:
        g = _Graphics.FromHwnd(IntPtr.Zero)
        dpi = g.DpiX
        g.Dispose()
        return max(1.0, float(dpi) / 96.0)
    except Exception:
        return 1.0

def _get_screen_size():
    """Return (width, height) of the primary screen in pixels."""
    try:
        b = _Screen.PrimaryScreen.Bounds
        return (b.Width, b.Height)
    except Exception:
        return (1920, 1080)

def _scale(value, factor):
    """Scale an integer pixel value by factor, always returning an int."""
    return int(round(value * factor))

# --- Additional refs to bind dialogs to Revit main window ---
try:
    clr.AddReference('RevitAPI')
    clr.AddReference('RevitAPIUI')
    from Autodesk.Revit.UI import UIApplication
except Exception:
    UIApplication = None  # Running outside Revit context

# --- Import optimizer module sitting next to this script ---
import sys as _sys
_this_dir = os.path.dirname(os.path.abspath(__file__))
if _this_dir not in _sys.path:
    _sys.path.insert(0, _this_dir)
# Force reload so edits to panel_calculator.py are always picked up
if 'panel_calculator' in _sys.modules:
    del _sys.modules['panel_calculator']
try:
    import panel_calculator as opt
except Exception as e:
    raise Exception("Failed to import panel_calculator.py from {0}: {1}".format(_this_dir, e))


# =========================
# Helpers: Revit window owner
# =========================
class WindowWrapper(IWin32Window):
    def __init__(self, handle):
        self._hwnd = handle
    @property
    def Handle(self):
        return self._hwnd

def get_revit_owner():
    try:
        uiapp = __revit__
        hwnd = uiapp.MainWindowHandle
        return WindowWrapper(IntPtr(hwnd))
    except Exception as e:
        print("Warning: could not retrieve Revit main window handle: {0}".format(e))
        return None


# =========================
# UI: Folder Picker
# =========================
def pick_data_folder():
    owner = get_revit_owner()
    dialog = FolderBrowserDialog()
    dialog.Description = "Select folder containing walls.csv and wall_openings.csv"
    initial_dir = os.path.join(os.path.expanduser("~"), "Desktop")
    if not os.path.exists(initial_dir):
        initial_dir = os.path.expanduser("~")
    dialog.SelectedPath = initial_dir
    result = dialog.ShowDialog(owner) if owner else dialog.ShowDialog()
    if result == DialogResult.OK and dialog.SelectedPath and os.path.isdir(dialog.SelectedPath):
        return dialog.SelectedPath
    else:
        return os.path.dirname(os.path.abspath(__file__))


# =============================================================================
# HELPERS
# =============================================================================
def _safe_float(text, default):
    try:
        return float(str(text).strip())
    except Exception:
        return default

def _fmt_len(inches):
    """Format inches as a friendly feet-inches string: 636 -> 53', 102 -> 8'6\",
    4 -> 4\", 6.625 -> 6.625\"."""
    try:
        v = float(inches)
    except Exception:
        return str(inches)
    ft = int(v // 12)
    rem = v - ft * 12
    if ft and abs(rem) < 0.001:
        return "{}'".format(ft)
    if ft:
        return "{}'{:g}\"".format(ft, rem)
    return "{:g}\"".format(v)

# Visual constants matching Revit's Family Types palette
CLR_TITLE_BG   = Color.FromArgb(51,  51,  51)
CLR_GROUP_BG   = Color.FromArgb(214, 222, 236)
CLR_GROUP_FG   = Color.FromArgb(20,  20,  20)
CLR_SUB_BG     = Color.FromArgb(235, 239, 247)
CLR_ROW_BG     = Color.White
CLR_ROW_ALT    = Color.FromArgb(248, 249, 252)
CLR_ACCENT     = Color.FromArgb(0,   114, 198)
CLR_BODY_BG    = Color.FromArgb(240, 240, 240)

from System.Drawing import GraphicsUnit

# Compute once at import time so every dialog shares the same factor
_DPI_SCALE = _get_dpi_scale()

def _make_fonts(scale):
    """Build the four font objects scaled to the current DPI."""
    base  = max(8.0,  9.0  * scale)
    title = max(9.0,  10.0 * scale)
    small = max(7.0,  8.0  * scale)
    return (
        Font("Segoe UI", base,  FontStyle.Regular, GraphicsUnit.Point),
        Font("Segoe UI", base,  FontStyle.Bold,    GraphicsUnit.Point),
        Font("Segoe UI", title, FontStyle.Bold,    GraphicsUnit.Point),
        Font("Segoe UI", small, FontStyle.Regular, GraphicsUnit.Point),
    )

FNT_NORMAL, FNT_BOLD, FNT_TITLE, FNT_SMALL = _make_fonts(_DPI_SCALE)

ROW_H   = _scale(26,  _DPI_SCALE)
LBL_W   = _scale(200, _DPI_SCALE)
TXT_W   = _scale(110, _DPI_SCALE)
INDENT  = 0     # rows dock to fill, no manual indent needed


# =============================================================================
# UNIFIED CONFIG DIALOG
# =============================================================================
class ConfigDialog(Form):
    """Single Revit-style parameter grid - all config in one scrollable window."""

    def __init__(self, config, trucking=None):
        self._cfg = config
        self._trucking = dict(opt.DEFAULT_TRUCKING_SETTINGS)
        if trucking:
            self._trucking.update(trucking)
        self._init_scaled_geometry()
        self._build()

    def _init_scaled_geometry(self):
        """Compute all pixel constants relative to the current DPI scale."""
        s = _DPI_SCALE
        self._LEFT   = _scale(4,   s)
        self._LBL_W  = _scale(265, s) # WIDENED for long labels
        self._TXT_X  = _scale(275, s) # SHIFTED right
        self._TXT_W  = _scale(115, s)
        self._CLR1_X = _scale(275, s) # SHIFTED right
        self._CLR2_X = _scale(420, s) # WIDENED gap to 145px
        self._TOT_X  = _scale(570, s) # WIDENED gap to 150px
        self._TOT_W  = _scale(75,  s)
        self._ROW_H  = _scale(34,  s)


    def _build(self):
        s = _DPI_SCALE
        self.Text            = "RNGD Panel Optimizer - Configuration"
        self.StartPosition   = FormStartPosition.CenterScreen
        self.FormBorderStyle = FormBorderStyle.FixedDialog
        self.MaximizeBox     = False
        
        self.ClientSize      = Size(_scale(780, s), _scale(720, s))
        self.TopMost         = True
        self.BackColor       = CLR_BODY_BG
        self.Font            = FNT_NORMAL

        # --- Title strip ---
        tp = Panel()
        tp.Dock      = DockStyle.Top
        tp.Height    = _scale(45, s)
        tp.BackColor = CLR_TITLE_BG
        tl = Label()
        tl.Text      = "RNGD Panel Optimizer"
        tl.Font      = FNT_TITLE
        tl.ForeColor = Color.White
        tl.AutoSize  = True
        tl.Location  = Point(_scale(15, s), _scale(12, s))
        tp.Controls.Add(tl)
        self.Controls.Add(tp)

        # --- Button bar ---
        bb = Panel()
        bb.Dock      = DockStyle.Bottom
        bb.Height    = _scale(55, s)
        bb.BackColor = Color.FromArgb(230, 230, 230)

        self._btnOK = Button()
        self._btnOK.Text      = "Run Optimizer"
        self._btnOK.Size      = Size(_scale(140, s), _scale(32, s))
        self._btnOK.BackColor = CLR_ACCENT
        self._btnOK.ForeColor = Color.White
        self._btnOK.FlatStyle = FlatStyle.Flat
        self._btnOK.Font      = FNT_BOLD
        self._btnOK.Click    += self._on_ok
        self.AcceptButton     = self._btnOK

        self._btnCancel = Button()
        self._btnCancel.Text         = "Cancel"
        self._btnCancel.Size         = Size(_scale(100, s), _scale(32, s))
        self._btnCancel.DialogResult = DialogResult.Cancel
        self._btnCancel.FlatStyle    = FlatStyle.Flat
        self.CancelButton            = self._btnCancel
        
        # Position buttons
        w = self.ClientSize.Width
        self._btnOK.Location = Point(w - _scale(260, s), _scale(11, s))
        self._btnCancel.Location = Point(w - _scale(110, s), _scale(11, s))

        bb.Controls.Add(self._btnOK)
        bb.Controls.Add(self._btnCancel)
        self.Controls.Add(bb)

        # --- Tab Control ---
        self._tabs = TabControl()
        self._tabs.Dock = DockStyle.Fill
        self._tabs.Padding = Point(_scale(15, s), _scale(6, s))
        self._tabs.Font = FNT_BOLD
        self.Controls.Add(self._tabs)
        self._tabs.BringToFront()

        self._populate_tabs()

    def _populate_tabs(self):
        s = _DPI_SCALE
        
        # TAB 1: Project & Strategy
        tab_strat = TabPage("1. Strategy & Goals")
        tab_strat.BackColor = CLR_BODY_BG
        tab_strat.Font = FNT_NORMAL
        self._scroll = ScrollableControl()
        self._scroll.Dock = DockStyle.Fill
        self._scroll.AutoScroll = True
        tab_strat.Controls.Add(self._scroll)
        self._tabs.TabPages.Add(tab_strat)
        
        y = 10
        y = self._section(y, "Project Identity")
        y = self._text_row(y, "Project Name", self._cfg.project_name, "_prj")
        
        y = self._section(y, "Primary Optimization Goal")
        os_ = self._cfg.optimization_strategy
        
        if getattr(os_, 'best_to_manufacture', False): cur_goal = "Tournament (Best of All)"
        elif getattr(os_, 'use_ga_optimizer', False): cur_goal = "Minimize Total + Unique Panels"
        elif getattr(os_, 'minimize_unique_panels', False): cur_goal = "Minimize Unique Panels"
        else: cur_goal = "Minimize Total Panels"

        y = self._radio_row(y, "Select Strategy",
                            ["Minimize Total Panels", "Minimize Unique Panels", 
                             "Minimize Total + Unique Panels", "Tournament (Best of All)"],
                            cur_goal, "_rb_goal")

        y = self._num_row(y, "DfMA Penalty (1 Unique Type = X Panels)", getattr(os_, "unique_weight", 10.0), "_txt_weight")
        
        # Strategy Descriptions - WRAPPED IN A ROW PANEL TO PREVENT OVERLAP
        desc_text = (
            "• Minimize Total Panels: Uses largest panels possible. Fewest crane picks. Highest unique count.\n"
            "• Minimize Unique Panels: Standardizes patterns. High symmetry. Forces identical bounding boxes.\n"
            "• Minimize Total + Unique: Uses evolutionary math to find the perfect numerical balance.\n"
            "• Tournament: Runs all three engines and automatically selects the lowest DfMA score."
        )
        desc_h = _scale(75, s)
        desc_row = self._make_row(y, CLR_BODY_BG, desc_h)
        
        desc_lbl = Label()
        desc_lbl.Text = desc_text
        desc_lbl.Font = FNT_SMALL
        desc_lbl.ForeColor = Color.FromArgb(80, 80, 80)
        desc_lbl.Location = Point(self._LEFT + _scale(16, s), 0)
        desc_lbl.Size = Size(_scale(720, s), desc_h)
        
        desc_row.Controls.Add(desc_lbl)
        self._scroll.Controls.Add(desc_row)
        y += desc_h + 1

        
        # TAB 2: Physical Constraints
        tab_dims = TabPage("2. Panel Constraints")
        tab_dims.BackColor = CLR_BODY_BG
        tab_dims.Font = FNT_NORMAL
        scroll_dims = ScrollableControl()
        scroll_dims.Dock = DockStyle.Fill
        scroll_dims.AutoScroll = True
        tab_dims.Controls.Add(scroll_dims)
        self._tabs.TabPages.Add(tab_dims)
        
        # Temporarily re-route self._scroll so row builders place items in Tab 2
        self._scroll = scroll_dims
        y = 10
        y = self._section(y, "Orientation & Spacing")
        cur_orient = self._cfg.optimization_strategy.panel_orientation.capitalize()
        y = self._radio_row(y, "Orientation", ["Vertical", "Horizontal"], cur_orient, "_rb_orient")
        sp = self._cfg.panel_constraints.panel_spacing
        y = self._radio_row(y, "Panel Type / Gap", ["Backer (1/8\")", "Fully Finished (3/4\")"], 
                            "Fully Finished (3/4\")" if abs(sp - 0.75) < 0.01 else "Backer (1/8\")", "_rb_type")

        _cur_swap = getattr(self._cfg.optimization_strategy, "horizontal_to_vertical_threshold_in", 143.0)
        y = self._num_row(y, "Auto-Vertical Threshold for Horizontal Mode (in)", _cur_swap, "_txt_swap_thresh")

        # --- NEW UI ELEMENT ---
        _cur_flex = getattr(self._cfg.optimization_strategy, "flexible_top_panel_allowance_in", 24.0)
        y = self._num_row(y, "Flexible Top Panel Allowance (Absorb Parapet) (in)", _cur_flex, "_txt_flex_top")
        
        y = self._section(y, "Manufacturing Dimensions")
        pc = self._cfg.panel_constraints
        for lbl, val, attr in [
            ("Min Width (in)", pc.min_width, "_dim_min_w"), ("Max Width (in)", pc.max_width, "_dim_max_w"),
            ("Min Height (in)", pc.min_height, "_dim_min_h"), ("Max Height (in)", pc.max_height, "_dim_max_h"),
            ("Short Max (in)", pc.short_max, "_dim_short"), ("Long Max (in)", pc.long_max, "_dim_long"),
            ("Dimension Increment (in)", pc.dimension_increment, "_dim_inc"),
        ]:
            y = self._num_row(y, lbl, val, attr)

        _cur_floor = getattr(self._cfg.optimization_strategy, "limit_panel_height_to_floor", False)
        y = self._radio_row(y, "Limit Panel Height to Floor",
                            ["No", "Yes (Floor-to-Floor)"],
                            "Yes (Floor-to-Floor)" if _cur_floor else "No", "_rb_floor_h")


        # TAB 3: Clearances
        tab_clr = TabPage("3. Clearances")
        tab_clr.BackColor = CLR_BODY_BG
        tab_clr.Font = FNT_NORMAL
        scroll_clr = ScrollableControl()
        scroll_clr.Dock = DockStyle.Fill
        scroll_clr.AutoScroll = True
        tab_clr.Controls.Add(scroll_clr)
        self._tabs.TabPages.Add(tab_clr)
        
        self._scroll = scroll_clr
        y = 10
        y = self._section(y, "Door Clearances")
        y = self._clr_header(y)
        dc = self._cfg.door_clearances
        for side, rv, pv, ra, pa in [("Jamb", dc.rough_jamb, dc.panel_jamb, "_dc_rj", "_dc_pj"), 
                                     ("Header", dc.rough_header, dc.panel_header, "_dc_rh", "_dc_ph"), 
                                     ("Sill", dc.rough_sill, dc.panel_sill, "_dc_rs", "_dc_ps")]:
            y = self._clr_row(y, side, rv, pv, ra, pa)

        y = self._section(y, "Window Clearances")
        y = self._clr_header(y)
        wc = self._cfg.window_clearances
        for side, rv, pv, ra, pa in [("Jamb", wc.rough_jamb, wc.panel_jamb, "_wc_rj", "_wc_pj"), 
                                     ("Header", wc.rough_header, wc.panel_header, "_wc_rh", "_wc_ph"), 
                                     ("Sill", wc.rough_sill, wc.panel_sill, "_wc_rs", "_wc_ps")]:
            y = self._clr_row(y, side, rv, pv, ra, pa)
            
        y = self._section(y, "Storefront Clearances")
        y = self._clr_header(y)
        sc = self._cfg.storefront_clearances
        for side, rv, pv, ra, pa in [("Jamb", sc.rough_jamb, sc.panel_jamb, "_sc_rj", "_sc_pj"), 
                                     ("Header", sc.rough_header, sc.panel_header, "_sc_rh", "_sc_ph"), 
                                     ("Sill", sc.rough_sill, sc.panel_sill, "_sc_rs", "_sc_ps")]:
            y = self._clr_row(y, side, rv, pv, ra, pa)
            
        y = self._section(y, "Wall Opening Clearances")
        y = self._clr_header(y)
        woc = self._cfg.wall_opening_clearances
        for side, rv, pv, ra, pa in [
            ("Jamb",   woc.rough_jamb,   woc.panel_jamb,   "_woc_rj", "_woc_pj"),
            ("Header", woc.rough_header, woc.panel_header, "_woc_rh", "_woc_ph"),
            ("Sill",   woc.rough_sill,   woc.panel_sill,   "_woc_rs", "_woc_ps"),
        ]:
            y = self._clr_row(y, side, rv, pv, ra, pa)

        # TAB 4: Trucking
        tab_trk = TabPage("4. Trucking")
        tab_trk.BackColor = CLR_BODY_BG
        tab_trk.Font = FNT_NORMAL
        scroll_trk = ScrollableControl()
        scroll_trk.Dock = DockStyle.Fill
        scroll_trk.AutoScroll = True
        tab_trk.Controls.Add(scroll_trk)
        self._tabs.TabPages.Add(tab_trk)

        self._scroll = scroll_trk
        trk = self._trucking
        y = 10
        y = self._section(y, "Truck & Load Geometry (accepts 53', 8'6\", 1' 3\", or plain inches)")
        y = self._text_row(y, "Truck Length",
                           _fmt_len(trk.get("truck_length_in", 636.0)), "_trk_len")
        y = self._text_row(y, "Truck Width",
                           _fmt_len(trk.get("truck_width_in", 102.0)), "_trk_wid")
        y = self._text_row(y, "Max Stack Height (incl. dunnage)",
                           _fmt_len(trk.get("max_stack_height_in", 102.0)), "_trk_stack")
        y = self._text_row(y, "Dunnage Height",
                           _fmt_len(trk.get("dunnage_height_in", 4.0)), "_trk_dun")
        y = self._text_row(y, "Gap Between Panels (2 per row)",
                           _fmt_len(trk.get("two_panel_gap_in", 6.0)), "_trk_gap2")
        y = self._text_row(y, "Gap Between Panels (3 per row)",
                           _fmt_len(trk.get("three_panel_gap_in", 15.0)), "_trk_gap3")
        y = self._text_row(y, "Overhang Length Limit (end of truck)",
                           _fmt_len(trk.get("overhang_length_in", 0.0)), "_trk_ovl")
        y = self._text_row(y, "Overhang Width Limit (each side)",
                           _fmt_len(trk.get("overhang_width_in", 0.0)), "_trk_ovw")
        y = self._text_row(y, "Panel Thickness",
                           _fmt_len(trk.get("panel_thickness_in", 6.625)), "_trk_thick")

        y = self._section(y, "Sequencing & Site Logistics")
        y = self._text_row(y, "Trucks On Site (simultaneously)",
                           str(int(trk.get("trucks_on_site", 2))), "_trk_count")
        _cur_pat = ("Column-by-column"
                    if str(trk.get("install_pattern", "spiral")).lower() == "column"
                    else "Spiral (course-by-course)")
        y = self._radio_row(y, "Install Pattern",
                            ["Spiral (course-by-course)", "Column-by-column"],
                            _cur_pat, "_trk_pattern")
        _cur_fac = str(trk.get("start_facade", "north")).capitalize()
        if _cur_fac not in ("North", "East", "South", "West"):
            _cur_fac = "North"
        y = self._radio_row(y, "Start Facade",
                            ["North", "East", "South", "West"],
                            _cur_fac, "_trk_facade")
        _cur_rot = ("Clockwise"
                    if str(trk.get("rotation", "ccw")).lower() == "cw"
                    else "Counter-Clockwise")
        y = self._radio_row(y, "Rotation",
                            ["Counter-Clockwise", "Clockwise"],
                            _cur_rot, "_trk_rot")

        trk_desc = (
            "Rules: panels ride flat, longer side along the truck. A row holds 1-3 panels end-to-end;\n"
            "row total (panel lengths + gaps) must fit Truck Length + Overhang Length Limit. Panel's\n"
            "across-truck side must fit Truck Width + 2 x Overhang Width Limit, else flagged OVERSIZE.\n"
            "Stacks: dunnage below the bottom layer and between layers; stack total <= Max Stack Height.\n"
            "Spiral: complete the bottom course around the building, then the next course up, etc.\n"
            "Column: full vertical stack at each slot before moving sideways along the wall.\n"
            "Compass (plan view): South = left, North = right, East = front, West = back.\n"
            "First-installed panel is loaded at the TOP of Truck 1. Output: trucking_plan.csv + layout txt."
        )
        trk_h = _scale(110, s)
        trk_row = self._make_row(y, CLR_BODY_BG, trk_h)
        trk_lbl = Label()
        trk_lbl.Text = trk_desc
        trk_lbl.Font = FNT_SMALL
        trk_lbl.ForeColor = Color.FromArgb(80, 80, 80)
        trk_lbl.Location = Point(self._LEFT + _scale(16, s), 0)
        trk_lbl.Size = Size(_scale(720, s), trk_h)
        trk_row.Controls.Add(trk_lbl)
        self._scroll.Controls.Add(trk_row)
        y += trk_h + 1


    def _reposition_buttons(self, s, e):
        w = self._bb.ClientSize.Width
        ok_w  = self._btnOK.Width
        can_w = self._btnCancel.Width
        gap   = _scale(10, _DPI_SCALE)
        self._btnOK.Location     = Point(w - ok_w - can_w - gap * 2, _scale(9, _DPI_SCALE))
        self._btnCancel.Location = Point(w - can_w - gap,             _scale(9, _DPI_SCALE))


    def _populate_rows(self):
        s = _DPI_SCALE
        y = 4

        # Identity
        y = self._section(y, "Identity")
        y = self._text_row(y, "Project Name", self._cfg.project_name, "_prj")

        # Orientation
        y = self._section(y, "Orientation")
        cur = self._cfg.optimization_strategy.panel_orientation.capitalize()
        y = self._radio_row(y, "Panel Orientation",
                            ["Vertical", "Horizontal"], cur, "_rb_orient")

        # Panel Type
        y = self._section(y, "Panel Type")
        sp = self._cfg.panel_constraints.panel_spacing
        cur_type = "Fully Finished (3/4\")" if abs(sp - 0.75) < 0.01 else "Backer (1/8\")"
        y = self._radio_row(y, "Panel Type / Spacing",
                            ["Backer (1/8\")", "Fully Finished (3/4\")"],
                            cur_type, "_rb_type")

        # Panel Dimensions
        y = self._section(y, "Panel Dimensions")
        pc = self._cfg.panel_constraints
        for lbl, val, attr in [
            ("Min Width (in)",           pc.min_width,           "_dim_min_w"),
            ("Max Width (in)",           pc.max_width,           "_dim_max_w"),
            ("Min Height (in)",          pc.min_height,          "_dim_min_h"),
            ("Max Height (in)",          pc.max_height,          "_dim_max_h"),
            ("Short Max (in)",           pc.short_max,           "_dim_short"),
            ("Long Max (in)",            pc.long_max,            "_dim_long"),
            ("Dimension Increment (in)", pc.dimension_increment, "_dim_inc"),
        ]:
            y = self._num_row(y, lbl, val, attr)

        # Clearances...
        y = self._section(y, "Door Clearances")
        y = self._clr_header(y)
        dc = self._cfg.door_clearances
        for side, rv, pv, ra, pa in [
            ("Jamb",   dc.rough_jamb,   dc.panel_jamb,   "_dc_rj", "_dc_pj"),
            ("Header", dc.rough_header, dc.panel_header, "_dc_rh", "_dc_ph"),
            ("Sill",   dc.rough_sill,   dc.panel_sill,   "_dc_rs", "_dc_ps"),
        ]:
            y = self._clr_row(y, side, rv, pv, ra, pa)

        y = self._section(y, "Window Clearances")
        y = self._clr_header(y)
        wc = self._cfg.window_clearances
        for side, rv, pv, ra, pa in [
            ("Jamb",   wc.rough_jamb,   wc.panel_jamb,   "_wc_rj", "_wc_pj"),
            ("Header", wc.rough_header, wc.panel_header, "_wc_rh", "_wc_ph"),
            ("Sill",   wc.rough_sill,   wc.panel_sill,   "_wc_rs", "_wc_ps"),
        ]:
            y = self._clr_row(y, side, rv, pv, ra, pa)

        y = self._section(y, "Storefront Clearances")
        y = self._clr_header(y)
        sc = self._cfg.storefront_clearances
        for side, rv, pv, ra, pa in [
            ("Jamb",   sc.rough_jamb,   sc.panel_jamb,   "_sc_rj", "_sc_pj"),
            ("Header", sc.rough_header, sc.panel_header, "_sc_rh", "_sc_ph"),
            ("Sill",   sc.rough_sill,   sc.panel_sill,   "_sc_rs", "_sc_ps"),
        ]:
            y = self._clr_row(y, side, rv, pv, ra, pa)

        y = self._section(y, "Wall Opening Clearances")
        y = self._clr_header(y)
        woc = self._cfg.wall_opening_clearances
        for side, rv, pv, ra, pa in [
            ("Jamb",   woc.rough_jamb,   woc.panel_jamb,   "_woc_rj", "_woc_pj"),
            ("Header", woc.rough_header, woc.panel_header, "_woc_rh", "_woc_ph"),
            ("Sill",   woc.rough_sill,   woc.panel_sill,   "_woc_rs", "_woc_ps"),
        ]:
            y = self._clr_row(y, side, rv, pv, ra, pa)

        # Optimization Strategy (4 Approaches)
        y = self._section(y, "Optimization Strategy")
        os_ = self._cfg.optimization_strategy

        # Determine current selection
        if getattr(os_, 'best_to_manufacture', False):
            cur_goal = "Best to Manufacture"
        elif getattr(os_, 'use_ga_optimizer', False):
            cur_goal = "Genetic Algorithm"
        elif getattr(os_, 'minimize_unique_panels', False):
            cur_goal = "Minimize Unique"
        else:
            cur_goal = "Minimize Total Panels"

        y = self._radio_row(y, "Optimization Goal",
                            ["Minimize Total Panels", "Minimize Unique", "Genetic Algorithm", "Best to Manufacture"],
                            cur_goal, "_rb_goal")

        _cur_weight = getattr(os_, "unique_weight", 10.0)
        y = self._num_row(y, "DfMA Penalty: 1 Unique Type equals how many Total Panels?",
                          _cur_weight, "_txt_weight")

        DESC_H = self._ROW_H * 2 - _scale(6, _DPI_SCALE)
        for _desc, _bg in [
            ("Minimize Total Panels: Places the biggest panel that meets all constraints across the facade, resulting in the fewest physical panels to install. Each opening may produce a unique panel type.", CLR_ROW_ALT),
            ("Minimize Unique: Designs identical panels around repeating window patterns so as many panels as possible share the same fabrication type.", CLR_ROW_ALT),
            ("Genetic Algorithm: Uses evolutionary math to find the perfect 1:1 balance between total installation labor and unique fabrication complexity.", CLR_ROW_ALT),
            ("Best to Manufacture: Runs a tournament between all three strategies and automatically selects the layout with the lowest total DfMA score.", CLR_ROW_ALT)
        ]:
            _dr = self._make_row(y, _bg, DESC_H)
            _dl = Label()
            _dl.Text      = _desc
            _dl.Font      = FNT_SMALL
            _dl.ForeColor = Color.FromArgb(80, 80, 80)
            _dl.AutoSize  = False
            row_w = self._scroll.ClientSize.Width
            if row_w < 100: row_w = self.ClientSize.Width - 20
            
            _dl.Size     = Size(max(row_w - _scale(30, _DPI_SCALE), _scale(400, _DPI_SCALE)),
                                DESC_H - _scale(4, _DPI_SCALE))
            _dl.Location = Point(self._LEFT + _scale(16, _DPI_SCALE), _scale(4, _DPI_SCALE))
            _dr.Controls.Add(_dl)
            self._scroll.Controls.Add(_dr)
            y += DESC_H + 1


        self._scroll.AutoScrollMinSize = Size(_scale(600, _DPI_SCALE), y + _scale(60, _DPI_SCALE))



    # ---------------------------------------------------------------- row builders

    def _make_row(self, y, bg, h=None):
        row = Panel()
        row.Location  = Point(0, y)
        w = self._scroll.ClientSize.Width
        if w < 100: w = self.ClientSize.Width - 20
        h = h if h is not None else self._ROW_H
        row.Size = Size(max(w, _scale(580, _DPI_SCALE)), h)
        row.Anchor    = AnchorStyles.Top | AnchorStyles.Left | AnchorStyles.Right
        row.BackColor = bg
        return row

    def _section(self, y, text):
        row = self._make_row(y, CLR_GROUP_BG)
        lbl = Label()
        lbl.Text      = text
        lbl.Font      = FNT_BOLD
        lbl.ForeColor = CLR_GROUP_FG
        lbl.AutoSize  = False
        lbl.Anchor    = AnchorStyles.Top | AnchorStyles.Left | AnchorStyles.Right
        lbl.Location  = Point(_scale(6, _DPI_SCALE), _scale(5, _DPI_SCALE))
        lbl.Size      = Size(_scale(660, _DPI_SCALE), self._ROW_H)
        row.Controls.Add(lbl)
        self._scroll.Controls.Add(row)
        return y + self._ROW_H + 1

    def _clr_header(self, y):
        row = self._make_row(y, CLR_SUB_BG)
        
        # Dynamically calculate column widths so text is never cut off
        w_clr1 = self._CLR2_X - self._CLR1_X
        w_clr2 = self._TOT_X - self._CLR2_X
        
        for txt, x, w in [
            ("Side",                 _scale(6, _DPI_SCALE),  self._LBL_W - _scale(14, _DPI_SCALE)),
            ("Rough Opening (in)",   self._CLR1_X,           w_clr1),
            ("Panel Clearance (in)", self._CLR2_X,           w_clr2),
            ("Total (in)",           self._TOT_X,            self._TOT_W),
        ]:
            l = Label()
            l.Text      = txt
            l.Font      = FNT_BOLD
            l.ForeColor = Color.FromArgb(55, 55, 55)
            l.AutoSize  = False
            l.Size      = Size(w, self._ROW_H)
            l.Location  = Point(x, _scale(5, _DPI_SCALE))
            row.Controls.Add(l)
        self._scroll.Controls.Add(row)
        return y + self._ROW_H + 1

    def _clr_row(self, y, side, rough_val, panel_val, rough_attr, panel_attr):
        row = self._make_row(y, CLR_ROW_BG)

        lbl = Label()
        lbl.Text     = side
        lbl.Font     = FNT_NORMAL
        lbl.AutoSize = False
        lbl.Size     = Size(self._LBL_W - _scale(6, _DPI_SCALE), self._ROW_H)
        lbl.Location = Point(self._LEFT + _scale(4, _DPI_SCALE), _scale(5, _DPI_SCALE))
        row.Controls.Add(lbl)

        txt_r = TextBox()
        txt_r.Text     = str(rough_val)
        txt_r.Size     = Size(self._TXT_W, _scale(20, _DPI_SCALE))
        txt_r.Location = Point(self._CLR1_X, _scale(3, _DPI_SCALE))
        setattr(self, rough_attr, txt_r)
        row.Controls.Add(txt_r)

        txt_p = TextBox()
        txt_p.Text     = str(panel_val)
        txt_p.Size     = Size(self._TXT_W, _scale(20, _DPI_SCALE))
        txt_p.Location = Point(self._CLR2_X, _scale(3, _DPI_SCALE))
        setattr(self, panel_attr, txt_p)
        row.Controls.Add(txt_p)

        tot = Label()
        tot.Text      = "{:.4g}\"".format(rough_val + panel_val)
        tot.Font      = FNT_NORMAL
        tot.ForeColor = Color.FromArgb(60, 60, 60)
        tot.AutoSize  = False
        tot.Size      = Size(self._TOT_W, self._ROW_H)
        tot.Location  = Point(self._TOT_X, _scale(5, _DPI_SCALE))
        tot.TextAlign = ContentAlignment.MiddleCenter
        row.Controls.Add(tot)

        def make_upd(tr, tp, tl):
            def upd(s, e):
                tl.Text = "{:.4g}\"".format(
                    _safe_float(tr.Text, 0.0) + _safe_float(tp.Text, 0.0))
            return upd
        upd = make_upd(txt_r, txt_p, tot)
        txt_r.TextChanged += upd
        txt_p.TextChanged += upd

        self._scroll.Controls.Add(row)
        return y + self._ROW_H + 1

    def _num_row(self, y, label_text, value, attr):
        row = self._make_row(y, CLR_ROW_BG)

        lbl = Label()
        lbl.Text     = label_text
        lbl.Font     = FNT_NORMAL
        lbl.AutoSize = False
        lbl.Size     = Size(self._LBL_W, self._ROW_H)
        lbl.Location = Point(self._LEFT + _scale(4, _DPI_SCALE), _scale(5, _DPI_SCALE))
        row.Controls.Add(lbl)

        txt = TextBox()
        txt.Text     = str(value)
        txt.Size     = Size(self._TXT_W, _scale(20, _DPI_SCALE))
        txt.Location = Point(self._TXT_X, _scale(3, _DPI_SCALE))
        setattr(self, attr, txt)
        row.Controls.Add(txt)

        hint = Label()
        hint.Text      = "in"
        hint.Font      = FNT_SMALL
        hint.ForeColor = Color.Gray
        hint.AutoSize  = True
        hint.Location  = Point(self._TXT_X + self._TXT_W + _scale(6, _DPI_SCALE), _scale(7, _DPI_SCALE))
        row.Controls.Add(hint)

        self._scroll.Controls.Add(row)
        return y + self._ROW_H + 1

    def _text_row(self, y, label_text, value, attr):
        row = self._make_row(y, CLR_ROW_BG)

        lbl = Label()
        lbl.Text     = label_text
        lbl.Font     = FNT_NORMAL
        lbl.AutoSize = False
        lbl.Size     = Size(self._LBL_W, self._ROW_H)
        lbl.Location = Point(self._LEFT + _scale(4, _DPI_SCALE), _scale(5, _DPI_SCALE))
        row.Controls.Add(lbl)

        txt = TextBox()
        txt.Text     = str(value)
        txt.Size     = Size(_scale(260, _DPI_SCALE), _scale(20, _DPI_SCALE))
        txt.Location = Point(self._TXT_X, _scale(3, _DPI_SCALE))
        setattr(self, attr, txt)
        row.Controls.Add(txt)

        self._scroll.Controls.Add(row)
        return y + self._ROW_H + 1

    def _radio_row(self, y, label_text, options, selected, attr):
        row = self._make_row(y, CLR_ROW_BG)

        lbl = Label()
        lbl.Text     = label_text
        lbl.Font     = FNT_NORMAL
        lbl.AutoSize = False
        lbl.Size     = Size(self._LBL_W, self._ROW_H)
        lbl.Location = Point(self._LEFT + _scale(4, _DPI_SCALE), _scale(5, _DPI_SCALE))
        row.Controls.Add(lbl)

        radios = []
        rx = self._TXT_X
        ry = _scale(4, _DPI_SCALE)
        max_row_h = self._ROW_H
        
        # Max width before wrapping to the next line
        wrap_limit = _scale(780, _DPI_SCALE) 
        
        for opt_text in options:
            rb = RadioButton()
            rb.Text     = opt_text
            rb.Checked  = (opt_text == selected)
            rb.AutoSize = True
            rb.Font     = FNT_NORMAL
            
            # Safe pixel width estimation
            est_w = _scale(len(opt_text) * 7.5 + 35, _DPI_SCALE)
            
            # Wrap to next line if needed
            if rx + est_w > wrap_limit:
                rx = self._TXT_X
                ry += _scale(24, _DPI_SCALE)
                max_row_h += _scale(24, _DPI_SCALE)
                lbl.Size = Size(self._LBL_W, max_row_h)

            rb.Location = Point(rx, ry)
            row.Controls.Add(rb)
            radios.append(rb)
            rx += est_w + _scale(10, _DPI_SCALE)

        setattr(self, attr, radios)
        row.Height = max_row_h
        self._scroll.Controls.Add(row)
        return y + max_row_h + 1

    # ---------------------------------------------------------------- helpers

    def _selected(self, attr):
        for rb in getattr(self, attr):
            if rb.Checked:
                return rb.Text
        return getattr(self, attr)[0].Text

    def _flt(self, attr, default):
        obj = getattr(self, attr, None)
        return _safe_float(obj.Text, default) if obj else default

    # ---------------------------------------------------------------- OK handler

    def _on_ok(self, sender, e):
        errors = []

        project_name = self._prj.Text.strip()
        if not project_name:
            errors.append("Project Name cannot be empty.")

        orientation   = self._selected("_rb_orient").lower()
        panel_spacing = 0.75 if "Fully" in self._selected("_rb_type") else 0.125

        pc    = self._cfg.panel_constraints
        min_w = self._flt("_dim_min_w", pc.min_width)
        max_w = self._flt("_dim_max_w", pc.max_width)
        min_h = self._flt("_dim_min_h", pc.min_height)
        max_h = self._flt("_dim_max_h", pc.max_height)
        short = self._flt("_dim_short", pc.short_max)
        long_ = self._flt("_dim_long",  pc.long_max)
        inc   = self._flt("_dim_inc",   pc.dimension_increment)

        if min_w <= 0:     errors.append("Min Width must be > 0.")
        if max_w <= min_w: errors.append("Max Width must be > Min Width.")
        if min_h <= 0:     errors.append("Min Height must be > 0.")
        if max_h <= min_h: errors.append("Max Height must be > Min Height.")
        if short <= 0:     errors.append("Short Max must be > 0.")
        if long_ < short:  errors.append("Long Max must be >= Short Max.")
        if inc   <= 0:     errors.append("Dimension Increment must be > 0.")

        for attr in ["_dc_rj","_dc_pj","_dc_rh","_dc_ph","_dc_rs","_dc_ps",
                     "_wc_rj","_wc_pj","_wc_rh","_wc_ph","_wc_rs","_wc_ps",
                     "_sc_rj","_sc_pj","_sc_rh","_sc_ph","_sc_rs","_sc_ps",
                     "_woc_rj","_woc_pj","_woc_rh","_woc_ph","_woc_rs","_woc_ps"]:
            if self._flt(attr, -1) < 0:
                errors.append("Clearance values cannot be negative.")
                break

        if errors:
            MessageBox.Show("\n".join(errors), "Validation Errors",
                            MessageBoxButtons.OK, MessageBoxIcon.Warning)
            return

        # Commit
        self._cfg.project_name = project_name
        self._cfg.optimization_strategy.panel_orientation = orientation
        self._cfg.optimization_strategy.prefer_full_height_panels = (orientation == "vertical")

        pc.min_width           = min_w
        pc.max_width           = max_w
        pc.min_height          = min_h
        pc.max_height          = max_h
        pc.short_max           = short
        pc.long_max            = long_
        pc.dimension_increment = inc
        pc.panel_spacing       = panel_spacing

        dc = self._cfg.door_clearances
        dc.rough_jamb   = self._flt("_dc_rj", dc.rough_jamb)
        dc.panel_jamb   = self._flt("_dc_pj", dc.panel_jamb)
        dc.rough_header = self._flt("_dc_rh", dc.rough_header)
        dc.panel_header = self._flt("_dc_ph", dc.panel_header)
        dc.rough_sill   = self._flt("_dc_rs", dc.rough_sill)
        dc.panel_sill   = self._flt("_dc_ps", dc.panel_sill)

        wc = self._cfg.window_clearances
        wc.rough_jamb   = self._flt("_wc_rj", wc.rough_jamb)
        wc.panel_jamb   = self._flt("_wc_pj", wc.panel_jamb)
        wc.rough_header = self._flt("_wc_rh", wc.rough_header)
        wc.panel_header = self._flt("_wc_ph", wc.panel_header)
        wc.rough_sill   = self._flt("_wc_rs", wc.rough_sill)
        wc.panel_sill   = self._flt("_wc_ps", wc.panel_sill)

        sc = self._cfg.storefront_clearances
        sc.rough_jamb   = self._flt("_sc_rj", sc.rough_jamb)
        sc.panel_jamb   = self._flt("_sc_pj", sc.panel_jamb)
        sc.rough_header = self._flt("_sc_rh", sc.rough_header)
        sc.panel_header = self._flt("_sc_ph", sc.panel_header)
        sc.rough_sill   = self._flt("_sc_rs", sc.rough_sill)
        sc.panel_sill   = self._flt("_sc_ps", sc.panel_sill)

        woc = self._cfg.wall_opening_clearances
        woc.rough_jamb   = self._flt("_woc_rj", woc.rough_jamb)
        woc.panel_jamb   = self._flt("_woc_pj", woc.panel_jamb)
        woc.rough_header = self._flt("_woc_rh", woc.rough_header)
        woc.panel_header = self._flt("_woc_ph", woc.panel_header)
        woc.rough_sill   = self._flt("_woc_rs", woc.rough_sill)
        woc.panel_sill   = self._flt("_woc_ps", woc.panel_sill)


        # --- Optimization Strategy Selection ---
        os_ = self._cfg.optimization_strategy
        selected_goal = self._selected("_rb_goal")
        
        os_.best_to_manufacture    = ("Tournament" in selected_goal)
        os_.use_ga_optimizer       = ("Total + Unique" in selected_goal)
        os_.minimize_unique_panels = (selected_goal == "Minimize Unique Panels")

        os_.unique_weight = self._flt("_txt_weight", 10.0)
        # The single "1 Unique Type = X Panels" field IS the unique weight.
        # The calculator's GA/tournament read np_weight & nu_weight directly,
        # so mirror the field into nu_weight and keep np_weight as the baseline.
        os_.nu_weight = os_.unique_weight
        os_.np_weight = 1.0

        os_.horizontal_to_vertical_threshold_in = self._flt("_txt_swap_thresh", 143.0)
        os_.limit_panel_height_to_floor = ("Yes" in self._selected("_rb_floor_h"))
        os_.flexible_top_panel_allowance_in     = self._flt("_txt_flex_top", 30.0)

        # --- Trucking tab commit (kept OUT of the optimizer config on purpose) ---
        trk = self._trucking
        P = opt.parse_length_to_inches
        trk["truck_length_in"]     = P(self._trk_len.Text,   636.0)
        trk["truck_width_in"]      = P(self._trk_wid.Text,   102.0)
        trk["max_stack_height_in"] = P(self._trk_stack.Text, 102.0)
        trk["dunnage_height_in"]   = P(self._trk_dun.Text,   4.0)
        trk["two_panel_gap_in"]    = P(self._trk_gap2.Text,  6.0)
        trk["three_panel_gap_in"]  = P(self._trk_gap3.Text,  15.0)
        trk["overhang_length_in"]  = P(self._trk_ovl.Text,   0.0)
        trk["overhang_width_in"]   = P(self._trk_ovw.Text,   0.0)
        trk["panel_thickness_in"]  = P(self._trk_thick.Text, 6.625)
        try:
            trk["trucks_on_site"] = max(1, int(float(self._trk_count.Text.strip())))
        except Exception:
            trk["trucks_on_site"] = 2
        trk["install_pattern"] = ("column"
            if "Column" in self._selected("_trk_pattern") else "spiral")
        trk["start_facade"] = self._selected("_trk_facade").lower()
        trk["rotation"] = ("cw"
            if "Clockwise" == self._selected("_trk_rot") else "ccw")
        trk.pop("first_panel_to_install", None)  # retired setting

        trk_errors = []
        if trk["truck_length_in"] <= 0: trk_errors.append("Truck Length must be > 0.")
        if trk["truck_width_in"]  <= 0: trk_errors.append("Truck Width must be > 0.")
        if trk["max_stack_height_in"] <= trk["dunnage_height_in"] + trk["panel_thickness_in"]:
            trk_errors.append("Max Stack Height must exceed dunnage + one panel thickness.")
        if trk_errors:
            MessageBox.Show("\n".join(trk_errors), "Trucking Validation",
                            MessageBoxButtons.OK, MessageBoxIcon.Warning)
            return

        self.DialogResult = DialogResult.OK
        self.Close()

def show_config_dialog(config, trucking=None):
    form = ConfigDialog(config, trucking)
    if form.ShowDialog() == DialogResult.OK:
        return form._trucking
    return None


# =================
# Utility functions
# =================
def _ensure_dir(path):
    if not os.path.exists(path):
        try:
            os.makedirs(path)
        except Exception as e:
            print("Failed to create directory {0}: {1}".format(path, e))

def _sanitize_folder_name(name):
    invalid_chars = '<>:"/\\|?*'
    for char in invalid_chars:
        name = name.replace(char, '_')
    return name


# =====
# Main
# =====
def main():
    try:
        Application.EnableVisualStyles()
    except Exception as e:
        print("EnableVisualStyles failed: {0}".format(e))

    # 1) Pick input folder
    input_dir = pick_data_folder()
    _ensure_dir(input_dir)

    # 2) Load starting config (saved file if present, else vertical preset)
    config_file = os.path.join(input_dir, "optimizer_config.json")
    presets = opt.get_preset_configs()
    if os.path.exists(config_file):
        try:
            config = opt.OptimizerConfig.load(config_file)
            print("[CONFIG] Loaded from: {}".format(config_file))
        except Exception as ex:
            print("[CONFIG] Failed to load ({}), using preset.".format(ex))
            config = presets["vertical"]
    else:
        config = presets["vertical"]

    # 2b) Load trucking settings (persisted separately from the optimizer config)
    trucking_file = os.path.join(input_dir, "trucking_config.json")
    trucking = None
    if os.path.exists(trucking_file):
        try:
            import json as _json
            with open(trucking_file, "r") as _tf:
                trucking = _json.load(_tf)
            print("[TRUCK] Loaded settings from: {}".format(trucking_file))
        except Exception as ex:
            print("[TRUCK] Failed to load settings ({}), using defaults.".format(ex))

    # 3) Show the single unified dialog
    trucking = show_config_dialog(config, trucking)
    if trucking is None:
        MessageBox.Show("Operation canceled.", "Panel Optimizer",
                        MessageBoxButtons.OK, MessageBoxIcon.Information)
        return

    # persist trucking settings for next run
    try:
        import json as _json
        with open(trucking_file, "w") as _tf:
            _json.dump(trucking, _tf, indent=2)
    except Exception as ex:
        print("[TRUCK] Could not save settings: {}".format(ex))

    orientation     = config.optimization_strategy.panel_orientation
    panel_spacing   = config.panel_constraints.panel_spacing
    panel_type_name = "Fully Finished" if abs(panel_spacing - 0.75) < 0.01 else "Backer"

    # 4) Create timestamped output directory
    timestamp    = datetime.now().strftime("%Y%m%d_%H%M%S")
    output_dir   = os.path.join(
        input_dir, "{}_{}".format(_sanitize_folder_name(config.project_name), timestamp))
    _ensure_dir(output_dir)

    # 5) Resolve CSV paths
    walls_csv    = os.path.join(input_dir, "walls.csv")
    openings_csv = os.path.join(input_dir, "wall_openings.csv")

    if not os.path.exists(walls_csv):
        MessageBox.Show(
            "Could not find walls.csv in:\n{0}".format(input_dir),
            "Missing Input", MessageBoxButtons.OK, MessageBoxIcon.Error)
        return

    if not os.path.exists(openings_csv):
        MessageBox.Show(
            "Could not find wall_openings.csv in:\n{0}\n\nProceeding without openings.".format(input_dir),
            "Missing Input", MessageBoxButtons.OK, MessageBoxIcon.Warning)

    # 6) Run optimizer
    walls_rows    = opt.load_walls_from_csv(walls_csv)
    openings_rows = opt.load_openings_from_csv(openings_csv)

    panels_path, config_path = opt.optimize_building(
        walls_rows, openings_rows, output_dir,
        config.door_clearances,
        config.window_clearances,
        config.storefront_clearances,
        config,
        orientation
    )

    if config_path:
        print("Configuration saved to: {}".format(config_path))

    # 6b) Trucking plan (additional output; failure here never blocks the run)
    trucking_path = None
    if panels_path and os.path.exists(panels_path):
        try:
            trucking_path = opt.generate_trucking_plan(
                panels_path, trucking,
                os.path.join(output_dir, "trucking_plan.csv"))
        except Exception as ex:
            print("[TRUCK ERROR] {}".format(ex))

    # 7) Done message
    if panels_path and os.path.exists(panels_path):
        _trk_line = ("\nTrucking plan: trucking_plan.csv" if trucking_path
                     else "\nTrucking plan: FAILED (see console)")
        MessageBox.Show(
            "Optimization complete.\n\nPanel Type: {0}\nSpacing: {1}\"{2}\n\nExported panels to:\n{3}".format(
                panel_type_name, panel_spacing, _trk_line, output_dir),
            "Panel Optimizer", MessageBoxButtons.OK, MessageBoxIcon.Information)
    else:
        MessageBox.Show(
            "No panels generated.\nPlease check inputs and configuration.",
            "Panel Optimizer", MessageBoxButtons.OK, MessageBoxIcon.Warning)


# Entrypoint for pyRevit button
if __name__ == "__main__":
    main()