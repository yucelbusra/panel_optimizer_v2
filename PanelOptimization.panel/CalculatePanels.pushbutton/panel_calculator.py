# -*- coding: utf-8 -*-

from __future__ import print_function
import os
import csv
import json
import math
from datetime import datetime
import io

# Provide Py2/Py3 compatibility for type checks when run outside IronPython
try:
    basestring
except NameError:
    basestring = (str,)

# ------------------ ANSI COLOR HELPERS ------------------
class Ansi(object):
    RESET = "\033[0m"
    BOLD = "\033[1m"
    GREEN = "\033[92m"
    CYAN = "\033[96m"
    YELLOW = "\033[93m"
    MAGENTA = "\033[95m"
    RED = "\033[91m"

# Global active configuration (used for orientation & constraints)
ACTIVE_CONFIG = None

# =============================================================================
# SECTION 1: DATA STRUCTURES & BASIC CONFIGURATION
# =============================================================================
class OpeningClearances(object):
    """Two-fold clearance criteria for openings (inches)."""
    def __init__(self, rough_jamb=0.5, rough_header=0.5, rough_sill=0.5,
                 panel_jamb=5.5, panel_header=7.5, panel_sill=5.5):
        # Rough opening clearances (space for rough opening frame)
        self.rough_jamb = float(rough_jamb)
        self.rough_header = float(rough_header)
        self.rough_sill = float(rough_sill)
        
        # Panel clearances (additional space from rough opening to panel edge)
        self.panel_jamb = float(panel_jamb)
        self.panel_header = float(panel_header)
        self.panel_sill = float(panel_sill)
    
    @property
    def jamb_min(self):
        """Total minimum distance from opening edge to panel edge (jamb)"""
        return self.rough_jamb + self.panel_jamb
    
    @property
    def header_min(self):
        """Total minimum distance from opening edge to panel edge (header)"""
        return self.rough_header + self.panel_header
    
    @property
    def sill_min(self):
        """Total minimum distance from opening edge to panel edge (sill)"""
        return self.rough_sill + self.panel_sill

class Panel(object):
    def __init__(self, x=0, y=0, w=0, h=0, name="", cutouts=None):
        self.x = float(x)
        self.y = float(y)
        self.w = float(w)
        self.h = float(h)
        self.name = name or ""
        self.cutouts = cutouts or []

class Opening(object):
    """Opening with clearance zones."""
    def __init__(self, oid, otype, x, y, w, h, clearances_template):
        self.id = str(oid)
        self.type = str(otype)
        self.x = float(x)      # Left edge in inches
        self.y = float(y)      # Bottom edge (sill) in inches
        self.w = float(w)      # Width in inches
        self.h = float(h)      # Height in inches
        
# [CRITICAL FIX] Create independent copies of clearances.
        self.original_clearances = OpeningClearances(
            clearances_template.rough_jamb,
            clearances_template.rough_header,
            clearances_template.rough_sill,
            clearances_template.panel_jamb,
            clearances_template.panel_header,
            clearances_template.panel_sill
        )
        self.clearances = OpeningClearances(
            clearances_template.rough_jamb,
            clearances_template.rough_header,
            clearances_template.rough_sill,
            clearances_template.panel_jamb,
            clearances_template.panel_header,
            clearances_template.panel_sill
        )
        
        self.force_blocker = False

    @property
    def left_clearance_zone(self):
        # Never allow clearance zones to extend past wall start
        return max(0.0, self.x - self.clearances.jamb_min)


    @property
    def right_clearance_zone(self):
        """X coordinate where jamb clearance ends (right side)."""
        return self.x + self.w + self.clearances.jamb_min

    @property
    def top_clearance_zone(self):
        """Y coordinate where header clearance ends (top side)."""
        return self.y + self.h + self.clearances.header_min

    @property
    def bottom_clearance_zone(self):
        # Never allow clearance zones to go below wall base
        return max(0.0, self.y - self.clearances.sill_min)


# ------------------ PANEL DIMENSION CONFIGURATION (LEGACY CONSTANTS) ------------------
PANEL_WIDTH_MIN = 24
PANEL_HEIGHT_MIN = 24
LONG_MAX = 348
SHORT_MAX = 138
DIMENSION_INCREMENT = 1


