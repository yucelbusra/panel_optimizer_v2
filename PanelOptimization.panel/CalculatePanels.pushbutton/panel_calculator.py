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

# Set True to see the full per-wall / per-band diagnostic firehose (stage1-6,
# GAP/UNIFY/ABSORB/CONSOLIDATE detail, etc.). Leave False to see only the
# per-strategy tournament findings (np/nu per strategy, the winner, and the
# final unique-panel-type table) plus any real errors/warnings, which always
# print regardless of this flag.
SHOW_DIAGNOSTICS = False

def _diag(*args, **kwargs):
    if SHOW_DIAGNOSTICS:
        print(*args, **kwargs)

# Global active configuration (used for orientation & constraints)
ACTIVE_CONFIG = None
LAST_RUN_STATS = {"np": 0, "nu": 0}  # building-wide counts from last process_all_walls

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
                 void1_x_offset_left=6.0, nonwindow_strategy="largest",
                 np_weight=1.0, nu_weight=10.0,
                 best_to_manufacture=False, unique_weight=10.0,
                 merge_tolerance_in=0.0,
                 limit_panel_height_to_floor=False,
                 flexible_top_panel_allowance_in=24.0):
        
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
        self.np_weight                 = float(np_weight)
        self.nu_weight                 = float(nu_weight)
        self.best_to_manufacture       = bool(best_to_manufacture)
        self.unique_weight             = float(unique_weight)
        self.merge_tolerance_in        = float(merge_tolerance_in)
        self.limit_panel_height_to_floor = bool(limit_panel_height_to_floor)
        self.flexible_top_panel_allowance_in     = float(flexible_top_panel_allowance_in)

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
                "nonwindow_strategy": os.nonwindow_strategy,
                "best_to_manufacture": getattr(os, "best_to_manufacture", False),
                "unique_weight": getattr(os, "unique_weight", 10.0),
                "np_weight": getattr(os, "np_weight", 1.0),
                "nu_weight": getattr(os, "nu_weight", 10.0),
                "merge_tolerance_in": getattr(os, "merge_tolerance_in", 0.0),
                "limit_panel_height_to_floor": getattr(os, "limit_panel_height_to_floor", False)
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
                panel_jamb=wc.get("panel_jamb", 5.5), panel_header=wc.get("panel_header", 7.5), panel_sill=wc.get("panel_sill", 5.5)
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
                nonwindow_strategy=os_.get("nonwindow_strategy", "largest"),
                np_weight=os_.get("np_weight", 1.0),
                nu_weight=os_.get("nu_weight", os_.get("unique_weight", 10.0)),
                best_to_manufacture=os_.get("best_to_manufacture", False),
                unique_weight=os_.get("unique_weight", 10.0),
                merge_tolerance_in=os_.get("merge_tolerance_in", 0.0),
                limit_panel_height_to_floor=os_.get("limit_panel_height_to_floor", False)
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
                      wall_base_z_ft=0.0, wall_map=None, wall_geom=None): # <-- UPDATE: Added wall_geom
    if not openings_rows: return []
    if wall_opening_clearances is None:
        wall_opening_clearances = OpeningClearances(
            rough_jamb=0.0, rough_header=0.0, rough_sill=0.0,
            panel_jamb=0.0, panel_header=0.0, panel_sill=0.0
        )
        
    try:
        wall_id_str = str(int(float(wall_id)))
    except Exception: 
        wall_id_str = str(wall_id)
        
    # --- FACADE RESCUE: Get all original Revit IDs for this combined wall ---
    valid_hosts = {wall_id_str}
    if wall_map and wall_id_str in wall_map:
        valid_hosts.update(wall_map[wall_id_str])

    wall_openings = []
    for r in openings_rows:
        host_val = safe_float(r.get("HostWallId"), None)
        if host_val is not None:
            host_str = str(int(host_val))
            if host_str in valid_hosts:
                wall_openings.append(r)
                
    if not wall_openings: return []

    openings = []
    for row in wall_openings:
        width_ft = safe_float(row.get("Width(ft)", 0))
        height_ft = safe_float(row.get("Height(ft)", 0))
        sill_ft = safe_float(row.get("SillHeight(ft)", 0))
        
        if width_ft <= 0 or height_ft <= 0: continue
        
        has_left_edge = not _is_empty(row.get("LeftEdgeAlongWall(ft)"))
        has_position  = not _is_empty(row.get("PositionAlongWall(ft)"))
        has_location  = not _is_empty(row.get("Location(X,Y,Z)"))
        if not has_left_edge and not has_position and not has_location:
            print(Ansi.YELLOW + "[WARN] Opening {} (type={}) has no position data "
                  "- skipping to avoid phantom placement at x=0".format(
                  row.get("OpeningId", "?"), row.get("OpeningType", "?")) + Ansi.RESET)
            continue

        w_in = float(width_ft * 12)
        h_in =float(height_ft * 12)

        # --- FIX (ISSUE 1): THE PROJECTION FIX ---
        loc_xyz = _parse_xyz(row.get("Location(X,Y,Z)", ""))
        
        if loc_xyz is not None and wall_geom and "wall_origin_x" in wall_geom:
            # Vector from combined wall start to opening center
            dx = loc_xyz[0] - wall_geom["wall_origin_x"]
            dy = loc_xyz[1] - wall_geom["wall_origin_y"]
            
            # Dot product projects the opening perfectly onto the combined facade line
            dist_along_facade = (dx * wall_geom["wall_dir_x"] + dy * wall_geom["wall_dir_y"])
            x_in = float(dist_along_facade * 12.0) - (w_in / 2.0)
        else:
            # Fallback to local
            left_ft = safe_float(row.get("LeftEdgeAlongWall(ft)", 0))
            if left_ft == 0 and "PositionAlongWall(ft)" in row:
                pos = safe_float(row.get("PositionAlongWall(ft)", 0))
                if pos != 0: left_ft = pos - (width_ft/2.0)
            x_in = float(left_ft * 12)

        # Prefer absolute Location Z minus wall base Z for correct multi-level support.
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
def evaluate_manufacturing_score(panels, constraints, weight):
    """
    Calculates the DfMA manufacturing score dynamically based on user weight.
    Recognizes Left-Hand (LH) and Right-Hand (RH) mirrored panels as a single unique type!
    """
    if not panels:
        return (9999, 9999, {"panels": 0, "unique": 0})

    total_panels = len(panels)
    
    unique_signatures = set()
    for p in panels:
        sig_w = round(p.w * 8.0) / 8.0
        sig_h = round(p.h * 8.0) / 8.0
        
        std_cutouts = []
        mir_cutouts = []
        
        for c in getattr(p, 'cutouts', []):
            cx = round(c['x_in'], 1)
            cy = round(c['y_in'], 1)
            cw = round(c['width_in'], 1)
            ch = round(c['height_in'], 1)
            
            std_cutouts.append((cx, cy, cw, ch))
            
            # Calculate the mathematically mirrored X position
            mir_x = round(p.w - c['x_in'] - c['width_in'], 1)
            mir_cutouts.append((mir_x, cy, cw, ch))
            
        std_cutouts.sort()
        mir_cutouts.sort()
        
        std_sig = (sig_w, sig_h, tuple(std_cutouts))
        mir_sig = (sig_w, sig_h, tuple(mir_cutouts))
        
        canonical_sig = min(std_sig, mir_sig)
        unique_signatures.add(canonical_sig)
        
    unique_types = len(unique_signatures)
    
    # Score = Total Panels + (Unique Types * User Weight)
    primary_score = total_panels + (unique_types * weight)
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
    # THE RESCUE MISSION: Force all doors/windows under 100 inches 
    # to be treated as cutouts, regardless of what Revit named them.
    if opening.w <= 100.0:
        return True
        
    # If it's larger than 100 inches, let the normal blocker logic handle it
    return not getattr(opening, 'force_blocker', False)

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


def fill_horizontal_courses(x0, x1, y0, y1, panels, panel_counter,
                            constraints, all_openings):
    """Fill the rectangle [x0,x1] x [y0,y1] with HORIZONTAL panels: stacked
    courses, each no taller than short_max, width-tiled up to long_max. Course
    heights are EQUAL-split so the region is fully covered with no sub-minimum
    remainder at the top. Used above (and optionally below) a blocking storefront
    when the wall is placed vertically, so the spandrel band over a wide
    storefront becomes wide horizontal panels instead of short vertical slivers."""
    import math
    minw = constraints.min_width
    minh = constraints.min_height
    SHORT = constraints.short_max
    LONG = constraints.long_max
    # A configured max_width acts as an additional hard cap. If the user set
    # max_width = 138 in the config, panel widths must respect 138 even when
    # the "long-short" panel rule would allow up to LONG (348). Without this
    # cap, short-height fill panels ran up to 348" wide even though the
    # config said 138".
    _cap_w = float(getattr(constraints, "max_width", LONG) or LONG)
    LONG_CAPPED = min(LONG, _cap_w)
    sp = float(getattr(constraints, "panel_spacing", 0.0) or 0.0)

    W = x1 - x0
    H = y1 - y0
    if W < minw or H < minh:
        return panel_counter

    # number of equal courses so each <= short_max, then height split to fill H
    n = max(1, int(math.ceil((H + sp) / (SHORT + sp))))
    course_h = (H - (n - 1) * sp) / float(n)
    while course_h < minh and n > 1:
        n -= 1
        course_h = (H - (n - 1) * sp) / float(n)

    y = y0
    for _ci in range(n):
        # Equal-split the width across this course too (same idea as the
        # course-height split above), instead of greedily maxing out the
        # first panel and dumping whatever's left as a narrow leftover.
        # A greedy leftover (e.g. 126" + 24") can end up TALLER than it is
        # WIDE, which reads as a portrait/vertical sliver even though this
        # function is supposed to produce horizontal panels.
        m = max(1, int(math.ceil((W + sp) / (LONG_CAPPED + sp))))
        panel_w = (W - (m - 1) * sp) / float(m)
        while panel_w < minw and m > 1:
            m -= 1
            panel_w = (W - (m - 1) * sp) / float(m)
        panel_w = round(panel_w, 6)

        x = x0
        for _pi in range(m):
            cand = Panel(round(x, 6), round(y, 6), panel_w, round(course_h, 6),
                         "PH{:02d}".format(panel_counter))
            cand.cutouts = calculate_panel_cutouts(cand, all_openings)
            panels.append(cand)
            panel_counter += 1
            x += panel_w + sp
        y += course_h + sp
    return panel_counter


def fill_vertical_gap(region_x_start, region_x_end, gap_y_start, gap_y_end,
                      opening_left, opening_right, panels, panel_counter,
                      constraints, all_openings, label,
                      is_storefront=False):
    PANEL_WIDTH_MIN = constraints.min_width
    PANEL_HEIGHT_MIN = constraints.min_height
    SHORT_MAX = constraints.short_max
    LONG_MAX = constraints.long_max
    # A configured max_width acts as an absolute hard cap. If the user set
    # max_width = 138 in the config, panel widths must respect 138 even
    # when the "short panels can be wide" rule would allow up to LONG_MAX
    # (348). Without this cap, the fill above a wide storefront could drop
    # a single 143"-wide panel across the whole spandrel even though the
    # config said 138" max.
    _CAP_W = float(getattr(constraints, "max_width", LONG_MAX) or LONG_MAX)
    LONG_CAPPED = min(LONG_MAX, _CAP_W)
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

        max_width = SHORT_MAX if panel_h > SHORT_MAX else LONG_CAPPED
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
                    max_w_here = SHORT_MAX if panel_h > SHORT_MAX else LONG_CAPPED
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


def _equalize_sibling_panels(panels, openings, constraints):
    """ROOT-CAUSE drift fix (Option 1: sibling equalization).

    The sequential tiler snaps the first panel of an evenly-divided region down
    to the increment and dumps the leftover onto the LAST panel, so two halves
    that should be identical come out e.g. 305.000 + 305.625. This pass finds
    runs of consecutive, same-band, same-cutout-count panels and re-flows them
    to EXACTLY equal widths (covering the same span, spacing preserved), then
    recomputes their cutouts. It only commits when doing so genuinely reduces
    the number of distinct panel types in the run AND no new seam lands inside
    an opening's clearance zone. Otherwise it leaves the run untouched -- so it
    can never create a gap, an overlap, or a seam through a window.
    """
    from collections import defaultdict
    spacing = float(getattr(constraints, "panel_spacing", 0.0) or 0.0)

    bands = defaultdict(list)
    for p in panels:
        bands[(round(p.y, 3), round(p.h, 3))].append(p)

    for _key, bp in bands.items():
        bp.sort(key=lambda p: p.x)
        n = len(bp)
        i = 0
        while i < n:
            cc = len(getattr(bp[i], "cutouts", []) or [])
            j = i + 1
            while j < n:
                prev, cur = bp[j - 1], bp[j]
                contiguous = abs((prev.x + prev.w + spacing) - cur.x) < 0.2
                same_cc = len(getattr(cur, "cutouts", []) or []) == cc
                if contiguous and same_cc:
                    j += 1
                else:
                    break
            if (j - i) >= 2:
                _try_equalize_run(bp[i:j], openings, constraints)
            i = j
    return panels


def _try_equalize_run(run, openings, constraints):
    """Attempt to give every panel in `run` the same exact width. Commits only
    if it strictly lowers the distinct-type count of the run and stays seam-safe."""
    spacing = float(getattr(constraints, "panel_spacing", 0.0) or 0.0)
    N = len(run)
    x0 = run[0].x
    span = (run[-1].x + run[-1].w) - x0
    W = (span - (N - 1) * spacing) / float(N)
    if W < constraints.min_width:
        return

    _lib = PanelLibrary()
    before = set()
    for p in run:
        canon, _m = _lib._signature(p.w, p.h, getattr(p, "cutouts", []) or [])
        before.add(canon)

    # Build the tentative equalized layout.
    tentative = []
    cx = x0
    for _k in range(N):
        q = Panel(round(cx, 6), run[0].y, round(W, 6), run[0].h)
        q.cutouts = calculate_panel_cutouts(q, openings)
        tentative.append(q)
        cx += W + spacing

    # Seam safety: no interior seam may fall inside an opening's clearance zone.
    for k in range(N - 1):
        seam = tentative[k].x + tentative[k].w
        for o in openings:
            if o.left_clearance_zone - 0.01 < seam < o.right_clearance_zone + 0.01:
                return

    after = set()
    for q in tentative:
        canon, _m = _lib._signature(q.w, q.h, q.cutouts)
        after.add(canon)

    if len(after) < len(before):
        for old, q in zip(run, tentative):
            old.x = q.x
            old.w = q.w
            old.cutouts = q.cutouts

def _accordion_seam_shifter(panels, openings, constraints, orientation):
    """
    Consolidates cutout-bearing panels by RENEGOTIATING SEAM POSITIONS.
    The opening does not move. The panel's left and right seams are pulled
    left or right so the cutout ends up at a canonical local X. The two
    immediate neighbors absorb the delta -- one widens, the other narrows
    by the same amount. Net effect: several near-identical types collapse
    into one, without moving any opening or changing panel counts.

    Runs for ALL strategies (Min Total / Min Unique / GA).
    """
    from collections import defaultdict, Counter

    band_map = defaultdict(list)
    for p in panels:
        band_map[(round(p.y, 3), round(p.h, 3))].append(p)

    global_max_w = constraints.long_max if str(orientation).lower() == 'horizontal' else constraints.max_width
    min_w = constraints.min_width

    shifted = 0
    skipped_edge = 0
    skipped_neighbor_cutout = 0
    skipped_neighbor_width = 0

    for (_yk, _hk), band_panels in band_map.items():
        band_panels.sort(key=lambda pp: pp.x)

        sig_groups = defaultdict(list)
        for idx, pp in enumerate(band_panels):
            cuts = getattr(pp, 'cutouts', []) or []
            if not cuts:
                continue
            cuts_sorted = sorted(cuts, key=lambda c: c.get('x_in', 0.0))
            xs = [c.get('x_in', 0.0) for c in cuts_sorted]
            base = xs[0]
            rel_xs = tuple(round(x - base, 3) for x in xs)
            shape = tuple((round(c.get('w', 0.0), 3),
                           round(c.get('h', 0.0), 3),
                           round(c.get('y_in', 0.0), 3))
                          for c in cuts_sorted)
            sig = (round(pp.w, 3), round(pp.h, 3), shape, rel_xs)
            sig_groups[sig].append((idx, base))

        for sig, members in sig_groups.items():
            if len(members) < 2:
                continue

            counts = Counter(round(bx, 3) for _, bx in members)
            top_count = counts.most_common(1)[0][1]
            canon_candidates = [v for v, c in counts.items() if c == top_count]
            canon_x = min(canon_candidates)

            for idx, base_x in members:
                delta = round(canon_x - base_x, 3)
                if abs(delta) < 1e-3:
                    continue

                if idx == 0 or idx == len(band_panels) - 1:
                    skipped_edge += 1
                    continue

                target_p = band_panels[idx]
                left_n   = band_panels[idx - 1]
                right_n  = band_panels[idx + 1]

                if (getattr(left_n,  'cutouts', []) or []) or \
                   (getattr(right_n, 'cutouts', []) or []):
                    skipped_neighbor_cutout += 1
                    continue

                new_target_x = target_p.x - delta
                new_left_w   = left_n.w - delta
                new_right_x  = right_n.x - delta
                new_right_w  = right_n.w + delta

                if not (min_w - 0.05 <= new_left_w  <= global_max_w + 0.05):
                    skipped_neighbor_width += 1
                    continue
                if not (min_w - 0.05 <= new_right_w <= global_max_w + 0.05):
                    skipped_neighbor_width += 1
                    continue

                left_n.w   = round(new_left_w,  3)
                target_p.x = round(new_target_x, 3)
                right_n.x  = round(new_right_x, 3)
                right_n.w  = round(new_right_w, 3)

                left_n.cutouts   = calculate_panel_cutouts(left_n,   openings)
                target_p.cutouts = calculate_panel_cutouts(target_p, openings)
                right_n.cutouts  = calculate_panel_cutouts(right_n,  openings)

                shifted += 1
                _diag(Ansi.CYAN +
                      "  [CONSOLIDATE] {} pulled {:+.2f}\" (cutout x_in -> {:.2f}\"); "
                      "L:{}.w={:.2f}\"  R:{}.w={:.2f}\"".format(
                        target_p.name, -delta, canon_x,
                        left_n.name, left_n.w,
                        right_n.name, right_n.w) + Ansi.RESET)

    if shifted or skipped_edge or skipped_neighbor_cutout or skipped_neighbor_width:
        _diag(Ansi.CYAN +
              "  [CONSOLIDATE] Shifted {} panel(s). Skipped: "
              "edge={} neighbor-has-cutout={} neighbor-width={}".format(
                shifted, skipped_edge,
                skipped_neighbor_cutout, skipped_neighbor_width) + Ansi.RESET)

    return [pp for band in band_map.values() for pp in band]

