# --- CONFIGURATION & CONSTANTS ---
APP_NAME = "Map Editor"

DEFAULT_W, DEFAULT_H = 15, 15
DEFAULT_HGT = 0x7F
DEFAULT_HOST_POS_Y = -700
DEFAULT_HOST_RELOAD_CONST = 500000
DEFAULT_HOST_VIEWANGLE = 0
DEFAULT_SCRIPT_CONTENT = ";include data:scripts/startup2.scr"
MAX_SPECIAL_SLOTS = 10

# GEM action templates supported by OpenUA. The same list drives both the
# editor dropdown and LDF import, so saved upgrades are never dropped on load.
GEM_ACTION_PARAMS_BY_TARGET = {
    "modify_vehicle": (
        "enable",
        "add_energy",
        "add_shield",
        "add_radar",
        "add_unhide_radar",
        "num_weapons",
    ),
    "modify_building": ("enable",),
    "modify_weapon": (
        "add_energy",
        "add_energy_heli",
        "add_energy_tank",
        "add_energy_flyer",
        "add_energy_Robo",
        "add_shot_time",
        "add_shot_time_user",
    ),
}
GEM_ACTION_PARAMS_BY_TARGET_LOWER = {
    target: {param.lower(): param for param in params}
    for target, params in GEM_ACTION_PARAMS_BY_TARGET.items()
}

# Height Constraints (+/- 30 from 0x7F)
HGT_MIN = 0x61 # 97
HGT_MAX = 0x9D # 157
EDITOR_HEIGHT_BASE = 30
EDITOR_HEIGHT_MIN = 0
EDITOR_HEIGHT_MAX = 60

GRID_COLOR = "#707070"
MARKER_COLOR = "#FF00FF"
CUSTOM_BORDER_COLOR = "#FF0000" # Bright Red for Custom
TEXT_COLOR = "#00FFFF"
SCROLL_BG = "#00AAAA"
SCROLL_ACTIVE = TEXT_COLOR
SELECTION_COLOR = "#FFFFFF"
SELECTION_SHADOW_COLOR = "#001018"
SELECTION_OUTLINE_WIDTH = 4
LISTBOX_SELECTION_BG = "#00A8C8"
LISTBOX_SELECTION_FG = "#000000"

HEIGHT_HIGH_COLOR = "#00E5FF"
HEIGHT_BASE_COLOR = "#F2F2F2"
HEIGHT_LOW_COLOR = "#FF7A00"
HEIGHT_PANEL_BG = "#1a1a1a"
HEIGHT_SLIDER_W = 170
HEIGHT_SLIDER_H = 260
HEIGHT_SLIDER_TOP_Y = 24
HEIGHT_SLIDER_BOTTOM_Y = 236
HEIGHT_SLIDER_TRACK_W = 44

DEBUG_RENDER_PERF = False
SHOW_HEIGHT_NUMBERS_OUTSIDE_HGT = False
HGT_NUMBER_MIN_ZOOM = 32

IMAGE_EXTENSIONS = (".png", ".jpg", ".jpeg")
TILE_IMAGE_EXTENSIONS = (".png", ".jpg")
GEM_IMAGE_EXTENSIONS = (".png",)

# WORLD SCALE
SECTOR_SIZE = 3072
HALF_SECTOR = SECTOR_SIZE // 2

# Tech Layout
TECH_ITEM_W = 450
TECH_ITEM_H = 24

# Data Structures
FACTIONS = {
    0: ("NEUTRAL", None),
    1: ("RESISTANCE", "#0088FF"),
    2: ("SULGOGAR", "#00FF00"),
    3: ("MYKON", "#FFFFFF"),
    4: ("TAERKASTEN", "#FFFF00"),
    5: ("BLACK SECT", "#555555"),
    6: ("GHORKOV", "#FF0000"),
    7: ("DRONES", "#2A1F3D")
}

FACTION_MAP_COLORS = {
    5: "#555555",
    7: "#2A1F3D",
}

FACTION_MAP_OUTLINES = {
    5: "#D0D0D0",
    7: "#A99BC5",
}

FACTION_TEXT_COLORS = {
    0: "#FFFFFF", 1: "#0088FF", 2: "#00FF00", 3: "#FFFFFF",
    4: "#FFFF00", 5: "#AAAAAA", 6: "#FF4444", 7: "#D0D0D0"
}

HEIGHTS = list(range(256))

PALETTE_ONLY_BUILDING_OVERLAYS = {"beamgate", "gem", "superitem"}

HOST_AI_BUDGET_FIELDS = (
    "con_budget", "def_budget", "rec_budget", "rob_budget",
    "pow_budget", "rad_budget", "saf_budget", "cpl_budget",
)
HOST_AI_DELAY_FIELDS = (
    "con_delay", "def_delay", "rec_delay", "rob_delay",
    "pow_delay", "rad_delay", "saf_delay", "cpl_delay",
)
HOST_AI_FIELDS = (
    "con_budget", "con_delay",
    "def_budget", "def_delay",
    "rec_budget", "rec_delay",
    "rob_budget", "rob_delay",
    "pow_budget", "pow_delay",
    "rad_budget", "rad_delay",
    "saf_budget", "saf_delay",
    "cpl_budget", "cpl_delay",
)
DEFAULT_HOST_AI_PRESET = "Balanced"
FALLBACK_HOST_AI_PRESETS = {
    "Balanced": {
        "con_budget": 70,
        "con_delay": 0,
        "def_budget": 70,
        "def_delay": 0,
        "rec_budget": 70,
        "rec_delay": 0,
        "rob_budget": 70,
        "rob_delay": 0,
        "pow_budget": 40,
        "pow_delay": 0,
        "rad_budget": 10,
        "rad_delay": 0,
        "saf_budget": 40,
        "saf_delay": 0,
        "cpl_budget": 20,
        "cpl_delay": 0,
    }
}