def snap_down(value, inc):
    try:
        value = float(value)
        inc = float(inc)
        return (value // inc) * inc
    except Exception:
        return 0

def snap_up(value, inc):
    try:
        value = float(value)
        inc = float(inc)
        return ((value + inc - 1) // inc) * inc
    except Exception:
        return 0


# =============================================================================
# CONFIGURATION SYSTEM
# =============================================================================
class PanelConstraints(object):
    def __init__(self, min_width=24.0, max_width=138, min_height=24.0, max_height=348.0,
                 short_max=138, long_max=348.0, dimension_increment=1.0, panel_spacing=0.125):
        self.min_width = float(min_width)
        self.max_width = float(max_width)
        self.min_height = float(min_height)
        self.max_height = float(max_height)
        self.short_max = float(short_max)
        self.long_max = float(long_max)
        self.dimension_increment = float(dimension_increment)
        self.panel_spacing = float(panel_spacing)


class OptimizationStrategy(object):
    def __init__(self, prioritize_coverage=True, allow_vertical_stacking=True,
                 prefer_full_height_panels=True, fill_above_storefronts=True,
                 panel_orientation="vertical", minimize_unique_panels=False, 
                 use_ga_optimizer=False,
                 cutout_tolerance=0.0, opening_alignment="opening_derived", 
                 void1_x_offset_left=6.0, nonwindow_strategy="largest"):
        
        self.prioritize_coverage       = bool(prioritize_coverage)
        self.allow_vertical_stacking   = bool(allow_vertical_stacking)
        self.prefer_full_height_panels = bool(prefer_full_height_panels)
        self.fill_above_storefronts    = bool(fill_above_storefronts)
        self.panel_orientation         = str(panel_orientation)
        self.minimize_unique_panels    = bool(minimize_unique_panels)
        self.use_ga_optimizer          = bool(use_ga_optimizer)
        self.cutout_tolerance          = float(cutout_tolerance)
        self.opening_alignment         = str(opening_alignment)
        self.void1_x_offset_left       = float(void1_x_offset_left)
        self.nonwindow_strategy        = str(nonwindow_strategy)

class OptimizerConfig(object):
    def __init__(self, project_name="Default Project", panel_constraints=None,
                 door_clearances=None, window_clearances=None,
                 storefront_clearances=None, wall_opening_clearances=None,
                 optimization_strategy=None):
        self.project_name = project_name
        self.panel_constraints = panel_constraints or PanelConstraints()
        self.door_clearances = door_clearances or OpeningClearances()
        self.window_clearances = window_clearances or OpeningClearances()
        self.storefront_clearances = storefront_clearances or OpeningClearances()
        self.wall_opening_clearances = wall_opening_clearances or OpeningClearances(
            rough_jamb=0.0, rough_header=0.0, rough_sill=0.0,
            panel_jamb=0.0, panel_header=0.0, panel_sill=0.0
        )
        self.optimization_strategy = optimization_strategy or OptimizationStrategy()

    def to_dict(self):
        pc  = self.panel_constraints
        dc  = self.door_clearances
        wc  = self.window_clearances
        sc  = self.storefront_clearances
        woc = self.wall_opening_clearances
        os  = self.optimization_strategy
        
        return {
            "project_name": self.project_name,
            "panel_constraints": {
                "min_width": pc.min_width, "max_width": pc.max_width,
                "min_height": pc.min_height, "max_height": pc.max_height,
                "short_max": pc.short_max, "long_max": pc.long_max,
                "dimension_increment": pc.dimension_increment, "panel_spacing": pc.panel_spacing
            },
            "door_clearances": {
                "rough_jamb": dc.rough_jamb, "rough_header": dc.rough_header, "rough_sill": dc.rough_sill,
                "panel_jamb": dc.panel_jamb, "panel_header": dc.panel_header, "panel_sill": dc.panel_sill
            },
            "window_clearances": {
                "rough_jamb": wc.rough_jamb, "rough_header": wc.rough_header, "rough_sill": wc.rough_sill,
                "panel_jamb": wc.panel_jamb, "panel_header": wc.panel_header, "panel_sill": wc.panel_sill
            },
            "storefront_clearances": {
                "rough_jamb": sc.rough_jamb, "rough_header": sc.rough_header, "rough_sill": sc.rough_sill,
                "panel_jamb": sc.panel_jamb, "panel_header": sc.panel_header, "panel_sill": sc.panel_sill
            },
            "wall_opening_clearances": {
                "rough_jamb": woc.rough_jamb, "rough_header": woc.rough_header, "rough_sill": woc.rough_sill,
                "panel_jamb": woc.panel_jamb, "panel_header": woc.panel_header, "panel_sill": woc.panel_sill
            },
            "optimization_strategy": {
                "prioritize_coverage": os.prioritize_coverage,
                "allow_vertical_stacking": os.allow_vertical_stacking,
                "prefer_full_height_panels": os.prefer_full_height_panels,
                "fill_above_storefronts": os.fill_above_storefronts,
                "panel_orientation": os.panel_orientation,
                "minimize_unique_panels": os.minimize_unique_panels,
                "use_ga_optimizer": getattr(os, "use_ga_optimizer", False),
                "cutout_tolerance": os.cutout_tolerance,
                "opening_alignment": os.opening_alignment,
                "void1_x_offset_left": os.void1_x_offset_left,
                "nonwindow_strategy": os.nonwindow_strategy
            }
        }

    @classmethod
    def from_dict(cls, data):
        pc  = data.get("panel_constraints", {})
        dc  = data.get("door_clearances", {})
        wc  = data.get("window_clearances", {})
        sc  = data.get("storefront_clearances", {})
        woc = data.get("wall_opening_clearances", {})
        os_ = data.get("optimization_strategy", {})
        
        return cls(
            project_name=data.get("project_name", "Default Project"),
            panel_constraints=PanelConstraints(
                pc.get("min_width", 24.0), pc.get("max_width", 348.0),
                pc.get("min_height", 24.0), pc.get("max_height", 144.0),
                pc.get("short_max", 138), pc.get("long_max", 348.0),
                pc.get("dimension_increment", 1.0), pc.get("panel_spacing", 0.125)
            ),
            door_clearances=OpeningClearances(
                rough_jamb=dc.get("rough_jamb", 1.0), rough_header=dc.get("rough_header", 2.0), rough_sill=dc.get("rough_sill", 0.0),
                panel_jamb=dc.get("panel_jamb", 5.0), panel_header=dc.get("panel_header", 6.0), panel_sill=dc.get("panel_sill", 0.0)
            ),
            window_clearances=OpeningClearances(
                rough_jamb=wc.get("rough_jamb", 0.5), rough_header=wc.get("rough_header", 0.5), rough_sill=wc.get("rough_sill", 0.5),
                panel_jamb=wc.panel_jamb, panel_header=wc.get("panel_header", 7.5), panel_sill=wc.get("panel_sill", 5.5)
            ),
            storefront_clearances=OpeningClearances(
                rough_jamb=sc.get("rough_jamb", 0.5), rough_header=sc.get("rough_header", 0.5), rough_sill=sc.get("rough_sill", 0.0),
                panel_jamb=sc.get("panel_jamb", 5.5), panel_header=sc.get("panel_header", 7.5), panel_sill=sc.get("panel_sill", 0.0)
            ),
            wall_opening_clearances=OpeningClearances(
                rough_jamb=woc.get("rough_jamb", 0.0), rough_header=woc.get("rough_header", 0.0), rough_sill=woc.get("rough_sill", 0.0),
                panel_jamb=woc.get("panel_jamb", 0.0), panel_header=woc.get("panel_header", 0.0), panel_sill=woc.get("panel_sill", 0.0)
            ),
            optimization_strategy=OptimizationStrategy(
                prioritize_coverage=os_.get("prioritize_coverage", True),
                allow_vertical_stacking=os_.get("allow_vertical_stacking", True),
                prefer_full_height_panels=os_.get("prefer_full_height_panels", True),
                fill_above_storefronts=os_.get("fill_above_storefronts", True),
                panel_orientation=os_.get("panel_orientation", "vertical"),
                minimize_unique_panels=os_.get("minimize_unique_panels", False),
                use_ga_optimizer=os_.get("use_ga_optimizer", False),
                cutout_tolerance=os_.get("cutout_tolerance", 0.0),
                opening_alignment=os_.get("opening_alignment", "opening_derived").replace("as_placed", "opening_derived").replace("fixed_offset", "set_x_offset"),
                void1_x_offset_left=os_.get("void1_x_offset_left", os_.get("fixed_offset_in", 6.0)),
                nonwindow_strategy=os_.get("nonwindow_strategy", "largest")
            )
        )

    def save(self, filepath):
        try:
            f = io.open(filepath, "w", newline="")
        except TypeError:
            f = open(filepath, "w")
        with f:
            json.dump(self.to_dict(), f, indent=2)
        print("{} Config saved: {}{}".format(Ansi.GREEN, filepath, Ansi.RESET))

    @classmethod
    def load(cls, filepath):
        with open(filepath, "r") as f:
            data = json.load(f)
        return cls.from_dict(data)

def get_preset_configs():
    _woc_zero = OpeningClearances(
        rough_jamb=0.0, rough_header=0.0, rough_sill=0.0,
        panel_jamb=0.0, panel_header=0.0, panel_sill=0.0
    )
    presets = {}
    presets["vertical"] = OptimizerConfig(
        project_name="Vertical Panels",
        panel_constraints=PanelConstraints(
            min_width=24, max_width=138, min_height=24, max_height=348.0,
            short_max=138, long_max=348.0, dimension_increment=1, panel_spacing=0.125
        ),
        door_clearances=OpeningClearances(
            rough_jamb=1.0, rough_header=2.0, rough_sill=0.0,
            panel_jamb=5.0, panel_header=6.0, panel_sill=0.0
        ),
        window_clearances=OpeningClearances(
            rough_jamb=0.5, rough_header=0.5, rough_sill=0.5,
            panel_jamb=5.5, panel_header=7.5, panel_sill=5.5
        ),
        storefront_clearances=OpeningClearances(
            rough_jamb=0.5, rough_header=0.5, rough_sill=0.0,
            panel_jamb=5.5, panel_header=7.5, panel_sill=0.0
        ),
        wall_opening_clearances=_woc_zero,
        optimization_strategy=OptimizationStrategy(
            prioritize_coverage=True, allow_vertical_stacking=True,
            prefer_full_height_panels=True, fill_above_storefronts=True,
            panel_orientation="vertical", minimize_unique_panels=False,
            use_ga_optimizer=False
        )
    )
    presets["horizontal"] = OptimizerConfig(
        project_name="Horizontal Panels",
        panel_constraints=PanelConstraints(
            min_width=12, max_width=348.0, min_height=12, max_height=138,
            short_max=138, long_max=348.0, dimension_increment=1, panel_spacing=0.125
        ),
        door_clearances=OpeningClearances(
            rough_jamb=1.0, rough_header=2.0, rough_sill=0.0,
            panel_jamb=5.0, panel_header=6.0, panel_sill=0.0
        ),
        window_clearances=OpeningClearances(
            rough_jamb=0.5, rough_header=0.5, rough_sill=0.5,
            panel_jamb=5.5, panel_header=7.5, panel_sill=5.5
        ),
        storefront_clearances=OpeningClearances(
            rough_jamb=0.5, rough_header=0.5, rough_sill=0.0,
            panel_jamb=5.5, panel_header=7.5, panel_sill=0.0
        ),
        wall_opening_clearances=_woc_zero,
        optimization_strategy=OptimizationStrategy(
            prioritize_coverage=False, allow_vertical_stacking=True,
            prefer_full_height_panels=False, fill_above_storefronts=True,
            panel_orientation="horizontal", minimize_unique_panels=False,
            use_ga_optimizer=False
        )
    )
    return presets


# =============================================================================
# SECTION 2: DATA LOADING & VALIDATION (CSV-based)
# =============================================================================

def read_csv_rows(path):
    if not os.path.exists(path): return []
    rows = []
    with open(path, "r") as f:
        reader = csv.DictReader(f)
        for row in reader: rows.append(row)
    return rows


def load_walls_from_csv(walls_csv):
    if not os.path.exists(walls_csv):
        raise IOError("Walls CSV not found: {}".format(walls_csv))
    rows = read_csv_rows(walls_csv)
    print(Ansi.CYAN + "[INFO] Loaded {} walls from CSV".format(len(rows)) + Ansi.RESET)
    return rows


def load_openings_from_csv(openings_csv):
    if not os.path.exists(openings_csv):
        print(Ansi.YELLOW + "[WARN] Openings CSV not found." + Ansi.RESET)
        return []
    rows = read_csv_rows(openings_csv)
    norm_rows = []
    for r in rows:
        nr = {}
        for k, v in r.items():
            nk = k.strip() if isinstance(k, basestring) else k
            nr[nk] = v
        norm_rows.append(nr)
    print(Ansi.CYAN + "[INFO] Loaded {} openings from CSV".format(len(norm_rows)) + Ansi.RESET)
    return norm_rows


def _is_empty(v):
    if v is None: return True
    if isinstance(v, basestring):
        s = v.strip()
        return s == "" or s.lower() == "nan" or s.lower() == "none"
    try:
        return math.isnan(float(v))
    except Exception:
        return False


def safe_float(v, default=0.0):
    try:
        if _is_empty(v): return default
        return float(v)
    except Exception: return default


def get_wall_id(wall_row):
    for col in ["WallId", "ElementId", "Id"]:
        if col in wall_row and not _is_empty(wall_row.get(col)):
            val = wall_row.get(col)
            try: return str(int(float(val)))
            except: return str(val)
    if "Name" in wall_row and not _is_empty(wall_row.get("Name")):
        return str(wall_row.get("Name"))
    return "unknown"


def get_wall_dimensions(wall_row):
    try:
        length_ft = safe_float(wall_row.get("Length(ft)", 0))
        height_ft = safe_float(wall_row.get("UnconnectedHeight(ft)", 0))
        if length_ft <= 0 or height_ft <= 0: return None
        return (float((length_ft * 12)), float((height_ft * 12)))
    except Exception: return None


def _parse_xyz(xyz_str):
    """Parse '(X,Y,Z)' string from Revit CSV into (float,float,float). Returns None on failure."""
    try:
        s = str(xyz_str).strip().lstrip('(').rstrip(')')
        parts = [float(p.strip()) for p in s.split(',')]
        if len(parts) == 3:
            return tuple(parts)
    except Exception:
        pass
    return None


def get_wall_base_z_ft(wall_row):
    """
    Extract the wall curve start Z in Revit feet from the wall CSV row.
    Uses Start(X,Y,Z) Z component. Falls back to 0.0 if missing.
    Used to convert absolute opening Location Z into panel-local y_in offsets.
    """
    try:
        xyz = _parse_xyz(wall_row.get("Start(X,Y,Z)", ""))
        if xyz is not None:
            return float(xyz[2])
    except Exception:
        pass
    return 0.0


def get_wall_openings(wall_id, openings_rows, door_clearances, window_clearances,
                      storefront_clearances, wall_opening_clearances=None,
                      wall_base_z_ft=0.0):
    if not openings_rows: return []
    if wall_opening_clearances is None:
        wall_opening_clearances = OpeningClearances(
            rough_jamb=0.0, rough_header=0.0, rough_sill=0.0,
            panel_jamb=0.0, panel_header=0.0, panel_sill=0.0
        )
    try:
        wall_id_int = int(float(wall_id))
    except Exception: return []
    
    wall_openings = [r for r in openings_rows if safe_float(r.get("HostWallId"), None) == wall_id_int]
    if not wall_openings: return []

    openings = []
    for row in wall_openings:
        width_ft = safe_float(row.get("Width(ft)", 0))
        height_ft = safe_float(row.get("Height(ft)", 0))
        sill_ft = safe_float(row.get("SillHeight(ft)", 0))
        
        if width_ft <= 0 or height_ft <= 0: continue
        
        left_ft = safe_float(row.get("LeftEdgeAlongWall(ft)", 0))
        if left_ft == 0 and "PositionAlongWall(ft)" in row:
            pos = safe_float(row.get("PositionAlongWall(ft)", 0))
            if pos != 0: left_ft = pos - (width_ft/2.0)

        # Skip openings with no recoverable position — they would be placed at x=0
        # which creates phantom clearance zones at the wall start.
        # This happens when doors are hosted by a curtain wall sub-element rather
        # than the outer basic wall directly (Revit can't report their wall position).
        has_left_edge = not _is_empty(row.get("LeftEdgeAlongWall(ft)"))
        has_position  = not _is_empty(row.get("PositionAlongWall(ft)"))
        has_location  = not _is_empty(row.get("Location(X,Y,Z)"))
        if not has_left_edge and not has_position and not has_location:
            print(Ansi.YELLOW + "[WARN] Opening {} (type={}) has no position data "
                  "- skipping to avoid phantom placement at x=0".format(
                  row.get("OpeningId", "?"), row.get("OpeningType", "?")) + Ansi.RESET)
            continue

        x_in = float(left_ft * 12)
        # Prefer absolute Location Z minus wall base Z for correct multi-level support.
        # SillHeight(ft) is relative to the opening's own level — wrong for L2+ openings.
        loc_xyz = _parse_xyz(row.get("Location(X,Y,Z)", ""))
        if loc_xyz is not None:
            y_in = max(0.0, (loc_xyz[2] - wall_base_z_ft) * 12.0)
        else:
            y_in = float(sill_ft * 12)
            opening_level = str(row.get("Level", "")).strip()
            if opening_level and not _is_empty(opening_level) and sill_ft > 0:
                print(Ansi.YELLOW + "[WARN] Opening {} has no Location(X,Y,Z); "
                      "SillHeight={:.3f}ft may be level-relative for Level='{}'. "
                      "Result may be wrong for multi-level walls.".format(
                      row.get("OpeningId", "?"), sill_ft, opening_level) + Ansi.RESET)
        w_in = float(width_ft * 12)
        h_in =float(height_ft * 12)
        
        opening_type = str(row.get("OpeningType", "Unknown"))
        otype_lower = opening_type.lower()

        if "door" in otype_lower:
            clearances = door_clearances
        elif ("storefront" in otype_lower) or ("curtain" in otype_lower):
            clearances = storefront_clearances
            opening_type = "Storefront/Curtain"
        elif "wall opening" in otype_lower or otype_lower == "wall opening":
            clearances = wall_opening_clearances
        else:
            clearances = window_clearances

        openings.append(Opening(row.get("OpeningId", ""), opening_type, x_in, y_in, w_in, h_in, clearances))
    
    return openings


def adjust_panels_for_small_openings(panels, openings, constraints, dim_inc):
    SMALL_W = 72.0 
    SMALL_H = 120.0
    spacing = constraints.panel_spacing

    for opening in openings:
        if not (opening.w < SMALL_W and opening.h < SMALL_H): continue

        opening_right = opening.x + opening.w
        band_panels = [
            p for p in panels
            if not (p.y + p.h <= opening.y or p.y >= opening.y + opening.h)
        ]
        band_panels.sort(key=lambda p: p.x)

        for i in range(len(band_panels) - 1):
            left = band_panels[i]
            right = band_panels[i + 1]
            actual_seam = left.x + left.w + spacing

            if abs(right.x - actual_seam) > 1.0: continue
            if not (opening.x < actual_seam < opening_right): continue

            new_seam = snap_down(opening.x, dim_inc)
            if new_seam <= left.x: continue

            delta = actual_seam - new_seam
            new_left_fab_w = left.w - delta
            new_right_x = new_seam
            new_right_fab_w = (right.x + right.w) - new_right_x

            if new_left_fab_w < constraints.min_width or new_right_fab_w < constraints.min_width:
                continue

            left.w = float(new_left_fab_w)
            right.x = float(new_right_x)
            right.w = float(new_right_fab_w)
            break
def evaluate_manufacturing_score(panels, constraints):
    """
    Calculates the DfMA manufacturing score for a given panel layout.
    Lower is better.
    """
    if not panels:
        return (9999, 9999, {"panels": 0, "unique": 0})

    total_panels = len(panels)
    
    # Calculate Unique Panel Types (Fabrication complexity)
    unique_signatures = set()
    for p in panels:
        # Round dimensions to 1/8" to group panels with microscopic floating-point differences
        sig_w = round(p.w * 8.0) / 8.0
        sig_h = round(p.h * 8.0) / 8.0
        
        # Cutout signature (A panel with a hole is fabricated differently than a solid one)
        cutouts = []
        for c in getattr(p, 'cutouts', []):
            cutouts.append((round(c['width_in'], 1), round(c['height_in'], 1)))
        cutouts.sort()
        
        unique_signatures.add((sig_w, sig_h, tuple(cutouts)))
        
    unique_types = len(unique_signatures)

    # --- NEW SCORING HIERARCHY ---
    # Score = Total Panels + (Unique Types * User Weight)
    primary_score = total_panels + (unique_types * weight)
    
    # Tie-breaker is the Total Panels
    tie_breaker = total_panels
    
    return (primary_score, tie_breaker, {"panels": total_panels, "unique": unique_types})
    

# =============================================================================
# SECTION 3: COLLISION DETECTION
# =============================================================================

def panels_overlap(p1, p2):
    return not (p1.x + p1.w <= p2.x or p2.x + p2.w <= p1.x or
                p1.y + p1.h <= p2.y or p2.y + p2.h <= p1.y)

def is_storefront_like(opening):
    otype = (opening.type or "").lower()
    return ("storefront" in otype) or ("curtain" in otype)

def classify_openings_dynamic(openings, constraints):
    """
    Decides if an opening is a CUTOUT (bridged) or BLOCKER (stop).
    [FIXED] Storefronts now follow same size rules as windows/doors.
    [FIXED] Updated for property-based clearances - must set rough/panel attributes, not computed properties.
    """
    max_panel_w = constraints.max_width
    spacing = constraints.panel_spacing

    for op in openings:
        otype = op.type.lower()
        is_storefront = "storefront" in otype or "curtain" in otype
        
        # Check if opening (including storefronts) fits within a panel
        required_span = op.w + op.original_clearances.jamb_min * 2
        
        if required_span <= max_panel_w:
            # Small enough to fit -> Cutout (bridge over it)
            op.force_blocker = False
            
            # Restore original clearances by copying rough and panel components
            op.clearances.rough_jamb = op.original_clearances.rough_jamb
            op.clearances.rough_header = op.original_clearances.rough_header
            op.clearances.rough_sill = op.original_clearances.rough_sill
            op.clearances.panel_jamb = op.original_clearances.panel_jamb
            op.clearances.panel_header = op.original_clearances.panel_header
            op.clearances.panel_sill = op.original_clearances.panel_sill
            
            if is_storefront:
                print("    [CUTOUT] Small Storefront {} (Width={:.1f}\") - will be bridged like a window.".format(op.id, op.w))
        else:
            # Too wide -> Blocker (split regions)
            op.force_blocker = True
            
            # Set minimal clearances = spacing (no rough opening, all panel clearance)
            op.clearances.rough_jamb = 0.0
            op.clearances.rough_header = 0.0
            op.clearances.rough_sill = 0.0
            op.clearances.panel_jamb = spacing
            op.clearances.panel_header = spacing
            op.clearances.panel_sill = spacing
            # Now jamb_min, header_min, sill_min properties will compute as 0 + spacing = spacing
            
            opening_type = "Storefront" if is_storefront else "Opening"
            print("    [BLOCKER] {} {} (Width={:.1f}\") > Max Panel. Gap set to {}.".format(
                opening_type, op.id, op.w, spacing))
            

def is_blocking_storefront(opening, constraints):
    return opening.force_blocker

def is_cutout_opening(opening, constraints):
    # Ensure Storefronts are included as cutouts
    valid_types = ["Window", "Door", "Storefront", "Curtain", "Opening"]
    
    # Safely get the type string whether the attribute is named 'type' or 'type_name'
    op_type = getattr(opening, 'type_name', getattr(opening, 'type', ''))
    
    # Check if the opening type matches our valid list
    is_valid_type = any(t in op_type for t in valid_types)
    
    return is_valid_type and not opening.force_blocker

def panel_overlaps_clearance(panel, openings, constraints, allow_intentional=False):
    p_right = panel.x + panel.w
    p_top = panel.y + panel.h

    for opening in openings:
        if allow_intentional: continue
        if is_cutout_opening(opening, constraints): continue

        if not (
            p_right <= opening.left_clearance_zone or
            panel.x >= opening.right_clearance_zone or
            p_top <= opening.bottom_clearance_zone or
            panel.y >= opening.top_clearance_zone
        ):
            return True
    return False


def fill_vertical_gap(region_x_start, region_x_end, gap_y_start, gap_y_end,
                      opening_left, opening_right, panels, panel_counter,
                      constraints, all_openings, label,
                      is_storefront=False):
    PANEL_WIDTH_MIN = constraints.min_width
    PANEL_HEIGHT_MIN = constraints.min_height
    SHORT_MAX = constraints.short_max
    LONG_MAX = constraints.long_max
    DIMENSION_INCREMENT = constraints.dimension_increment
    spacing = float(getattr(constraints, "panel_spacing", 0.0) or 0.0)

    if is_storefront:
        panel_x_start = region_x_start + spacing
        panel_x_end   = region_x_end - spacing
    else:
        panel_x_start = max(opening_left + spacing, region_x_start)
        panel_x_end   = min(opening_right - spacing, region_x_end)

    gap_width  = panel_x_end - panel_x_start
    gap_height = gap_y_end - gap_y_start

    if gap_width < PANEL_WIDTH_MIN or gap_height < PANEL_HEIGHT_MIN:
        return panel_counter

    y_cursor = gap_y_start
    while y_cursor < gap_y_end:
        remaining_height = gap_y_end - y_cursor
        if remaining_height < PANEL_HEIGHT_MIN: break

        panel_h = snap_down(remaining_height, DIMENSION_INCREMENT)
        if panel_h < PANEL_HEIGHT_MIN: break

        max_width = SHORT_MAX if panel_h > SHORT_MAX else LONG_MAX
        x_cursor = panel_x_start
        row_placed = False

        while x_cursor < panel_x_end:
            remaining_width = panel_x_end - x_cursor

            # [FIX] Absorb sub-min remainder into last panel rather than leaving a gap
            if remaining_width > 0 and remaining_width < PANEL_WIDTH_MIN:
                row_panels = [p for p in panels if abs(p.y - y_cursor) < 0.01
                              and p.x >= panel_x_start]
                if row_panels:
                    last = row_panels[-1]
                    extended_w = round(last.w + spacing + remaining_width, 8)
                    max_w_here = SHORT_MAX if panel_h > SHORT_MAX else LONG_MAX
                    if extended_w <= max_w_here:
                        last.w = extended_w
                        last.cutouts = calculate_panel_cutouts(last, all_openings)
                break

            if remaining_width < PANEL_WIDTH_MIN: break

            panel_w = min(remaining_width, max_width)
            panel_w = snap_down(panel_w, DIMENSION_INCREMENT)

            leftover = remaining_width - panel_w
            if leftover > 0 and leftover < (PANEL_WIDTH_MIN + spacing):
                panel_w = snap_down(remaining_width, DIMENSION_INCREMENT)

            if panel_w < PANEL_WIDTH_MIN or not is_valid_panel(panel_w, panel_h, constraints): break

            candidate = Panel(x_cursor, y_cursor, panel_w, panel_h, "P{:02d}".format(panel_counter))

            if any(panels_overlap(candidate, p) for p in panels): break
            if panel_overlaps_clearance(candidate, all_openings, constraints, allow_intentional=True): break

            candidate.cutouts = calculate_panel_cutouts(candidate, all_openings)
            panels.append(candidate)
            panel_counter += 1
            row_placed = True
            x_cursor += (panel_w + spacing)

        if row_placed:
            y_cursor += (panel_h + spacing)
        else:
            break

    return panel_counter


def calculate_segment_layout(start_x, target_x, max_w, min_w, inc, spacing):
    total_dist = target_x - start_x
    
    if total_dist < min_w: 
        return total_dist
        
    if total_dist <= max_w: 
        return snap_down(total_dist, inc)
    
    # FIX: Check if taking the maximum greedy width leaves a tiny "splinter" panel
    leftover = total_dist - max_w - spacing
    if 0 < leftover < min_w:
        # Splinter alert! 
        # Instead of max_w, split the remaining distance equally to generate 
        # two identically-sized standard panels that are larger than min_w.
        half_width = (total_dist - spacing) / 2.0
        return snap_down(half_width, inc)
        
    # GREEDY: Place largest possible panel first
    return snap_down(max_w, inc)

def place_panels_sequential(wall_width, wall_height, openings, constraints, orientation="vertical"):
    """
    Middleware Wrapper: Intercepts constraints before handing them to the greedy engine.
    If Horizontal Mode is active, it safely swaps the 10-foot max_width ceiling 
    for the massive long_max ceiling, allowing the engine to swallow entire blank walls.
    """
    import copy
    
    # Create a local copy so we don't permanently overwrite the user's global config
    active_constraints = copy.copy(constraints)
    
    if str(orientation).lower() == "horizontal":
        # Unlock the massive width limit for horizontal panel generation!
        active_constraints.max_width = constraints.long_max
        
    # Pass the unlocked constraints into your original, untouched logic
    return _core_place_panels_sequential(wall_width, wall_height, openings, active_constraints, orientation)


def _core_place_panels_sequential(wall_width, wall_height, openings, constraints, orientation="vertical"):
    """
    Fixed panel placement with Lookahead Logic + Seam Validation.
    """
    orientation = str(orientation or "vertical").lower()
    horizontal_mode = (orientation == "horizontal")

    # 1. Run Dynamic Classification
    classify_openings_dynamic(openings, constraints)

    # Bind constraints
    PANEL_WIDTH_MIN = constraints.min_width
    PANEL_HEIGHT_MIN = constraints.min_height
    SHORT_MAX = constraints.short_max
    LONG_MAX = constraints.long_max
    DIMENSION_INCREMENT = constraints.dimension_increment
    spacing = float(getattr(constraints, "panel_spacing", 0.0) or 0.0)

    panels = []
    panel_counter = 1

    sorted_openings = sorted(openings, key=lambda o: o.x)
    blocking_storefronts = [o for o in sorted_openings if is_blocking_storefront(o, constraints)]
    regular_openings = [o for o in sorted_openings if o not in blocking_storefronts]

    # BUILD X-REGIONS
    regions = []
    if not blocking_storefronts:
        regions.append({
            'x_start': 0, 'x_end': wall_width,
            'y_start': 0, 'y_end': wall_height,
            'openings': regular_openings
        })
    else:
        storefronts_sorted = sorted(blocking_storefronts, key=lambda sf: sf.left_clearance_zone)
        x_boundaries = [0]
        for sf in storefronts_sorted:
            x_boundaries.extend([sf.left_clearance_zone, sf.right_clearance_zone])
        x_boundaries.append(wall_width)
        x_boundaries = sorted(list(set(x_boundaries)))

        for i in range(len(x_boundaries) - 1):
            x_start, x_end = x_boundaries[i], x_boundaries[i + 1]
            if (x_end - x_start) < PANEL_WIDTH_MIN: continue

            blocked = any(
                not (x_end <= sf.left_clearance_zone or x_start >= sf.right_clearance_zone)
                for sf in storefronts_sorted
            )
            if not blocked:
                region_openings_list = [
                    o for o in regular_openings
                    if not (o.right_clearance_zone <= x_start or o.left_clearance_zone >= x_end)
                ]
                regions.append({
                    'x_start': x_start, 'x_end': x_end,
                    'y_start': 0, 'y_end': wall_height,
                    'openings': region_openings_list
                })

    # PROCESS EACH REGION
    for region in regions:
        region_openings = region['openings']
        bands = []
        if horizontal_mode:
            cy = 0
            while cy < wall_height:
                rem_h = wall_height - cy
                bh = snap_down(min(rem_h, SHORT_MAX), DIMENSION_INCREMENT)
                if bh >= PANEL_HEIGHT_MIN:
                    bands.append((cy, cy + bh))
                    cy += bh
                else: break
        else:
            bands = [(region['y_start'], region['y_end'])]

        for y_start, y_end in bands:
            band_height = y_end - y_start
            max_width_for_band = SHORT_MAX if band_height > SHORT_MAX else LONG_MAX
            x_cursor = max(0.0, region['x_start'])

            while x_cursor < region['x_end']:
                remaining_wall = region['x_end'] - x_cursor

                # [FIX] Widen last panel to absorb end-of-wall remainders
                if remaining_wall > 0 and remaining_wall < PANEL_WIDTH_MIN:
                    band_panels = [p for p in panels if abs(p.y - y_start) < 0.01]
                    if band_panels:
                        last = band_panels[-1]
                        extended_w = round(last.w + spacing + remaining_wall, 8)
                        if extended_w <= max_width_for_band:
                            last.w = extended_w
                            last.cutouts = calculate_panel_cutouts(last, region_openings)
                            print("    [FIX] Widened {} to {:.3f}\" to absorb remainder".format(last.name, extended_w))
                        else:
                            snap_w = round(remaining_wall, 8)
                            candidate = Panel(x_cursor, y_start, snap_w, band_height, "P{:02d}".format(panel_counter))
                            candidate.cutouts = calculate_panel_cutouts(candidate, region_openings)
                            panels.append(candidate)
                            panel_counter += 1
                            print("    [FIX] Placed narrow end panel {:.3f}\"".format(snap_w))
                    break

                if remaining_wall < PANEL_WIDTH_MIN: break

                # --- FIX: Prevent swallowing Storefronts/Doors ---
                future_openings = [
                    o for o in region_openings
                    if (o.right_clearance_zone > x_cursor + 0.01)
                    and not (o.top_clearance_zone <= y_start or o.bottom_clearance_zone >= y_end)
                    and not is_cutout_opening(o, constraints)
                ]
                next_opening = min(future_openings, key=lambda o: o.left_clearance_zone) if future_openings else None

                hard_stop_x = region['x_end']
                target_is_opening = False

                if next_opening:
                    bridge_dist = next_opening.right_clearance_zone - x_cursor
                    can_bridge = (bridge_dist <= max_width_for_band and bridge_dist >= PANEL_WIDTH_MIN)

                    if can_bridge:
                        panel_w = snap_down(bridge_dist, DIMENSION_INCREMENT)
                        candidate = Panel(x_cursor, y_start, panel_w, band_height, "P{:02d}".format(panel_counter))
                        candidate.cutouts = calculate_panel_cutouts(candidate, region_openings)
                        panels.append(candidate)
                        panel_counter += 1
                        x_cursor += (panel_w + spacing)
                        continue
                    else:
                        hard_stop_x = next_opening.left_clearance_zone
                        target_is_opening = True

                dist_to_stop = hard_stop_x - x_cursor
                if dist_to_stop < PANEL_WIDTH_MIN:
                    if target_is_opening: x_cursor = next_opening.right_clearance_zone
                    else: break
                    continue

                panel_w = calculate_segment_layout(x_cursor, hard_stop_x, max_width_for_band, PANEL_WIDTH_MIN, DIMENSION_INCREMENT, spacing)
                candidate_right = x_cursor + panel_w
                
                # Check for jamb clashes and adjust
                for op in region_openings:
                    if (op.left_clearance_zone + 0.1) < candidate_right < (op.right_clearance_zone - 0.1):
                        dist_to_left_jamb = op.left_clearance_zone - x_cursor
                        if dist_to_left_jamb >= PANEL_WIDTH_MIN:
                            panel_w = snap_down(dist_to_left_jamb, DIMENSION_INCREMENT)
                        else:
                            dist_to_right_jamb = op.right_clearance_zone - x_cursor
                            width_to_clear = snap_up(dist_to_right_jamb, DIMENSION_INCREMENT)
                            if width_to_clear <= max_width_for_band: panel_w = width_to_clear
                            else: panel_w = snap_down(max_width_for_band, DIMENSION_INCREMENT)
                        break

                if not is_valid_panel(panel_w, band_height, constraints): break

                candidate = Panel(x_cursor, y_start, panel_w, band_height, "P{:02d}".format(panel_counter))
                if panel_overlaps_clearance(candidate, region_openings, constraints, allow_intentional=False):
                    print("    [WARN] Panel overlaps hard clearance")

                candidate.cutouts = calculate_panel_cutouts(candidate, region_openings)
                panels.append(candidate)
                panel_counter += 1
                x_cursor += (panel_w + spacing)

                if target_is_opening and abs(x_cursor - (hard_stop_x + spacing)) < 1.0:
                    x_cursor = next_opening.right_clearance_zone

        # FILL VERTICAL GAPS
        gap_openings = [o for o in region_openings if not is_cutout_opening(o, constraints)
                        and not is_storefront_like(o)]
        for opening in gap_openings:
            if opening.bottom_clearance_zone <= 0 and opening.top_clearance_zone >= wall_height:
                continue

            if opening.bottom_clearance_zone > 0:
                gap_height = opening.bottom_clearance_zone - 0
                if gap_height >= PANEL_HEIGHT_MIN:
                    panel_counter = fill_vertical_gap(
                        region['x_start'], region['x_end'],
                        0, opening.bottom_clearance_zone,
                        opening.left_clearance_zone, opening.right_clearance_zone,
                        panels, panel_counter, constraints, sorted_openings,
                        "below"
                    )

            if opening.top_clearance_zone < wall_height:
                gap_height = wall_height - opening.top_clearance_zone
                if gap_height >= PANEL_HEIGHT_MIN:
                    panel_counter = fill_vertical_gap(
                        region['x_start'], region['x_end'],
                        opening.top_clearance_zone, wall_height,
                        opening.left_clearance_zone, opening.right_clearance_zone,
                        panels, panel_counter, constraints, sorted_openings,
                        "above",
                        False
                    )

        region_panels = [p for p in panels if (region['x_start'] <= p.x < region['x_end'])]
        adjust_panels_for_small_openings(region_panels, region_openings, constraints, DIMENSION_INCREMENT)

    # EXTRA: Fill ABOVE AND BELOW BLOCKING storefront spans
    extra_filled = 0
    for sf in blocking_storefronts:
        # Fill ABOVE
        if sf.top_clearance_zone < wall_height:
            gap_height = wall_height - sf.top_clearance_zone
            if gap_height >= PANEL_HEIGHT_MIN:
                before_count = len(panels)
                panel_counter = fill_vertical_gap(
                    sf.left_clearance_zone, sf.right_clearance_zone,
                    sf.top_clearance_zone, wall_height,
                    sf.left_clearance_zone, sf.right_clearance_zone,
                    panels, panel_counter, constraints, sorted_openings,
                    "above",
                    True
                )
                extra_filled += len(panels) - before_count
        
        # Fill BELOW
        if sf.bottom_clearance_zone > 0:
            gap_height = sf.bottom_clearance_zone - 0
            if gap_height >= PANEL_HEIGHT_MIN:
                before_count = len(panels)
                panel_counter = fill_vertical_gap(
                    sf.left_clearance_zone, sf.right_clearance_zone,
                    0, sf.bottom_clearance_zone,
                    sf.left_clearance_zone, sf.right_clearance_zone,
                    panels, panel_counter, constraints, sorted_openings,
                    "below",
                    True
                )
                extra_filled += len(panels) - before_count

    return panels

    
def _place_ga_optimized(wall_width, wall_height, openings, constraints, orientation, strategy):
    """
    Placement engine for the Genetic Algorithm strategy.
    Runs the GA to find the optimal sequence, then converts that sequence 
    into physical Panel objects that respect your opening clearances.
    """
    print(Ansi.MAGENTA + "  [GA] Solving for optimal layout using GA sequence..." + Ansi.RESET)
    
    # 1. Run the GA to get the "Winning Chromosome" 
    # FIX: Remove 'openings' and add 'strategy' to match the new solve_with_ga definition
    best_sequence = solve_with_ga(wall_width, constraints, strategy)
    
    panels = []
    panel_counter = 1
    x_cursor = 0.0
    spacing = constraints.panel_spacing
    
    # 2. Convert the GA widths into Panel objects
    for w in best_sequence:
        # Safety check: stop if the GA sequence is longer than the wall
        if x_cursor + w > wall_width:
            break
            
        # Create the panel
        p = Panel(x_cursor, 0, w, wall_height, "PGA{:02d}".format(panel_counter))
        
        # 3. Calculate cutouts (this uses your existing clearance/opening logic)
        # This ensures that even though the GA chose the width, the cutouts
        # remain correctly positioned relative to the window openings.
        p.cutouts = calculate_panel_cutouts(p, openings)
        
        panels.append(p)
        panel_counter += 1
        x_cursor += (w + spacing)
        
    print(Ansi.GREEN + "  [GA] Successfully placed {} GA-optimized panels."
          .format(len(panels)) + Ansi.RESET)
        
    return panels


    
def calculate_panel_cutouts(panel, openings):
    cutouts = []
    p_left = panel.x
    p_right = panel.x + panel.w
    p_bottom = panel.y
    p_top = panel.y + panel.h

    for opening in openings:
        # --- FIX 1: Partial string matching for "Storefront/Curtain" ---
        # Look for the type safely. Use getattr in case your object uses 'type' instead of 'type_name'
        op_type = getattr(opening, 'type_name', getattr(opening, 'type', ''))
        is_cutout = any(term in op_type for term in ["Window", "Door", "Storefront", "Curtain", "Opening"])
        
        # --- FIX 2: Override force_blocker for valid cutouts ---
        # If it's not a valid cutout AND it's a blocker, we skip it.
        # Otherwise, we proceed to check for physical intersections.
        if not is_cutout and opening.force_blocker: 
            continue

        hole_left = opening.x - opening.clearances.rough_jamb
        hole_right = opening.x + opening.w + opening.clearances.rough_jamb
        hole_bottom = opening.y - opening.clearances.rough_sill
        hole_top = opening.y + opening.h + opening.clearances.rough_header

        inter_left = max(p_left, hole_left)
        inter_right = min(p_right, hole_right)
        inter_bottom = max(p_bottom, hole_bottom)
        inter_top = min(p_top, hole_top)

        # If there is a physical overlap between the panel and the hole
        if inter_right > inter_left and inter_top > inter_bottom and is_cutout:
            cutout_x = inter_left   - p_left
            cutout_y = inter_bottom - p_bottom
            cutout_w = inter_right  - inter_left
            cutout_h = inter_top    - inter_bottom

            # Raw opening position relative to panel bottom-left
            # (used by placement script as UNIT WIDTH/HEIGHT — the actual opening size)
            raw_x = opening.x - p_left    # left edge of raw opening from panel left
            raw_y = max(0.0, opening.y - p_bottom)  # bottom of raw opening from panel bottom, clamped to 0

            cutout_info = {
                "id":            opening.id,
                "type":          opening.type,
                # Cutout box (includes rough clearance — defines physical void shape)
                "x_in":          float(cutout_x),
                "y_in":          float(cutout_y),
                "width_in":      float(cutout_w),
                "height_in":     float(cutout_h),
                # Raw opening dimensions (no clearance — use for UNIT WIDTH/HEIGHT family params)
                "raw_x_in":      float(raw_x),
                "raw_y_in":      float(raw_y),
                "raw_width_in":  float(opening.w),
                "raw_height_in": float(opening.h),
                # Exact rough clearance — needed to recompute x_in after panel shifts
                "rough_jamb":    float(opening.clearances.rough_jamb),
            }
            cutouts.append(cutout_info)

    # Sort: Void 1 = highest on panel (largest y_in), ties broken left-to-right.
    cutouts.sort(key=lambda c: (-c["y_in"], c["x_in"]))
    return cutouts


def solve_with_ga(zone_len, constraints, strategy):
    import random
    
    POP_SIZE = 50
    # Bumped slightly to give the GA time to find the right length
    GENERATIONS = 50 
    MUTATION_RATE = 0.3
    
    min_w = int(constraints.min_width)
    max_w = int(constraints.max_width)
    inc = int(constraints.dimension_increment)
    
    if min_w >= max_w: max_w = min_w + inc
        
    valid_widths = list(range(min_w, max_w + inc, inc))
    if not valid_widths: valid_widths = [min_w]

    # Read the dynamic weight from the UI (defaults to 10.0 if not found)
    user_weight = getattr(strategy, 'unique_weight', 10.0)

    def fitness(chromosome):
        total_panels = len(chromosome)
        unique_types = len(set(chromosome))
        
        length_penalty = abs(zone_len - sum(chromosome)) * 100.0 
        
        # --- NEW DYNAMIC FITNESS ---
        # Evaluates exactly based on the user's UI input
        score = (unique_types * user_weight) + total_panels + length_penalty
        
        return score

    # 1. Generate initial population with length variance
    population = []
    for _ in range(POP_SIZE):
        guess_w = random.choice(valid_widths)
        num_panels = max(1, int(zone_len // guess_w))
        population.append([random.choice(valid_widths) for _ in range(num_panels)])

    for gen in range(GENERATIONS):
        population.sort(key=fitness)
        next_gen = population[:10] # Elitism
        
        while len(next_gen) < POP_SIZE:
            parent1, parent2 = random.sample(population[:20], 2)
            
            # Crossover (Safely handles parents of different lengths)
            split1 = len(parent1) // 2
            split2 = len(parent2) // 2
            child = parent1[:split1] + parent2[split2:]
            
            # 2. Dynamic Mutation: Allow adding, removing, or modifying panels
            if random.random() < MUTATION_RATE and child:
                mut_type = random.choice(["modify", "add", "remove"])
                
                if mut_type == "modify":
                    idx = random.randint(0, len(child)-1)
                    child[idx] = random.choice(valid_widths)
                
                elif mut_type == "add":
                    child.insert(random.randint(0, len(child)), random.choice(valid_widths))
                
                elif mut_type == "remove" and len(child) > 1:
                    child.pop(random.randint(0, len(child)-1))
                    
            next_gen.append(child)
        population = next_gen

    population.sort(key=fitness)
    return population[0] # Return the best sequence

def _cutout_fingerprint(cutout):
    """
    Exact hashable fingerprint for one cutout using raw opening dimensions.
    Uses raw_x_in/raw_y_in (opening position) and raw_width_in/raw_height_in
    (opening size) when available, falling back to cutout box values.
    All values rounded to 4 decimal places to avoid floating point noise.
    """
    x = round(float(cutout.get("raw_x_in",  cutout.get("x_in",      0))), 4)
    y = round(float(cutout.get("raw_y_in",  cutout.get("y_in",      0))), 4)
    w = round(float(cutout.get("raw_width_in",  cutout.get("width_in",  0))), 4)
    h = round(float(cutout.get("raw_height_in", cutout.get("height_in", 0))), 4)
    return (x, y, w, h)


def _opening_size_key(cutout):
    """Key based only on opening SIZE (not position) — used for grouping by opening type."""
    w = round(float(cutout.get("raw_width_in",  cutout.get("width_in",  0))), 4)
    h = round(float(cutout.get("raw_height_in", cutout.get("height_in", 0))), 4)
    return (w, h)


def _panel_type_key(panel, alignment):
    """
    Hashable key for grouping panels into fabrication types.
    UPDATED: Includes Mirror-Symmetry detection. Left-offset and 
    Right-offset equivalents will now group as the exact same type.
    """
    bw = round(float(panel.w), 4)
    bh = round(float(panel.h), 4)

    if alignment == "as_placed":
        # 1. Standard orientation cuts
        cuts = sorted(
            [_cutout_fingerprint(c) for c in panel.cutouts],
            key=lambda t: (-t[1], t[0])
        )
        
        # 2. Mirrored orientation cuts (Flipped across panel width)
        mirrored_cuts = []
        for c in panel.cutouts:
            w = round(float(c.get("raw_width_in", c.get("width_in", 0))), 4)
            x = round(float(c.get("raw_x_in", c.get("x_in", 0))), 4)
            y = round(float(c.get("raw_y_in", c.get("y_in", 0))), 4)
            h = round(float(c.get("raw_height_in", c.get("height_in", 0))), 4)
            
            # The mathematical mirror: (Panel Width) - (Cutout X) - (Cutout Width)
            mirrored_x = round(bw - x - w, 4)
            mirrored_cuts.append((mirrored_x, y, w, h))
            
        mirrored_cuts = sorted(mirrored_cuts, key=lambda t: (-t[1], t[0]))
        
        # 3. Canonical Selection: Whichever tuple evaluates as smaller becomes the universal ID.
        # This forces a left-heavy panel and a right-heavy panel to share the exact same key.
        cut_keys = tuple(min(cuts, mirrored_cuts))
        
    else:
        # Group by opening SIZE only (center / set_x_offset alignment handles position later)
        cut_keys = tuple(sorted(
            [_opening_size_key(c) for c in panel.cutouts],
            key=lambda t: (-t[1], t[0])
        ))
        
    return (bw, bh, cut_keys)


def normalize_panel_types(panels, alignment, fixed_offset_in, constraints):
    """
    Groups panels by identical geometries and assigns Type labels (T01, T02).
    Now recognizes Left-Hand and Right-Hand mirrored panels as the exact same Type!
    """
    unique_types = {}
    type_counter = 1

    for p in panels:
        sig_w = round(p.w * 8.0) / 8.0
        sig_h = round(p.h * 8.0) / 8.0

        std_cutouts = []
        mir_cutouts = []

        for c in getattr(p, 'cutouts', []):
            cx = round(c['x_in'], 2)
            cy = round(c['y_in'], 2)
            cw = round(c['width_in'], 2)
            ch = round(c['height_in'], 2)

            std_cutouts.append((cx, cy, cw, ch))

            # Simulate the panel being flipped to group LH/RH twins
            mir_x = round(p.w - c['x_in'] - c['width_in'], 2)
            mir_cutouts.append((mir_x, cy, cw, ch))

        std_cutouts.sort()
        mir_cutouts.sort()

        std_sig = (sig_w, sig_h, tuple(std_cutouts))
        mir_sig = (sig_w, sig_h, tuple(mir_cutouts))

        # Always take the mathematically smaller signature so mirrors snap to the same ID
        canonical_sig = min(std_sig, mir_sig)

        if canonical_sig not in unique_types:
            unique_types[canonical_sig] = "T{:02d}".format(type_counter)
            type_counter += 1

        p.name = unique_types[canonical_sig]

    return panels

    dim_inc = constraints.dimension_increment if constraints else 1.0
    min_w   = constraints.min_width           if constraints else 24.0

    # ── Step 1: Build groups ──────────────────────────────────────────────
    groups = {}
    order  = []
    for p in panels:
        key = _panel_type_key(p, alignment)
        if key not in groups:
            groups[key] = []
            order.append(key)
        groups[key].append(p)

    # Sort by group size desc, then by first panel x
    sorted_keys = sorted(order, key=lambda k: (-len(groups[k]), groups[k][0].x))

    # ── Step 2: For center/fixed_offset, align panel positions ───────────
    if alignment in ("center", "fixed_offset") and constraints is not None:
        for key in sorted_keys:
            group = groups[key]
            if not group[0].cutouts:
                continue  # no opening → nothing to align

            panel_w = float(key[0])

            for p in group:
                # Use first cutout (primary opening) to determine shift.
                # Capture each cutout's absolute wall position NOW, before p.x
                # is mutated, so all arithmetic below stays correct.
                c = p.cutouts[0]
                open_w = float(c.get("raw_width_in", c.get("width_in", 0)))
                cut_wall_xs = [
                    p.x + float(cut.get("raw_x_in", cut.get("x_in", 0)))
                    for cut in p.cutouts
                ]
                open_x_wall = cut_wall_xs[0]  # primary opening for shift calc

                if alignment == "center":
                    # Panel left = opening_center - panel_w/2
                    new_panel_x = open_x_wall + open_w / 2.0 - panel_w / 2.0
                else:  # fixed_offset
                    # Panel left = opening_left - fixed_offset_in
                    new_panel_x = open_x_wall - fixed_offset_in

                # Snap and clamp
                new_panel_x = round(round(new_panel_x / dim_inc) * dim_inc, 8)
                new_panel_x = max(0.0, new_panel_x)

                if new_panel_x == p.x:
                    continue

                # Guard: reject the shift if it causes an overlap with any
                # other already-placed panel.
                test = _copy.copy(p)
                test.x = float(new_panel_x)
                if any(panels_overlap(test, other) for other in panels if other is not p):
                    print("    [ALIGN] Skipping shift for {} — new x={:.3f} would overlap "
                          "a neighbor (was {:.3f})".format(p.name, new_panel_x, p.x))
                    continue

                p.x = float(new_panel_x)

                # Recompute every cutout's x offsets relative to the new panel
                # left, using the pre-captured absolute wall positions and the
                # exact rough_jamb stored in each cutout dict.
                for cut, cut_open_x_wall in zip(p.cutouts, cut_wall_xs):
                    rough_j = float(cut.get("rough_jamb", 0.0))
                    cut["raw_x_in"] = float(cut_open_x_wall - p.x)
                    cut["x_in"]     = max(0.0, cut["raw_x_in"] - rough_j)

    # ── Step 3: Assign type labels ────────────────────────────────────────
    type_counter = 1
    for key in sorted_keys:
        group = groups[key]
        label = "T{:02d}".format(type_counter)
        type_counter += 1
        for inst_idx, p in enumerate(group, start=1):
            p.name = "{}-P{:02d}".format(label, inst_idx)

    # ── Step 4: Summary log ───────────────────────────────────────────────
    unique   = len(groups)
    repeated = sum(1 for g in groups.values() if len(g) > 1)
    print(Ansi.CYAN + "  [TYPES] {} unique type(s) across {} panels "
          "({} type(s) used more than once)  alignment={}".format(
          unique, len(panels), repeated, alignment) + Ansi.RESET)
    for key in sorted_keys:
        group = groups[key]
        if len(group) > 1:
            print("    T{:02d}: {} panels  {}\"x{}\"  {} cutout(s)".format(
                sorted_keys.index(key) + 1,
                len(group),
                round(float(key[0]), 4),
                round(float(key[1]), 4),
                len(key[2])
            ))
    return panels


# =============================================================================
# SECTION 5: REPEATING PATTERN DETECTION & STANDARD PANEL STRATEGY
# =============================================================================

def detect_repeating_opening_groups(openings, constraints):
    """
    Find groups of openings that are identical in size AND evenly spaced.
    Returns a list of groups, each group being a sorted list of Opening objects.
    Only groups with 2+ members qualify.

    'Evenly spaced' means the center-to-center distance between consecutive
    openings is consistent within one dimension_increment.
    """
    if not openings:
        return []

    inc = constraints.dimension_increment

    # Bucket openings by (rounded_width, rounded_height)
    size_groups = {}
    for o in openings:
        if o.force_blocker:
            continue   # blockers cannot be standard panel openings
        key = (round(o.w / inc) * inc, round(o.h / inc) * inc)
        size_groups.setdefault(key, []).append(o)

    repeating = []
    for key, group in size_groups.items():
        if len(group) < 2:
            continue
        # Sort by x position
        group_sorted = sorted(group, key=lambda o: o.x)

        # Sub-group by sill height (y) so L1 and L2 windows are never mixed.
        # Two openings belong to the same sill-height band if their y values
        # are within one dimension_increment of each other.
        y_bands = []
        for o in group_sorted:
            placed = False
            for band in y_bands:
                if abs(o.y - band[0].y) <= inc:
                    band.append(o)
                    placed = True
                    break
            if not placed:
                y_bands.append([o])

        for band in y_bands:
            if len(band) < 2:
                continue
            band_sorted = sorted(band, key=lambda o: o.x)
            # Check center-to-center spacing consistency within this band
            centers    = [o.x + o.w / 2.0 for o in band_sorted]
            spacings   = [centers[i+1] - centers[i] for i in range(len(centers)-1)]
            ref        = spacings[0]
            consistent = all(abs(s - ref) <= inc for s in spacings)
            if consistent:
                repeating.append(band_sorted)
                print(Ansi.CYAN + "  [PATTERN] Repeating group: {} opening(s) "
                      "{}\"x{}\" y={:.1f}\" spaced {:.3f}\" c-to-c".format(
                      len(band_sorted), round(key[0], 3), round(key[1], 3),
                      round(band_sorted[0].y, 1), ref) + Ansi.RESET)

    return repeating


def compute_opening_derived_width(opening, window_spacing, constraints):
    """
    TILING MODULE STRATEGY:
    Finds the exact mathematical width to maintain perfect grid sync with windows.
    """
    d = constraints.panel_spacing
    max_w = min(constraints.short_max, constraints.long_max)
    
    n_panels = 1
    while n_panels < 10:
        W_exact = (window_spacing / n_panels) - d
        
        # FIX: Do not use snap_down here. Snapping destroys the mathematical 
        # period of the grid, causing panels to slowly drift away from the windows.
        if constraints.min_width <= W_exact <= max_w:
            optimum_left_jamb = opening.clearances.jamb_min
            return W_exact, optimum_left_jamb
            
        n_panels += 1
        
    return None, None


def find_standard_panel_width(opening, window_zone_length, constraints):
    """
    Most-Repetitions strategy (used by Center Openings and Set X Offset modes).

    Find the panel width W that:
      - Is >= min_width, snaps to dimension_increment
      - Opening fits with minimum physical clearance on both sides
      - window_zone_length / W is an integer (zone divides evenly)
      - Among all valid W, picks the SMALLEST (= most repetitions)

    Returns (W, N) or (None, 0) if no valid W found.
    """
    inc    = constraints.dimension_increment
    min_w  = constraints.min_width
    short_m = constraints.short_max
    long_m  = constraints.long_max

    # Minimum width: opening must fit with at least rough_jamb on each side
    rough_j    = opening.clearances.rough_jamb
    min_needed = opening.w + 2.0 * rough_j
    min_w_eff  = max(min_w, round(math.ceil(min_needed / inc) * inc, 8))
    max_w_eff  = snap_down(min(short_m, long_m), inc)

    if min_w_eff > max_w_eff:
        return None, 0

    w = min_w_eff
    while w <= max_w_eff:
        if window_zone_length > 0:
            n_raw = window_zone_length / w
            n     = int(round(n_raw))
            if n >= 1 and abs(n_raw - n) * w <= inc * 0.5:
                return w, n   # smallest valid W = most repetitions
        w = round(w + inc, 8)

    return None, 0


def place_standard_panels_in_zone(zone_start, zone_end, standard_w, opening,
                                   panel_h, panel_y, panel_counter, constraints,
                                   all_openings, opening_alignment,
                                   fixed_offset_in):
    """
    Fill [zone_start, zone_end] with N identical panels of width standard_w.
    Each panel gets a cutout for `opening` positioned according to opening_alignment.
    panel_y: the bottom y of the panel strip (0 for full-height, or gap_y_start
             for above-storefront fills).
    Returns (list of Panel objects, updated panel_counter).
    """
    panels = []
    n      = int(round((zone_end - zone_start) / standard_w))
    inc    = constraints.dimension_increment

    for i in range(n):
        panel_x = round(zone_start + i * standard_w, 8)
        p = Panel(panel_x, panel_y, standard_w, panel_h,
                  "P{:02d}".format(panel_counter))
        panel_counter += 1

        # Compute cutout x within panel based on alignment
        if opening_alignment == "center":
            open_x_in_panel = (standard_w - opening.w) / 2.0
        elif opening_alignment == "fixed_offset":
            open_x_in_panel = fixed_offset_in
        else:  # as_placed
            open_x_in_panel = opening.x - panel_x

        # Clamp so opening stays inside panel with jamb clearance
        jamb_min = opening.clearances.jamb_min
        open_x_in_panel = max(jamb_min, min(open_x_in_panel,
                                             standard_w - opening.w - jamb_min))
        open_x_in_panel = round(open_x_in_panel, 8)

        rough_j = opening.clearances.rough_jamb
        rough_s = opening.clearances.rough_sill
        rough_h = opening.clearances.rough_header

        # y offset is relative to panel bottom (panel_y)
        raw_y_in_panel = max(0.0, opening.y - panel_y)

        cutout = {
            "id":            opening.id,
            "type":          opening.type,
            "x_in":          max(0.0, open_x_in_panel - rough_j),
            "y_in":          max(0.0, raw_y_in_panel - rough_s),
            "width_in":      opening.w + 2 * rough_j,
            "height_in":     opening.h + rough_s + rough_h,
            "raw_x_in":      open_x_in_panel,
            "raw_y_in":      raw_y_in_panel,
            "raw_width_in":  opening.w,
            "raw_height_in": opening.h,
        }
        p.cutouts = [cutout]
        panels.append(p)

    return panels, panel_counter


def _clip_openings_to_band(openings, y0_in, y1_in):
    """
    Return Opening objects clipped to the vertical band [y0_in, y1_in].
    y coordinates are relative to the wall base; returned openings have
    y relative to y0_in (band bottom).

    Openings outside the band are dropped.
    Openings that straddle a band edge are clipped — rare in practice
    because windows/doors almost never span a floor-level boundary.
    """
    import copy as _cp
    result = []
    for op in openings:
        bottom = op.y
        top    = op.y + op.h
        if top <= y0_in or bottom >= y1_in:
            continue
        new_op         = _cp.deepcopy(op)
        clipped_bottom = max(bottom, y0_in)
        clipped_top    = min(top,    y1_in)
        new_op.y       = round(clipped_bottom - y0_in, 4)
        new_op.h       = round(clipped_top    - clipped_bottom, 4)
        result.append(new_op)
    return result


def _compute_elevation_bands(wall_h, rel_elevs, max_ph):
    """
    Greedy band builder: place the tallest possible panel (up to max_ph),
    snapping to the HIGHEST reachable level elevation within that range.

    Three cases:
      max_ph >= story_height  → one band per story (snap to level, no fill)
      max_ph < story_height   → full-height band (max_ph) + short fill band
                                that reaches the next level, per story
      max_ph spans N stories  → one band covers N stories (e.g. max_ph=300"
                                spanning L2 at 144" and L3 at 288" → single band)

    Always guarantees every band height <= max_ph.
    """
    if not rel_elevs or wall_h <= max_ph + 6.0:
        return [(0.0, round(wall_h, 4))]

    bands    = []
    pos      = 0.0
    s_elevs  = sorted(e for e in rel_elevs if 6.0 < e < wall_h - 6.0)

    while pos < wall_h - 6.0:
        tall_top  = pos + max_ph
        # All level elevations reachable within one panel height from current pos
        reachable = [e for e in s_elevs if pos + 6.0 < e <= tall_top]

        if reachable:
            # Snap to the HIGHEST reachable level → spans as many stories as possible
            band_top = max(reachable)
        else:
            # No level within reach — use max_ph or whatever remains
            band_top = min(tall_top, wall_h)

        band_top = min(band_top, wall_h)
        if band_top <= pos + 6.0:
            break

        bands.append((round(pos, 4), round(band_top, 4)))
        pos = band_top

    return bands or [(0.0, round(wall_h, 4))]


def process_wall(wall_id, wall_width, wall_height, openings):
    global ACTIVE_CONFIG

    if ACTIVE_CONFIG is None:
        presets = get_preset_configs()
        ACTIVE_CONFIG = presets.get("horizontal")

    orientation = str(ACTIVE_CONFIG.optimization_strategy.panel_orientation or "vertical").lower()
    strategy    = ACTIVE_CONFIG.optimization_strategy
    constraints = ACTIVE_CONFIG.panel_constraints

    # =================================================================
    # FIX: Safely initialize panels so it physically cannot be Unbound
    # =================================================================
    panels = []


    # --- CLEAN BRANCHING LOGIC WITH TOURNAMENT ---
    if getattr(strategy, 'best_to_manufacture', False):
        print(Ansi.MAGENTA + "  [TOURNAMENT] Evaluating strategies for Wall {}...".format(wall_id) + Ansi.RESET)
        
        # Fetch user weight
        u_weight = getattr(strategy, 'unique_weight', 10.0)
        
        # Run 1: Minimize Total Panels
        panels_largest = place_panels_sequential(wall_width, wall_height, openings, constraints, orientation)
        score_largest = evaluate_manufacturing_score(panels_largest, constraints, u_weight)
        
        # Run 2: Minimize Unique
        panels_min = _place_minimize_unique(wall_width, wall_height, openings, constraints, orientation, strategy)
        score_min = evaluate_manufacturing_score(panels_min, constraints, u_weight)
        
        # Run 3: Genetic Algorithm
        panels_ga = _place_ga_optimized(wall_width, wall_height, openings, constraints, orientation, strategy)
        score_ga = evaluate_manufacturing_score(panels_ga, constraints, u_weight)

        
        # Group and sort results
        tournament = [
            ("Minimize Total Panels", panels_largest, score_largest),
            ("Minimize Unique", panels_min, score_min),
            ("Genetic Algorithm", panels_ga, score_ga)
        ]
        
        # Sort: Primary Score (Total + Unique), then Tie-breaker (Total Panels)
        tournament.sort(key=lambda x: (x[2][0], x[2][1]))
        
        # Print the Scoreboard
        print(Ansi.CYAN + "    --- Scoreboard ---" + Ansi.RESET)
        for rank, (name, p_list, score) in enumerate(tournament, start=1):
            print("    {}. {}: Score {} ({} panels, {} unique)".format(
                rank, name.ljust(22), score[0], score[2]['panels'], score[2]['unique']
            ))
        
        winner_name, winner_panels, winner_score = tournament[0]
        print(Ansi.GREEN + "  [WINNER] {} takes the wall!".format(winner_name) + Ansi.RESET)
        
        # Assign winning list to our safe variable
        panels = winner_panels

    elif getattr(strategy, 'use_ga_optimizer', False):
        print(Ansi.MAGENTA + "  [EXECUTE] Running Genetic Algorithm..." + Ansi.RESET)
        panels = _place_ga_optimized(wall_width, wall_height, openings, constraints, orientation, strategy)
        
    elif getattr(strategy, 'minimize_unique_panels', False):
        print(Ansi.MAGENTA + "  [EXECUTE] Running Minimize Unique..." + Ansi.RESET)
        panels = _place_minimize_unique(wall_width, wall_height, openings, constraints, orientation, strategy)
        
    else:
        # =================================================================
        # FIX: The hard 'else' guarantees this runs if nothing else does
        # =================================================================
        print(Ansi.MAGENTA + "  [EXECUTE] Running Minimize Total Panels..." + Ansi.RESET)
        panels = place_panels_sequential(wall_width, wall_height, openings, constraints, orientation)

    # ── Assign type labels ────────────────────────────────────────────────
    if getattr(strategy, 'best_to_manufacture', False) or getattr(strategy, 'minimize_unique_panels', False) or getattr(strategy, 'use_ga_optimizer', False):
        print(Ansi.CYAN + "  [TYPES] Running panel type grouping "
              "(alignment={})...".format(getattr(strategy, 'opening_alignment', 'center')) + Ansi.RESET)
        panels = normalize_panel_types(
            panels,
            alignment=getattr(strategy, 'opening_alignment', 'center'),
            fixed_offset_in=getattr(strategy, 'void1_x_offset_left', 6.0),
            constraints=constraints
        )

    records = []
    for panel in panels:
        records.append({
            "panel_name":   panel.name,
            "panel_type":   "{}x{}".format(panel.w, panel.h),
            "wall_id":      wall_id,
            "x_in":         panel.x,
            "y_in":         panel.y,
            "width_in":     panel.w,
            "height_in":    panel.h,
            "area_in2":     panel.w * panel.h,
            "rotation_deg": 0.0,
            "x_ref":        "start",
            "cutouts_json": json.dumps(panel.cutouts) if getattr(panel, 'cutouts', None) else ""
        })

    print(Ansi.GREEN + " Result: {} panels generated".format(len(panels)) + Ansi.RESET)
    return records

def _place_minimize_unique(wall_width, wall_height, openings, constraints,
                            orientation, strategy):
    """
    Placement pass for the Minimize Unique strategy using Anchored Tiling.
    Maximizes panel width by swallowing multiple windows per panel to reduce 
    total panels, while strictly anchoring cutouts to standardize fabrication types.
    """
    import math

    orientation = str(orientation or "vertical").lower()
    horizontal_mode = (orientation == "horizontal")

    # --- Generate Horizontal Bands ---
    def get_y_bands(total_h):
        if not horizontal_mode: return [(0, total_h)]
        bands = []
        cy = 0
        inc = constraints.dimension_increment
        short_max = constraints.short_max
        while cy < total_h:
            rem_h = total_h - cy
            max_h_allow = min(rem_h, short_max)
            bh = math.floor(max_h_allow / inc) * inc
            if bh < constraints.min_height:
                if bands: bands[-1] = (bands[-1][0], total_h)
                else: bands.append((0, total_h))
                break
            bands.append((cy, cy + bh))
            cy += bh
        return bands

    wall_bands = get_y_bands(wall_height)

    # 1. Classify openings 
    classify_openings_dynamic(openings, constraints)
    repeating_groups = detect_repeating_opening_groups(openings, constraints)

    if not repeating_groups:
        print(Ansi.YELLOW + "  [PATTERN] No repeating groups found — "
              "using Minimize Total Panels fallback." + Ansi.RESET)
        return place_panels_sequential(wall_width, wall_height, openings, constraints, orientation)

    panels        = []
    panel_counter = 1
    covered_zones = []

    def _zone_of(grp): return grp[0].left_clearance_zone, grp[-1].right_clearance_zone

    merged_groups = []
    for group in repeating_groups:
        zs, ze = _zone_of(group)
        merged = None
        for m in merged_groups:
            overlap = min(ze, m[1]) - max(zs, m[0])
            smaller = min(ze - zs, m[1] - m[0])
            if overlap > 0.8 * smaller:
                merged = m
                break
        if merged is not None:
            merged[2].extend(group)
            merged[0] = min(merged[0], zs)
            merged[1] = max(merged[1], ze)
        else:
            merged_groups.append([zs, ze, list(group)])

 # 3. Process each merged zone (MULTI-WINDOW ANCHORED GRID WITH LH/RH MEMORY)
    memory_bank = {} # Stores: window_signature -> (standard_w, canonical_left)

    for zone_start, zone_end, zone_openings in merged_groups:
        primary_band = sorted(zone_openings, key=lambda o: o.x)
        primary_o = primary_band[0]

        standard_w     = None
        canonical_left = None
        n_panels       = 0

        max_w_allowed = constraints.long_max if horizontal_mode else constraints.max_width
        
        # --- NEW: LH/RH Pattern Memory Recognition ---
        # Generate a mathematical signature for this specific cluster of windows
        band_w = primary_band[-1].x + primary_band[-1].w - primary_o.x
        local_windows = tuple((round(w.x - primary_o.x, 2), round(w.w, 2)) for w in primary_band)
        
        # Generate what the reverse (mirrored) version of this cluster would look like
        mirrored_windows = tuple((round(band_w - (w.x - primary_o.x) - w.w, 2), round(w.w, 2)) for w in reversed(primary_band))

        if mirrored_windows in memory_bank:
            # TWIN DETECTED! Pull the exact dimensions of its mirror and flip the anchor.
            standard_w, orig_canonical_left = memory_bank[mirrored_windows]
            canonical_left = standard_w - band_w - orig_canonical_left
            print(Ansi.CYAN + "  [PATTERN] Mirrored LH/RH Twin Detected! Applying reversed anchor." + Ansi.RESET)
            
        else:
            # Normal Calculation (No mirror found yet)
            window_spacing = 0
            if len(primary_band) >= 2:
                ctrs = [o.x + o.w / 2.0 for o in primary_band]
                window_spacing = sum(ctrs[i+1] - ctrs[i] for i in range(len(ctrs)-1)) / (len(ctrs) - 1)

            align_strat = getattr(strategy, 'opening_alignment', 'opening_derived')

            if len(primary_band) == 1 or window_spacing < constraints.min_width:
                standard_w = max_w_allowed
                inc = constraints.dimension_increment
                standard_w = math.floor(standard_w / inc) * inc
                
                if align_strat == 'center':
                    canonical_left = (standard_w - primary_o.w) / 2.0
                else:
                    canonical_left = getattr(strategy, 'void1_x_offset_left', 6.0)
            else:
                best_N = 1
                for N in range(1, 50): 
                    w_test = (N * window_spacing) - constraints.panel_spacing
                    if w_test <= max_w_allowed:
                        best_N = N
                    else:
                        break 
                
                standard_w = (best_N * window_spacing) - constraints.panel_spacing
                
                if align_strat == 'center':
                    group_w = ((best_N - 1) * window_spacing) + primary_o.w
                    canonical_left = (standard_w - group_w) / 2.0
                elif align_strat == 'set_x_offset':
                    canonical_left = getattr(strategy, 'void1_x_offset_left', 6.0)
                else:
                    canonical_left = primary_o.clearances.panel_jamb

            # Save this newly calculated layout to the memory bank for future mirroring!
            if standard_w is not None:
                memory_bank[local_windows] = (standard_w, canonical_left)

        if standard_w is None or standard_w < constraints.min_width:
            print(Ansi.YELLOW + '  [PATTERN] Skipping zone - math resolved below min width' + Ansi.RESET)
            continue 

        # Anchor the Grid to the Window (Eliminates Snowflakes)
        actual_zone_start = primary_o.x - canonical_left
        if actual_zone_start < 0:
            actual_zone_start = 0.0

        last_o = primary_band[-1]
        dist_to_cover = (last_o.x + last_o.w + canonical_left) - actual_zone_start
        n_panels = max(1, int(math.ceil((dist_to_cover - 0.1) / (standard_w + constraints.panel_spacing))))

        print(Ansi.CYAN + ('  [PATTERN] Multi-Window Tiling: W={:.4g}" N={} '
              'start={:.1f}"').format(standard_w, n_panels, actual_zone_start) + Ansi.RESET)

        actual_zone_end = actual_zone_start

        for i in range(n_panels):
            panel_x = round(actual_zone_start + i * (standard_w + constraints.panel_spacing), 8)
            actual_w = standard_w
            if panel_x + actual_w > wall_width:
                actual_w = wall_width - panel_x
                if actual_w < constraints.min_width:
                    break
            
            for y_s, y_e in wall_bands:
                p = Panel(panel_x, y_s, actual_w, y_e - y_s, 'P{:02d}'.format(panel_counter))
                panel_counter += 1
                p.cutouts = calculate_panel_cutouts(p, openings)
                panels.append(p)
            
            actual_zone_end = panel_x + actual_w + constraints.panel_spacing

        covered_zones.append((actual_zone_start, actual_zone_end))

    # 4. Fill uncovered regions (Solid Panels between Window Grids)
    covered_zones.sort(key=lambda z: z[0])
    
    merged_covered = []
    if covered_zones:
        curr_zs, curr_ze = covered_zones[0]
        for next_zs, next_ze in covered_zones[1:]:
            if next_zs <= curr_ze + 0.01: 
                curr_ze = max(curr_ze, next_ze)
            else:
                merged_covered.append((curr_zs, curr_ze))
                curr_zs, curr_ze = next_zs, next_ze
        merged_covered.append((curr_zs, curr_ze))

    uncovered = []
    cursor = 0.0
    for zs, ze in merged_covered:
        if cursor < zs - 0.01: 
            uncovered.append((cursor, zs))
        cursor = max(cursor, ze)
        
    if cursor < wall_width - 0.01:
        uncovered.append((cursor, wall_width))

    for u_start, u_end in uncovered:
        u_width = u_end - u_start
        
        # Widen adjacent solid panels to absorb any tiny leftover slivers
        if u_width < constraints.min_width and panels:
            adjacent_panels = [p for p in panels if abs((p.x + p.w) - u_start) < 0.1]
            if adjacent_panels:
                can_widen = True
                max_w_allowed = constraints.long_max if horizontal_mode else constraints.max_width
                for ap in adjacent_panels:
                    if ap.w + u_width > max_w_allowed:
                        can_widen = False
                
                if can_widen:
                    for ap in adjacent_panels:
                        ap.w += u_width
                        ap.cutouts = calculate_panel_cutouts(ap, openings)
                    continue
        
        u_openings = [o for o in openings if not (o.right_clearance_zone <= u_start or o.left_clearance_zone >= u_end)]
        for o in u_openings: o.x -= u_start

        # Use Greedy logic to fill gaps with massive panels
        u_panels = place_panels_sequential(u_width, wall_height, u_openings, constraints, orientation)
        for p in u_panels:
            p.x += u_start
            p.name = "P{:02d}".format(panel_counter)
            panel_counter += 1
        panels.extend(u_panels)
        
        for o in u_openings: o.x += u_start

    panels.sort(key=lambda p: (p.y, p.x))
    for idx, p in enumerate(panels, start=1): p.name = "P{:02d}".format(idx)
    return panels


def process_all_walls(walls_rows, openings_rows, output_dir,
                      door_clearances, window_clearances, storefront_clearances,
                      config=None, orientation="vertical", output_filename="optimized_panel_placement.csv"):
    global ACTIVE_CONFIG
    if config is not None: ACTIVE_CONFIG = config
    elif ACTIVE_CONFIG is None:
        presets = get_preset_configs()
        ACTIVE_CONFIG = presets.get(orientation, presets["vertical"])
    
    all_panel_records = []
    processed_endpoints = [] # Tracks corners for butt joints
    
    # Sort walls to process connected facades sequentially
    walls_rows.sort(key=lambda w: (w.get("FacadeId", ""), safe_float(w.get("wall_origin_x")), safe_float(w.get("wall_origin_y"))))

    for wall_row in walls_rows:
        wall_id = get_wall_id(wall_row)
        dims = get_wall_dimensions(wall_row)
        if dims is None: continue

        wall_width, wall_height = dims
        wall_base_z = get_wall_base_z_ft(wall_row)
        
        # Extract thickness for butt joint calculation
        panel_thick_in = safe_float(wall_row.get("panel_total_thickness_in"), 6.625)

        # Always read clearances from ACTIVE_CONFIG
        active_door_cl         = ACTIVE_CONFIG.door_clearances
        active_window_cl       = ACTIVE_CONFIG.window_clearances
        active_storefront_cl   = ACTIVE_CONFIG.storefront_clearances
        active_wall_opening_cl = ACTIVE_CONFIG.wall_opening_clearances

        openings = get_wall_openings(
            wall_id, openings_rows,
            active_door_cl, active_window_cl, active_storefront_cl,
            active_wall_opening_cl,
            wall_base_z_ft=wall_base_z
        )
        
        _start_xyz = _parse_xyz(wall_row.get("Start(X,Y,Z)", ""))
        _end_xyz   = _parse_xyz(wall_row.get("End(X,Y,Z)",   ""))
        if _start_xyz and _end_xyz:
            _dx = _end_xyz[0] - _start_xyz[0]
            _dy = _end_xyz[1] - _start_xyz[1]
            _dz = _end_xyz[2] - _start_xyz[2]
            _mag = math.sqrt(_dx*_dx + _dy*_dy + _dz*_dz)
            _wall_geom = {
                "wall_origin_x": round(_start_xyz[0], 8),
                "wall_origin_y": round(_start_xyz[1], 8),
                "wall_origin_z": round(_start_xyz[2], 8),
                "wall_dir_x":   round(_dx / _mag, 8) if _mag > 1e-9 else 1.0,
                "wall_dir_y":   round(_dy / _mag, 8) if _mag > 1e-9 else 0.0,
                "wall_dir_z":   round(_dz / _mag, 8) if _mag > 1e-9 else 0.0,
            }
        else:
            _wall_geom = {"wall_origin_x": 0.0, "wall_origin_y": 0.0, "wall_origin_z": 0.0, "wall_dir_x": 1.0, "wall_dir_y": 0.0, "wall_dir_z": 0.0}

        # True Corner Detection (Butt Joints)
        _p_start_ext = 0.0
        _p_end_ext = 0.0
        if _start_xyz and _end_xyz:
            for p_pt, p_dir in processed_endpoints:
                if math.sqrt((_start_xyz[0]-p_pt[0])**2 + (_start_xyz[1]-p_pt[1])**2 + (_start_xyz[2]-p_pt[2])**2) < 1.0: 
                    if abs(_wall_geom["wall_dir_x"]*p_dir[0] + _wall_geom["wall_dir_y"]*p_dir[1] + _wall_geom["wall_dir_z"]*p_dir[2]) < 0.5:
                        _p_start_ext = -panel_thick_in
                        break
            for p_pt, p_dir in processed_endpoints:
                if math.sqrt((_end_xyz[0]-p_pt[0])**2 + (_end_xyz[1]-p_pt[1])**2 + (_end_xyz[2]-p_pt[2])**2) < 1.0:
                    if abs(_wall_geom["wall_dir_x"]*p_dir[0] + _wall_geom["wall_dir_y"]*p_dir[1] + _wall_geom["wall_dir_z"]*p_dir[2]) < 0.5:
                        _p_end_ext = -panel_thick_in
                        break
            current_dir = (_wall_geom["wall_dir_x"], _wall_geom["wall_dir_y"], _wall_geom["wall_dir_z"])
            processed_endpoints.append((_start_xyz, current_dir))
            processed_endpoints.append((_end_xyz, current_dir))

        _eff_wall_w = wall_width + _p_start_ext + _p_end_ext
        import copy as _cp_ext
        _ext_openings = _cp_ext.deepcopy(openings)
        for _eo in _ext_openings: _eo.x += _p_start_ext

        # Elevation bands and process_wall loop
        _wall_h_in, _base_z_in, _max_ph_in = wall_height, wall_base_z * 12.0, ACTIVE_CONFIG.panel_constraints.max_height
        try: _lvl_abs_in = json.loads(wall_row.get("LevelElevations(in)", "[]"))
        except: _lvl_abs_in = []
        _rel_elevs = sorted({round(e - _base_z_in, 2) for e in _lvl_abs_in if 6.0 < (e - _base_z_in) < (_wall_h_in - 6.0)})
        _bands = _compute_elevation_bands(_wall_h_in, _rel_elevs, _max_ph_in)

        panel_records = []
        for _y0, _y1 in _bands:
            _band_h_in = _y1 - _y0
            _band_ops  = _clip_openings_to_band(_ext_openings, _y0, _y1)
            _band_recs = process_wall(wall_id, _eff_wall_w, _band_h_in, _band_ops)
            for _r in _band_recs: _r["y_in"] = round(_r["y_in"] + _y0, 4)
            panel_records.extend(_band_recs)
        for _idx, _r in enumerate(panel_records, start=1): _r["panel_name"] = "P{:02d}".format(_idx)

        # Shift x_in back to wall_origin coordinates
        if _p_start_ext != 0.0:
            for _r in panel_records: _r["x_in"] = round(_r["x_in"] - _p_start_ext, 4)

        # Bake exterior normal
        _normal_xyz = _parse_xyz(wall_row.get("Normal(unit XYZ)", ""))
        _nm = math.sqrt(sum(v*v for v in _normal_xyz)) if _normal_xyz else 0.0
        _wall_geom["wall_normal_x"], _wall_geom["wall_normal_y"], _wall_geom["wall_normal_z"] = \
            (round(_normal_xyz[0]/_nm, 8), round(_normal_xyz[1]/_nm, 8), round(_normal_xyz[2]/_nm, 8)) if _normal_xyz and _nm > 1e-9 else (0.0, 0.0, 0.0)

        _rot_deg = round(math.degrees(math.atan2(_wall_geom["wall_dir_y"], _wall_geom["wall_dir_x"])) % 360.0, 4)
        for _pr in panel_records:
            _pr.update(_wall_geom)
            _pr["rotation_deg"] = _rot_deg
        all_panel_records.extend(panel_records)

    if not all_panel_records: return None, None
    panels_path = write_csv(os.path.join(output_dir, output_filename), all_panel_records, [
        "panel_name", "panel_type", "wall_id", "x_in", "y_in", "width_in", "height_in", "area_in2", 
        "rotation_deg", "x_ref", "cutouts_json", "wall_origin_x", "wall_origin_y", "wall_origin_z", 
        "wall_dir_x", "wall_dir_y", "wall_dir_z", "wall_normal_x", "wall_normal_y", "wall_normal_z"
    ])
    
    if panels_path and ACTIVE_CONFIG:
        config_path = os.path.join(output_dir, "config_used.json")
        try: ACTIVE_CONFIG.save(config_path)
        except: pass
    return panels_path, config_path

def write_csv(path, rows, fieldnames=None):
    if not rows: return None
    if fieldnames is None: fieldnames = list(rows[0].keys())
    try: f = open(path, "w", newline="")
    except TypeError: f = open(path, "w")
    with f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for r in rows:
            # csv.DictWriter raises ValueError if a row contains keys that are
            # not listed in fieldnames.  This keeps the export stable if future
            # processing steps add extra metadata to panel records.
            clean_row = dict((name, r.get(name, "")) for name in fieldnames)
            writer.writerow(clean_row)
    return path

def is_valid_panel(w, h, constraints):
    try: w, h = float(w), float(h)
    except: return False
    if w < constraints.min_width or h < constraints.min_height: return False
    if w > constraints.long_max or h > constraints.long_max: return False
    if w > constraints.short_max and h > constraints.short_max: return False
    return True



def find_next_opening_in_range(x_start, x_end, y_start, y_end, openings):
    """Find the leftmost opening that intersects with the given range."""
    candidates = []
    for o in openings:
        # Check if opening intersects horizontally
        if o.right_clearance_zone <= x_start or o.left_clearance_zone >= x_end:
            continue
        # Check if opening intersects vertically
        if o.top_clearance_zone <= y_start or o.bottom_clearance_zone >= y_end:
            continue
        candidates.append(o)
    
    if not candidates:
        return None
    return min(candidates, key=lambda o: o.left_clearance_zone)


def determine_panel_width_with_opening(x_cursor, x_end, y_start, y_end, max_width, openings, constraints):
    """
    Determine panel width considering openings ahead.
    Returns: (panel_width, should_include_opening, opening_obj or None)
    """
    PANEL_WIDTH_MIN = constraints.min_width
    DIMENSION_INCREMENT = constraints.dimension_increment
    
    # Find next opening in our path
    next_opening = find_next_opening_in_range(x_cursor, x_end, y_start, y_end, openings)
    
    if next_opening is None:
        # No opening ahead - use remaining space or max_width
        available = x_end - x_cursor
        panel_w = min(available, max_width)
        panel_w = snap_down(panel_w, DIMENSION_INCREMENT)
        return (panel_w, False, None)
    
    # Opening exists - decide whether to include or trim before it
    opening_left = next_opening.left_clearance_zone
    opening_right = next_opening.right_clearance_zone
    opening_width = opening_right - opening_left
    
    # Distance to opening start
    dist_to_opening = opening_left - x_cursor
    
    # Can we include the entire opening?
    width_with_opening = opening_right - x_cursor
    
    if dist_to_opening < PANEL_WIDTH_MIN:
        # Opening is very close - we should include it if possible
        if width_with_opening <= max_width:
            # Include the opening
            panel_w = snap_down(width_with_opening, DIMENSION_INCREMENT)
            if panel_w >= PANEL_WIDTH_MIN:
                return (panel_w, True, next_opening)
    
    # Opening is far enough - check if we should trim before it
    if dist_to_opening >= PANEL_WIDTH_MIN and dist_to_opening <= max_width:
        # Trim before the opening
        panel_w = snap_down(dist_to_opening, DIMENSION_INCREMENT)
        if panel_w >= PANEL_WIDTH_MIN:
            return (panel_w, False, next_opening)
    
    # Opening is too far or including it exceeds max_width
    # Use max_width or remaining space
    available = x_end - x_cursor
    panel_w = min(available, max_width)
    panel_w = snap_down(panel_w, DIMENSION_INCREMENT)
    return (panel_w, False, next_opening)



# =============================================================================
# SECTION 6: VISUALIZATION (optional)
# =============================================================================

def visualize_wall_layout(wall_id, panels_csv, openings_csv, walls_csv, output_image=None):
    try:
        import plotly.graph_objects as go
    except Exception:
        print(Ansi.YELLOW + "[VIS] Plotly not installed, skipping" + Ansi.RESET)
        return
    panels_rows = read_csv_rows(panels_csv)
    openings_rows = read_csv_rows(openings_csv)
    walls_rows = read_csv_rows(walls_csv)
    try:
        wall_id_int = int(float(wall_id))
    except Exception:
        wall_id_int = None
    wall_row = None
    for r in walls_rows:
        for col in ["WallId", "ElementId", "Id"]:
            try:
                if col in r and int(float(r.get(col))) == wall_id_int:
                    wall_row = r
                    break
            except Exception:
                pass
        if wall_row:
            break
    if wall_row is None:
        print(Ansi.YELLOW + "[VIS] Wall {} not found".format(wall_id) + Ansi.RESET)
        return
    wall_width = safe_float(wall_row.get("Length(ft)", 0)) * 12.0
    wall_height = safe_float(wall_row.get("UnconnectedHeight(ft)", 0)) * 12.0
    wall_panels = [p for p in panels_rows if str(p.get("wall_id")) == str(wall_id)]
    wall_openings = [o for o in openings_rows if safe_float(o.get("HostWallId"), None) == wall_id_int]
    fig = go.Figure()
    fig.add_shape(type="rect", x0=0, y0=0, x1=wall_width, y1=wall_height,
                  line=dict(color="black", width=3), fillcolor="lightgray", opacity=0.1)
    colors = ['rgba(65,105,225,0.3)', 'rgba(30,144,255,0.3)', 'rgba(100,149,237,0.3)']
    for i, panel in enumerate(wall_panels):
        x_in = float(panel.get("x_in", 0)); y_in = float(panel.get("y_in", 0))
        w_in = float(panel.get("width_in", 0)); h_in = float(panel.get("height_in", 0))
        fig.add_shape(type="rect", x0=x_in, y0=y_in, x1=x_in + w_in, y1=y_in + h_in,
                      line=dict(color="blue", width=2), fillcolor=colors[i % len(colors)])
        fig.add_annotation(x=x_in + w_in/2.0, y=y_in + h_in/2.0,
                           text="<b>{}</b><br/>{}\"x{}\"".format(panel.get('panel_name', ''), w_in, h_in),
                           showarrow=False, font=dict(size=10), bgcolor="white", opacity=0.8)
    for opening in wall_openings:
        left_ft = safe_float(opening.get("LeftEdgeAlongWall(ft)", 0))
        width_ft = safe_float(opening.get("Width(ft)", 0))
        sill_ft = safe_float(opening.get("SillHeight(ft)", 0))
        height_ft = safe_float(opening.get("Height(ft)", 0))
        if width_ft <= 0 or height_ft <= 0:
            continue
        left_in = left_ft * 12.0
        width_in = width_ft * 12.0
        sill_in = sill_ft * 12.0
        height_in = height_ft * 12.0
        opening_type = str(opening.get("OpeningType", "")).lower()
        if "door" in opening_type:
            color = "red"; rgb = "255,0,0"; label = "Door"
        elif ("storefront" in opening_type) or ("curtain" in opening_type):
            color = "darkgreen"; rgb = "0,100,0"; label = "Storefront"
        else:
            color = "purple"; rgb = "128,0,128"; label = "Window"
        fig.add_shape(type="rect",
                      x0=left_in - 6, y0=sill_in - 6,
                      x1=left_in + width_in + 6, y1=sill_in + height_in + 8,
                      line=dict(color="orange", width=1, dash="dash"), fillcolor="rgba(255,165,0,0.1)")
        fig.add_shape(type="rect",
                      x0=left_in, y0=sill_in,
                      x1=left_in + width_in, y1=sill_in + height_in,
                      line=dict(color=color, width=2), fillcolor="rgba({},{})".format(rgb, "0.4"))
        fig.add_annotation(x=left_in + width_in/2.0, y=sill_in + height_in/2.0,
                           text="{}<br/>{}\"x{}\"".format(label, float(width_in), float(height_in)),
                           showarrow=False, font=dict(size=9, color="white"), bgcolor=color, opacity=0.9)
    fig.update_layout(title="Wall {} - Sequential Panel Layout (Doors, Windows & Storefronts)".format(wall_id),
                      xaxis=dict(range=[0, wall_width], title="Length (inches)", showgrid=True),
                      yaxis=dict(range=[0, wall_height], title="Height (inches)", showgrid=True, scaleanchor="x"),
                      width=1400, height=600, showlegend=False, plot_bgcolor='white')
    if output_image:
        try:
            fig.write_image(output_image)
            print(Ansi.CYAN + "[VIS] Saved: {}".format(output_image) + Ansi.RESET)
        except Exception:
            fig.show()
    else:
        fig.show()


def visualize_all_walls(panels_csv, openings_csv, walls_csv, output_dir, save_as_image=True):
    panels_rows = read_csv_rows(panels_csv)
    wall_ids = sorted(set([r.get("wall_id") for r in panels_rows]))
    print(Ansi.MAGENTA + "\n[VIS] Generating {} visualizations...".format(len(wall_ids)) + Ansi.RESET)
    for wid in wall_ids:
        output_image = os.path.join(output_dir, "wall_{}_layout.png".format(wid)) if save_as_image else None
        visualize_wall_layout(wid, panels_csv, openings_csv, walls_csv, output_image)


# =============================================================================
# SECTION 7A: INTERACTIVE CONFIG CREATOR (ORIENTATION-FOCUSED)
# =============================================================================
def create_simple_config():
    """Interactive configuration creator with parameter preview/editing."""
    # IronPython vs CPython input
    try:
        get_input = raw_input  # type: ignore
    except NameError:
        get_input = input

    print("\n" + "=" * 60)
    print("  PANEL OPTIMIZER CONFIGURATION")
    print("=" * 60)

    print("\nSelect configuration preset:")
    print("1. Vertical Panels   (tall, narrow - best for high-rise)")
    print("2. Horizontal Panels (wide, short - best for retail/commercial)")
    print("3. Custom            (define all parameters)")

    choice = get_input("\nChoice (1-3) [default: 1]: ").strip() or "1"

    presets = get_preset_configs()

    if choice == "1":
        config = presets["vertical"]
        print("\n{}=== VERTICAL PANEL PRESET ==={}".format(Ansi.CYAN, Ansi.RESET))
        print_config_summary(config)
        pc = config.panel_constraints
        print("  Spacing:      {}\"".format(pc.panel_spacing))


        # Ask for confirmation first
        confirm = get_input("\nUse these preset values? (y/n/edit) [y]: ").strip().lower()
        if confirm == "n":
            print("{}Cancelled. Returning to menu...{}".format(Ansi.YELLOW, Ansi.RESET))
            return create_simple_config()  # Start over
        elif confirm == "edit" or confirm == "e":
            config = edit_panel_constraints(config)

    elif choice == "2":
        config = presets["horizontal"]
        print("\n{}=== HORIZONTAL PANEL PRESET ==={}".format(Ansi.CYAN, Ansi.RESET))
        print_config_summary(config)

        # Ask for confirmation first
        confirm = get_input("\nUse these preset values? (y/n/edit) [y]: ").strip().lower()
        if confirm == "n":
            print("{}Cancelled. Returning to menu...{}".format(Ansi.YELLOW, Ansi.RESET))
            return create_simple_config()  # Start over
        elif confirm == "edit" or confirm == "e":
            config = edit_panel_constraints(config)

    else:  # choice == "3"
        print("\n{}=== CUSTOM CONFIGURATION ==={}".format(Ansi.CYAN, Ansi.RESET))
        config = create_custom_config()

    # Optional: project name override
    project_name = get_input("\nProject name [{}]: ".format(config.project_name)).strip()
    if project_name:
        config.project_name = project_name

    return config


def print_config_summary(config):
    """Display configuration parameters."""
    pc = config.panel_constraints
    print("\nPanel Constraints:")
    print("  Orientation:  {}".format(config.optimization_strategy.panel_orientation))
    print("  Min Width:    {}\"".format(pc.min_width))
    print("  Max Width:    {}\"".format(pc.max_width))
    print("  Min Height:   {}\"".format(pc.min_height))
    print("  Max Height:   {}\"".format(pc.max_height))
    print("  Short Max:    {}\"".format(pc.short_max))
    print("  Long Max:     {}\"".format(pc.long_max))
    print("  Increment:    {}\"".format(pc.dimension_increment))

    print("\nClearances:")
    dc = config.door_clearances
    print("  Doors:")
    print("    Rough Opening:  jamb={}\" header={}\" sill={}\"".format(
        dc.rough_jamb, dc.rough_header, dc.rough_sill))
    print("    To Panel:       jamb={}\" header={}\" sill={}\"".format(
        dc.panel_jamb, dc.panel_header, dc.panel_sill))
    print("    TOTAL:          jamb={}\" header={}\" sill={}\"".format(
        dc.jamb_min, dc.header_min, dc.sill_min))
    
    wc = config.window_clearances
    print("  Windows:")
    print("    Rough Opening:  jamb={}\" header={}\" sill={}\"".format(
        wc.rough_jamb, wc.rough_header, wc.rough_sill))
    print("    To Panel:       jamb={}\" header={}\" sill={}\"".format(
        wc.panel_jamb, wc.panel_header, wc.panel_sill))
    print("    TOTAL:          jamb={}\" header={}\" sill={}\"".format(
        wc.jamb_min, wc.header_min, wc.sill_min))
    
    sc = config.storefront_clearances
    print("  Storefronts:")
    print("    Rough Opening:  jamb={}\" header={}\" sill={}\"".format(
        sc.rough_jamb, sc.rough_header, sc.rough_sill))
    print("    To Panel:       jamb={}\" header={}\" sill={}\"".format(
        sc.panel_jamb, sc.panel_header, sc.panel_sill))
    print("    TOTAL:          jamb={}\" header={}\" sill={}\"".format(
        sc.jamb_min, sc.header_min, sc.sill_min))

def edit_panel_constraints(config):
    """Enhanced parameter editor with grouping, validation, and full customization."""
    try:
        get_input = raw_input  # type: ignore
    except NameError:
        get_input = input

    print("\n{}=== PARAMETER CUSTOMIZATION ==={}".format(Ansi.YELLOW, Ansi.RESET))
    print("\nWhat would you like to edit?")
    print("1. Panel Dimensions (width/height limits)")
    print("2. Clearances (doors, windows, storefronts)")
    print("3. Panel Orientation")
    print("4. All Parameters")
    print("5. Done (keep current values)")

    while True:
        choice = get_input("\nChoice (1-5) [5]: ").strip() or "5"

        if choice == "1":
            config = edit_panel_dimensions(config)
        elif choice == "2":
            config = edit_clearances(config)
        elif choice == "3":
            config = edit_orientation(config)
        elif choice == "4":
            config = edit_panel_dimensions(config)
            config = edit_clearances(config)
            config = edit_orientation(config)
            break
        elif choice == "5":
            break
        else:
            print("{}Invalid choice. Please enter 1-5.{}".format(Ansi.RED, Ansi.RESET))
            continue

        # After each edit, ask if they want to edit more
        if choice in ["1", "2", "3"]:
            more = get_input("\nEdit another section? (y/n) [n]: ").strip().lower()
            if more != "y":
                break

    print("\n{}Final Configuration:{}".format(Ansi.GREEN, Ansi.RESET))
    print_config_summary(config)

    confirm = get_input("\nUse this configuration? (y/n) [y]: ").strip().lower()
    if confirm == "n":
        print("{}Discarding changes...{}".format(Ansi.YELLOW, Ansi.RESET))
        # Return original preset
        presets = get_preset_configs()
        if "vertical" in config.project_name.lower():
            return presets["vertical"]
        else:
            return presets["horizontal"]

    return config


def edit_panel_dimensions(config):
    """Edit panel dimension constraints with validation."""
    try:
        get_input = raw_input  # type: ignore
    except NameError:
        get_input = input

    print("\n{}--- PANEL DIMENSIONS ---{}".format(Ansi.CYAN, Ansi.RESET))
    print("(Press Enter to keep current value)")

    pc = config.panel_constraints

    # Min Width
    while True:
        val = get_input("  Min Width [{}\"]: ".format(pc.min_width)).strip()
        if not val:
            break
        try:
            new_val = float(val)
            if new_val <= 0:
                print("    {}Error: Must be positive{}".format(Ansi.RED, Ansi.RESET))
                continue
            if new_val >= pc.max_width:
                print("    {}Error: Must be less than Max Width ({}\"){}".format(
                    Ansi.RED, pc.max_width, Ansi.RESET))
                continue
            pc.min_width = new_val
            break
        except ValueError:
            print("    {}Error: Invalid number{}".format(Ansi.RED, Ansi.RESET))

    # Max Width
    while True:
        val = get_input("  Max Width [{}\"]: ".format(pc.max_width)).strip()
        if not val:
            break
        try:
            new_val = float(val)
            if new_val <= pc.min_width:
                print("    {}Error: Must be greater than Min Width ({}\"){}".format(
                    Ansi.RED, pc.min_width, Ansi.RESET))
                continue
            if new_val > pc.long_max:
                print("    {}Warning: Exceeds Long Max ({}\")-consider adjusting Long Max too{}".format(
                    Ansi.YELLOW, pc.long_max, Ansi.RESET))
            pc.max_width = new_val
            break
        except ValueError:
            print("    {}Error: Invalid number{}".format(Ansi.RED, Ansi.RESET))

    # Min Height
    while True:
        val = get_input("  Min Height [{}\"]: ".format(pc.min_height)).strip()
        if not val:
            break
        try:
            new_val = float(val)
            if new_val <= 0:
                print("    {}Error: Must be positive{}".format(Ansi.RED, Ansi.RESET))
                continue
            if new_val >= pc.max_height:
                print("    {}Error: Must be less than Max Height ({}\"){}".format(
                    Ansi.RED, pc.max_height, Ansi.RESET))
                continue
            pc.min_height = new_val
            break
        except ValueError:
            print("    {}Error: Invalid number{}".format(Ansi.RED, Ansi.RESET))

    # Max Height
    while True:
        val = get_input("  Max Height [{}\"]: ".format(pc.max_height)).strip()
        if not val:
            break
        try:
            new_val = float(val)
            if new_val <= pc.min_height:
                print("    {}Error: Must be greater than Min Height ({}\"){}".format(
                    Ansi.RED, pc.min_height, Ansi.RESET))
                continue
            if new_val > pc.long_max:
                print("    {}Warning: Exceeds Long Max ({}\")-consider adjusting Long Max too{}".format(
                    Ansi.YELLOW, pc.long_max, Ansi.RESET))
            pc.max_height = new_val
            break
        except ValueError:
            print("    {}Error: Invalid number{}".format(Ansi.RED, Ansi.RESET))

    # Short Max
    while True:
        val = get_input("  Short Max (one dimension must be <= this) [{}\"]: ".format(pc.short_max)).strip()
        if not val:
            break
        try:
            new_val = float(val)
            if new_val <= 0:
                print("    {}Error: Must be positive{}".format(Ansi.RED, Ansi.RESET))
                continue
            if new_val > pc.long_max:
                print("    {}Error: Must be <= Long Max ({}\"){}".format(
                    Ansi.RED, pc.long_max, Ansi.RESET))
                continue
            pc.short_max = new_val
            break
        except ValueError:
            print("    {}Error: Invalid number{}".format(Ansi.RED, Ansi.RESET))

    # Long Max
    while True:
        val = get_input("  Long Max (absolute maximum for either dimension) [{}\"]: ".format(pc.long_max)).strip()
        if not val:
            break
        try:
            new_val = float(val)
            if new_val < pc.short_max:
                print("    {}Error: Must be >= Short Max ({}\"){}".format(
                    Ansi.RED, pc.short_max, Ansi.RESET))
                continue
            pc.long_max = new_val
            break
        except ValueError:
            print("    {}Error: Invalid number{}".format(Ansi.RED, Ansi.RESET))

    # Dimension Increment
    while True:
        val = get_input("  Dimension Increment (snap grid) [{}\"]: ".format(pc.dimension_increment)).strip()
        if not val:
            break
        try:
            new_val = float(val)
            if new_val <= 0:
                print("    {}Error: Must be positive{}".format(Ansi.RED, Ansi.RESET))
                continue
            if new_val > 12:
                print("    {}Warning: Large increment ({}\")-panels may not fit well{}".format(
                    Ansi.YELLOW, new_val, Ansi.RESET))
            pc.dimension_increment = new_val
            break
        except ValueError:
            print("    {}Error: Invalid number{}".format(Ansi.RED, Ansi.RESET))

    print("  {}✓ Panel dimensions updated{}".format(Ansi.GREEN, Ansi.RESET))
    return config


def edit_clearances(config):
    """Edit clearance values for openings."""
    try:
        get_input = raw_input  # type: ignore
    except NameError:
        get_input = input

    print("\n{}--- CLEARANCES (inches) ---{}".format(Ansi.CYAN, Ansi.RESET))
    print("(Press Enter to keep current value)")

    # Door Clearances
    print("\n  {}DOOR CLEARANCES:{}".format(Ansi.BOLD, Ansi.RESET))
    config.door_clearances = edit_opening_clearance(
        "Door", config.door_clearances)

    # Window Clearances
    print("\n  {}WINDOW CLEARANCES:{}".format(Ansi.BOLD, Ansi.RESET))
    config.window_clearances = edit_opening_clearance(
        "Window", config.window_clearances)

    # Storefront Clearances
    print("\n  {}STOREFRONT CLEARANCES:{}".format(Ansi.BOLD, Ansi.RESET))
    config.storefront_clearances = edit_opening_clearance(
        "Storefront", config.storefront_clearances)

    print("  {}✓ Clearances updated{}".format(Ansi.GREEN, Ansi.RESET))
    return config


def edit_opening_clearance(opening_type, clearances):
    """Edit clearances for a specific opening type with two-fold structure."""
    try:
        get_input = raw_input  # type: ignore
    except NameError:
        get_input = input

    print("    Current: Rough + Panel = Total")
    print("    Jamb:   {}\" + {}\" = {}\"".format(
        clearances.rough_jamb, clearances.panel_jamb, clearances.jamb_min))
    print("    Header: {}\" + {}\" = {}\"".format(
        clearances.rough_header, clearances.panel_header, clearances.header_min))
    print("    Sill:   {}\" + {}\" = {}\"".format(
        clearances.rough_sill, clearances.panel_sill, clearances.sill_min))
    
    print("\n    Enter new values (or press Enter to skip):")
    
    # Rough Jamb
    while True:
        val = get_input("    Rough Opening Jamb [{}\"]: ".format(clearances.rough_jamb)).strip()
        if not val:
            break
        try:
            new_val = float(val)
            if new_val < 0:
                print("      {}Error: Cannot be negative{}".format(Ansi.RED, Ansi.RESET))
                continue
            clearances.rough_jamb = new_val
            break
        except ValueError:
            print("      {}Error: Invalid number{}".format(Ansi.RED, Ansi.RESET))
    
    # Panel Jamb
    while True:
        val = get_input("    Panel Clearance Jamb [{}\"]: ".format(clearances.panel_jamb)).strip()
        if not val:
            break
        try:
            new_val = float(val)
            if new_val < 0:
                print("      {}Error: Cannot be negative{}".format(Ansi.RED, Ansi.RESET))
                continue
            if (clearances.rough_jamb + new_val) > 24:
                print("      {}Warning: Total clearance ({}\")-may reduce coverage{}".format(
                    Ansi.YELLOW, clearances.rough_jamb + new_val, Ansi.RESET))
            clearances.panel_jamb = new_val
            break
        except ValueError:
            print("      {}Error: Invalid number{}".format(Ansi.RED, Ansi.RESET))

    # Rough Header
    while True:
        val = get_input("    Rough Opening Header [{}\"]: ".format(clearances.rough_header)).strip()
        if not val:
            break
        try:
            new_val = float(val)
            if new_val < 0:
                print("      {}Error: Cannot be negative{}".format(Ansi.RED, Ansi.RESET))
                continue
            clearances.rough_header = new_val
            break
        except ValueError:
            print("      {}Error: Invalid number{}".format(Ansi.RED, Ansi.RESET))
    
    # Panel Header
    while True:
        val = get_input("    Panel Clearance Header [{}\"]: ".format(clearances.panel_header)).strip()
        if not val:
            break
        try:
            new_val = float(val)
            if new_val < 0:
                print("      {}Error: Cannot be negative{}".format(Ansi.RED, Ansi.RESET))
                continue
            if (clearances.rough_header + new_val) > 24:
                print("      {}Warning: Total clearance ({}\")-may reduce coverage{}".format(
                    Ansi.YELLOW, clearances.rough_header + new_val, Ansi.RESET))
            clearances.panel_header = new_val
            break
        except ValueError:
            print("      {}Error: Invalid number{}".format(Ansi.RED, Ansi.RESET))

    # Rough Sill
    while True:
        val = get_input("    Rough Opening Sill [{}\"]: ".format(clearances.rough_sill)).strip()
        if not val:
            break
        try:
            new_val = float(val)
            if new_val < 0:
                print("      {}Error: Cannot be negative{}".format(Ansi.RED, Ansi.RESET))
                continue
            clearances.rough_sill = new_val
            break
        except ValueError:
            print("      {}Error: Invalid number{}".format(Ansi.RED, Ansi.RESET))
    
    # Panel Sill
    while True:
        val = get_input("    Panel Clearance Sill [{}\"]: ".format(clearances.panel_sill)).strip()
        if not val:
            break
        try:
            new_val = float(val)
            if new_val < 0:
                print("      {}Error: Cannot be negative{}".format(Ansi.RED, Ansi.RESET))
                continue
            if (clearances.rough_sill + new_val) > 24:
                print("      {}Warning: Total clearance ({}\")-may reduce coverage{}".format(
                    Ansi.YELLOW, clearances.rough_sill + new_val, Ansi.RESET))
            clearances.panel_sill = new_val
            break
        except ValueError:
            print("      {}Error: Invalid number{}".format(Ansi.RED, Ansi.RESET))
    
    print("\n    Updated totals:")
    print("    Jamb:   {}\" + {}\" = {}\"".format(
        clearances.rough_jamb, clearances.panel_jamb, clearances.jamb_min))
    print("    Header: {}\" + {}\" = {}\"".format(
        clearances.rough_header, clearances.panel_header, clearances.header_min))
    print("    Sill:   {}\" + {}\" = {}\"".format(
        clearances.rough_sill, clearances.panel_sill, clearances.sill_min))

    return clearances


def edit_orientation(config):
    """Change panel orientation."""
    try:
        get_input = raw_input  # type: ignore
    except NameError:
        get_input = input

    print("\n{}--- PANEL ORIENTATION ---{}".format(Ansi.CYAN, Ansi.RESET))
    current = config.optimization_strategy.panel_orientation
    print("  Current: {}".format(current))
    print("\n  1. Vertical (tall panels)")
    print("  2. Horizontal (wide panels)")

    choice = get_input("\nChoice (1-2) or Enter to keep [{}]: ".format(current)).strip()

    if choice == "1":
        config.optimization_strategy.panel_orientation = "vertical"
        config.optimization_strategy.prefer_full_height_panels = True
        print("  {}✓ Changed to VERTICAL{}".format(Ansi.GREEN, Ansi.RESET))
    elif choice == "2":
        config.optimization_strategy.panel_orientation = "horizontal"
        config.optimization_strategy.prefer_full_height_panels = False
        print("  {}✓ Changed to HORIZONTAL{}".format(Ansi.GREEN, Ansi.RESET))
    else:
        print("  Keeping current orientation: {}".format(current))

    return config

def create_custom_config():
    """Create fully custom configuration using the enhanced editing workflow."""
    try:
        get_input = raw_input  # type: ignore
    except NameError:
        get_input = input

    print("\n{}=== CUSTOM CONFIGURATION ==={}".format(Ansi.CYAN, Ansi.RESET))
    print("Starting with default values. You'll customize each section.")

    # Start with a default base config
    presets = get_preset_configs()
    config = presets["vertical"]  # Use vertical as starting point
    config.project_name = "Custom Configuration"

    # Orientation first
    print("\n{}Step 1: Panel Orientation{}".format(Ansi.BOLD, Ansi.RESET))
    print("  1. Vertical (tall panels)")
    print("  2. Horizontal (wide panels)")

    orient_choice = get_input("\nChoice (1-2) [1]: ").strip() or "1"
    if orient_choice == "2":
        config = presets["horizontal"]
        config.project_name = "Custom Configuration"
        config.optimization_strategy.panel_orientation = "horizontal"
        print("  {}✓ Starting with horizontal preset{}".format(Ansi.GREEN, Ansi.RESET))
    else:
        config.optimization_strategy.panel_orientation = "vertical"
        print("  {}✓ Starting with vertical preset{}".format(Ansi.GREEN, Ansi.RESET))

    # Show starting values
    print("\n{}Starting Configuration:{}".format(Ansi.CYAN, Ansi.RESET))
    print_config_summary(config)

    # Panel Dimensions
    print("\n{}Step 2: Panel Dimensions{}".format(Ansi.BOLD, Ansi.RESET))
    customize = get_input("Customize panel dimensions? (y/n) [y]: ").strip().lower()
    if customize != "n":
        config = edit_panel_dimensions(config)

    # Clearances
    print("\n{}Step 3: Clearances{}".format(Ansi.BOLD, Ansi.RESET))
    customize = get_input("Customize clearances? (y/n) [y]: ").strip().lower()
    if customize != "n":
        config = edit_clearances(config)

    # Final review
    print("\n{}=== FINAL CUSTOM CONFIGURATION ==={}".format(Ansi.GREEN, Ansi.RESET))
    print_config_summary(config)

    confirm = get_input("\nUse this configuration? (y/n) [y]: ").strip().lower()
    if confirm == "n":
        print("{}Cancelled. Using default vertical preset.{}".format(Ansi.YELLOW, Ansi.RESET))
        return presets["vertical"]

    return config




# =============================================================================
# SECTION 7: MAIN ENTRY POINT
# =============================================================================
def main():
    """
    OPTIONAL standalone CLI entry point for debugging only.
    The UI script should provide input/output folders.
    """

    global ACTIVE_CONFIG

    print(Ansi.YELLOW + "[INFO] No input directory provided. "
                        "This CLI mode is only for manual debugging." + Ansi.RESET)

    try:
        input_dir = raw_input("Enter input folder path: ").strip()
    except NameError:
        input_dir = input("Enter input folder path: ").strip()

    if not input_dir or not os.path.isdir(input_dir):
        print(Ansi.RED + "[ERROR] Invalid folder. Exiting." + Ansi.RESET)
        return

    OUTPUT_DIR = input_dir   # Save results next to input files unless user changes it

    GENERATE_VISUALIZATIONS = True
    SAVE_VISUALIZATIONS_AS_PNG = True

    # Load or create config inside input_dir
    config_file = os.path.join(input_dir, "optimizer_config.json")
    if os.path.exists(config_file):
        print(Ansi.CYAN + "[CONFIG] Loading: {}".format(config_file) + Ansi.RESET)
        config = OptimizerConfig.load(config_file)
    else:
        print(Ansi.YELLOW + "[CONFIG] No configuration found. Creating new..." + Ansi.RESET)
        config = create_simple_config()
        config.save(config_file)

    ACTIVE_CONFIG = config

    used_config_path = os.path.join(input_dir, "config_used.json")
    config.save(used_config_path)
    print(" Saved run config to: {}".format(used_config_path))

    walls_csv = os.path.join(input_dir, "walls.csv")
    openings_csv = os.path.join(input_dir, "wall_openings.csv")

    if not os.path.exists(walls_csv):
        print(Ansi.RED + "[ERROR] walls.csv not found. Exiting." + Ansi.RESET)
        return

    openings_rows = []
    if os.path.exists(openings_csv):
        openings_rows = load_openings_from_csv(openings_csv)
    else:
        print(Ansi.YELLOW + "[WARN] wall_openings.csv missing. Continuing without openings." + Ansi.RESET)

    walls_rows = load_walls_from_csv(walls_csv)

    panels_path, config_path = process_all_walls(
        walls_rows, openings_rows, input_dir,
        config.door_clearances,
        config.window_clearances,
        config.storefront_clearances
    )


    # Copy config next to placement file
    if panels_path:
        dst = os.path.join(os.path.dirname(panels_path), "config_used.json")
        if dst != used_config_path:
            import shutil
            shutil.copy(used_config_path, dst)
            print("Copied config to: {}".format(dst))

    if GENERATE_VISUALIZATIONS and panels_path:
        try:
            visualize_all_walls(
                panels_path,
                openings_csv,
                walls_csv,
                input_dir,
                save_as_image=SAVE_VISUALIZATIONS_AS_PNG
            )
            print(" Visualization complete.")
        except Exception as e:
            print("[VIS ERROR]", e)


if __name__ == "__main__":
    # Do NOT run automatically inside pyRevit.
    # Only executes if someone manually runs calculator.py from command line.
    main()