def _absorb_narrow_fills(panels, openings, constraints, orientation):
    """
    Absorb solid fill panels sitting between two matching host panels.

    Pattern detected:
      [Left host P_l with cutouts]
      [Solid fill P_m no cutouts]
      [Right host P_r with cutouts, same local cutout layout as P_l]

    Effect:
      Delete P_m. Widen P_l rightward and P_r leftward to meet at the mid-seam.
      The two widened panels are geometric mirrors -> collapse to 1 unique
      type under mirror-aware matching. Result: -1 total panel, 0 net unique
      types (the fill's type leaves, the widened-mirror type enters).
    """
    if not panels or len(panels) < 3:
        return panels

    horizontal_mode = str(orientation or "vertical").lower() == "horizontal"
    max_w = constraints.long_max if horizontal_mode else constraints.max_width
    spacing = constraints.panel_spacing
    band_tol = 0.25  # tolerate height drift

    def _cutout_local_key(cutouts):
        # Fingerprint of cutouts relative to their host panel (local coords).
        # Two hosts match if this key is equal (order-independent).
        # Quantize to the 1/16" match_tol grid used by PanelLibrary._signature
        # so sub-1/16" drift from pier-width drift (e.g. 71.876 vs 71.874
        # rippling into downstream host x) doesn't split otherwise-identical
        # cutout patterns into different keys.
        q = 0.0625
        def _qv(v): return round(float(v) / q) * q
        return tuple(sorted(
            (_qv(c.get('x_in', 0)),
             _qv(c.get('y_in', 0)),
             _qv(c.get('width_in', 0)),
             _qv(c.get('height_in', 0)))
            for c in (cutouts or [])
        ))

    def _same_band(p1, p2):
        return (abs(p1.y - p2.y) <= band_tol and
                abs((p1.y + p1.h) - (p2.y + p2.h)) <= band_tol)

    # Group panels by approximate y-band
    by_band = {}
    for p in panels:
        key = (round(p.y / band_tol) * band_tol,
               round((p.y + p.h) / band_tol) * band_tol)
        by_band.setdefault(key, []).append(p)

    absorbed = 0
    skip = {"cutout_mismatch": 0, "mid_not_solid": 0, "over_max": 0,
            "under_min": 0, "seam_in_opening": 0, "cutout_ejected": 0,
            "not_contiguous": 0}
    remove_ids = set()

    for band_key, band_panels in by_band.items():
        band_panels.sort(key=lambda p: p.x)
        i = 0
        while i < len(band_panels) - 2:
            pl, pm, pr = band_panels[i], band_panels[i + 1], band_panels[i + 2]

            if id(pl) in remove_ids or id(pm) in remove_ids or id(pr) in remove_ids:
                i += 1
                continue

            gap_lm = pm.x - (pl.x + pl.w)
            gap_mr = pr.x - (pm.x + pm.w)
            if gap_lm < -0.5 or gap_lm > spacing + 0.5:
                skip["not_contiguous"] += 1
                i += 1
                continue
            if gap_mr < -0.5 or gap_mr > spacing + 0.5:
                skip["not_contiguous"] += 1
                i += 1
                continue

            pl_cutouts = getattr(pl, 'cutouts', []) or []
            pm_cutouts = getattr(pm, 'cutouts', []) or []
            pr_cutouts = getattr(pr, 'cutouts', []) or []

            if pm_cutouts:
                skip["mid_not_solid"] += 1
                i += 1
                continue
            if not pl_cutouts or not pr_cutouts:
                skip["cutout_mismatch"] += 1
                i += 1
                continue
            if _cutout_local_key(pl_cutouts) != _cutout_local_key(pr_cutouts):
                skip["cutout_mismatch"] += 1
                i += 1
                continue

            total_span = (pr.x + pr.w) - pl.x
            new_w = round((total_span - spacing) / 2.0, 3)

            if new_w > max_w + 0.01:
                skip["over_max"] += 1
                i += 1
                continue
            if new_w < constraints.min_width - 0.01:
                skip["under_min"] += 1
                i += 1
                continue

            pl_new_x = pl.x
            pr_new_x = round(pl.x + new_w + spacing, 3)

            # Verify pr cutouts still fit inside pr' with valid jambs.
            # Each cutout must land at local_x >= panel_jamb of its opening type,
            # and its right edge must land <= new_w - panel_jamb.
            _valid = True
            for c in pr_cutouts:
                old_local_x = c.get('x_in', 0)
                abs_x = pr.x + old_local_x
                new_local_x = abs_x - pr_new_x
                cw = c.get('width_in', 0)
                if new_local_x < -0.01 or new_local_x + cw > new_w + 0.01:
                    _valid = False
                    break
            if not _valid:
                skip["cutout_ejected"] += 1
                i += 1
                continue

            # Seam safety: no opening's clearance zone crosses [pl_new_x + new_w, pr_new_x]
            seam_gap_lo = pl_new_x + new_w
            seam_gap_hi = pr_new_x
            seam_bad = False
            for o in openings:
                lcz = getattr(o, 'left_clearance_zone', None)
                rcz = getattr(o, 'right_clearance_zone', None)
                if lcz is None or rcz is None:
                    continue
                if lcz < seam_gap_hi + 0.01 and rcz > seam_gap_lo - 0.01:
                    seam_bad = True
                    break
            if seam_bad:
                skip["seam_in_opening"] += 1
                i += 1
                continue

            # Unify heights on anchor's height (smallest)
            band_h = min(pl.h, pr.h)

            # Commit
            pl.w = new_w
            pl.h = band_h
            pl.cutouts = calculate_panel_cutouts(pl, openings)

            pr.x = pr_new_x
            pr.w = new_w
            pr.h = band_h
            pr.cutouts = calculate_panel_cutouts(pr, openings)

            remove_ids.add(id(pm))
            absorbed += 1
            i += 2  # skip past pr; if next triplet starts at pr, it becomes new pl

    if remove_ids:
        panels = [p for p in panels if id(p) not in remove_ids]

    if absorbed > 0 or any(v > 0 for v in skip.values()):
        skipped_summary = " ".join("{}={}".format(k, v) for k, v in skip.items() if v > 0)
        _diag(Ansi.CYAN + "  [ABSORB] Merged {} fill panel(s) into adjacent hosts. {}".format(
            absorbed, "Skipped: " + skipped_summary if skipped_summary else "") + Ansi.RESET)

    return panels


def _unify_pattern_runs(panels, openings, constraints, orientation):
    """
    Detects runs of contiguous panels sharing the same CUTOUT SHAPE (not the
    same panel width) around evenly-spaced openings, and forces them to a
    single common width so all panels in the run become the SAME TYPE.

    Approach:
      1. Group panels by band, then find contiguous runs where each panel has
         the same cutout shape signature (cutout dims + relative x-deltas).
      2. Verify the openings hosted by the run are evenly spaced. Bail
         otherwise -- irregular spacing can't be unified.
      3. Ideal width = opening c-to-c spacing minus panel_spacing. This is
         the width at which every opening sits at the same local X.
      4. Compute delta = new_span - current_span. Split this evenly between
         the run's LEFT and RIGHT outer neighbors ("midpoint approach" -- the
         run's center stays put).
      5. Commit only if all constraints are satisfied:
           * every run panel's ideal width is within [min_w, max_w]
           * neither outer neighbor exits [min_w, max_w] after absorbing delta
           * neither outer neighbor is a cutout panel
           * no new seam falls inside an opening's clearance zone
      6. Recompute cutouts for the run and both outer neighbors after commit.

    Runs for ALL strategies. This is what actually catches the T27-vs-T30
    "same opening pattern, different width" case that the accordion shifter
    can't touch.
    """
    from collections import defaultdict

    spacing = float(getattr(constraints, "panel_spacing", 0.0) or 0.0)
    global_max_w = constraints.long_max if str(orientation).lower() == 'horizontal' else constraints.max_width
    min_w = constraints.min_width

    band_map = defaultdict(list)
    for p in panels:
        band_map[(round(p.y, 3), round(p.h, 3))].append(p)

    unified_runs = 0
    skipped_no_neighbor = 0
    skipped_irregular_ops = 0
    skipped_width_bounds = 0
    skipped_neighbor_cutout = 0
    skipped_neighbor_bounds = 0
    skipped_seam_in_opening = 0

    def _shape_sig(pp):
        cuts = getattr(pp, 'cutouts', []) or []
        if not cuts:
            return None
        cuts_sorted = sorted(cuts, key=lambda c: c.get('x_in', 0.0))
        xs = [c.get('x_in', 0.0) for c in cuts_sorted]
        base = xs[0]
        rel_xs = tuple(round(x - base, 3) for x in xs)
        shape = tuple((round(c.get('w', 0.0), 3),
                       round(c.get('h', 0.0), 3),
                       round(c.get('y_in', 0.0), 3))
                      for c in cuts_sorted)
        return (shape, rel_xs)

    for (_yk, _hk), bp in band_map.items():
        bp.sort(key=lambda pp: pp.x)

        i = 0
        while i < len(bp):
            sig_i = _shape_sig(bp[i])
            if sig_i is None:
                i += 1
                continue

            j = i + 1
            while j < len(bp):
                if not (abs((bp[j - 1].x + bp[j - 1].w + spacing) - bp[j].x) < 0.2):
                    break
                if _shape_sig(bp[j]) != sig_i:
                    break
                j += 1

            run = bp[i:j]
            run_start = i
            i = j

            if len(run) < 2:
                continue

            # Outer neighbors must exist and be solid (no cutouts).
            if run_start == 0 or run_start + len(run) >= len(bp):
                skipped_no_neighbor += 1
                continue
            left_n  = bp[run_start - 1]
            right_n = bp[run_start + len(run)]
            if (getattr(left_n,  'cutouts', []) or []) or \
               (getattr(right_n, 'cutouts', []) or []):
                skipped_neighbor_cutout += 1
                continue

            # World positions of the leftmost cutout of each run panel.
            op_ws = []
            for pp in run:
                cuts_sorted = sorted(pp.cutouts, key=lambda c: c.get('x_in', 0.0))
                op_ws.append(pp.x + cuts_sorted[0].get('x_in', 0.0))

            # Openings must be evenly spaced within the run.
            step = op_ws[1] - op_ws[0]
            if step <= 0:
                skipped_irregular_ops += 1
                continue
            regular = True
            for k in range(2, len(op_ws)):
                if abs((op_ws[k] - op_ws[k - 1]) - step) > 0.5:
                    regular = False
                    break
            if not regular:
                skipped_irregular_ops += 1
                continue

            # Target width unifies cutouts across the run.
            W_target = round(step - spacing, 3)
            if not (min_w <= W_target <= global_max_w):
                skipped_width_bounds += 1
                continue

            # Compute the new run span and the delta to distribute.
            N = len(run)
            current_span = (run[-1].x + run[-1].w) - run[0].x
            new_span = N * W_target + (N - 1) * spacing
            delta = new_span - current_span

            # Midpoint approach: center of run stays put, so both outer
            # neighbors absorb half the delta each.
            center = run[0].x + current_span / 2.0
            new_x0    = round(center - new_span / 2.0, 3)
            new_x_end = round(new_x0 + new_span, 3)

            new_left_w  = round(left_n.w  + (new_x0 - run[0].x), 3)
            new_right_x = round(new_x_end + spacing, 3)
            new_right_w = round(right_n.w + ((run[-1].x + run[-1].w) - new_x_end), 3)

            if not (min_w - 0.05 <= new_left_w  <= global_max_w + 0.05):
                skipped_neighbor_bounds += 1
                continue
            if not (min_w - 0.05 <= new_right_w <= global_max_w + 0.05):
                skipped_neighbor_bounds += 1
                continue

            # Seam-safety: no seam (interior or outer boundary) may fall in
            # any opening's clearance zone.
            seams = [round(new_x0 - spacing, 3), new_x_end]
            for k in range(N - 1):
                seams.append(round(new_x0 + (k + 1) * W_target + k * spacing, 3))
            seam_bad = False
            for seam in seams:
                for o in openings:
                    if getattr(o, 'left_clearance_zone', None) is None:
                        continue
                    if o.left_clearance_zone - 0.01 < seam < o.right_clearance_zone + 0.01:
                        seam_bad = True
                        break
                if seam_bad:
                    break
            if seam_bad:
                skipped_seam_in_opening += 1
                continue

            # Commit.
            left_n.w = new_left_w
            for k, pp in enumerate(run):
                pp.x = round(new_x0 + k * (W_target + spacing), 3)
                pp.w = W_target
            right_n.x = new_right_x
            right_n.w = new_right_w

            left_n.cutouts  = calculate_panel_cutouts(left_n,  openings)
            for pp in run:
                pp.cutouts = calculate_panel_cutouts(pp, openings)
            right_n.cutouts = calculate_panel_cutouts(right_n, openings)

            unified_runs += 1
            _diag(Ansi.CYAN +
                  "  [UNIFY] Run of {} panel(s) forced to W={:.3f}\" "
                  "(delta {:+.2f}\", split {:+.2f}\"/{:+.2f}\" L/R); "
                  "L:{}.w={:.2f}\"  R:{}.w={:.2f}\"".format(
                    N, W_target, delta,
                    new_x0 - run[0].x,
                    (run[-1].x + run[-1].w) - new_x_end,
                    left_n.name, left_n.w,
                    right_n.name, right_n.w) + Ansi.RESET)

    if unified_runs or (skipped_no_neighbor + skipped_irregular_ops +
                        skipped_width_bounds + skipped_neighbor_cutout +
                        skipped_neighbor_bounds + skipped_seam_in_opening):
        _diag(Ansi.CYAN +
              "  [UNIFY] Unified {} run(s). Skipped: "
              "no-outer-neighbor={} irregular-openings={} "
              "target-width-oob={} neighbor-has-cutout={} "
              "neighbor-width-oob={} seam-in-opening={}".format(
                unified_runs, skipped_no_neighbor, skipped_irregular_ops,
                skipped_width_bounds, skipped_neighbor_cutout,
                skipped_neighbor_bounds, skipped_seam_in_opening) + Ansi.RESET)

    return panels

def calculate_segment_layout(start_x, target_x, max_w, min_w, inc, spacing):
    total_dist = target_x - start_x
    
    if total_dist < min_w: 
        return total_dist
        
    if total_dist <= max_w: 
        return snap_down(total_dist, inc)
    
    # --- THE ANTI-SPLINTER EQUALIZER ---
    # Instead of being greedy (e.g., 30ft gap = 24ft max + 6ft splinter),
    # we divide the long gap evenly to drastically reduce unique types! 
    # (e.g., 30ft gap = 15ft + 15ft identical twins)
    import math
    n_panels = math.ceil((total_dist + spacing) / (max_w + spacing))
    optimal_w = (total_dist - (n_panels - 1) * spacing) / n_panels
    
    return snap_down(optimal_w, inc)


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

        # --- DYNAMIC ORIENTATION SWAP (UI DRIVEN) ---
        global ACTIVE_CONFIG
        swap_thresh = 143.0
        if ACTIVE_CONFIG and hasattr(ACTIVE_CONFIG.optimization_strategy, 'horizontal_to_vertical_threshold_in'):
            swap_thresh = ACTIVE_CONFIG.optimization_strategy.horizontal_to_vertical_threshold_in

        # Parapet allowance is consulted deeper down (line ~1218) when deciding
        # whether an over-tall band is an intentional parapet override. Must be
        # defined here so that reference resolves regardless of orientation.
        parapet_allowance = 0.0
        if ACTIVE_CONFIG and hasattr(ACTIVE_CONFIG.optimization_strategy, 'flexible_top_panel_allowance_in'):
            parapet_allowance = float(getattr(ACTIVE_CONFIG.optimization_strategy,
                                              'flexible_top_panel_allowance_in', 0.0) or 0.0)

        region_w = region['x_end'] - region['x_start']
        local_horizontal = horizontal_mode
        
        # If threshold is 0, the feature is disabled. Otherwise, swap if narrower than threshold.
        if horizontal_mode and swap_thresh > 0 and region_w < swap_thresh:
            local_horizontal = False
            
        bands = []
        cy = 0
        while cy < wall_height:
            rem_h = wall_height - cy
            
            # --- FIX: TALL VERTICAL STACKING ---
            # Horizontal panels stack at SHORT_MAX (138"). Vertical panels stack at LONG_MAX (348").
            h_limit = SHORT_MAX if local_horizontal else LONG_MAX

            bh = snap_down(min(rem_h, h_limit), DIMENSION_INCREMENT)

            # --- PARAPET ABSORBER ---
            # In horizontal mode, if capping this band at h_limit would leave
            # a remainder that fits within the flexible top allowance (e.g.,
            # a 24" parapet strip on top of a 138" course), extend this band
            # to swallow the remainder instead of adding a short splinter
            # band. This is what makes the "Flexible Top Panel Allowance
            # (Absorb Parapet)" config setting actually take effect on
            # horizontal panels.
            if local_horizontal and parapet_allowance > 0:
                would_leave = rem_h - bh
                if 0 < would_leave <= parapet_allowance:
                    bh = snap_down(rem_h, DIMENSION_INCREMENT)

            if bh >= PANEL_HEIGHT_MIN:
                bands.append((cy, cy + bh))
                cy += bh
            else: 
                # If remainder is too small, absorb it into the last band to prevent errors
                if bands:
                    bands[-1] = (bands[-1][0], wall_height)
                else:
                    bands.append((0, wall_height))
                break
            

        for y_start, y_end in bands:
            band_height = y_end - y_start
            
            # Use massive LONG_MAX if horizontal, or strictly SHORT_MAX if swapped to vertical
            max_width_for_band = LONG_MAX if local_horizontal else SHORT_MAX
            
            x_cursor = max(0.0, region['x_start'])

            while x_cursor < region['x_end']:
                remaining_wall = region['x_end'] - x_cursor

                # Widen last panel to absorb end-of-wall remainders
                if remaining_wall > 0 and remaining_wall < PANEL_WIDTH_MIN:
                    band_panels = [p for p in panels if abs(p.y - y_start) < 0.01]
                    if band_panels:
                        last = band_panels[-1]
                        
                        # --- FIX 2: ROUND WIDENED PANEL EXTENSION ---
                        extended_w = round(last.w + spacing + remaining_wall, 3) 
                        
                        if extended_w <= max_width_for_band:
                            last.w = extended_w
                            last.cutouts = calculate_panel_cutouts(last, region_openings)
                        else:
                            # --- FIX 3: ROUND REMAINDER SLIVER ---
                            snap_w = round(remaining_wall, 3) 
                            candidate = Panel(x_cursor, y_start, snap_w, band_height, "P{:02d}".format(panel_counter))
                            candidate.cutouts = calculate_panel_cutouts(candidate, region_openings)
                            panels.append(candidate)
                            panel_counter += 1
                    break

                if remaining_wall < PANEL_WIDTH_MIN: break

                # Prevent swallowing Storefronts/Doors
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
                        
                        # --- FIX 4: ROUND BRIDGED PANEL ---
                        panel_w = round(panel_w, 3)
                        
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
                
                # --- FIX: MAX 2 CUTOUTS LIMIT ---
                # Prevent panels from swallowing more windows than the Revit family can physically cut
                contained_openings = [
                    o for o in region_openings 
                    if o.right_clearance_zone > x_cursor and o.left_clearance_zone < candidate_right
                    and not (o.top_clearance_zone <= y_start or o.bottom_clearance_zone >= y_end)
                    and is_cutout_opening(o, constraints)
                ]
                
                if len(contained_openings) > 2:
                    # Shrink panel width to stop safely before the 3rd opening
                    third_op = contained_openings[2]
                    safe_w = third_op.left_clearance_zone - x_cursor
                    panel_w = snap_down(safe_w, DIMENSION_INCREMENT)
                    candidate_right = x_cursor + panel_w

                # Check for jamb clashes and adjust
                for op in region_openings:
                    if (op.top_clearance_zone <= y_start or op.bottom_clearance_zone >= y_end):
                        continue
                        
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

                # Bypass the validity break if this is an intentional parapet override
                is_parapet = (parapet_allowance > 0 and band_height > SHORT_MAX)
                if not is_valid_panel(panel_w, band_height, constraints) and not is_parapet: 
                    break


                # --- FIX 5: ROUND FINAL GRID PANEL ---
                panel_w = round(panel_w, 3)

                candidate = Panel(x_cursor, y_start, panel_w, band_height, "P{:02d}".format(panel_counter))
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
                if not horizontal_mode:
                    # vertical wall -> spandrel over the storefront is HORIZONTAL panels
                    panel_counter = fill_horizontal_courses(
                        sf.left_clearance_zone, sf.right_clearance_zone,
                        sf.top_clearance_zone, wall_height,
                        panels, panel_counter, constraints, sorted_openings)
                else:
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
    print(Ansi.MAGENTA + "  [EXECUTE] Solving for Min Total + Unique (GA)..." + Ansi.RESET)
    
    best_sequence = solve_with_ga(wall_width, constraints, strategy, openings)
    panels = []
    panel_counter = 1
    x_cursor = 0.0
    spacing = constraints.panel_spacing
    
    min_w = constraints.min_width
    max_w = constraints.max_width

    # 2. Convert the GA widths into Panel objects (clamp, never silently drop)
    for w in best_sequence:
        remaining = wall_width - x_cursor
        if remaining < min_w:
            break
        actual_w = w if (x_cursor + w) <= wall_width else remaining  # clamp last
        p = Panel(x_cursor, 0, actual_w, wall_height, "PGA{:02d}".format(panel_counter))
        p.cutouts = calculate_panel_cutouts(p, openings)
        panels.append(p)
        panel_counter += 1
        x_cursor += (actual_w + spacing)

    # 2b. If the GA undershot, tile the remaining tail so the wall is fully covered
    while (wall_width - x_cursor) >= min_w:
        remaining = wall_width - x_cursor
        actual_w = remaining if remaining <= max_w else max_w
        # avoid leaving a sub-min sliver behind
        if 0 < (remaining - actual_w) < min_w:
            actual_w = remaining - min_w
        p = Panel(x_cursor, 0, actual_w, wall_height, "PGA{:02d}".format(panel_counter))
        p.cutouts = calculate_panel_cutouts(p, openings)
        panels.append(p)
        panel_counter += 1
        x_cursor += (actual_w + spacing)
        
    print(Ansi.GREEN + "  [EXECUTE] Successfully placed {} panels."
          .format(len(panels)) + Ansi.RESET)
    return panels

    
def calculate_panel_cutouts(panel, openings):
    cutouts = []
    
    for opening in openings:
        if getattr(opening, 'force_blocker', False) and opening.w > 100.0:
            continue

        hole_left = opening.x - getattr(opening.clearances, 'rough_jamb', 0.0)
        hole_right = opening.x + opening.w + getattr(opening.clearances, 'rough_jamb', 0.0)
        hole_bottom = opening.y - getattr(opening.clearances, 'rough_sill', 0.0)
        hole_top = opening.y + opening.h + getattr(opening.clearances, 'rough_header', 0.0)

        inter_left = max(panel.x, hole_left)
        inter_right = min(panel.x + panel.w, hole_right)
        inter_bottom = max(panel.y, hole_bottom)
        inter_top = min(panel.y + panel.h, hole_top)

        if inter_right > inter_left + 0.01 and inter_top > inter_bottom + 0.01:
            raw_x = float(opening.x - panel.x)
            raw_y = float(max(0.0, opening.y - panel.y))
            
            # --- FIX: THE LAYERED GEOMETRY DEDUPLICATOR ---
            # Prevent nested Revit geometry from spawning 6 cutouts in the same spot
            is_dup = False
            for c in cutouts:
                if (abs(c["raw_x_in"] - raw_x) < 2.0 and 
                    abs(c["raw_y_in"] - raw_y) < 2.0 and 
                    abs(c["raw_width_in"] - float(opening.w)) < 2.0 and 
                    abs(c["raw_height_in"] - float(opening.h)) < 2.0):
                    is_dup = True
                    break
                    
            if not is_dup:
                cutout_info = {
                    "id":            opening.id,
                    "type":          getattr(opening, 'type', 'Opening'),
                    "x_in":          float(inter_left - panel.x),
                    "y_in":          float(inter_bottom - panel.y),
                    "width_in":      float(inter_right - inter_left),
                    "height_in":     float(inter_top - inter_bottom),
                    "raw_x_in":      raw_x,
                    "raw_y_in":      raw_y,
                    "raw_width_in":  float(opening.w),
                    "raw_height_in": float(opening.h),
                    "rough_jamb":    float(getattr(opening.clearances, 'rough_jamb', 0.0)),
                }
                cutouts.append(cutout_info)

    cutouts.sort(key=lambda c: (-c["y_in"], c["x_in"]))
    return cutouts


def solve_with_ga(zone_len, constraints, strategy, openings=None):
    import random
    # Deterministic per-zone seed: identical input -> identical output
    # (reproducible tournament/GA runs), while different zones still differ.
    random.seed(int(round(zone_len * 1000.0)))
    
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

    # Two weights: np (total panels) and nu (unique types). Fall back to the
    # legacy single weight if the new ones aren't present.
    np_w = getattr(strategy, 'np_weight', 1.0)
    nu_w = getattr(strategy, 'nu_weight', getattr(strategy, 'unique_weight', 10.0))

    def fitness(chromosome):
        total_panels = len(chromosome)
        unique_types = len(set(chromosome))

        length_penalty = abs(zone_len - sum(chromosome)) * 100.0

        # Opening-aware: penalize interior seams that fall inside an opening's
        # clearance zone (a panel break across a window is bad fabrication).
        seam_penalty = 0.0
        if openings:
            x = 0.0
            for w in chromosome[:-1]:
                x += w
                for o in openings:
                    if o.left_clearance_zone < x < o.right_clearance_zone:
                        seam_penalty += 50.0
                        break
                x += constraints.panel_spacing

        score = (nu_w * unique_types) + (np_w * total_panels) + length_penalty + seam_penalty
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

# =============================================================================
# SECTION 5: REPEATING PATTERN DETECTION & STANDARD PANEL STRATEGY
# =============================================================================

def detect_repeating_opening_groups(openings, constraints):
    """
    Find groups of openings that are identical in size AND evenly spaced.
    Returns a list of groups, each group being a sorted list of Opening objects.
    Only groups with 2+ members qualify.
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
            centers = [o.x + o.w / 2.0 for o in band_sorted]

            # --- DYNAMIC CHUNKING LOGIC ---
            i = 0
            while i < len(band_sorted) - 1:
                ref_spacing = centers[i+1] - centers[i]
                run = [band_sorted[i], band_sorted[i+1]]
                j = i + 1
                
                # Check how many subsequent windows share this exact spacing
                while j < len(band_sorted) - 1:
                    next_spacing = centers[j+1] - centers[j]
                    if abs(next_spacing - ref_spacing) <= inc:
                        run.append(band_sorted[j+1])
                        j += 1
                    else:
                        break

                if len(run) >= 2:
                    repeating.append(run)
                    _diag(Ansi.CYAN + "  [PATTERN] Repeating group: {} opening(s) "
                          "{}\"x{}\" y={:.1f}\" spaced {:.3f}\" c-to-c".format(
                          len(run), round(key[0], 3), round(key[1], 3),
                          round(run[0].y, 1), ref_spacing) + Ansi.RESET)
                
                # Advance PAST this run. band_sorted[j] is already consumed as
                # the run's last member -- restarting at j (old behavior) reused
                # that same window as the first element of the next candidate
                # run, fabricating a bogus 2-window "group" out of the boundary
                # (e.g. last window of cluster A + first window of cluster B),
                # which corrupted zone-merging and mirror/identical detection.
                i = j + 1

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


def _floor_bands(wall_h, rel_elevs, min_band=24.0, max_band=None, parapet_allowance=0.0):
    """One band per storey: slice the wall at every interior level elevation so
    each panel spans floor-to-floor. Used when 'limit panel height to floor' is on.

    Coverage runs base (0) -> true wall top (wall_h), so a parapet above the top
    level is included. Three corrections keep every band fabricable AND covered:
      * If the topmost segment (parapet strip) is <= parapet_allowance, MERGE it
        into the underlying floor band so the top panel absorbs the parapet
        instead of shipping as a sub-fabricable stub.
      * A band shorter than min_band (a sub-fabricable remnant) is MERGED into
        its neighbour rather than dropped.
      * A band taller than max_band (e.g. a merged floor+parapet that exceeds the
        horizontal Short Max) is SPLIT into equal legal courses -- EXCEPT the
        top band, which is allowed to exceed max_band by up to parapet_allowance
        so the parapet absorption isn't undone. Pass max_band = short_max for
        horizontal, long_max for vertical."""
    import math
    bnds = [0.0] + [float(e) for e in rel_elevs if 0.0 < float(e) < wall_h] + [float(wall_h)]
    bnds = sorted(set(round(b, 4) for b in bnds))

    # --- PARAPET ABSORBER ---
    # If the topmost segment is a parapet strip within the flexible-top allowance,
    # remove the level break beneath it so it merges into the underlying floor.
    if parapet_allowance > 0 and len(bnds) >= 3:
        top_segment = bnds[-1] - bnds[-2]
        if 0 < top_segment <= parapet_allowance + 1e-6:
            del bnds[-2]

    # Merge any band below the minimum fabricable height into its neighbour.
    changed = True
    while changed and len(bnds) > 2:
        changed = False
        for i in range(len(bnds) - 1):
            if (bnds[i + 1] - bnds[i]) < min_band:
                if i + 1 < len(bnds) - 1:
                    del bnds[i + 1]
                else:
                    del bnds[i]
                changed = True
                break
    bands = [(bnds[i], bnds[i + 1]) for i in range(len(bnds) - 1)] or [(0.0, round(wall_h, 4))]
    # [FIX] Split any band taller than the max panel height greedily, pushing min_band to the top.
    if max_band and max_band > 0:
        out = []
        n_bands = len(bands)
        for idx, (a, b) in enumerate(bands):
            h = b - a
            # --- PARAPET EXEMPTION ---
            # The top band is allowed to exceed max_band by up to
            # parapet_allowance -- otherwise the split logic here would undo the
            # absorption we just did above.
            is_top_band = (idx == n_bands - 1)
            effective_max = max_band + (parapet_allowance if is_top_band else 0.0)
            if h > effective_max + 1e-6:
                cy = a
                while cy < b:
                    rem_h = b - cy
                    if rem_h > max_band and (rem_h - max_band) < min_band:
                        bh = rem_h - min_band
                    else:
                        bh = min(rem_h, max_band)

                    if bh < min_band:
                        if out and out[-1][1] == cy:
                            out[-1] = (out[-1][0], b)
                        else:
                            out.append((cy, b))
                        break

                    out.append((round(cy, 4), round(cy + bh, 4)))
                    cy += bh
            else:
                out.append((a, b))
        bands = out
    return bands


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
    if wall_h <= max_ph + 6.0:
        return [(0.0, round(wall_h, 4))]
    if not rel_elevs:
        # No interior levels but the wall is taller than one panel: split into
        # equal bands, each <= max_ph, so vertical panels stay within Long Max
        # and the whole wall is covered (previously returned one over-tall band).
        import math
        n = int(math.ceil(wall_h / float(max_ph)))
        step = wall_h / float(n)
        return [(round(k * step, 4), round((k + 1) * step, 4)) for k in range(n)]

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

    # --- SINGLE-STRATEGY DISPATCH ---
    # Tournament is handled building-wide in optimize_building(), which forces one
    # base mode per pass. process_wall only ever runs ONE strategy. If a stray
    # tournament flag reaches here, fall through to the base branches below.
    if getattr(strategy, 'use_ga_optimizer', False):
        _diag(Ansi.MAGENTA + "  [EXECUTE] Running Min Total + Unique..." + Ansi.RESET)
        panels = _place_ga_optimized(wall_width, wall_height, openings, constraints, orientation, strategy)
        
    elif getattr(strategy, 'minimize_unique_panels', False):
        _diag(Ansi.MAGENTA + "  [EXECUTE] Running Minimize Unique..." + Ansi.RESET)
        panels = _place_minimize_unique(wall_width, wall_height, openings, constraints, orientation, strategy)
        
    else:
        # =================================================================
        # FIX: The hard 'else' guarantees this runs if nothing else does
        # =================================================================
        _diag(Ansi.MAGENTA + "  [EXECUTE] Running Minimize Total Panels..." + Ansi.RESET)
        panels = place_panels_sequential(wall_width, wall_height, openings, constraints, orientation)

    # Option 1: equalize drifted sibling panels at the source.
    panels = _equalize_sibling_panels(panels, openings, constraints)

    # Option 2: unify runs of same-cutout-shape panels around evenly-spaced
    # openings by forcing a common width (renegotiates with outer neighbors).
    # This catches "same opening pattern, different width" cases (e.g., T27/T30
    # in the storefront wall runs).
    panels = _unify_pattern_runs(panels, openings, constraints, orientation)

    # Option 3: fixed-width residual pass. Same panel_w + same cutout shape but
    # different cutout local X -> pull seams so cutouts land at a common local X.
    # Catches stragglers _unify_pattern_runs couldn't touch.

    panels = _accordion_seam_shifter(panels, openings, constraints, orientation)

    # Option 4: absorb narrow solid fill panels between two matching hosts.
    # Two anchor hosts + solid fill -> 2 mirror-symmetric widened hosts. Same
    # unique-type count (mirror-aware), one fewer total panel.
    panels = _absorb_narrow_fills(panels, openings, constraints, orientation)

    
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

    _diag(Ansi.GREEN + " Result: {} panels generated".format(len(panels)) + Ansi.RESET)
    return records

def _place_minimize_unique(wall_width, wall_height, openings, constraints,
                            orientation, strategy):
    """
    Placement pass for the Minimize Unique strategy using Anchored Tiling.
    Includes Global Bands, Parapet Absorption, 3-Decimal Snapping, and Dynamic Swap.
    """
    import math

    orientation = str(orientation or "vertical").lower()
    horizontal_mode = (orientation == "horizontal")

    # --- 1. Generate Global Horizontal Bands (With Parapet Absorber) ---
    def get_global_y_bands(total_h, wall_openings):
        if not horizontal_mode: 
            return [(0.0, float(total_h))]
        
        critical_y = set([0.0, float(total_h)])
        for o in wall_openings:
            critical_y.add(round(o.y, 3))
            critical_y.add(round(o.y + o.h, 3))
            
        sorted_y = sorted(list(critical_y))
        bands = []
        
        cy = 0.0
        inc = constraints.dimension_increment
        short_max = constraints.short_max
        parapet_allowance = getattr(strategy, 'flexible_top_panel_allowance_in', 0.0)
        
        for next_y in sorted_y:
            if next_y <= cy + 0.01: continue
            rem_span = next_y - cy
            
            while rem_span > 0.01:
                # --- FIX: PARAPET ABSORBER ---
                if parapet_allowance > 0 and short_max < rem_span <= (short_max + parapet_allowance):
                    bh = math.floor(rem_span / inc) * inc
                else:
                    max_h_allow = min(rem_span, short_max)
                    bh = math.floor(max_h_allow / inc) * inc if rem_span >= constraints.min_height else rem_span
                
                if bh < constraints.min_height and bands:
                    bands[-1] = (bands[-1][0], cy + rem_span)
                    cy += rem_span
                    break
                    
                bands.append((cy, cy + bh))
                cy += bh
                rem_span = next_y - cy
                
        return bands

    wall_bands = get_global_y_bands(wall_height, openings)

    # 2. Classify openings 
    classify_openings_dynamic(openings, constraints)
    repeating_groups = detect_repeating_opening_groups(openings, constraints)

    if not repeating_groups:
        _diag(Ansi.YELLOW + "  [PATTERN] No repeating groups found — "
              "using Minimize Total Panels fallback." + Ansi.RESET)
        return place_panels_sequential(wall_width, wall_height, openings, constraints, orientation)

    panels        = []
    panel_counter = 1
    covered_zones = []

    def _zone_of(grp): return grp[0].left_clearance_zone, grp[-1].right_clearance_zone

    # --- THE FIX: DOMINANT GRID ELIMINATOR ---
    # 1. Sort groups by longest physical span so the largest, most dominant pattern wins.
    repeating_groups.sort(key=lambda g: _zone_of(g)[1] - _zone_of(g)[0], reverse=True)
    
    merged_groups = []
    for group in repeating_groups:
        zs, ze = _zone_of(group)
        clash = False
        
        for m in merged_groups:
            # 2. Check if this pattern physically overlaps an already-approved dominant pattern
            overlap = min(ze, m[1]) - max(zs, m[0])
            if overlap > 1.0: # If they overlap by more than 1 inch
                clash = True
                break
            

# 3. If there is NO clash, we keep the pattern pure and unpolluted.
        # If there IS a clash, we discard this inferior pattern entirely.
        if not clash:
            merged_groups.append([zs, ze, list(group)])

    # --- INTRUDER CHECK ---
    # The anchor grid sizes panels for the pattern openings (typically storefronts).
    # If a non-pattern opening (e.g. a door) falls inside a pattern's zone and its
    # clearance width exceeds the anchor's predicted standard_w, the anchor cannot
    # host it in a single panel and would split it across a seam. Bail those zones
    # and let the greedy tiler handle them so openings stay intact.
    def _predict_standard_w(zone_ops):
        max_w = constraints.long_max if horizontal_mode else constraints.max_width
        if len(zone_ops) < 2:
            return max_w
        _s = sorted(zone_ops, key=lambda o: o.x)
        _c = [o.x + o.w / 2.0 for o in _s]
        _ws = sum(_c[i+1] - _c[i] for i in range(len(_c)-1)) / (len(_c)-1)
        _trial = _ws - constraints.panel_spacing
        if _trial <= max_w:
            return _trial
        _divs = math.ceil(_ws / (max_w + constraints.panel_spacing))
        return (_ws / _divs) - constraints.panel_spacing

    filtered_merged_groups = []
    for zs_, ze_, zone_ops_ in merged_groups:
        _group_ids = set(id(o) for o in zone_ops_)
        _std_w = _predict_standard_w(zone_ops_)
        _intruders = []
        for _op in openings:
            if id(_op) in _group_ids:
                continue
            if _op.right_clearance_zone <= zs_ + 0.1:
                continue
            if _op.left_clearance_zone >= ze_ - 0.1:
                continue
            _op_clr_w = _op.right_clearance_zone - _op.left_clearance_zone
            if _op_clr_w > _std_w + 0.1:
                _intruders.append(_op)
        if _intruders:
            _desc = ", ".join("{}@x={:.0f}".format(
                getattr(_o, 'opening_type', '?'), _o.x) for _o in _intruders[:3])
            _diag(Ansi.YELLOW + "  [ANCHOR-SKIP] Pattern zone {:.1f}\"..{:.1f}\": {} wider-than-host opening(s) ({}); greedy will handle.".format(
                zs_, ze_, len(_intruders), _desc) + Ansi.RESET)
            continue
        filtered_merged_groups.append([zs_, ze_, zone_ops_])
    merged_groups = filtered_merged_groups

    # 3. Process each merged zone (MULTI-WINDOW WITH ASYMMETRICAL ANCHOR EXPANSION)
    memory_bank = {}

    # --- INTRUDER CHECK ---
    # The anchor grid sizes panels for the pattern openings (typically storefronts).
    # If a non-pattern opening (e.g. a door) falls inside a pattern's zone and its
    # clearance width exceeds the anchor's predicted standard_w, the anchor cannot
    # host it in a single panel and would split it across a seam. Bail those zones
    # and let the greedy tiler handle them so openings stay intact.
    def _predict_standard_w(zone_ops):
        max_w = constraints.long_max if horizontal_mode else constraints.max_width
        if len(zone_ops) < 2:
            return max_w
        _s = sorted(zone_ops, key=lambda o: o.x)
        _c = [o.x + o.w / 2.0 for o in _s]
        _ws = sum(_c[i+1] - _c[i] for i in range(len(_c)-1)) / (len(_c)-1)
        _trial = _ws - constraints.panel_spacing
        if _trial <= max_w:
            return _trial
        _divs = math.ceil(_ws / (max_w + constraints.panel_spacing))
        return (_ws / _divs) - constraints.panel_spacing

    filtered_merged_groups = []
    for zs_, ze_, zone_ops_ in merged_groups:
        _group_ids = set(id(o) for o in zone_ops_)
        _std_w = _predict_standard_w(zone_ops_)
        _intruders = []
        for _op in openings:
            if id(_op) in _group_ids:
                continue
            if _op.right_clearance_zone <= zs_ + 0.1:
                continue
            if _op.left_clearance_zone >= ze_ - 0.1:
                continue
            _op_clr_w = _op.right_clearance_zone - _op.left_clearance_zone
            if _op_clr_w > _std_w + 0.1:
                _intruders.append(_op)
        if _intruders:
            _desc = ", ".join("{}@x={:.0f}".format(
                getattr(_o, 'opening_type', '?'), _o.x) for _o in _intruders[:3])
            _diag(Ansi.YELLOW + "  [ANCHOR-SKIP] Pattern zone {:.1f}\"..{:.1f}\": {} wider-than-host opening(s) ({}); greedy will handle.".format(
                zs_, ze_, len(_intruders), _desc) + Ansi.RESET)
            continue
        filtered_merged_groups.append([zs_, ze_, zone_ops_])
    merged_groups = filtered_merged_groups

    # 3. Process each merged zone (MULTI-WINDOW WITH ASYMMETRICAL ANCHOR EXPANSION)
    memory_bank = {}
                


    for zone_start, zone_end, zone_openings in merged_groups:
        primary_band = sorted(zone_openings, key=lambda o: o.x)
        primary_o = primary_band[0]

        standard_w     = None
        canonical_left = None
        n_panels       = 0

        max_w_allowed = constraints.long_max if horizontal_mode else constraints.max_width
        
        band_w = primary_band[-1].x + primary_band[-1].w - primary_o.x
        local_windows = tuple((round(w.x - primary_o.x, 2), round(w.w, 2)) for w in primary_band)
        mirrored_windows = tuple((round(band_w - (w.x - primary_o.x) - w.w, 2), round(w.w, 2)) for w in reversed(primary_band))

        if mirrored_windows in memory_bank:
            standard_w, orig_canonical_left = memory_bank[mirrored_windows]
            # If the original zone used a full-band anchor (standard_w == band_w),
            # a genuine LH/RH mirror needs the flipped offset. If the strict-limit
            # enforcer split the zone into per-panel modules, standard_w is a
            # per-panel width and mirroring is meaningless — reuse the original
            # canonical_left directly. (Otherwise the mixed-units formula produces
            # a negative offset and strands most of the zone.)
            if abs(standard_w - band_w) < 0.5:
                canonical_left = band_w - orig_canonical_left - primary_o.w
            else:
                canonical_left = orig_canonical_left
            _diag(Ansi.CYAN + "  [PATTERN] Mirrored LH/RH Twin Detected!" + Ansi.RESET)
        
            
        else:
            window_spacing = 0
            if len(primary_band) >= 2:
                ctrs = [o.x + o.w / 2.0 for o in primary_band]
                window_spacing = sum(ctrs[i+1] - ctrs[i] for i in range(len(ctrs)-1)) / (len(ctrs) - 1)

            # --- ASYMMETRICAL MAXIMAL ANCHOR EXPANSION ---
            if band_w <= max_w_allowed and len(primary_band) <= 2:
                matching_bands = []
                for mz_start, mz_end, mz_openings in merged_groups:
                    mz_band = sorted(mz_openings, key=lambda o: o.x)
                    if not mz_band: continue
                    mz_band_w = mz_band[-1].x + mz_band[-1].w - mz_band[0].x
                    mz_sig = tuple((round(w.x - mz_band[0].x, 2), round(w.w, 2)) for w in mz_band)
                    mz_mir = tuple((round(mz_band_w - (w.x - mz_band[0].x) - w.w, 2), round(w.w, 2)) for w in reversed(mz_band))
                    
                    if mz_sig == local_windows:
                        matching_bands.append((mz_band, True))
                    elif mz_mir == local_windows:
                        matching_bands.append((mz_band, False))
                
                min_safe_left = max_w_allowed
                min_safe_right = max_w_allowed
                
                for m_band, is_direct in matching_bands:
                    m_first = m_band[0]
                    m_last = m_band[-1]
                    
                    left_obs = 0.0
                    right_obs = wall_width
                    for o in openings:
                        if o not in m_band:
                            if o.x + o.w <= m_first.x + 0.01:
                                left_obs = max(left_obs, o.x + o.w + constraints.panel_spacing)
                            if o.x >= m_last.x + m_last.w - 0.01:
                                right_obs = min(right_obs, o.x - constraints.panel_spacing)
                                
                    dist_left = max(0.0, m_first.x - left_obs)
                    dist_right = max(0.0, right_obs - (m_last.x + m_last.w))
                    
                    if is_direct:
                        min_safe_left = min(min_safe_left, dist_left)
                        min_safe_right = min(min_safe_right, dist_right)
                    else:
                        min_safe_left = min(min_safe_left, dist_right)
                        min_safe_right = min(min_safe_right, dist_left)
                
                inc = constraints.dimension_increment
                exp_L = math.floor(min_safe_left / inc) * inc
                exp_R = math.floor(min_safe_right / inc) * inc
                
                while (band_w + exp_L + exp_R) > max_w_allowed + 0.001:
                    if exp_L > exp_R and exp_L >= inc: exp_L -= inc
                    elif exp_R >= inc: exp_R -= inc
                    elif exp_L >= inc: exp_L -= inc
                    else: break
                
                standard_w = band_w + exp_L + exp_R
                canonical_left = exp_L

            # --- MULTI-WINDOW TILING ---

            else:
                best_N = 1
                for N in range(1, 3): 
                    w_test = (N * window_spacing) - constraints.panel_spacing
                    if w_test <= max_w_allowed:
                        best_N = N
                    else:
                        break 
                
                standard_w = (best_N * window_spacing) - constraints.panel_spacing
                
                # --- THE STRICT LIMIT ENFORCER ---
                # If the window spacing itself exceeds the physical board limit, 
                # we must divide the span into mathematically equal sub-panels.
                if standard_w > max_w_allowed:
                    divs = math.ceil(window_spacing / (max_w_allowed + constraints.panel_spacing))
                    standard_w = (window_spacing / divs) - constraints.panel_spacing
                    # Center the window within the divided group
                    group_w = primary_o.w
                    canonical_left = (standard_w - group_w) / 2.0
                else:
                    group_w = ((best_N - 1) * window_spacing) + primary_o.w
                    canonical_left = (standard_w - group_w) / 2.0

            if standard_w is not None:
                memory_bank[local_windows] = (standard_w, canonical_left)

        if standard_w is None or standard_w < constraints.min_width:
            continue 

        # 4. Anchor the Grid 
        actual_zone_start = primary_o.x - canonical_left
        if actual_zone_start < 0: actual_zone_start = 0.0

        last_o = primary_band[-1]
        dist_to_cover = (last_o.x + last_o.w + canonical_left) - actual_zone_start
        n_panels = max(1, int(math.ceil((dist_to_cover - 0.1) / (standard_w + constraints.panel_spacing))))

        actual_zone_end = actual_zone_start

        for i in range(n_panels):
            # --- FIX: ROUND GRID ANCHOR TO 3 DECIMALS ---
            panel_x = round(actual_zone_start + i * (standard_w + constraints.panel_spacing), 3) 
            actual_w = standard_w
            
            if panel_x + actual_w > wall_width:
                actual_w = wall_width - panel_x
                if actual_w < constraints.min_width:
                    break
                
                # --- FIX: PREVENT HORIZONTAL SLIVERS ---
                swap_thresh = getattr(strategy, 'horizontal_to_vertical_threshold_in', 143.0)
                if horizontal_mode and swap_thresh > 0 and actual_w < swap_thresh:
                    has_cutout = any(op.x < panel_x + actual_w and op.x + op.w > panel_x for op in openings)
                    if not has_cutout:
                        break # Leave this blank zone for the vertical gap filler!
            
            for y_s, y_e in wall_bands:
                raw_h = y_e - y_s
                actual_h = raw_h - constraints.panel_spacing if (raw_h - constraints.panel_spacing) >= constraints.min_height else raw_h
                
                # --- FIX: ROUND HEIGHT AND WIDTH TO 3 DECIMALS ---
                actual_w = round(actual_w, 3)
                actual_h = round(actual_h, 3)
                
                p = Panel(panel_x, y_s, actual_w, actual_h, 'P{:02d}'.format(panel_counter))
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
                        # --- FIX: ROUND WIDENED PANEL TO 3 DECIMALS ---
                        ap.w = round(ap.w + u_width, 3)
                        ap.cutouts = calculate_panel_cutouts(ap, openings)
                    continue
        
        u_openings = [o for o in openings if not (o.right_clearance_zone <= u_start or o.left_clearance_zone >= u_end)]
        for o in u_openings: o.x -= u_start

        # --- FIX: DYNAMIC ORIENTATION SWAP (UI DRIVEN) ---
        swap_thresh = getattr(strategy, 'horizontal_to_vertical_threshold_in', 143.0)
        zone_orientation = orientation
        
        if orientation == "horizontal" and swap_thresh > 0 and u_width < swap_thresh:
            zone_orientation = "vertical"

        u_panels = place_panels_sequential(u_width, wall_height, u_openings, constraints, zone_orientation)
        
        for p in u_panels:
            p.x += u_start
            p.name = "P{:02d}".format(panel_counter)
            panel_counter += 1
        panels.extend(u_panels)
        
        for o in u_openings: o.x += u_start

    # --- THE SEAM WELDER (Solid Panel Combiner) ---
    #
    # Two things this step needs to get right that the previous version got
    # wrong:
    #   1. When an opening straddles a panel joint, each side carries a
    #      "half cutout" for that opening. The old cutout-count check saw
    #      the raw sum (e.g. 2 halves on left + 2 halves on right = 4) and
    #      refused to weld, so straddling stayed forever. What matters is
    #      the count AFTER coalescing back into whole cutouts.
    #   2. After welding, the two halves of the straddling opening have to
    #      actually be coalesced into one cutout on the merged panel, so
    #      fabrication sees one hole instead of two.

    def _would_coalesce(a, b, tol=1.0):
        """Physics-based check: if two holes touch at the seam and share an elevation, they are one window."""
        if abs(a.get('y_in', 0.0) - b.get('y_in', 0.0)) > tol: return False
        if abs(a.get('height_in', 0.0) - b.get('height_in', 0.0)) > tol: return False
        
        a_right = a.get('x_in', 0.0) + a.get('width_in', 0.0)
        b_left = b.get('x_in', 0.0)
        return abs(a_right - b_left) <= tol

    def _coalesce_split_cutouts(cutouts, tol=1.5):
        from collections import defaultdict
        if not cutouts: return cutouts
        _groups = defaultdict(list)
        for _c in cutouts:
            # Group by geometry instead of Revit ID
            _grp_key = "geom_{}_{}".format(round(_c.get('y_in', 0.0), 1), round(_c.get('height_in', 0.0), 1))
            _groups[(_grp_key, round(_c.get('y_in', 0.0), 2), round(_c.get('height_in', 0.0), 2))].append(_c)
            
        _out = []
        for _grp in _groups.values():
            if len(_grp) == 1:
                _out.append(_grp[0])
                continue
            _grp.sort(key=lambda c: c.get('x_in', 0.0))
            _merged = [dict(_grp[0])]
            for _c in _grp[1:]:
                _last = _merged[-1]
                _last_right = _last.get('x_in', 0.0) + _last.get('width_in', 0.0)
                if _c.get('x_in', 0.0) <= _last_right + tol:
                    _new_right = max(_last_right, _c.get('x_in', 0.0) + _c.get('width_in', 0.0))
                    _last['width_in'] = _new_right - _last.get('x_in', 0.0)
                    _raw_last_right = _last.get('raw_x_in', 0.0) + _last.get('raw_width_in', 0.0)
                    _raw_new_right  = max(_raw_last_right, _c.get('raw_x_in', 0.0) + _c.get('raw_width_in', 0.0))
                    _last['raw_width_in'] = _raw_new_right - _last.get('raw_x_in', 0.0)
                else:
                    _merged.append(dict(_c))
            _out.extend(_merged)
        
        return _out


    def _effective_cutout_count(curr_cuts, next_cuts_shifted):
        """How many distinct cutouts we'd end up with after welding + coalescing."""
        _total = len(curr_cuts) + len(next_cuts_shifted)
        _used_next = set()
        for _c in curr_cuts:
            for _ni, _n in enumerate(next_cuts_shifted):
                if _ni in _used_next: continue
                if _would_coalesce(_c, _n):
                    _total -= 1
                    _used_next.add(_ni)
                    break
        return _total

    welded_panels = []
    from collections import defaultdict

    bands = defaultdict(list)
    for p in panels:
        bands[(round(p.y, 3), round(p.h, 3))].append(p)

    global_max_w = constraints.long_max if horizontal_mode else constraints.max_width
    spacing = constraints.panel_spacing

    for (y, h), band_panels in bands.items():
        band_panels.sort(key=lambda p: p.x)

        i = 0
        while i < len(band_panels):
            curr_p = band_panels[i]

            j = i + 1
            while j < len(band_panels):
                next_p = band_panels[j]

                # Break if they aren't touching or if they exceed max width
                if abs((curr_p.x + curr_p.w + spacing) - next_p.x) > 0.1:
                    break
                if (curr_p.w + spacing + next_p.w) > global_max_w:
                    break

                # POST-COALESCE cutout count: two panels each holding half of
                # a straddling opening count as ONE cutout on the merged panel,
                # not two.
                curr_cuts = list(getattr(curr_p, 'cutouts', []) or [])
                shift_delta = curr_p.w + spacing
                next_cuts_shifted = []
                for _c in (getattr(next_p, 'cutouts', []) or []):
                    _nc = dict(_c)
                    _nc['x_in']     = _c.get('x_in', 0.0)     + shift_delta
                    _nc['raw_x_in'] = _c.get('raw_x_in', 0.0) + shift_delta
                    next_cuts_shifted.append(_nc)
                if _effective_cutout_count(curr_cuts, next_cuts_shifted) > 2:
                    break

                # Weld.
                old_w = curr_p.w
                curr_p.w = round(curr_p.w + spacing + next_p.w, 3)
                if not getattr(curr_p, 'cutouts', None):
                    curr_p.cutouts = []
                for _c in next_cuts_shifted:
                    curr_p.cutouts.append(_c)
                # Coalesce split cutouts on this panel now that the join is
                # sealed. Any straddling opening becomes one hole again.
                curr_p.cutouts = _coalesce_split_cutouts(curr_p.cutouts)

                j += 1

            welded_panels.append(curr_p)
            i = j


    # --- CONTAINMENT DEDUP ---
    # Only remove a panel that is FULLY contained inside another and whose
    # cutouts are all also covered by the outer panel (matched by opening id).
    # This kills T25-inside-T24 duplicates safely without touching partial
    # overlaps like T23 sticking into T24's territory -- those panels carry
    # different openings and removing either one leaves a coverage gap.
    def _dedup_contained(panel_list):
        from collections import defaultdict
        if not panel_list: return panel_list
        _bands = defaultdict(list)
        for _i, _p in enumerate(panel_list):
            _bands[(round(_p.y, 2), round(_p.h, 2))].append((_i, _p))
        _removed = set()
        _tol = 0.5  # in
        for _band_panels in _bands.values():
            _band_panels.sort(key=lambda ip: ip[1].w, reverse=True)  # widest first
            for _a in range(len(_band_panels)):
                _idx_a, _pa = _band_panels[_a]
                if _idx_a in _removed: continue
                _a_l = _pa.x
                _a_r = _pa.x + _pa.w
                _pa_ids = set(_c.get('id') for _c in (getattr(_pa, 'cutouts', []) or []))
                for _b in range(_a + 1, len(_band_panels)):
                    _idx_b, _pb = _band_panels[_b]
                    if _idx_b in _removed: continue
                    _b_l = _pb.x
                    _b_r = _pb.x + _pb.w
                    if _b_l < _a_l - _tol or _b_r > _a_r + _tol:
                        continue  # not fully contained
                    _pb_ids = set(_c.get('id') for _c in (getattr(_pb, 'cutouts', []) or []))
                    if _pb_ids.issubset(_pa_ids):
                        _removed.add(_idx_b)
        if _removed:
            _diag(Ansi.YELLOW + "  [DEDUP] Removed {} fully-contained duplicate panel(s) "
                  "from minimize-unique output.".format(len(_removed)) + Ansi.RESET)
        return [_p for _i, _p in enumerate(panel_list) if _i not in _removed]

    welded_panels = _dedup_contained(welded_panels)

    # ---- GAP DIAGNOSTIC ----
    # Scan each (y, h) band and report any uncovered wall segment wider than
    # tolerance. If gaps show up here, that's the smoking gun: some panel
    # placement step left a real strip of wall uncovered. The output is
    # exact x-ranges so we can trace it back to the responsible step.
    def _report_gaps(panel_list, wall_w):
        from collections import defaultdict
        _bands = defaultdict(list)
        for _p in panel_list:
            _bands[(round(_p.y, 2), round(_p.h, 2))].append(_p)
        _gap_tol = spacing + 0.05  # tolerance = spacing plus float noise
        for (y_key, h_key), band in _bands.items():
            band.sort(key=lambda p: p.x)
            _cursor = 0.0
            _gaps = []
            for _p in band:
                if _p.x > _cursor + _gap_tol:
                    _gaps.append((round(_cursor, 2), round(_p.x, 2),
                                  round(_p.x - _cursor, 2)))
                _cursor = max(_cursor, _p.x + _p.w)
            if _cursor + _gap_tol < wall_w:
                _gaps.append((round(_cursor, 2), round(wall_w, 2),
                              round(wall_w - _cursor, 2)))
            if _gaps:
                _diag(Ansi.RED + "  [GAP] band y={:.2f} h={:.2f}:".format(y_key, h_key) + Ansi.RESET)
                for _s, _e, _w in _gaps:
                    _diag(Ansi.RED + "        [{:.2f} .. {:.2f}]  ({:.2f}\" wide)".format(_s, _e, _w) + Ansi.RESET)

    _report_gaps(welded_panels, wall_width)

    welded_panels.sort(key=lambda p: (p.y, p.x))
    for idx, p in enumerate(welded_panels, start=1): 
        p.name = "P{:02d}".format(idx)
        


    # ==========================================
    # 2. PASTE THE AUDITOR DEFINITION HERE
    # ==========================================
    def _verify_facade_integrity(panels, constraints):
        """Sweeps the final layout to guarantee zero gaps and zero overlaps."""
        from collections import defaultdict
        bands = defaultdict(list)
        for p in panels:
            bands[(round(p.y, 2), round(p.h, 2))].append(p)
            
        spacing = constraints.panel_spacing
        global_max_w = constraints.long_max if str(orientation).lower() == 'horizontal' else constraints.max_width
        errors = 0
        
        for (y, h), band_panels in bands.items():
            band_panels.sort(key=lambda p: p.x)
            for i in range(len(band_panels)):
                p = band_panels[i]
                if p.w < constraints.min_width - 0.1:
                    print(Ansi.RED + "  [AUDIT FAIL] {} width ({:.2f}\") is UNDER min limit.".format(p.name, p.w) + Ansi.RESET)
                    errors += 1
                if p.w > global_max_w + 0.1:
                    print(Ansi.RED + "  [AUDIT FAIL] {} width ({:.2f}\") is OVER max limit.".format(p.name, p.w) + Ansi.RESET)
                    errors += 1
                if i < len(band_panels) - 1:
                    next_p = band_panels[i+1]
                    delta = next_p.x - (p.x + p.w + spacing)
                    if delta > 0.1:
                        print(Ansi.RED + "  [AUDIT FAIL] GAP DETECTED! {:.2f}\" gap between {} and {}.".format(delta, p.name, next_p.name) + Ansi.RESET)
                        errors += 1
                    elif delta < -0.1:
                        print(Ansi.RED + "  [AUDIT FAIL] OVERLAP DETECTED! {:.2f}\" collision between {} and {}.".format(abs(delta), p.name, next_p.name) + Ansi.RESET)
                        errors += 1
        if errors == 0:
            _diag(Ansi.GREEN + "  [AUDIT PASS] Facade geometry is perfectly sealed." + Ansi.RESET)

    # ==========================================
    # 3. CALL THEM BOTH BEFORE RETURNING
    # ==========================================
    _verify_facade_integrity(welded_panels, constraints)
    
    return welded_panels
    


        
def _rebuild_aligned_columns(records, ext_openings, eff_w, constraints):
    """Re-tile solid piers and storefront spandrels so stacked courses share
    joints, WITHOUT slicing existing panels (which bred junk widths before).

    Partition the wall in x into solid piers and blocker spans. Solid piers are
    re-tiled once (equal division <= max_width) and that single division is used
    for EVERY course in the pier -> joints stack. A blocker span is re-tiled only
    for the courses above the storefront's head (the spandrel); the glazed part
    gets no panel. Because piers/storefronts of equal width divide identically,
    the type count stays about the same. Segments containing a regular window are
    left untouched (kept as originally placed) so cutouts are preserved."""
    import math
    sp = float(getattr(constraints, "panel_spacing", 0.0) or 0.0)
    maxw = constraints.max_width
    minw = constraints.min_width

    blockers = []
    for o in ext_openings:
        try:
            req = o.w + o.original_clearances.jamb_min * 2.0
        except Exception:
            req = o.w
        if req > maxw:
            blockers.append((max(0.0, o.x - sp), o.x + o.w + sp, o.y + o.h))
    if not blockers:
        return records
    blockers.sort()

    # regular (non-blocking) openings -> segments overlapping these are left as-is
    reg = []
    for o in ext_openings:
        try:
            req = o.w + o.original_clearances.jamb_min * 2.0
        except Exception:
            req = o.w
        if req <= maxw:
            reg.append((o.left_clearance_zone, o.right_clearance_zone))

    def divide(width):
        n = max(1, int(math.ceil((width + sp) / (maxw + sp))))
        pw = (width - (n - 1) * sp) / float(n)
        return [(i * (pw + sp), pw) for i in range(n)]

    # courses present on solid full-height piers = distinct (y,h) of solid records
    courses = sorted(set((round(r["y_in"], 3), round(r["height_in"], 3))
                         for r in records if not r["cutouts_json"]))
    if not courses:
        return records
    tmpl = records[0]

    def mk(x, y, w, h):
        r = dict(tmpl)
        r["x_in"] = round(x, 4); r["y_in"] = round(y, 4)
        r["width_in"] = round(w, 4); r["height_in"] = round(h, 4)
        r["x_ref"] = round(x, 4); r["area_in2"] = round(w * h, 4)
        r["panel_type"] = "{}x{}".format(round(w, 4), round(h, 4))
        r["cutouts_json"] = ""
        return r

    # x-partition
    bnds = sorted(set([0.0, round(eff_w, 3)]
                      + [b[0] for b in blockers] + [b[1] for b in blockers]))
    new = []
    kept_ranges = []
    for i in range(len(bnds) - 1):
        a, b = bnds[i], bnds[i + 1]
        w = b - a
        if w < minw:
            continue
        # segment overlaps a regular window? -> keep original records here
        if any(not (rr <= a + 0.5 or rl >= b - 0.5) for (rl, rr) in reg):
            kept_ranges.append((a, b))
            continue
        blk = None
        for bl in blockers:
            if bl[0] - 0.5 <= a and b <= bl[1] + 0.5:
                blk = bl; break
        div = divide(w)
        for (cy, ch) in courses:
            if blk is not None and (cy + ch * 0.5) < blk[2] - 1.0:
                continue  # this course is behind the glazing -> no panel
            for (off, pw) in div:
                new.append(mk(a + off, cy, pw, ch))

    # keep original records that fall in a window-bearing segment
    for r in records:
        cx = r["x_in"] + r["width_in"] * 0.5
        if any(a <= cx <= b for (a, b) in kept_ranges):
            new.append(r)
    return new


def _align_courses_to_piers(records, ext_openings, eff_w, constraints):
    """Re-tile stacked courses so joints stack over solid piers.

    A wall taller than one panel is placed as separate elevation courses that
    tile their widths independently, so the course above a pier lands on a
    different module than the pier below -> staggered joints. This pass re-tiles
    each course region-by-region using ONE canonical division per span, so the
    same pier width divides the same way in every course (joints stack). Spans
    over storefront glazing are tiled on their own. A span that contains a real
    window (regular opening) is left exactly as placed, so cutouts are never
    disturbed. Walls with no blocking openings return unchanged."""
    import math
    sp = float(getattr(constraints, "panel_spacing", 0.0) or 0.0)
    maxw = constraints.max_width
    minw = constraints.min_width
    if not records:
        return records

    # blocker x-spans (storefronts / wide doors), merged
    bl = []
    for o in ext_openings:
        try:
            req = o.w + o.original_clearances.jamb_min * 2.0
        except Exception:
            req = o.w
        if req > maxw:
            bl.append((o.x - sp, o.x + o.w + sp))
    if not bl:
        return records
    bl.sort()
    merged = []
    for l, r in bl:
        if merged and l <= merged[-1][1] + 0.5:
            merged[-1] = (merged[-1][0], max(merged[-1][1], r))
        else:
            merged.append([l, r])
    bl = merged

    # pier edges = every blocker boundary (these are the shared joint lines)
    pier_edges = sorted(set([round(e, 3) for span in bl for e in span]))

    # regular (non-blocking) opening x-spans -> spans we must NOT re-tile
    reg_spans = []
    for o in ext_openings:
        try:
            req = o.w + o.original_clearances.jamb_min * 2.0
        except Exception:
            req = o.w
        if req <= maxw:
            reg_spans.append((o.left_clearance_zone, o.right_clearance_zone))

    def has_window(a, b):
        return any(not (rr <= a + 0.5 or rl >= b - 0.5) for (rl, rr) in reg_spans)

    def divide(a, b):
        W = b - a
        n = max(1, int(math.ceil((W + sp) / (maxw + sp))))
        pw = (W - (n - 1) * sp) / float(n)
        return [(round(a + i * (pw + sp), 4), round(pw, 4)) for i in range(n)]

    def mk(tmpl, x, y, w, h, wid):
        r = dict(tmpl)
        r.update(x_in=round(x, 4), y_in=round(y, 4), width_in=round(w, 4),
                 height_in=round(h, 4), x_ref=round(x, 4),
                 area_in2=round(w * h, 4),
                 panel_type="{}x{}".format(round(w, 4), round(h, 4)),
                 cutouts_json="", wall_id=wid)
        return r

    # group into courses (same y, same height)
    courses = {}
    for r in records:
        courses.setdefault((round(r["y_in"], 2), round(r["height_in"], 2)), []).append(r)

    out = []
    for (y, h), panels in courses.items():
        wid = panels[0]["wall_id"]
        # contiguous solid runs this course actually covers (respecting glazing gaps)
        segs = sorted((p["x_in"], p["x_in"] + p["width_in"]) for p in panels)
        runs = []
        for x0, x1 in segs:
            if runs and x0 <= runs[-1][1] + sp + 0.6:
                runs[-1][1] = max(runs[-1][1], x1)
            else:
                runs.append([x0, x1])
        for rx0, rx1 in runs:
            # break each run at pier edges that fall inside it
            pts = [rx0, rx1] + [e for e in pier_edges if rx0 + 0.6 < e < rx1 - 0.6]
            pts = sorted(set(round(p, 3) for p in pts))
            for i in range(len(pts) - 1):
                a, b = pts[i], pts[i + 1]
                if b - a < minw - 0.01:
                    continue
                if has_window(a, b):
                    # keep original panels overlapping this span untouched
                    for p in panels:
                        if p["x_in"] >= a - 0.6 and (p["x_in"] + p["width_in"]) <= b + 0.6:
                            out.append(p)
                else:
                    for (ox, ow) in divide(a, b):
                        out.append(mk(panels[0], ox, y, ow, h, wid))
    return out

def _plumb_align_vertical_seams(records, openings, constraints, tol=4.0):
    """
    Scans all panel courses on the wall. If vertical seams (joints) in different 
    courses are staggered by a small tolerance, snaps them to a single plumb line 
    based on doors or foundation seams.
    """
    import json
    spacing = float(getattr(constraints, "panel_spacing", 0.125))
    min_w = float(constraints.min_width)
    max_w_absolute = float(constraints.long_max) # Absolute physical board limit
    
    from collections import defaultdict
    courses = defaultdict(list)
    for r in records:
        courses[(round(r["y_in"], 2), round(r["height_in"], 2))].append(r)
        
    seams = []
    for (y, h), course_recs in courses.items():
        course_recs.sort(key=lambda r: r["x_in"])
        for i in range(len(course_recs) - 1):
            left_r = course_recs[i]
            right_r = course_recs[i+1]
            if abs(right_r["x_in"] - (left_r["x_in"] + left_r["width_in"] + spacing)) < 0.5:
                seam_x = left_r["x_in"] + left_r["width_in"]
                seams.append((seam_x, left_r, right_r, y, h))
                
    if not seams: return records
    
    seams.sort(key=lambda s: s[0])
    groups = []
    curr_group = [seams[0]]
    for s in seams[1:]:
        if s[0] - curr_group[-1][0] <= tol:
            curr_group.append(s)
        else:
            groups.append(curr_group)
            curr_group = [s]
    if curr_group: groups.append(curr_group)
    
    shifts = 0
    for group in groups:
        if len(group) < 2: continue
        
        xs = [s[0] for s in group]
        if max(xs) - min(xs) < 0.1: continue 
        
        # --- THE MASTER SEAM SELECTOR ---
        canon_x = None
        for sx, left_r, right_r, cy, ch in group:
            cuts_str = (str(left_r.get("cutouts_json", "")) + str(right_r.get("cutouts_json", ""))).lower()
            if "door" in cuts_str or "storefront" in cuts_str:
                canon_x = sx
                break
                
        if canon_x is None:
            lowest_course = min(group, key=lambda item: item[3])
            canon_x = lowest_course[0]
            
        safe = True
        for sx, left_r, right_r, cy, ch in group:
            delta = canon_x - sx
            new_l_w = left_r["width_in"] + delta
            new_r_w = right_r["width_in"] - delta
            
            # --- BYPASS STRICT VALIDITY CHECK FOR ALREADY PLACED PANELS ---
            # We only verify the new width is physically possible (min_w to long_max).
            # Height (parapets, etc) was already validated during placement.
            if not (min_w <= new_l_w <= max_w_absolute + 0.1): safe = False; break
            if not (min_w <= new_r_w <= max_w_absolute + 0.1): safe = False; break
            
            # Jamb protector
            for o in openings:
                if getattr(o, 'left_clearance_zone', None) is None: continue
                if o.y + o.h <= cy or o.y >= cy + ch: continue
                if o.left_clearance_zone + 0.1 < canon_x < o.right_clearance_zone - 0.1:
                    safe = False; break
            if not safe: break
            
        if safe:
            for sx, left_r, right_r, cy, ch in group:
                delta = canon_x - sx
                if abs(delta) < 0.01: continue
                
                left_r["width_in"] = round(left_r["width_in"] + delta, 3)
                left_r["area_in2"] = round(left_r["width_in"] * left_r["height_in"], 3)
                left_r["panel_type"] = "{}x{}".format(left_r["width_in"], left_r["height_in"])
                
                right_r["x_in"] = round(right_r["x_in"] + delta, 3)
                right_r["x_ref"] = right_r["x_in"]
                right_r["width_in"] = round(right_r["width_in"] - delta, 3)
                right_r["area_in2"] = round(right_r["width_in"] * right_r["height_in"], 3)
                right_r["panel_type"] = "{}x{}".format(right_r["width_in"], right_r["height_in"])
                
                lp = Panel(left_r["x_in"], left_r["y_in"], left_r["width_in"], left_r["height_in"])
                rp = Panel(right_r["x_in"], right_r["y_in"], right_r["width_in"], right_r["height_in"])
                
                lp.cutouts = calculate_panel_cutouts(lp, openings)
                rp.cutouts = calculate_panel_cutouts(rp, openings)
                
                left_r["cutouts_json"] = json.dumps(lp.cutouts) if lp.cutouts else ""
                right_r["cutouts_json"] = json.dumps(rp.cutouts) if rp.cutouts else ""
                
                shifts += 1
                
    if shifts > 0:
        print(Ansi.CYAN + "  [PLUMB] Vertically aligned {} staggered seam(s) across courses (tol={}\").".format(shifts, tol) + Ansi.RESET)
        
    return records


def process_all_walls(walls_rows, openings_rows, output_dir,
                      door_clearances, window_clearances, storefront_clearances,
                      config=None, orientation="vertical", output_filename="optimized_panel_placement.csv",
                      force_mode=None):
    print("PANEL_CALCULATOR_FILE: " + __file__)
    global ACTIVE_CONFIG, LAST_RUN_STATS
    if config is not None: ACTIVE_CONFIG = config
    elif ACTIVE_CONFIG is None:
        presets = get_preset_configs()
        ACTIVE_CONFIG = presets.get(orientation, presets["vertical"])

    if force_mode is not None:
        _s = ACTIVE_CONFIG.optimization_strategy
        _s.best_to_manufacture    = False
        _s.minimize_unique_panels = (force_mode == "nu")
        _s.use_ga_optimizer       = (force_mode == "npnu")
        
    # --- NEW: LOAD WALL MAPPING FOR FACADES ---
    wall_map = {}
    mapping_csv = os.path.join(output_dir, "wall_mapping.csv")
    if os.path.exists(mapping_csv):
        for r in read_csv_rows(mapping_csv):
            c_id = str(r.get("CombinedWallId", "")).strip()
            o_id = str(r.get("OriginalWallId", "")).strip()
            if c_id and o_id:
                wall_map.setdefault(c_id, set()).add(o_id)

    all_panel_records = []
    processed_endpoints = [] 
    _process_wall_errors = []

    # --- FIX (ISSUE 3): PANEL LIBRARY TOLERANCE OVERRIDE ---
    # The LIVE panel set: one library for the whole building.
    # Overriding the merge limits to aggressively merge floating point drift
    # and mirrored twin types, allowing Min Unique to correctly use pattern panels.
    panel_library = PanelLibrary(
        match_tol=0.125, # Give it 1/8" breathing room for standard float drift
        merge_tol=0.25   # Force it to merge physical mirrors with up to 1/4" variation
    )
    
    # Sort walls to process connected facades sequentially
    walls_rows.sort(key=lambda w: (w.get("FacadeId", ""), safe_float(w.get("wall_origin_x")), safe_float(w.get("wall_origin_y"))))

    # --- BUTT CORNER RESOLVER ---
    # Pre-pass, run once before any wall is placed. For every pair of walls
    # that meet at a true 90-degree corner, decide ONE winner (full length,
    # runs through the corner) and ONE loser (recedes by the WINNER's panel
    # thickness). Without this, the per-wall loop below independently
    # shortens EACH wall by its OWN thickness at every 90-degree neighbor,
    # so neither wall ever runs through -- leaving a square notch at the
    # corner (both walls receding) instead of a clean butt joint.
    #
    # Winner selection: a corner's choice affects each wall's resulting
    # effective width, which is the dominant driver of the panel widths it
    # will generate. As a proxy for "produces fewer unique panel types"
    # without re-running full placement for every corner combination, we
    # pick whichever assignment leaves both walls' resulting widths closer
    # to widths already used elsewhere in the building. Ties fall back to
    # the longer wall winning (the shorter run is more disposable to alter).
    def _resolve_butt_corners(_walls_rows):
        geoms = []
        for _w in _walls_rows:
            _wid = get_wall_id(_w)
            _s = _parse_xyz(_w.get("Start(X,Y,Z)", ""))
            _e = _parse_xyz(_w.get("End(X,Y,Z)", ""))
            if not (_s and _e):
                continue
            _dx, _dy, _dz = _e[0]-_s[0], _e[1]-_s[1], _e[2]-_s[2]
            _mag = math.sqrt(_dx*_dx + _dy*_dy + _dz*_dz)
            if _mag < 1e-9:
                continue
            _dims = get_wall_dimensions(_w)
            _width = _dims[0] if _dims else 0.0
            _thick = safe_float(_w.get("panel_total_thickness_in"), 6.625)
            geoms.append({"id": _wid, "start": _s, "end": _e,
                          "dir": (_dx/_mag, _dy/_mag, _dz/_mag),
                          "width": _width, "thick": _thick})

        _all_widths = [g["width"] for g in geoms]

        def _width_match_score(_w):
            return sum(1 for _ow in _all_widths if abs(_ow - _w) < 0.5)

        seen_pairs = set()
        ext_lookup = {}

        for _i, g in enumerate(geoms):
            for h in geoms[_i + 1:]:
                pair_key = tuple(sorted([g["id"], h["id"]]))
                if pair_key in seen_pairs:
                    continue
                for g_end, g_pt in (("start", g["start"]), ("end", g["end"])):
                    for h_end, h_pt in (("start", h["start"]), ("end", h["end"])):
                        d = math.sqrt(sum((g_pt[k] - h_pt[k]) ** 2 for k in range(3)))
                        if d >= 1.0:
                            continue
                        dot = sum(g["dir"][k] * h["dir"][k] for k in range(3))
                        if abs(dot) >= 0.5:
                            continue  # not a true 90-degree corner

                        seen_pairs.add(pair_key)

                        # Candidate A: g wins, h recedes by g's thickness
                        scoreA = (_width_match_score(g["width"]) +
                                  _width_match_score(h["width"] - g["thick"]))
                        # Candidate B: h wins, g recedes by h's thickness
                        scoreB = (_width_match_score(h["width"]) +
                                  _width_match_score(g["width"] - h["thick"]))

                        g_wins = scoreA > scoreB or (scoreA == scoreB and g["width"] >= h["width"])

                        if g_wins:
                            ext_lookup[(g["id"], g_end)] = 0.0
                            ext_lookup[(h["id"], h_end)] = -g["thick"]
                        else:
                            ext_lookup[(h["id"], h_end)] = 0.0
                            ext_lookup[(g["id"], g_end)] = -h["thick"]
        return ext_lookup

    _corner_ext_lookup = _resolve_butt_corners(walls_rows)

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

        # --- FIX (ISSUE 1): CALCULATE WALL GEOMETRY FIRST ---
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

        # --- FIX (ISSUE 1): PASS GEOMETRY TO OPENING HANDLER ---
        openings = get_wall_openings(
            wall_id, openings_rows,
            active_door_cl, active_window_cl, active_storefront_cl,
            active_wall_opening_cl,
            wall_base_z_ft=wall_base_z,
            wall_map=wall_map,
            wall_geom=_wall_geom  # <-- Passed to project local coords onto the facade vector
        )
        # ---- [DIAG] opening pipeline stage 1: routing ----
        _wm_hosts = sorted(wall_map.get(str(wall_id), set())) if wall_map else []
        _diag(Ansi.CYAN + "  [DIAG wall={}] stage1 get_wall_openings: {} opening(s)  "
              "wall_map_hosts={}".format(wall_id, len(openings), _wm_hosts or "(self only)") + Ansi.RESET)
        if openings:
            for _op in openings[:6]:
                _diag(Ansi.CYAN + "    op id={} type={} x={:.1f} y={:.1f} w={:.1f} h={:.1f}"
                      .format(_op.id, getattr(_op, 'type', '?'), _op.x, _op.y, _op.w, _op.h) + Ansi.RESET)
            if len(openings) > 6:
                _diag(Ansi.CYAN + "    ... {} more".format(len(openings) - 6) + Ansi.RESET)


        # --- SEAM DETECTOR (True Corners & Colinear Facades) ---
        _p_start_ext = 0.0
        _p_end_ext = 0.0
        
        try: _spacing = ACTIVE_CONFIG.panel_constraints.panel_spacing
        except: _spacing = 0.125

        if _start_xyz and _end_xyz:
            # Omni-Scan: Check this wall against all other walls in the dataset
            for other_w in walls_rows:
                if other_w == wall_row: continue
                o_s = _parse_xyz(other_w.get("Start(X,Y,Z)", ""))
                o_e = _parse_xyz(other_w.get("End(X,Y,Z)", ""))
                if not (o_s and o_e): continue
                
                # Calculate the neighboring wall's vector
                dx, dy, dz = o_e[0]-o_s[0], o_e[1]-o_s[1], o_e[2]-o_s[2]
                mag = math.sqrt(dx**2 + dy**2 + dz**2)
                if mag == 0: continue
                odir = (dx/mag, dy/mag, dz/mag)
                
                # Check Start Point Interactions
                if math.sqrt((_start_xyz[0]-o_s[0])**2 + (_start_xyz[1]-o_s[1])**2 + (_start_xyz[2]-o_s[2])**2) < 1.0 or \
                   math.sqrt((_start_xyz[0]-o_e[0])**2 + (_start_xyz[1]-o_e[1])**2 + (_start_xyz[2]-o_e[2])**2) < 1.0:
                    dot = _wall_geom["wall_dir_x"]*odir[0] + _wall_geom["wall_dir_y"]*odir[1] + _wall_geom["wall_dir_z"]*odir[2]
                    if abs(dot) < 0.5:
                        # 90-degree corner: use the pre-resolved winner/loser
                        # assignment so only ONE wall recedes (by the OTHER's
                        # thickness) instead of both walls shortening
                        # themselves by their own thickness.
                        _p_start_ext = _corner_ext_lookup.get((wall_id, "start"), -panel_thick_in)
                    elif abs(dot) > 0.8:
                        _p_start_ext = -(_spacing / 2.0) # It's a flat colinear facade connection
                
                # Check End Point Interactions
                if math.sqrt((_end_xyz[0]-o_s[0])**2 + (_end_xyz[1]-o_s[1])**2 + (_end_xyz[2]-o_s[2])**2) < 1.0 or \
                   math.sqrt((_end_xyz[0]-o_e[0])**2 + (_end_xyz[1]-o_e[1])**2 + (_end_xyz[2]-o_e[2])**2) < 1.0:
                    dot = _wall_geom["wall_dir_x"]*odir[0] + _wall_geom["wall_dir_y"]*odir[1] + _wall_geom["wall_dir_z"]*odir[2]
                    if abs(dot) < 0.5:
                        _p_end_ext = _corner_ext_lookup.get((wall_id, "end"), -panel_thick_in)
                    elif abs(dot) > 0.8:
                        _p_end_ext = -(_spacing / 2.0) # It's a flat colinear facade connection


        _eff_wall_w = wall_width + _p_start_ext + _p_end_ext
        import copy as _cp_ext
        _ext_openings = _cp_ext.deepcopy(openings)
        for _eo in _ext_openings: _eo.x += _p_start_ext
        # ---- [DIAG] opening pipeline stage 2: corner-extension shift ----
        if _p_start_ext != 0.0 or _p_end_ext != 0.0:
            _oob_ct = sum(1 for _o in _ext_openings
                          if _o.x + _o.w <= 0 or _o.x >= _eff_wall_w)
            _diag(Ansi.CYAN + "  [DIAG wall={}] stage2 corner shift: "
                  "start_ext={:.2f} end_ext={:.2f} eff_w={:.2f} "
                  "openings out-of-wall={}".format(
                      wall_id, _p_start_ext, _p_end_ext, _eff_wall_w, _oob_ct) + Ansi.RESET)

        # Elevation bands and process_wall loop
        _wall_h_in, _base_z_in, _max_ph_in = wall_height, wall_base_z * 12.0, ACTIVE_CONFIG.panel_constraints.max_height
        try: _lvl_abs_in = json.loads(wall_row.get("LevelElevations(in)", "[]"))
        except: _lvl_abs_in = []
        _rel_elevs = sorted({round(e - _base_z_in, 2) for e in _lvl_abs_in if 6.0 < (e - _base_z_in) < (_wall_h_in - 6.0)})

        if getattr(ACTIVE_CONFIG.optimization_strategy, 'limit_panel_height_to_floor', False):
            _pc = ACTIVE_CONFIG.panel_constraints
            _max_band = _pc.short_max if str(orientation).lower() == 'horizontal' else _pc.long_max
            _parapet = float(getattr(ACTIVE_CONFIG.optimization_strategy,
                                     'flexible_top_panel_allowance_in', 0.0) or 0.0)
            _bands = _floor_bands(_wall_h_in, _rel_elevs, _pc.min_height, _max_band, _parapet)
        else:
            _bands = _compute_elevation_bands(_wall_h_in, _rel_elevs, _max_ph_in)

        panel_records = []
        _n_bands = len(_bands)
        _band_gap_sp = float(getattr(ACTIVE_CONFIG.panel_constraints, "panel_spacing", 0.0) or 0.0)
        for _bidx, (_y0, _y1) in enumerate(_bands):
            _band_h_in = _y1 - _y0
            # Leave a panel_spacing reveal ABOVE every band except the last,
            # so stacked floor courses get a real vertical gap instead of
            # butting together with 0" between them.
            if _bidx < _n_bands - 1:
                _band_h_in = round(_band_h_in - _band_gap_sp, 4)
            _band_ops  = _clip_openings_to_band(_ext_openings, _y0, _y0 + _band_h_in)
            # ---- [DIAG] opening pipeline stage 3: band clipping ----
            _diag(Ansi.CYAN + "  [DIAG wall={}] stage3 band y=[{:.1f}, {:.1f}]: "
                  "{} opening(s) survive clip".format(
                      wall_id, _y0, _y1, len(_band_ops)) + Ansi.RESET)
            
            try:
                _band_recs = process_wall(wall_id, _eff_wall_w, _band_h_in, _band_ops)
            except Exception as _e:
                import traceback as _tb_mod
                _tb_text = _tb_mod.format_exc()
                print(Ansi.RED + "=" * 72 + Ansi.RESET)
                print(Ansi.RED + "  [ERROR] process_wall failed"
                      "  wall={}  band_y=[{:.2f}, {:.2f}]  wall_w={:.2f}\""
                      .format(wall_id, _y0, _y1, _eff_wall_w) + Ansi.RESET)
                print(Ansi.RED + "  {}: {}".format(type(_e).__name__, _e) + Ansi.RESET)
                print(Ansi.RED + _tb_text + Ansi.RESET)
                print(Ansi.RED + "=" * 72 + Ansi.RESET)
                _process_wall_errors.append(
                    (wall_id, _y0, _y1, type(_e).__name__, str(_e)))
                _band_recs = []
            # ---- [DIAG] opening pipeline stage 4: cutouts written per band ----
            _with_cut = sum(1 for _r in _band_recs if _r.get("cutouts_json"))
            _diag(Ansi.CYAN + "  [DIAG wall={}] stage4 band y=[{:.1f}, {:.1f}]: "
                  "{} panel(s) placed, {} with cutouts".format(
                      wall_id, _y0, _y1, len(_band_recs), _with_cut) + Ansi.RESET)
            for _r in _band_recs: _r["y_in"] = round(_r["y_in"] + _y0, 4)
            panel_records.extend(_band_recs)

        # ---- [DIAG] opening pipeline stage 5: pier alignment + plumb seam align ----
        # NOTE: previously gated behind `if str(orientation).lower() != 'horizontal':`
        # which silently skipped this ENTIRE block (both _align_courses_to_piers and
        # _plumb_align_vertical_seams) whenever orientation was 'horizontal'. Vertical
        # seam misalignment across floor courses happens in horizontal-coursed
        # facades too, so this now always runs regardless of orientation.
        _pre_cut = sum(1 for _r in panel_records if _r.get("cutouts_json"))

        panel_records = _align_courses_to_piers(
            panel_records, _ext_openings, _eff_wall_w, ACTIVE_CONFIG.panel_constraints)

        # --- THE PLUMB ALIGNER ---
        panel_records = _plumb_align_vertical_seams(
            panel_records, _ext_openings, ACTIVE_CONFIG.panel_constraints, tol=4.0)

        _post_cut = sum(1 for _r in panel_records if _r.get("cutouts_json"))
        _delta = _post_cut - _pre_cut
        _tag = "OK" if _delta == 0 else ("LOST {}".format(-_delta) if _delta < 0 else "GAINED {}".format(_delta))
        _color = Ansi.CYAN if _delta == 0 else Ansi.RED
        _diag(_color + "  [DIAG wall={}] stage5 align & plumb: "
              "cutouts before={} after={} ({})".format(
                  wall_id, _pre_cut, _post_cut, _tag) + Ansi.RESET)

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

        # --- LIVE PANEL SET ---
        _types_before = panel_library.unique_count()
        _reused_here = 0
        _cut_lost_to_lib = 0
        for _pr in panel_records:
            _had_cut = bool(_pr.get("cutouts_json"))
            _label, _was_reused = panel_library.register(_pr)
            _has_cut_now = bool(_pr.get("cutouts_json"))
            if _had_cut and not _has_cut_now:
                _cut_lost_to_lib += 1
            if _was_reused:
                _reused_here += 1
        _added_here = panel_library.unique_count() - _types_before
        _diag(Ansi.GREEN + "  [PANEL SET] Wall {}: {} reused / {} new type(s)"
              .format(wall_id, _reused_here, _added_here) + Ansi.RESET)
        if _cut_lost_to_lib > 0:
            _diag(Ansi.RED + "  [DIAG wall={}] stage6 panel_library.register STRIPPED "
                  "cutouts from {} panel(s)".format(wall_id, _cut_lost_to_lib) + Ansi.RESET)

        all_panel_records.extend(panel_records)

    # ---- ERROR SUMMARY ----
    if _process_wall_errors:
        print(Ansi.RED + "\n" + "=" * 72 + Ansi.RESET)
        print(Ansi.RED + "  [SUMMARY] process_wall failed on {} band(s):"
              .format(len(_process_wall_errors)) + Ansi.RESET)
        for _wid, _y0, _y1, _etype, _emsg in _process_wall_errors:
            print(Ansi.RED + "    wall={}  band=[{:.2f}, {:.2f}]  {}: {}"
                  .format(_wid, _y0, _y1, _etype, _emsg) + Ansi.RESET)
        print(Ansi.RED + "=" * 72 + Ansi.RESET)

    if not all_panel_records: return None, None

    panel_library.report()
    
    panels_path = write_csv(os.path.join(output_dir, output_filename), all_panel_records, [
        "panel_name", "panel_type", "wall_id", "x_in", "y_in", "width_in", "height_in", "area_in2", 
        "rotation_deg", "x_ref", "cutouts_json", "wall_origin_x", "wall_origin_y", "wall_origin_z", 
        "wall_dir_x", "wall_dir_y", "wall_dir_z", "wall_normal_x", "wall_normal_y", "wall_normal_z"
    ])
    
    if panels_path and ACTIVE_CONFIG:
        config_path = os.path.join(output_dir, "config_used.json")
        try: ACTIVE_CONFIG.save(config_path)
        except: pass

    LAST_RUN_STATS = {"np": len(all_panel_records), "nu": panel_library.unique_count()}
    return panels_path, config_path

def optimize_building(walls_rows, openings_rows, output_dir,
                      door_clearances, window_clearances, storefront_clearances,
                      config=None, orientation="vertical",
                      output_filename="optimized_panel_placement.csv"):
    """
    Top-level entry point. If the strategy is Tournament, runs ALL THREE base
    strategies across the WHOLE building, scores each by (np_weight*np +
    nu_weight*nu) using the live PanelLibrary's true building-wide unique count,
    and promotes the winner. Otherwise it's a single pass (identical to calling
    process_all_walls directly). Returns (panels_path, config_path).
    """
    global ACTIVE_CONFIG, LAST_RUN_STATS
    if config is not None: ACTIVE_CONFIG = config

    strat = ACTIVE_CONFIG.optimization_strategy if ACTIVE_CONFIG else None

    # ---- shared runner: execute candidate modes building-wide, keep the best ----
    # Each candidate is forced via process_all_walls(force_mode=...), and its
    # true building-wide (np, nu) come from the live PanelLibrary via LAST_RUN_STATS.


    
    def _run_and_promote(candidates, pick_key, selected_flags, banner):
        import copy as _copy, os as _os, shutil as _shutil
        print(Ansi.MAGENTA + banner + Ansi.RESET)
        runs = []
        for mode, label in candidates:
            tmp = "_cand_{0}_{1}".format(mode, output_filename)
            p, _c = process_all_walls(
                walls_rows, _copy.deepcopy(openings_rows), output_dir,
                door_clearances, window_clearances, storefront_clearances,
                config=ACTIVE_CONFIG, orientation=orientation,
                output_filename=tmp, force_mode=mode)
            runs.append({"mode": mode, "label": label,
                         "nu": LAST_RUN_STATS.get("nu", 0),
                         "np": LAST_RUN_STATS.get("np", 0),
                         "path": p})
            print(Ansi.CYAN + "    {0}: np={1}  nu={2}"
                  .format(label.ljust(20), runs[-1]["np"], runs[-1]["nu"]) + Ansi.RESET)

        runs.sort(key=pick_key)
        win = runs[0]
        print(Ansi.GREEN + "  [WINNER] {0}  (np={1}, nu={2})"
              .format(win["label"], win["np"], win["nu"]) + Ansi.RESET)

        # Promote the winner's CSV to the real output filename.
        final_path = _os.path.join(output_dir, output_filename)
        try:
            if win["path"] and _os.path.exists(win["path"]):
                _shutil.copyfile(win["path"], final_path)
        except Exception:
            final_path = win["path"]

        # config_used.json reflects the user's SELECTED strategy, not the
        # internally-forced winner mode (which is an implementation detail).
        s = ACTIVE_CONFIG.optimization_strategy
        s.best_to_manufacture    = selected_flags.get("best_to_manufacture", False)
        s.minimize_unique_panels = selected_flags.get("minimize_unique_panels", False)
        s.use_ga_optimizer       = selected_flags.get("use_ga_optimizer", False)
        config_path = _os.path.join(output_dir, "config_used.json")
        try: ACTIVE_CONFIG.save(config_path)
        except Exception: pass

        for r in runs:
            try:
                if r["path"] and r["path"] != final_path and _os.path.exists(r["path"]):
                    _os.remove(r["path"])
            except Exception:
                pass
        return final_path, config_path

    # ---- Tournament: all three, weighted, building-wide ----
    if strat and getattr(strat, 'best_to_manufacture', False):
        np_w = getattr(strat, 'np_weight', 1.0)
        nu_w = getattr(strat, 'nu_weight', getattr(strat, 'unique_weight', 10.0))
        return _run_and_promote(
            [("np", "Minimize Total"), ("nu", "Minimize Unique"), ("npnu", "Min Total + Unique")],
            pick_key=lambda r: (np_w * r["np"] + nu_w * r["nu"], r["nu"], r["np"]),
            selected_flags={"best_to_manufacture": True},
            banner="  [TOURNAMENT] Running all 3 strategies building-wide "
                   "(np_w={0:g}, nu_w={1:g})...".format(np_w, nu_w))

    # ---- Minimize Unique: HARD GUARANTEE it never regresses vs Min-Total ----
    # Run both building-wide and keep whichever has fewer UNIQUE types; on a tie,
    # fewer total panels wins (that is Min-Total). So Min-Unique can only ever
    # match or beat Min-Total on unique count -- never lose to it.
    if strat and getattr(strat, 'minimize_unique_panels', False):
        return _run_and_promote(
            [("nu", "Minimize Unique"), ("np", "Minimize Total")],
            pick_key=lambda r: (r["nu"], r["np"]),
            selected_flags={"minimize_unique_panels": True},
            banner="  [MIN-UNIQUE] Comparing vs Min-Total building-wide "
                   "(unique must not regress)...")

    # ---- Everything else (Min-Total, GA): single straight pass ----
    return process_all_walls(walls_rows, openings_rows, output_dir,
        door_clearances, window_clearances, storefront_clearances,
        config=ACTIVE_CONFIG, orientation=orientation,
        output_filename=output_filename)

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

class PanelLibrary(object):
    """
    The LIVE, building-wide set of panel types. Every strategy registers its
    panels here as walls are processed, and every panel inherits its type label
    (and its master's exact cutout floats) from this single source of truth.

    Reuse is STRICT. A panel only collapses onto an existing type when its
    canonical signature matches within ``match_tol`` -- a pure float-noise floor
    (1/16" by default), NOT a merge tolerance. Widths are never distorted to
    force a match. Panels that differ by more than the noise floor stay distinct,
    even if they are physically close: those are surfaced as near-duplicate
    warnings so they can be reconciled upstream rather than silently merged
    (which is what the old 1" normalize did -- and that relocated cutouts).

    Mirror-aware: a left-hand panel and its right-hand twin share one type.
    """

    def __init__(self, match_tol=0.0625, near_tol=0.5, merge_tol=0.0):
        self.match_tol = float(match_tol)   # exact-match noise floor (<< 0.5")
        self.near_tol  = float(near_tol)    # report (don't merge) within this
        self.merge_tol = float(merge_tol)   # Option 2: ACTUALLY merge within this
        self.types = {}                     # canonical_sig -> master dict
        self._order = []                    # canonical_sigs in discovery order
        self._counter = 0
        self._merged = 0                    # how many panels were drift-merged

    # -- internal: quantize a value onto the strict match grid ----------------
    def _q(self, v):
        t = self.match_tol
        return round(float(v) / t) * t

    # -- build the mirror-aware canonical signature for a panel ---------------
    def _signature(self, w, h, cutouts):
        sig_w = self._q(w)
        sig_h = self._q(h)
        std, mir = [], []
        for c in cutouts:
            cy = self._q(c['y_in'])
            cw = self._q(c['width_in'])
            ch = self._q(c['height_in'])
            std.append((self._q(c['x_in']), cy, cw, ch))
            mir.append((self._q(w - c['x_in'] - c['width_in']), cy, cw, ch))
        std.sort()
        mir.sort()
        std_sig = (sig_w, sig_h, tuple(std))
        mir_sig = (sig_w, sig_h, tuple(mir))
        canonical = min(std_sig, mir_sig)
        return canonical, (canonical == mir_sig)

    # -- read-only query: does an identical type already exist? ---------------
    # (Hook for future placement-time reuse. Returns the type label or None.
    #  Does NOT mutate the library.)
    def find_reusable(self, w, h, cutouts):
        canon, _ = self._signature(w, h, cutouts)
        t = self.types.get(canon)
        return t["label"] if t else None

    # -- register a panel record, assigning its type label in place -----------
    def register(self, record):
        import copy, json
        cutouts = json.loads(record["cutouts_json"]) if record["cutouts_json"] else []
        w = float(record["width_in"])
        h = float(record["height_in"])
        canon, is_mir = self._signature(w, h, cutouts)

        # 1) EXACT strict twin -> inherit master floats, keep own (identical) dims.
        if canon in self.types:
            return self._absorb(record, self.types[canon], w, is_mir,
                                snap_dims=False, original_cutouts=cutouts)

        # 2) NEAR twin within merge_tol (Option 2). Collapses measurement/algorithm
        #    drift (e.g. 305.000 vs 305.625) into one real fabrication type. The
        #    child adopts the master's exact dims, so the only cost is a placement
        #    delta of at most merge_tol.
        if self.merge_tol > 0.0:
            near = self._find_near(w, h, cutouts)
            if near is not None:
                self._merged += 1
                return self._absorb(record, near, w, is_mir,
                                    snap_dims=True, original_cutouts=cutouts)

        # 3) Brand-new type.
        self._counter += 1
        label = "T{:02d}".format(self._counter)
        self.types[canon] = {
            "label": label, "is_mirrored": is_mir,
            "cutouts": copy.deepcopy(cutouts), "w": w, "h": h,
            "count": 1, "reused": False,
        }
        self._order.append(canon)
        record["panel_name"] = "{}-P{:02d}".format(label, 1)
        return label, False

    def _absorb(self, record, t, w, is_mir, snap_dims, original_cutouts=None):
        """Attach `record` to existing type `t`, inheriting master cutout floats.
        If snap_dims, also adopt the master's width/height (near-merge case).

        Cutout identity (opening id + type string) is preserved from the
        child's own cutout data, not copied from the master. Without this,
        every reuse of a type would relabel the child's opening ids to the
        master's, so a single opening id like 7708158 could end up stamped
        on 3+ physically distinct openings across the wall."""
        import copy, json
        t["count"] += 1
        t["reused"] = True
        ref_w = t["w"] if snap_dims else w

        # If the caller didn't pass the record's original cutouts, re-parse
        # them so this method stays safe to call from any entry point.
        if original_cutouts is None:
            original_cutouts = (json.loads(record["cutouts_json"])
                                if record.get("cutouts_json") else [])
        flip_needed = (is_mir != t["is_mirrored"])

        # Build (mirror-corrected x_in, y_in) keys for the child cutouts so
        # we can pair them with master cutouts by geometry regardless of the
        # order they arrived in.
        _child_by_key = []
        for oc in original_cutouts:
            _ox = oc.get("x_in", 0.0)
            _oy = oc.get("y_in", 0.0)
            _ow = oc.get("width_in", 0.0)
            _oh = oc.get("height_in", 0.0)
            _cx = (ref_w - _ox - _ow) if flip_needed else _ox
            _child_by_key.append({
                "cx": _cx, "cy": _oy, "cw": _ow, "ch": _oh, "data": oc,
            })

        def _find_match(mc):
            _tol = 1.0  # inches; matches _find_near cutout tolerance
            _mx = mc.get("x_in", 0.0)
            _my = mc.get("y_in", 0.0)
            _mw = mc.get("width_in", 0.0)
            _mh = mc.get("height_in", 0.0)
            for _e in _child_by_key:
                if (abs(_e["cx"] - _mx) < _tol and
                    abs(_e["cy"] - _my) < _tol and
                    abs(_e["cw"] - _mw) < _tol and
                    abs(_e["ch"] - _mh) < _tol):
                    return _e["data"]
            return None

        new_cutouts = []
        for mc in t["cutouts"]:
            nc = copy.deepcopy(mc)
            if flip_needed:
                nc["x_in"]     = ref_w - mc["x_in"]     - mc["width_in"]
                nc["raw_x_in"] = ref_w - mc["raw_x_in"] - mc["raw_width_in"]
            # Inherit the child's opening identity for this cutout.
            _oc = _find_match(nc)
            if _oc is not None:
                for _k in ("id", "type"):
                    if _k in _oc:
                        nc[_k] = _oc[_k]
            new_cutouts.append(nc)

        record["cutouts_json"] = json.dumps(new_cutouts) if new_cutouts else ""
        if snap_dims:
            record["width_in"]  = t["w"]
            record["height_in"] = t["h"]
            record["area_in2"]  = t["w"] * t["h"]
        record["panel_name"] = "{}-P{:02d}".format(t["label"], t["count"])
        return t["label"], True

    def _find_near(self, w, h, cutouts):
        """Linear scan for a master whose dims AND cutouts all sit within merge_tol
        (mirror-aware). Returns the master dict or None. Greedy: first discovered
        compatible type wins."""
        tol = self.merge_tol
        for canon in self._order:
            t = self.types[canon]
            if abs(w - t["w"]) > tol or abs(h - t["h"]) > tol:
                continue
            if len(cutouts) != len(t["cutouts"]):
                continue
            if self._cutouts_close(w, cutouts, t, tol):
                return t
        return None

    def _cutouts_close(self, w, cutouts, t, tol):
        master = sorted((m["x_in"], m["y_in"], m["width_in"], m["height_in"])
                        for m in t["cutouts"])
        direct = sorted((c["x_in"], c["y_in"], c["width_in"], c["height_in"])
                        for c in cutouts)
        if all(abs(a[i] - b[i]) <= tol for a, b in zip(direct, master) for i in range(4)):
            return True
        mir = sorted((w - c["x_in"] - c["width_in"], c["y_in"], c["width_in"], c["height_in"])
                     for c in cutouts)
        if all(abs(a[i] - b[i]) <= tol for a, b in zip(mir, master) for i in range(4)):
            return True
        return False

    # -- count of distinct types currently in the set -------------------------
    def unique_count(self):
        return len(self.types)

    # -- ordered (label, count, w, h) rows for reporting ----------------------
    def rows(self):
        out = []
        for canon in self._order:
            t = self.types[canon]
            out.append((t["label"], t["count"], t["w"], t["h"]))
        return out

    # -- find type pairs that are physically close but kept separate ----------
    def near_duplicates(self):
        rows = [(self.types[c]["label"], self.types[c]["w"], self.types[c]["h"],
                 self.types[c]["count"]) for c in self._order]
        pairs = []
        for i in range(len(rows)):
            for j in range(i + 1, len(rows)):
                la, wa, ha, ca = rows[i]
                lb, wb, hb, cb = rows[j]
                dw = abs(wa - wb)
                dh = abs(ha - hb)
                if dw <= self.near_tol and dh <= self.near_tol and (dw + dh) > 0:
                    pairs.append((la, wa, ha, ca, lb, wb, hb, cb, dw, dh))
        return pairs

    # -- print the live set + any near-duplicate warnings ---------------------
    def report(self):
        rows = self.rows()
        total_panels = sum(r[1] for r in rows)
        merged_note = (" ({} drift-merged @ {:g}\")".format(self._merged, self.merge_tol)
                       if self.merge_tol > 0 and self._merged else "")
        print(Ansi.CYAN + "  [PANEL SET] {} unique types across {} panels{}"
              .format(len(rows), total_panels, merged_note) + Ansi.RESET)
        for label, count, w, h in rows:
            print("    {}  {:>7.3f} x {:<7.3f}  -> {} panel(s)"
                  .format(label.ljust(5), w, h, count))
        nd = self.near_duplicates()
        if nd:
            print(Ansi.YELLOW + "  [NEAR-DUPLICATES] kept SEPARATE under strict "
                  "matching (reconcile upstream if intentional):" + Ansi.RESET)
            for la, wa, ha, ca, lb, wb, hb, cb, dw, dh in nd:
                print(Ansi.YELLOW + "    {} ({:.3f}x{:.3f}) vs {} ({:.3f}x{:.3f})"
                      "  -- dW={:.3f}\" dH={:.3f}\"".format(
                          la, wa, ha, lb, wb, hb, dw, dh) + Ansi.RESET)


def global_normalize_records(records, tol=0.0625):
    """
    Backward-compatible single-shot labeler. Now delegates to PanelLibrary so the
    type/mirror/inherit logic lives in exactly one place. ``tol`` is the strict
    float-noise floor (defaults to 1/16"), NOT a merge tolerance: panels that
    differ by more than ``tol`` are kept as distinct types. Prefer building one
    live PanelLibrary across the run; this wrapper exists for old call sites.
    """
    _diag(Ansi.CYAN + "  [TYPES] Labeling panels via live PanelLibrary "
          "(strict matching, tol={0:g}\")...".format(tol) + Ansi.RESET)
    lib = PanelLibrary(match_tol=tol)
    for r in records:
        lib.register(r)
    lib.report()
    return records

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

# =============================================================================
# SECTION: TRUCKING OPTIMIZATION  (pure addition -- does not touch the optimizer)
# =============================================================================
# Takes the finished optimized_panel_placement.csv and produces:
#   1) trucking_plan.csv   -- one row per panel: install seq, truck, layer, slot,
#                             physical load order.
#   2) trucking_layout.txt -- human-readable stack layout for EACH truck (every
#                             layer top-to-bottom with panel names, lengths, row
#                             usage) plus the TOTAL NUMBER OF TRUCKS NEEDED.
#
# Install order rule (isolated in _trk_install_sequence so it can be swapped):
#   install_pattern:
#     "spiral" (default) -- complete the ENTIRE BOTTOM COURSE around the
#         building in the chosen rotation, then the next course up, etc.
#         (practical for multistory: one lift elevation at a time)
#     "column" -- work around the building wall by wall; at each horizontal
#         slot install the full vertical stack bottom-to-top before moving on.
#   start_facade: "north" / "east" / "south" / "west" -- which facade the
#         sequence begins on, resolved from the panels' exterior normals.
#         Compass convention (per project plan view): South = left (-X),
#         North = right (+X), East = front (-Y), West = back (+Y).
#   rotation: "ccw" (default) or "cw" -- direction of travel around the
#         building.
#
# Truck model (v1 greedy packer, isolated in _trk_pack_trucks):
#   - Panels ride FLAT, longer dimension along the truck length.
#   - A "row" (layer) holds 1-3 panels end-to-end:
#       2 panels -> one gap of two_panel_gap_in between them
#       3 panels -> two gaps of three_panel_gap_in each
#     sum(panel lengths) + gaps <= truck_length_in + overhang_length_in
#   - Panel across-truck dimension <= truck_width_in + 2 * overhang_width_in
#     (otherwise flagged OVERSIZE and left off the trucks).
#   - Layers stack with dunnage_height_in under the bottom layer and between
#     layers: total = n*dunnage + sum(thickness) <= max_stack_height_in.
#   - trucks_on_site trucks are "open" simultaneously; installation can pull
#     from any open truck, but within each truck earlier-install panels must
#     sit ABOVE later ones (layers are built top-first here; physical load
#     order per truck is the reverse).

DEFAULT_TRUCKING_SETTINGS = {
    "truck_length_in":        636.0,   # 53'
    "truck_width_in":         102.0,   # 8'6"
    "max_stack_height_in":    102.0,
    "dunnage_height_in":      4.0,
    "two_panel_gap_in":       6.0,
    "three_panel_gap_in":     15.0,    # 1'3"
    "overhang_length_in":     0.0,
    "overhang_width_in":      0.0,     # per side
    "trucks_on_site":         2,
    "install_pattern":        "spiral",   # "spiral" | "column"
    "start_facade":           "north",    # "north"|"east"|"south"|"west"
    "rotation":               "ccw",      # "ccw" | "cw"
    "panel_thickness_in":     6.625,
}

def parse_length_to_inches(text, default):
    """Parse '53'', '8'6"', '1' 3"', '4"', or plain inches into float inches."""
    try:
        s = str(text).strip().replace('\u2019', "'").replace('\u201d', '"')
        if not s:
            return default
        s = s.replace(" ", "")
        feet, inches = 0.0, 0.0
        if "'" in s:
            ft_part, rest = s.split("'", 1)
            feet = float(ft_part) if ft_part else 0.0
            rest = rest.replace('"', "")
            inches = float(rest) if rest else 0.0
        else:
            inches = float(s.replace('"', ""))
        return feet * 12.0 + inches
    except Exception:
        return default

def _trk_fmt_ftin(inches):
    """96.0 -> 8'-0\"  |  102.5 -> 8'-6.5\"  |  6.6 -> 6.6\" """
    ft = int(inches // 12)
    rem = inches - ft * 12
    if ft:
        return "{0}'-{1:g}\"".format(ft, round(rem, 2))
    return "{0:g}\"".format(round(inches, 2))

# Compass convention per project plan view (see UI note):
#   South = left (-X) | North = right (+X) | East = front (-Y) | West = back (+Y)
_TRK_FACADE_NORMALS = {
    "north": ( 1.0,  0.0),
    "south": (-1.0,  0.0),
    "east":  ( 0.0, -1.0),
    "west":  ( 0.0,  1.0),
}

def _trk_install_sequence(rows, st):
    """Order panel rows for installation per st["install_pattern"],
    st["start_facade"], and st["rotation"]. Returns the same row dicts,
    annotated with _install_seq (1-based).

    spiral: complete each course around the whole building (bottom course
            first), traveling in the chosen rotation, starting on the wall
            whose exterior normal best matches the chosen facade.
    column: previous behavior -- walls in rotation order from the start
            facade; within each wall, each horizontal slot's full vertical
            stack bottom-to-top before moving sideways."""
    import math as _m

    pattern  = str(st.get("install_pattern", "spiral")).strip().lower()
    facade   = str(st.get("start_facade", "north")).strip().lower()
    rotation = str(st.get("rotation", "ccw")).strip().lower()
    ccw      = (rotation != "cw")
    fvec     = _TRK_FACADE_NORMALS.get(facade, _TRK_FACADE_NORMALS["north"])

    # global centroid of each panel (inches; wall_origin_* are in feet)
    for r in rows:
        ox = safe_float(r.get("wall_origin_x")) * 12.0
        oy = safe_float(r.get("wall_origin_y")) * 12.0
        dx = safe_float(r.get("wall_dir_x"))
        dy = safe_float(r.get("wall_dir_y"))
        mid = safe_float(r.get("x_in")) + safe_float(r.get("width_in")) / 2.0
        r["_gx"] = ox + dx * mid
        r["_gy"] = oy + dy * mid

    cx = sum(r["_gx"] for r in rows) / float(len(rows))
    cy = sum(r["_gy"] for r in rows) / float(len(rows))

    # group by wall; angular position + mean exterior normal of each wall
    walls = {}
    for r in rows:
        walls.setdefault(r.get("wall_id", ""), []).append(r)
    wall_ang, wall_nrm, wall_cen = {}, {}, {}
    for wid, wrows in walls.items():
        mx = sum(w["_gx"] for w in wrows) / float(len(wrows))
        my = sum(w["_gy"] for w in wrows) / float(len(wrows))
        wall_cen[wid] = (mx, my)
        wall_ang[wid] = _m.atan2(my - cy, mx - cx)
        nx = sum(safe_float(w.get("wall_normal_x")) for w in wrows) / float(len(wrows))
        ny = sum(safe_float(w.get("wall_normal_y")) for w in wrows) / float(len(wrows))
        nm = _m.sqrt(nx * nx + ny * ny)
        if nm > 1e-9:
            wall_nrm[wid] = (nx / nm, ny / nm)
        else:
            # normals missing in CSV -> fall back to radial direction
            rx, ry = mx - cx, my - cy
            rm = _m.sqrt(rx * rx + ry * ry) or 1.0
            wall_nrm[wid] = (rx / rm, ry / rm)

    # start wall = best match between exterior normal and requested facade;
    # tie-break: the wall farthest out in that compass direction
    def facade_score(wid):
        n = wall_nrm[wid]
        c = wall_cen[wid]
        return (n[0] * fvec[0] + n[1] * fvec[1],
                c[0] * fvec[0] + c[1] * fvec[1])
    start_wid = max(walls.keys(), key=facade_score)

    a0 = wall_ang[start_wid]
    TWO_PI = 2.0 * _m.pi

    def wall_key(wid):
        d = (wall_ang[wid] - a0) if ccw else (a0 - wall_ang[wid])
        return d % TWO_PI
    wall_order = sorted(walls.keys(), key=wall_key)

    def travel_slot(r, wid):
        """Position of the panel along its wall in the direction of travel."""
        a = wall_ang[wid]
        tx, ty = (-_m.sin(a), _m.cos(a)) if ccw else (_m.sin(a), -_m.cos(a))
        d = walls[wid][0]
        fwd = (safe_float(d.get("wall_dir_x")) * tx +
               safe_float(d.get("wall_dir_y")) * ty) >= 0.0
        xr = safe_float(r.get("x_in"))
        return xr if fwd else -xr

    ordered = []
    if pattern == "column":
        for wid in wall_order:
            def in_wall_key(r, _wid=wid):
                return (round(travel_slot(r, _wid), 0),
                        safe_float(r.get("y_in")))
            ordered.extend(sorted(walls[wid], key=in_wall_key))
    else:
        # spiral: cluster y_in into building-wide courses (12" tolerance --
        # courses are level-aligned by the optimizer, so clusters are clean)
        ys = sorted(set(round(safe_float(r.get("y_in")), 1) for r in rows))
        clusters = []
        for y in ys:
            if clusters and y - clusters[-1][-1] <= 12.0:
                clusters[-1].append(y)
            else:
                clusters.append([y])
        course_of = {}
        for ci, cl in enumerate(clusters):
            for y in cl:
                course_of[y] = ci
        n_courses = len(clusters)
        for ci in range(n_courses):
            for wid in wall_order:
                course_rows = [r for r in walls[wid]
                               if course_of[round(safe_float(r.get("y_in")), 1)] == ci]
                course_rows.sort(key=lambda r, _wid=wid: (
                    round(travel_slot(r, _wid), 0),
                    safe_float(r.get("y_in"))))
                ordered.extend(course_rows)

    for i, r in enumerate(ordered):
        r["_install_seq"] = i + 1
    print(Ansi.CYAN + "  [TRUCK] Sequence: {0}, start facade {1} (wall {2}), "
          "{3}".format("spiral (course-by-course)" if pattern != "column"
                       else "column-by-column",
                       facade.upper(), start_wid,
                       "CCW" if ccw else "CW") + Ansi.RESET)
    return ordered

def _trk_pack_trucks(seq_rows, st):
    """Greedy packer. Processes panels in INSTALL order and builds each truck's
    layer list TOP-FIRST (layers[0] = top of stack), so earlier-install panels
    always sit above later ones in the same truck. Returns (trucks, oversize)
    where trucks is a list of dicts {no, layers:[[row,..],..], height}."""
    L_max  = st["truck_length_in"] + st["overhang_length_in"]
    W_max  = st["truck_width_in"] + 2.0 * st["overhang_width_in"]
    H_max  = st["max_stack_height_in"]
    dun    = st["dunnage_height_in"]
    gap2   = st["two_panel_gap_in"]
    gap3   = st["three_panel_gap_in"]
    thick  = st["panel_thickness_in"]
    K      = max(1, int(st["trucks_on_site"]))

    def layer_len(panels_in_layer):
        n = len(panels_in_layer)
        total = sum(p["_lay_len"] for p in panels_in_layer)
        if n == 2:   total += gap2
        elif n == 3: total += 2.0 * gap3
        return total

    trucks, open_trucks, oversize = [], [], []
    next_no = [1]

    def new_truck():
        t = {"no": next_no[0], "layers": [], "height": 0.0}
        next_no[0] += 1
        trucks.append(t)
        open_trucks.append(t)
        return t

    for r in seq_rows:
        w = safe_float(r.get("width_in"))
        h = safe_float(r.get("height_in"))
        r["_lay_len"], r["_lay_across"] = max(w, h), min(w, h)
        if r["_lay_across"] > W_max or r["_lay_len"] > L_max:
            r["_truck"] = "OVERSIZE"
            oversize.append(r)
            continue

        placed = False
        # 1) try to extend the CURRENT (lowest, still-being-built) layer
        for t in open_trucks:
            if t["layers"]:
                lay = t["layers"][-1]
                if len(lay) < 3 and layer_len(lay + [r]) <= L_max:
                    lay.append(r)
                    placed = True
                    break
        # 2) else start a new layer (one dunnage + one thickness taller)
        if not placed:
            for t in open_trucks:
                if t["height"] + dun + thick <= H_max:
                    t["layers"].append([r])
                    t["height"] += dun + thick
                    placed = True
                    break
        # 3) else bring in a fresh truck (dispatch the oldest if K on site)
        if not placed:
            if len(open_trucks) >= K:
                open_trucks.pop(0)
            t = new_truck()
            t["layers"].append([r])
            t["height"] = dun + thick
        # remember assignment
        for t in trucks:
            for lay in t["layers"]:
                if r in lay:
                    r["_truck"] = t["no"]
    return trucks, oversize

def _trk_write_layout_txt(path, trucks, oversize, seq_len, st):
    """Human-readable per-truck stack layout + total trucks needed."""
    L_max = st["truck_length_in"] + st["overhang_length_in"]

    def layer_len(lay):
        n = len(lay)
        total = sum(p["_lay_len"] for p in lay)
        if n == 2:   total += st["two_panel_gap_in"]
        elif n == 3: total += 2.0 * st["three_panel_gap_in"]
        return total

    lines = []
    lines.append("=" * 78)
    lines.append("TRUCKING PLAN")
    lines.append("=" * 78)
    lines.append("TOTAL TRUCKS NEEDED: {0}".format(len(trucks)))
    if oversize:
        lines.append("OVERSIZE (special transport, not on trucks): {0} panel(s)"
                     .format(len(oversize)))
    lines.append("Panels loaded: {0} of {1}".format(seq_len - len(oversize), seq_len))
    lines.append("")
    lines.append("Settings: truck {0} x {1} | max stack {2} | dunnage {3} | "
                 "gaps 2-up {4} / 3-up {5} | overhang L {6} / W {7} per side | "
                 "panel thickness {8}".format(
        _trk_fmt_ftin(st["truck_length_in"]), _trk_fmt_ftin(st["truck_width_in"]),
        _trk_fmt_ftin(st["max_stack_height_in"]), _trk_fmt_ftin(st["dunnage_height_in"]),
        _trk_fmt_ftin(st["two_panel_gap_in"]), _trk_fmt_ftin(st["three_panel_gap_in"]),
        _trk_fmt_ftin(st["overhang_length_in"]), _trk_fmt_ftin(st["overhang_width_in"]),
        _trk_fmt_ftin(st["panel_thickness_in"])))
    lines.append("Sequence: {0} | start facade: {1} | rotation: {2}".format(
        "spiral (course-by-course, bottom first)"
        if str(st.get("install_pattern", "spiral")).lower() != "column"
        else "column-by-column",
        str(st.get("start_facade", "north")).upper(),
        "CW" if str(st.get("rotation", "ccw")).lower() == "cw" else "CCW"))
    lines.append("Rule: install order runs TOP-DOWN in each stack; physical "
                 "loading is the reverse (bottom layer first).")
    lines.append("")

    for t in trucks:
        n_p = sum(len(l) for l in t["layers"])
        n_lay = len(t["layers"])
        lines.append("-" * 78)
        lines.append("TRUCK {0}   ({1} layer(s), {2} panel(s), stack height "
                     "{3} / {4})".format(
            t["no"], n_lay, n_p, _trk_fmt_ftin(t["height"]),
            _trk_fmt_ftin(st["max_stack_height_in"])))
        for li, lay in enumerate(t["layers"]):
            tag = "TOP   " if li == 0 else ("BOTTOM" if li == n_lay - 1 else "      ")
            cells = "  |  ".join(
                "{0} ({1} x {2}) inst#{3}".format(
                    p.get("panel_name", "?"),
                    _trk_fmt_ftin(p["_lay_len"]), _trk_fmt_ftin(p["_lay_across"]),
                    p.get("_install_seq", "?"))
                for p in lay)
            used = layer_len(lay)
            lines.append("  Layer {0:>2} {1} : {2}".format(li + 1, tag, cells))
            lines.append("             row length {0} / {1}  ({2:.0f}% used)".format(
                _trk_fmt_ftin(used), _trk_fmt_ftin(L_max),
                100.0 * used / L_max if L_max else 0.0))
        lines.append("  Load order: Layer {0} (BOTTOM) first -> Layer 1 (TOP) last."
                     .format(n_lay))
    if oversize:
        lines.append("-" * 78)
        lines.append("OVERSIZE PANELS (exceed truck envelope; arrange special "
                     "transport):")
        for p in oversize:
            lines.append("  {0}  ({1} x {2})  inst#{3}".format(
                p.get("panel_name", "?"), _trk_fmt_ftin(p["_lay_len"]),
                _trk_fmt_ftin(p["_lay_across"]), p.get("_install_seq", "?")))
    lines.append("=" * 78)

    with open(path, "w") as f:
        f.write("\n".join(lines) + "\n")
    return path

def generate_trucking_plan(panels_csv_path, settings=None, output_path=None):
    """Read optimized_panel_placement.csv, compute the install sequence and the
    truck loading plan. Writes trucking_plan.csv (per-panel) AND
    trucking_layout.txt (per-truck stack layout + total trucks needed) next to
    it (or beside output_path). Returns the csv output path, or None if nothing
    to do. Pure addition: does not modify the placement CSV or optimizer state."""
    import csv as _csv, os as _os

    st = dict(DEFAULT_TRUCKING_SETTINGS)
    if settings:
        st.update(settings)

    with open(panels_csv_path, "r") as f:
        rows = [dict(r) for r in _csv.DictReader(f)]
    if not rows:
        print(Ansi.YELLOW + "  [TRUCK] No panels in {}; skipping trucking plan."
              .format(panels_csv_path) + Ansi.RESET)
        return None

    seq = _trk_install_sequence(rows, st)
    trucks, oversize = _trk_pack_trucks(seq, st)

    if output_path is None:
        output_path = _os.path.join(_os.path.dirname(panels_csv_path),
                                    "trucking_plan.csv")
    layout_path = _os.path.join(_os.path.dirname(output_path),
                                "trucking_layout.txt")

    # per-truck physical load order = bottom layer first = reversed layers
    load_order = {}
    for t in trucks:
        n_lay, order = len(t["layers"]), 1
        for li in range(n_lay - 1, -1, -1):      # bottom (last) first
            for r in t["layers"][li]:
                load_order[id(r)] = order
                order += 1

    _f = open(output_path, "wb") if _sys_is_py2() else open(output_path, "w")
    try:
        wtr = _csv.writer(_f, lineterminator="\n")
        wtr.writerow(["install_seq", "panel_name", "panel_type", "wall_id",
                      "width_in", "height_in", "truck_no",
                      "layer_from_top", "slot_in_layer",
                      "truck_load_order", "note"])
        for r in seq:
            tno = r.get("_truck", "")
            li_ft, slot = "", ""
            if tno != "OVERSIZE" and tno != "":
                for t in trucks:
                    if t["no"] == tno:
                        for li, lay in enumerate(t["layers"]):
                            if r in lay:
                                li_ft, slot = li + 1, lay.index(r) + 1
                                break
                        break
            note = ("exceeds truck envelope -- special transport"
                    if tno == "OVERSIZE" else "")
            wtr.writerow([r["_install_seq"], r.get("panel_name", ""),
                          r.get("panel_type", ""), r.get("wall_id", ""),
                          r.get("width_in", ""), r.get("height_in", ""),
                          tno, li_ft, slot, load_order.get(id(r), ""), note])
    finally:
        _f.close()

    _trk_write_layout_txt(layout_path, trucks, oversize, len(seq), st)

    print(Ansi.GREEN + "  [TRUCK] TOTAL TRUCKS NEEDED: {0}   ({1} panel(s) "
          "loaded, {2} oversize)".format(len(trucks), len(seq) - len(oversize),
                                         len(oversize)) + Ansi.RESET)
    print(Ansi.GREEN + "  [TRUCK] Plan:   {0}".format(output_path) + Ansi.RESET)
    print(Ansi.GREEN + "  [TRUCK] Layout: {0}".format(layout_path) + Ansi.RESET)
    for t in trucks:
        n_p = sum(len(l) for l in t["layers"])
        print(Ansi.CYAN + "    Truck {0}: {1} layer(s), {2} panel(s), "
              "stack {3:.1f}\" (max {4:.1f}\")".format(
                  t["no"], len(t["layers"]), n_p, t["height"],
                  st["max_stack_height_in"]) + Ansi.RESET)
    return output_path

def _sys_is_py2():
    import sys as _s
    return _s.version_info[0] == 2