"""
PiPedal WebSocket Client and PySide6 QML Bridge
Implements the exact PiPedal JSON-RPC protocol over WebSocket with asyncio,
automatic reconnection, live traffic logging, and thread-safe QObject bridge for QML.
"""

from __future__ import annotations
import sys

import asyncio
import json
import logging
import threading
from typing import Any, Dict, List, Optional, Tuple

import websockets
from PySide6.QtCore import Property, QObject, Qt, Signal, Slot

import config

logger = logging.getLogger("PiPedalClient")
logging.basicConfig(level=logging.INFO, format="[%(levelname)s] %(asctime)s - %(message)s")


# ---------------------------------------------------------------------------
# Category & Color Mapping (Quad Cortex Visual Style)
# ---------------------------------------------------------------------------
CATEGORY_AMP = "Amp"
CATEGORY_DRIVE = "Drive"
CATEGORY_MODULATION = "Modulation"
CATEGORY_DELAY_REVERB = "Delay/Reverb"
CATEGORY_UTILITY = "Utility"
CATEGORY_CAB = "Cab"
CATEGORY_DELAY = "Delay"
CATEGORY_REVERB = "Reverb"
CATEGORY_EQ = "Utility/EQ"

CATEGORY_COLORS = {
    CATEGORY_AMP: "#FF3B5C",
    CATEGORY_DRIVE: "#FF9F1C",
    CATEGORY_MODULATION: "#00E5FF",
    CATEGORY_DELAY_REVERB: "#A855F7",
    CATEGORY_UTILITY: "#10B981",
    CATEGORY_CAB: "#F97316",
    CATEGORY_DELAY: "#10B981",
    CATEGORY_REVERB: "#A855F7",
    CATEGORY_EQ: "#94A3B8",
}


SCENE_COLOR_MAP = {
    "red": "#EF4444",
    "pink": "#EC4899",
    "purple": "#A855F7",
    "deeppurple": "#7C3AED",
    "indigo": "#4F46E5",
    "blue": "#3B82F6",
    "lightblue": "#38BDF8",
    "cyan": "#06B6D4",
    "teal": "#14B8A6",
    "green": "#22C55E",
    "lightgreen": "#84CC16",
    "lime": "#A3E635",
    "yellow": "#FACC15",
    "amber": "#FACC15",
    "orange": "#F97316",
    "deeporange": "#EA580C",
    "brown": "#A16207",
    "grey": "#64748B",
    "gray": "#64748B",
    "bluegrey": "#64748B",
    "bluegray": "#64748B",
    "black": "#27272A",
    "white": "#F4F4F5",
}


def map_scene_color(color: Any) -> str:
    if isinstance(color, str):
        raw = color.strip().lower()
        if raw.startswith("#"):
            if len(raw) in (4, 7, 9):
                return raw.upper()
            return "#64748B"
        if raw.isdigit():
            palette = ["#22C55E", "#FACC15", "#EF4444", "#F97316", "#06B6D4", "#3B82F6", "#A855F7", "#EC4899"]
            index = int(raw)
            if 0 <= index < len(palette):
                return palette[index]
        return SCENE_COLOR_MAP.get(raw.replace("_", "").replace("-", "").replace(" ", ""), "#64748B")
    if isinstance(color, int):
        palette = ["#22C55E", "#FACC15", "#EF4444", "#F97316", "#06B6D4", "#3B82F6", "#A855F7", "#EC4899"]
        if 0 <= color < len(palette):
            return palette[color]
    return "#64748B"


def classify_plugin(plugin_type: str = "", name: str = "", uri: str = "") -> Tuple[str, str]:
    """Map LV2 plugin type/name/uri to Quad Cortex category and glowing accent color."""
    pt = (plugin_type or "").lower()
    nm = (name or "").lower()
    u = (uri or "").lower()
    combined = f"{pt} {nm} {u}"

    if any(k in combined for k in ["cabinet", "cab ir", "impulse", "cab-ir", "/cab"]):
        cat = CATEGORY_CAB
    elif any(k in combined for k in ["amp", "nam", "simulator"]):
        cat = CATEGORY_AMP
    elif any(k in combined for k in ["distortion", "waveshaper", "drive", "fuzz", "overdrive", "dist"]):
        cat = CATEGORY_DRIVE
    elif any(k in combined for k in ["chorus", "flange", "phase", "modulat", "tremolo", "vibrato", "rotary", "pitch"]):
        cat = CATEGORY_MODULATION
    elif any(k in combined for k in ["delay", "echo", "comb"]):
        cat = CATEGORY_DELAY
    elif any(k in combined for k in ["reverb", "spatial", "allpass"]):
        cat = CATEGORY_REVERB
    elif any(k in combined for k in ["equalizer", "filter", "eq"]):
        cat = CATEGORY_EQ
    else:
        cat = CATEGORY_UTILITY

    return cat, CATEGORY_COLORS[cat]


def format_control_units(units: str) -> str:
    """Normalize PiPedal LV2 unit names for touchscreen labels."""
    normalized = str(units or "").strip()
    aliases = {
        "db": "dB",
        "hz": "Hz",
        "khz": "kHz",
        "mhz": "MHz",
        "ms": "ms",
        "s": "s",
        "percent": "%",
        "pc": "%",
        "bpm": "BPM",
        "cent": "cent",
        "cents": "cents",
    }
    if normalized.lower() in ("", "none", "null"):
        return ""
    return aliases.get(normalized.lower(), normalized)


# ---------------------------------------------------------------------------
# PiPedalBridge (Qt QObject for QML Integration)
# ---------------------------------------------------------------------------
class PiPedalBridge(QObject):
    hostAddressChanged = Signal()
    serverVersionChanged = Signal()
    """
    Bridge connecting the asyncio PiPedal WebSocket client to Qt QML.
    Strictly reflects live state from the Pi (zero fake/demo mock data).
    """

    connectedChanged = Signal(bool)
    statusTextChanged = Signal(str)
    activePresetNameChanged = Signal(str)
    activePresetIdChanged = Signal(int)
    presetChangedStatusChanged = Signal(bool)
    presetsListChanged = Signal()
    effectBlocksChanged = Signal()
    selectedPedalIndexChanged = Signal(int)
    selectedPedalChanged = Signal()
    fileOptionsChanged = Signal()
    laneChanged = Signal()
    snapshotsChanged = Signal()
    signalTreeChanged = Signal()

    # Internal signals used for thread-safe cross-thread dispatch from asyncio to Qt
    _sig_conn_state = Signal(bool, str)
    _sig_full_state_updated = Signal()
    _sig_preset_list_updated = Signal()
    _sig_control_value_changed = Signal(int, str, float)
    _sig_item_enabled_changed = Signal(int, bool)
    _sig_preset_changed_flag = Signal(bool)
    _sig_snapshot_selected = Signal(int)
    _sig_file_list_updated = Signal(int, str, "QVariantList")
    _sig_path_property_changed = Signal(int, str, str)

    def __init__(self, parent: Optional[QObject] = None):
        super().__init__(parent)

        # Initial state before connecting to live hardware
        self._connected: bool = False
        self._status_text: str = f"Connecting to {config.PI_HOST}:{config.PI_PORT}..."
        self._active_preset_name: str = "Connecting..."
        self._active_preset_id: int = -1
        self._preset_changed: bool = False
        self._presets_list: List[Dict[str, Any]] = []
        self._effect_blocks: List[Dict[str, Any]] = []
        self._selected_pedal_index: int = 0
        self._file_options: List[Dict[str, Any]] = []
        self._file_options_instance_id: int = -1
        self._file_options_property_uri: str = ""
        self._dragging_control: Optional[Tuple[int, str]] = None
        self._lane1: List[Dict[str, Any]] = []
        self._lane2: List[Dict[str, Any]] = []
        self._lane3: List[Dict[str, Any]] = []
        self._lane4: List[Dict[str, Any]] = []
        self._snapshots: List[Any] = []
        self._snapshot_values: List[Any] = []
        self._selected_snapshot: int = -1
        self._scenes: List[Dict[str, Any]] = []
        self._active_scene_color: str = "#22C55E"
        self._active_scene_letter: str = "A"
        self._scene_debug_printed: bool = False
        self._favorites: Dict[str, bool] = {}
        self._signal_nodes: List[Dict[str, Any]] = []
        self._signal_wires: List[Dict[str, Any]] = []
        self._signal_canvas_width: int = 800
        self._signal_canvas_height: int = 416
        self._plugin_catalog: List[Dict[str, Any]] = []
        self._bank_index: Dict[str, Any] = {"selectedBank": -1, "banks": []}
        self._bank_presets: List[Dict[str, Any]] = []

        # Host and server version properties
        self._host_address: str = config.PI_HOST
        self._server_version: str = ""

        # Asyncio worker client reference
        self._async_client: Optional[PiPedalAsyncClient] = None

        # Connect internal cross-thread signals to main thread slots
        self._sig_conn_state.connect(self._handle_conn_state, Qt.QueuedConnection)
        self._sig_full_state_updated.connect(self._handle_full_state_updated, Qt.QueuedConnection)
        self._sig_preset_list_updated.connect(self._handle_preset_list_updated, Qt.QueuedConnection)
        self._sig_control_value_changed.connect(self._handle_control_value_changed, Qt.QueuedConnection)
        self._sig_item_enabled_changed.connect(self._handle_item_enabled_changed, Qt.QueuedConnection)
        self._sig_preset_changed_flag.connect(self._handle_preset_changed_flag, Qt.QueuedConnection)
        self._sig_snapshot_selected.connect(self._handle_snapshot_selected, Qt.QueuedConnection)
        self._sig_file_list_updated.connect(self._handle_file_list_updated, Qt.QueuedConnection)
        self._sig_path_property_changed.connect(self._handle_path_property_changed, Qt.QueuedConnection)

    def set_async_client(self, client: PiPedalAsyncClient):
        self._async_client = client

    def emit_connection_state(self, connected: bool, text: str):
        try:
            self._sig_conn_state.emit(connected, text)
        except RuntimeError:
            pass

    def _refresh_scenes(self):
        scenes = []
        raw_scenes = getattr(self, "_snapshot_values", [])
        for index in range(6):
            snapshot = raw_scenes[index] if index < len(raw_scenes) else None
            if isinstance(snapshot, dict):
                raw_name = str(snapshot.get("name") or "").strip()
                raw_color = snapshot.get("color")
                assigned = bool(raw_name)
                name = raw_name
                color = map_scene_color(raw_color)
            else:
                assigned = False
                name = ""
                raw_color = None
                color = "#64748B"
            scenes.append({
                "index": index,
                "letter": chr(65 + index),
                "name": name,
                "color": color,
                "assigned": assigned,
            })

        if not self._scene_debug_printed:
            for index, snapshot in enumerate(raw_scenes[:8]):
                if isinstance(snapshot, dict):
                    print(f"[Scene Color Debug] index={index} name={snapshot.get('name')!r} color={snapshot.get('color')!r}")
                else:
                    print(f"[Scene Color Debug] index={index} name=None color=None")
            self._scene_debug_printed = True

        active_index = max(0, min(5, int(self._selected_snapshot))) if self._selected_snapshot >= 0 else 0
        active = scenes[active_index]
        self._scenes = scenes
        self._active_scene_letter = active["letter"]
        self._active_scene_color = active["color"] if active["assigned"] else "#64748B"

    # -----------------------------------------------------------------------
    # Qt Properties exposed to QML
    # -----------------------------------------------------------------------
    @Property(bool, notify=connectedChanged)
    def connected(self) -> bool:
        return self._connected

    @Property(str, notify=statusTextChanged)
    def statusText(self) -> str:
        return self._status_text

    @Property(str, notify=activePresetNameChanged)
    def activePresetName(self) -> str:
        name = self._active_preset_name
        if self._preset_changed and self._connected:
            name += " *"
        return name

    @Property(int, notify=activePresetIdChanged)
    def activePresetId(self) -> int:
        return self._active_preset_id

    @Property(bool, notify=presetChangedStatusChanged)
    def presetChanged(self) -> bool:
        return self._preset_changed

    @Property("QVariantList", notify=presetsListChanged)
    def presetsList(self) -> List[Dict[str, Any]]:
        return self._presets_list

    @Property("QVariantList", notify=effectBlocksChanged)
    def effectBlocks(self) -> List[Dict[str, Any]]:
        return self._effect_blocks

    @Property(int, notify=selectedPedalIndexChanged)
    def selectedPedalIndex(self) -> int:
        return self._selected_pedal_index

    @Property("QVariantMap", notify=selectedPedalChanged)
    def selectedPedal(self) -> Dict[str, Any]:
        if 0 <= self._selected_pedal_index < len(self._effect_blocks):
            return self._effect_blocks[self._selected_pedal_index]
        return {}

    @Property("QVariantList", notify=fileOptionsChanged)
    def fileOptions(self) -> List[Dict[str, Any]]:
        return self._file_options

    @Property("QVariantList", notify=laneChanged)
    def lane1(self) -> List[Dict[str, Any]]:
        return self._lane1

    @Property("QVariantList", notify=laneChanged)
    def lane2(self) -> List[Dict[str, Any]]:
        return self._lane2

    @Property("QVariantList", notify=laneChanged)
    def lane3(self) -> List[Dict[str, Any]]:
        return self._lane3

    @Property("QVariantList", notify=laneChanged)
    def lane4(self) -> List[Dict[str, Any]]:
        return self._lane4

    @Property("QVariantList", notify=snapshotsChanged)
    def snapshots(self) -> List[Any]:
        return self._snapshots

    @Property(int, notify=snapshotsChanged)
    def selectedSnapshot(self) -> int:
        return self._selected_snapshot

    @Property("QVariantList", notify=snapshotsChanged)
    def scenes(self) -> List[Dict[str, Any]]:
        return self._scenes

    @Property(str, notify=snapshotsChanged)
    def activeSceneColor(self) -> str:
        return self._active_scene_color

    @Property(str, notify=snapshotsChanged)
    def activeSceneLetter(self) -> str:
        return self._active_scene_letter

    @Property("QVariantList", notify=signalTreeChanged)
    def signalNodes(self) -> List[Dict[str, Any]]:
        return self._signal_nodes

    @Property("QVariantList", notify=signalTreeChanged)
    def signalWires(self) -> List[Dict[str, Any]]:
        return self._signal_wires

    @Property(int, notify=signalTreeChanged)
    def signalCanvasWidth(self) -> int:
        return self._signal_canvas_width

    @Property(int, notify=signalTreeChanged)
    def signalCanvasHeight(self) -> int:
        return self._signal_canvas_height

    @Property("QVariantList", notify=signalTreeChanged)
    def pluginCatalog(self) -> List[Dict[str, Any]]:
        return self._plugin_catalog

    @Property("QVariantMap", notify=presetsListChanged)
    def bankIndex(self) -> Dict[str, Any]:
        return self._bank_index

    @Property("QVariantList", notify=presetsListChanged)
    def bankPresets(self) -> List[Dict[str, Any]]:
        return self._bank_presets

    @Property(str, notify=hostAddressChanged)
    def hostAddress(self) -> str:
        """The PiPedal server address (host)."""
        return self._host_address

    @Property(str, notify=serverVersionChanged)
    def serverVersion(self) -> str:
        """The PiPedal server version string."""
        return self._server_version

    def filtered_plugin_catalog(self, category: str = "ALL", search_text: str = "") -> List[Dict[str, Any]]:
        result = []
        normalized_search = str(search_text or "").strip().lower()
        for plugin in self._plugin_catalog:
            if not isinstance(plugin, dict):
                continue
            plugin_uri = str(plugin.get("uri", ""))
            browser_category = str(plugin.get("browserCategory", "ALL"))
            if category == "★ FAVORITES":
                if plugin_uri not in self._favorites or not self._favorites[plugin_uri]:
                    continue
            elif category != "ALL" and browser_category != category:
                continue
            if normalized_search:
                searchable = f"{plugin.get('name', '')} {plugin_uri}".lower()
                if normalized_search not in searchable:
                    continue
            result.append(plugin)
        return result

    @Property("QVariantMap", notify=presetsListChanged)
    def favorites(self) -> Dict[str, bool]:
        return self._favorites

    # -----------------------------------------------------------------------
    # Slots callable from QML
    # -----------------------------------------------------------------------
    @Slot(int)
    def selectPedal(self, index: int):
        """Select a pedal in the signal chain to show its parameters in the bottom strip."""
        if 0 <= index < len(self._effect_blocks):
            if self._selected_pedal_index != index:
                self._selected_pedal_index = index
                self.selectedPedalIndexChanged.emit(index)
                self.selectedPedalChanged.emit()

    @Slot(int)
    def selectPedalByInstance(self, instance_id: int):
        for index, block in enumerate(self._effect_blocks):
            if int(block.get("instanceId", -1)) == int(instance_id):
                self._selected_pedal_index = index
                self.selectedPedalIndexChanged.emit(index)
                self.selectedPedalChanged.emit()
                return

    @Slot()
    def nextPreset(self):
        """Switch to next preset in the live presets list."""
        if not self._presets_list:
            return
        current_idx = 0
        for i, p in enumerate(self._presets_list):
            if p["id"] == self._active_preset_id:
                current_idx = i
                break
        next_idx = (current_idx + 1) % len(self._presets_list)
        next_id = self._presets_list[next_idx]["id"]
        self.loadPreset(next_id)

    @Slot()
    def prevPreset(self):
        """Switch to previous preset in the live presets list."""
        if not self._presets_list:
            return
        current_idx = 0
        for i, p in enumerate(self._presets_list):
            if p["id"] == self._active_preset_id:
                current_idx = i
                break
        prev_idx = (current_idx - 1 + len(self._presets_list)) % len(self._presets_list)
        prev_id = self._presets_list[prev_idx]["id"]
        self.loadPreset(prev_id)

    @Slot(int)
    def loadPreset(self, preset_id: int):
        """Load a preset on the Pi by its instanceId."""
        logger.info(f"Requested loadPreset({preset_id})")
        self._active_preset_id = preset_id

        # Update local selection immediately for snappy UI
        for p in self._presets_list:
            if p["id"] == preset_id:
                self._active_preset_name = p["name"]
                p["selected"] = True
            else:
                p["selected"] = False

        self._preset_changed = False
        self.activePresetIdChanged.emit(self._active_preset_id)
        self.activePresetNameChanged.emit(self.activePresetName)
        self.presetChangedStatusChanged.emit(self._preset_changed)
        self.presetsListChanged.emit()

        if self._async_client:
            self._async_client.send_load_preset(preset_id)

    @Slot()
    def revertActivePreset(self):
        if self._active_preset_id >= 0:
            self.loadPreset(self._active_preset_id)

    @Slot()
    def saveCurrentPreset(self):
        if self._async_client:
            self._async_client.save_current_preset()

    @Slot(int, str, "QVariantMap", str)
    def requestFileList(self, instance_id: int, property_uri: str, file_property: Dict[str, Any], relative_path: str):
        """Query PiPedal for files belonging to a plugin file property."""
        if self._async_client:
            self._async_client.request_file_list(instance_id, property_uri, file_property, relative_path)

    @Slot(int, str, str)
    def setFileProperty(self, instance_id: int, property_uri: str, value: str):
        """Set a plugin path/patch property to the selected file."""
        if self._async_client:
            self._async_client.send_set_patch_property(instance_id, property_uri, value)

    @Slot(int, int)
    def reorderPedal(self, from_instance_id: int, to_instance_id: int):
        if self._async_client:
            self._async_client.reorder_pedalboard(from_instance_id, to_instance_id)

    @Slot(int)
    def switchSnapshot(self, snapshot_index: int):
        if self._async_client:
            self._async_client.switch_snapshot(snapshot_index)

    @Slot(int)
    def switchScene(self, scene_index: int):
        if self._async_client:
            self._async_client.switch_snapshot(scene_index)

    @Slot(int, str, str)
    def updateScene(self, scene_index: int, name: str, color: str):
        if self._async_client:
            self._async_client.update_snapshot(scene_index, str(name), str(color))

    @Slot(int, int)
    def swapScenes(self, first_index: int, second_index: int):
        if self._async_client:
            self._async_client.swap_snapshots(first_index, second_index)

    @Slot(int, int)
    def copyScene(self, source_index: int, target_index: int):
        if self._async_client:
            self._async_client.copy_snapshot(source_index, target_index)

    @Slot(int)
    def saveScene(self, scene_index: int):
        if self._async_client:
            self._async_client.save_snapshot(scene_index)

    @Slot(int)
    def openBank(self, bank_id: int):
        if self._async_client:
            self._async_client.open_bank(bank_id)

    @Slot(int)
    def requestBankPresets(self, bank_id: int):
        if self._async_client:
            self._async_client.request_bank_presets(bank_id)

    @Slot(str, int)
    def insertPlugin(self, plugin_uri: str, insertion_index: int):
        if self._async_client:
            self._async_client.insert_plugin(plugin_uri, insertion_index)

    @Slot(int)
    def deleteBlock(self, instance_id: int):
        if self._async_client:
            self._async_client.delete_block(instance_id)

    @Slot(int, int)
    def moveBlock(self, instance_id: int, direction: int):
        if self._async_client:
            self._async_client.move_block(instance_id, direction)

    @Slot(int, str, int)
    def dropBlock(self, instance_id: int, target_chain_id: str, target_index: int):
        if self._async_client:
            self._async_client.drop_block(int(instance_id), str(target_chain_id), int(target_index))

    @Slot(str, bool)
    def setFavorite(self, plugin_uri: str, is_favorite: bool):
        favorites = dict(self._favorites)
        if is_favorite:
            favorites[plugin_uri] = True
        else:
            favorites.pop(plugin_uri, None)
        self._favorites = favorites
        if self._async_client:
            self._async_client.set_favorites(favorites)

    @Slot(str, str, result="QVariantList")
    def filteredPluginCatalog(self, category: str, search_text: str) -> List[Dict[str, Any]]:
        return self.filtered_plugin_catalog(category, search_text)

    @Slot(int, str)
    def beginControlDrag(self, instance_id: int, symbol: str):
        self._dragging_control = (int(instance_id), str(symbol))

    @Slot()
    def endControlDrag(self):
        self._dragging_control = None

    @Slot(int)
    def toggleBypass(self, instance_id: int):
        """Toggle the bypass state of an effect block on the Pi."""
        inst_id = int(instance_id)
        target_item = None
        for block in self._effect_blocks:
            if int(block.get("instanceId", -1)) == inst_id:
                target_item = block
                break
        if target_item:
            new_enabled = not bool(target_item.get("enabled", True))
            target_item["enabled"] = new_enabled
            target_item["bypassed"] = not new_enabled
            target_item["active"] = new_enabled
            if self._async_client and isinstance(self._async_client.raw_pedalboard, dict):
                def _set_pb_enabled(lst):
                    for it in lst:
                        if not isinstance(it, dict):
                            continue
                        if int(it.get("instanceId", -1)) == inst_id:
                            it["isEnabled"] = new_enabled
                            return True
                        if _set_pb_enabled(it.get("topChain", []) or []) or _set_pb_enabled(it.get("bottomChain", []) or []):
                            return True
                    return False
                _set_pb_enabled(self._async_client.raw_pedalboard.get("items", []) or [])
            self.effectBlocksChanged.emit()
            if hasattr(self, '_signal_nodes') and isinstance(self._signal_nodes, list):
                new_nodes = []
                for nd in self._signal_nodes:
                    if isinstance(nd, dict) and int(nd.get("instanceId", -1)) == inst_id:
                        nd_copy = dict(nd)
                        nd_copy["bypassed"] = not new_enabled
                        nd_copy["enabled"] = new_enabled
                        new_nodes.append(nd_copy)
                    else:
                        new_nodes.append(nd)
                self._signal_nodes = new_nodes
                if hasattr(self, 'signalTreeChanged'):
                    self.signalTreeChanged.emit()
            self.selectedPedalChanged.emit()
            if self._async_client:
                self._async_client.send_set_item_enabled(inst_id, new_enabled)

    @Slot(int, str, float)
    def setParameter(self, instance_id: int, symbol: str, value: float):
        """Update a control parameter value on the Pi."""
        for block in self._effect_blocks:
            if block["instanceId"] == instance_id:
                for ctrl in block.get("controls", []):
                    if ctrl["symbol"] == symbol:
                        ctrl["value"] = value
                        break
                break

        if self._dragging_control != (int(instance_id), str(symbol)):
            self.selectedPedalChanged.emit()

        if self._async_client:
            self._async_client.send_set_control(instance_id, symbol, value)

    # -----------------------------------------------------------------------
    # Internal Handlers on Main Qt Thread
    # -----------------------------------------------------------------------
    def _handle_conn_state(self, is_connected: bool, text: str):
        self._connected = is_connected
        self._status_text = text
        if not is_connected:
            self._active_preset_name = "Connecting..."
        self.connectedChanged.emit(self._connected)
        self.statusTextChanged.emit(self._status_text)
        self.activePresetNameChanged.emit(self.activePresetName)

    def _handle_full_state_updated(self):
        # Clamp selected pedal index to valid range
        if len(self._effect_blocks) == 0:
            self._selected_pedal_index = 0
        elif self._selected_pedal_index >= len(self._effect_blocks):
            self._selected_pedal_index = max(0, len(self._effect_blocks) - 1)

        self.activePresetIdChanged.emit(self._active_preset_id)
        self.activePresetNameChanged.emit(self.activePresetName)
        self.presetChangedStatusChanged.emit(self._preset_changed)
        self.presetsListChanged.emit()
        self.laneChanged.emit()
        self.signalTreeChanged.emit()
        self.effectBlocksChanged.emit()
        self.selectedPedalIndexChanged.emit(self._selected_pedal_index)
        self.selectedPedalChanged.emit()

    def _handle_preset_list_updated(self):
        self.activePresetIdChanged.emit(self._active_preset_id)
        self.activePresetNameChanged.emit(self.activePresetName)
        self.presetsListChanged.emit()

    def _handle_control_value_changed(self, instance_id: int, symbol: str, value: float):
        if self._dragging_control == (int(instance_id), str(symbol)):
            return
        for block in self._effect_blocks:
            if block["instanceId"] == instance_id:
                for ctrl in block.get("controls", []):
                    if ctrl["symbol"] == symbol:
                        ctrl["value"] = value
                        break
                break
        self.selectedPedalChanged.emit()

    def _handle_item_enabled_changed(self, instance_id: int, enabled: bool):
        for block in self._effect_blocks:
            if block["instanceId"] == instance_id:
                block["enabled"] = enabled
                block["bypassed"] = not enabled
                break
        self.effectBlocksChanged.emit()
        if hasattr(self, '_signal_nodes') and isinstance(self._signal_nodes, list):
            new_nodes = []
            for nd in self._signal_nodes:
                if isinstance(nd, dict) and int(nd.get("instanceId", -1)) == int(instance_id):
                    nd_copy = dict(nd)
                    nd_copy["bypassed"] = not bool(enabled)
                    nd_copy["enabled"] = bool(enabled)
                    new_nodes.append(nd_copy)
                else:
                    new_nodes.append(nd)
            self._signal_nodes = new_nodes
            if hasattr(self, 'signalTreeChanged'):
                self.signalTreeChanged.emit()
        self.selectedPedalChanged.emit()

    def _handle_snapshot_selected(self, snapshot_index: int):
        self._selected_snapshot = int(snapshot_index)
        self._refresh_scenes()
        self.snapshotsChanged.emit()

    def _handle_preset_changed_flag(self, changed: bool):
        self._preset_changed = changed
        self.presetChangedStatusChanged.emit(changed)
        self.activePresetNameChanged.emit(self.activePresetName)

    def _handle_file_list_updated(self, instance_id: int, property_uri: str, files: List[Any]):
        options = []
        for entry in files:
            if isinstance(entry, str):
                name = entry
                path = entry
                is_directory = False
            elif isinstance(entry, dict):
                name = entry.get("displayName") or entry.get("name") or entry.get("pathname") or entry.get("path") or entry.get("filename") or ""
                path = entry.get("path") or entry.get("pathname") or entry.get("name") or name
                is_directory = bool(entry.get("isDirectory", entry.get("is_directory", False)))
            else:
                name = str(entry)
                path = name
                is_directory = False
            if name:
                options.append({"name": name, "path": path, "isDirectory": is_directory})
        self._file_options_instance_id = instance_id
        self._file_options_property_uri = property_uri
        self._file_options = options
        self.fileOptionsChanged.emit()

    def _handle_path_property_changed(self, instance_id: int, property_uri: str, atom_json: str):
        for block in self._effect_blocks:
            if block["instanceId"] == instance_id:
                path_properties = block.setdefault("pathProperties", {})
                path_properties[property_uri] = atom_json
                break
        self.selectedPedalChanged.emit()


# Module-level constants for tree helpers
SPLIT_URI = "uri://two-play/pipedal/pedalboard#Split"
EMPTY_URI = "uri://two-play/pipedal/pedalboard#Empty"
BLOCK_H = 68.0
INNER_GAP = 26.0

# ---------------------------------------------------------------------------
# PiPedalAsyncClient (Runs in background daemon thread)
# ---------------------------------------------------------------------------
class PiPedalAsyncClient:
    """
    Asynchronous WebSocket client managing direct connection to ws://{PI_HOST}:{PI_PORT},
    concurrent message listening, exact JSON-RPC handshake, and live traffic logging.
    """

    def __init__(self, bridge: PiPedalBridge, host: str = config.PI_HOST, port: int = config.PI_PORT):
        self.bridge = bridge
        self.host = host
        self.port = port

        self.loop: Optional[asyncio.AbstractEventLoop] = None
        self.thread: Optional[threading.Thread] = None
        self.ws: Optional[websockets.WebSocketClientProtocol] = None

        self.client_id: int = -1
        self.req_id: int = 0
        self.pending_requests: Dict[int, asyncio.Future] = {}

        self.ui_plugins_by_uri: Dict[str, Dict[str, Any]] = {}
        self.is_running: bool = False
        self.raw_pedalboard: Dict[str, Any] = {}

    def start(self):
        """Start the background asyncio client thread."""
        self.is_running = True
        self.thread = threading.Thread(target=self._thread_entry, daemon=True, name="PiPedalAsyncThread")
        self.thread.start()

    def stop(self):
        """Stop the background client."""
        self.is_running = False
        if self.loop and self.loop.is_running():
            self.loop.call_soon_threadsafe(self.loop.stop)

    def _thread_entry(self):
        """Entry point for the background daemon thread."""
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)
        self.loop.run_until_complete(self._main_reconnect_loop())

    async def _main_reconnect_loop(self):
        """Connect directly to ws://{self.host}:{self.port} with exponential backoff."""
        retry_delay = 1.0
        target_url = f"ws://{self.host}:{self.port}/pipedal"

        while self.is_running:
            try:
                print(f"[PiPedal] Connecting directly to {target_url}...")
                self.bridge.emit_connection_state(False, f"Connecting to {self.host}:{self.port}...")

                async with websockets.connect(
                    target_url,
                    open_timeout=5.0,
                    ping_interval=20,
                    ping_timeout=20,
                    max_size=None,
                ) as ws:
                    self.ws = ws
                    print(f"[PiPedal] Connected to {target_url}!")
                    self.bridge.emit_connection_state(True, "Online")
                    retry_delay = 1.0

                    # Start concurrent receive loop task FIRST so all replies/pushes are processed!
                    receive_task = asyncio.create_task(self._receive_loop(ws))
                    try:
                        # Perform the exact PiPedal handshake
                        await self._perform_handshake()
                        # Wait until connection ends
                        await receive_task
                    except asyncio.CancelledError:
                        pass
                    finally:
                        if not receive_task.done():
                            receive_task.cancel()
                            try:
                                await receive_task
                            except (asyncio.CancelledError, Exception):
                                pass

            except (websockets.exceptions.ConnectionClosed, OSError, asyncio.TimeoutError) as e:
                print(f"[PiPedal] Connection to {target_url} disconnected/failed: {e!r}")
            except Exception as e:
                logger.error(f"Unexpected connection error: {e!r}", exc_info=True)
            finally:
                self.ws = None
                # Reject any remaining pending requests
                for fut in list(self.pending_requests.values()):
                    if not fut.done():
                        fut.set_exception(ConnectionError("WebSocket connection lost"))
                self.pending_requests.clear()

            self.bridge.emit_connection_state(False, "Offline (Retrying...)")
            await asyncio.sleep(retry_delay)
            retry_delay = min(5.0, retry_delay * 1.5)

    # -----------------------------------------------------------------------
    # Concurrent Receive Loop & Live Traffic Logging
    # -----------------------------------------------------------------------
    async def _receive_loop(self, ws: websockets.WebSocketClientProtocol):
        """Continuously receive messages from WebSocket and dispatch them."""
        try:
            async for raw_msg in ws:
                if isinstance(raw_msg, bytes):
                    raw_msg = raw_msg.decode("utf-8", errors="replace")

                if not sys.platform.startswith("linux"):
                    rx_preview = raw_msg[:300] + ("..." if len(raw_msg) > 300 else "")
                    print(f"[WS RX] {rx_preview}")

                await self._handle_raw_message(raw_msg)
        except Exception as e:
            print(f"[PiPedal Error] {e!r}")

    async def _handle_raw_message(self, raw_msg: str):
        """Process incoming JSON messages from PiPedal WebSocket."""
        try:
            data = json.loads(raw_msg)
            if not isinstance(data, list) or len(data) == 0:
                return

            header = data[0]
            body = data[1] if len(data) > 1 else None

            # 1. Is this a reply to an RPC request?
            reply_code = header.get("reply")
            if reply_code is not None:
                fut = self.pending_requests.pop(reply_code, None)
                if fut and not fut.done():
                    if header.get("message") == "error":
                        fut.set_exception(RuntimeError(f"PiPedal RPC Error: {body}"))
                    else:
                        fut.set_result(body)
                return

            # 2. Check if server requested a reply on this notification
            reply_to = header.get("replyTo")
            if reply_to is not None and reply_to != -1:
                reply_msg = [{"reply": reply_to, "message": header.get("message", "")}, True]
                reply_json = json.dumps(reply_msg)
                if not sys.platform.startswith("linux"):
                    tx_preview = reply_json[:300] + ("..." if len(reply_json) > 300 else "")
                    print(f"[WS TX REPLY] {tx_preview}")
                if self.ws:
                    await self.ws.send(reply_json)

            # 3. Server Push Notifications
            message_type = header.get("message")

            if message_type == "onPedalboardChanged":
                pb = body.get("pedalboard") if isinstance(body, dict) else body
                if pb:
                    self._update_bridge_from_pedalboard(pb)

            elif message_type == "onPresetsChanged":
                presets_obj = body.get("presets") if isinstance(body, dict) else body
                if presets_obj:
                    self._update_bridge_from_presets(presets_obj)

            elif message_type == "onPresetChanged":
                self.bridge._sig_preset_changed_flag.emit(bool(body))

            elif message_type == "onBanksChanged":
                if isinstance(body, dict):
                    entries = body.get("entries", body.get("banks", []))
                    self.bridge._bank_index = {
                        "selectedBank": body.get("selectedBank", -1),
                        "banks": entries if isinstance(entries, list) else [],
                    }
                    self.bridge.presetsListChanged.emit()

            elif message_type == "onPluginPresetsChanged":
                plugin_uri = body if isinstance(body, str) else ""
                print(f"[PiPedal] Plugin presets changed: {plugin_uri}")

            elif message_type == "onFavoritesChanged":
                if isinstance(body, dict):
                    self.bridge._favorites = {str(k): bool(v) for k, v in body.items() if v}
                    self.bridge.presetsListChanged.emit()

            elif message_type == "onLv2PluginsChanging":
                print("[PiPedal] LV2 plugins are changing; server will reload")
                self.bridge.emit_connection_state(False, "Reloading LV2 Plugins...")

            elif message_type in ("onPluginLoaded", "onPluginsChanged", "onLv2PluginsChanged"):
                print(f"[PiPedal] Plugin catalog changed: {message_type}")
                asyncio.create_task(self._reload_plugin_catalog())

            elif message_type == "onSelectedSnapshotChanged":
                if body is not None:
                    self.bridge._sig_snapshot_selected.emit(int(body))

            elif message_type == "onNotifyPathPatchPropertyChanged":
                inst_id = body.get("instanceId") if isinstance(body, dict) else None
                property_uri = body.get("propertyUri") if isinstance(body, dict) else None
                atom_json = body.get("atomJson") if isinstance(body, dict) else None
                if inst_id is not None and property_uri is not None:
                    atom_text = atom_json if isinstance(atom_json, str) else json.dumps(atom_json)
                    self.bridge._sig_path_property_changed.emit(int(inst_id), str(property_uri), atom_text)

            elif message_type == "onItemEnabledChanged":
                inst_id = body.get("instanceId")
                enabled = body.get("enabled")
                if inst_id is not None and enabled is not None:
                    self.bridge._sig_item_enabled_changed.emit(int(inst_id), bool(enabled))

            elif message_type == "onControlChanged":
                inst_id = body.get("instanceId")
                symbol = body.get("symbol")
                value = body.get("value")
                if inst_id is not None and symbol is not None and value is not None:
                    self.bridge._sig_control_value_changed.emit(int(inst_id), str(symbol), float(value))

        except Exception as e:
            logger.error(f"Error handling message: {e}", exc_info=True)

    # -----------------------------------------------------------------------
    # JSON-RPC Handshake & Live State Loading
    # -----------------------------------------------------------------------
    async def _reload_plugin_catalog(self):
        try:
            plugins_list = await self.request("plugins")
            if isinstance(plugins_list, list):
                self.ui_plugins_by_uri = {p["uri"]: p for p in plugins_list if isinstance(p, dict) and "uri" in p}
                catalog = []
                for entry in plugins_list:
                    if not isinstance(entry, dict):
                        continue
                    combined = " ".join([str(entry.get("plugin_type", "")), str(entry.get("name", "")), str(entry.get("uri", ""))]).lower()
                    if "split" in combined: category = "SPLITTER"
                    elif any(k in combined for k in ("cab", "impulse", " ir ")): category = "CAB"
                    elif any(k in combined for k in ("nam", "amplifier", "simulator", "amp")): category = "AMP"
                    elif any(k in combined for k in ("distortion", "waveshaper", "overdrive", "drive", "fuzz")): category = "DRIVE"
                    elif any(k in combined for k in ("chorus", "flanger", "phaser", "modulator", "pitch")): category = "MOD"
                    elif any(k in combined for k in ("delay", "echo", "comb")): category = "DELAY"
                    elif any(k in combined for k in ("reverb", "spatial")): category = "REVERB"
                    elif any(k in combined for k in ("eq", "filter", "compressor", "limiter", "gate", "dynamics", "expander")): category = "EQ / DYN"
                    else: category = "ALL"
                    copy_entry = dict(entry)
                    copy_entry["browserCategory"] = category
                    catalog.append(copy_entry)
                self.bridge._plugin_catalog = catalog
                if isinstance(self.raw_pedalboard, dict):
                    self._update_bridge_from_pedalboard(self.raw_pedalboard)
                print(f"[PiPedal] Reloaded {len(self.ui_plugins_by_uri)} plugins")
        except Exception as e:
            print(f"[PiPedal Error] {e!r}")

    async def _perform_handshake(self):
        """Execute exact PiPedal handshake matching PiPedalModel.tsx."""
        self.req_id = 0
        self.pending_requests.clear()

        # 1. Hello -> returns clientId (int) and registers client as subscriber on server
        self.client_id = await self.request("hello")
        print(f"[PiPedal] Handshake 'hello' OK -> clientId={self.client_id}")

        # 2. Server Version
        version_info = await self.request("version")
        if isinstance(version_info, dict):
            server_version = str(version_info.get("serverVersion", "Unknown"))
            print(f"[PiPedal] Version: {server_version}")
            self.bridge._server_version = server_version
            self.bridge.serverVersionChanged.emit()
        else:
            print(f"[PiPedal] Version: {version_info}")

        # 3. LV2 Plugins Catalog
        plugins_list = await self.request("plugins")
        if not isinstance(plugins_list, list):
            raise RuntimeError(f"Invalid plugins response: {plugins_list!r}")
        self.ui_plugins_by_uri = {p["uri"]: p for p in plugins_list if isinstance(p, dict) and "uri" in p}
        catalog = []
        for plugin in plugins_list:
            if not isinstance(plugin, dict):
                continue
            entry = dict(plugin)
            combined = " ".join([
                str(entry.get("plugin_type", "")),
                str(entry.get("name", "")),
                str(entry.get("uri", "")),
                str(entry.get("plugin_display_type", "")),
            ]).lower()
            if "split" in combined:
                category = "SPLITTER"
            elif any(k in combined for k in ("cab", "impulse", "ir")):
                category = "CAB"
            elif any(k in combined for k in ("nam", "amplifier", "simulator", "amp")):
                category = "AMP"
            elif any(k in combined for k in ("distortion", "waveshaper", "overdrive", "drive", "fuzz")):
                category = "DRIVE"
            elif any(k in combined for k in ("chorus", "flanger", "phaser", "modulator", "pitch", "tremolo", "vibrato")):
                category = "MOD"
            elif any(k in combined for k in ("delay", "echo", "comb")):
                category = "DELAY"
            elif any(k in combined for k in ("reverb", "spatial")):
                category = "REVERB"
            elif any(k in combined for k in ("eq", "equalizer", "filter", "compressor", "limiter", "gate", "dynamics", "expander")):
                category = "EQ / DYN"
            else:
                category = "ALL"
            entry["browserCategory"] = category
            catalog.append(entry)
            if entry["uri"] in self.ui_plugins_by_uri:
                self.ui_plugins_by_uri[entry["uri"]] = entry
        self.bridge._plugin_catalog = catalog
        print(f"[PiPedal] Loaded {len(self.ui_plugins_by_uri)} plugins metadata")

        # 4. Current Pedalboard (Live effect blocks and active values)
        pedalboard = await self.request("currentPedalboard")
        self._update_bridge_from_pedalboard(pedalboard)

        # 5. Plugin classes
        plugin_classes = await self.request("pluginClasses")
        print(f"[PiPedal] Loaded plugin classes: {type(plugin_classes).__name__}")

        # 6. Preset Index (Presets & active preset)
        presets_index = await self.request("getPresets")
        self._update_bridge_from_presets(presets_index)

        # 7. Optional bank index
        try:
            bank_index = await self.request("getBankIndex")
            if isinstance(bank_index, dict):
                entries = bank_index.get("entries", bank_index.get("banks", []))
                self.bridge._bank_index = {
                    "selectedBank": bank_index.get("selectedBank", -1),
                    "banks": entries if isinstance(entries, list) else [],
                }
                self.bridge.presetsListChanged.emit()
        except Exception:
            pass

        # 8. Favorites
        try:
            favorites = await self.request("getFavorites")
            if isinstance(favorites, dict):
                self.bridge._favorites = {str(k): bool(v) for k, v in favorites.items() if v}
                self.bridge.presetsListChanged.emit()
        except Exception:
            pass

    def _update_bridge_from_presets(self, presets_index: Dict[str, Any]):
        """Update preset index and active preset name on the bridge."""
        if not isinstance(presets_index, dict):
            return
        presets = presets_index.get("presets", [])
        active_id = presets_index.get("selectedInstanceId", -1)
        preset_changed = presets_index.get("presetChanged", False)

        bridge_presets = []
        active_name = "Untitled Preset"
        for p in presets:
            p_id = p.get("instanceId", 0)
            p_name = p.get("name", "Preset")
            is_sel = p_id == active_id
            if is_sel:
                active_name = p_name
            bridge_presets.append({"id": p_id, "name": p_name, "selected": is_sel})

        self.bridge._presets_list = bridge_presets
        self.bridge._active_preset_id = active_id
        self.bridge._active_preset_name = active_name
        self.bridge._preset_changed = preset_changed
        self.bridge._sig_preset_list_updated.emit()
        print(f"[PiPedal] Active Preset: '{active_name}' ({len(bridge_presets)} total presets)")

    def _update_bridge_from_pedalboard(self, pedalboard: Dict[str, Any]):
        """Convert live PiPedal pedalboard items into flattened effectBlocks for QML."""
        if not isinstance(pedalboard, dict):
            return
        self.raw_pedalboard = pedalboard
        self.bridge._snapshot_values = pedalboard.get("snapshots", []) or []
        self.bridge._snapshots = self.bridge._snapshot_values
        self.bridge._sig_snapshot_selected.emit(int(pedalboard.get("selectedSnapshot", -1)))
        raw_items = pedalboard.get("items", [])
        flattened_items = self._flatten_pedalboard_items(raw_items)

        bridge_effects = []
        for item in flattened_items:
            uri = item.get("uri", "")
            plugin_def = self.ui_plugins_by_uri.get(uri, {})

            name = item.get("title") or plugin_def.get("name") or item.get("pluginName") or "Effect"
            plugin_type = plugin_def.get("plugin_type", "")
            cat_name, cat_color = classify_plugin(plugin_type, name, uri)

            # Map parameter values from pedalboard item
            control_val_map = {cv.get("key"): cv.get("value") for cv in item.get("controlValues", [])}

            if not hasattr(self, "_cached_ctrl_templates"):
                self._cached_ctrl_templates = {}
            templates = self._cached_ctrl_templates.get(uri)
            if templates is None:
                templates = []
                for ctrl in plugin_def.get("controls", []):
                    if ctrl.get("is_bypass", False) or ctrl.get("not_on_gui", False):
                        continue
                    symbol = ctrl.get("symbol", "")
                    ctrl_name = str(ctrl.get("name") or symbol or "Control")
                    min_v = float(ctrl.get("min_value", 0.0))
                    max_v = float(ctrl.get("max_value", 1.0))
                    def_v = float(ctrl.get("default_value", (min_v + max_v) / 2.0))
                    range_steps = int(ctrl.get("range_steps", 0) or 0)
                    is_integer = bool(ctrl.get("integer_property", False)) or range_steps == 1
                    is_log = bool(ctrl.get("is_logarithmic", False))
                    is_toggle = bool(ctrl.get("toggled_property", False))
                    is_enum = bool(ctrl.get("enumeration_property", False))
                    scale_points = [
                        {
                            "value": float(sp.get("value", 0.0)),
                            "label": str(sp.get("label") or sp.get("value", "") or "Option"),
                        }
                        for sp in ctrl.get("scale_points", [])
                    ]
                    if len(scale_points) > 0:
                        is_enum = True
                    if not is_toggle and min_v == 0.0 and max_v == 1.0 and is_integer and len(scale_points) <= 2:
                        is_toggle = True
                    unit_str = str(format_control_units(ctrl.get("custom_units") or ctrl.get("units") or ""))
                    templates.append({
                        "symbol": symbol,
                        "name": ctrl_name,
                        "min": min_v,
                        "max": max_v,
                        "default": def_v,
                        "isLogarithmic": is_log,
                        "isInteger": is_integer,
                        "isEnum": is_enum,
                        "isToggle": is_toggle,
                        "scalePoints": scale_points,
                        "unit": unit_str,
                    })
                self._cached_ctrl_templates[uri] = templates

            controls = [
                {**tpl, "value": float(control_val_map.get(tpl["symbol"], tpl["default"]))}
                for tpl in templates
            ]

            enabled = bool(item.get("isEnabled", True))
            file_properties = []
            path_properties = item.get("pathProperties", {}) if isinstance(item.get("pathProperties"), dict) else {}
            for file_property in plugin_def.get("fileProperties", []):
                if not isinstance(file_property, dict):
                    continue
                property_uri = file_property.get("patchProperty", "")
                if not property_uri:
                    continue
                raw_value = path_properties.get(property_uri)
                display_value = ""
                if isinstance(raw_value, str) and raw_value:
                    try:
                        parsed = json.loads(raw_value)
                        if isinstance(parsed, str):
                            display_value = parsed
                        elif isinstance(parsed, dict):
                            display_value = str(parsed.get("path") or parsed.get("name") or "")
                    except Exception:
                        display_value = raw_value
                file_properties.append({
                    "label": file_property.get("label", "File"),
                    "patchProperty": property_uri,
                    "directory": file_property.get("directory", ""),
                    "resourceDirectory": file_property.get("resourceDirectory", ""),
                    "fileTypes": file_property.get("fileTypes", []),
                    "modDirectories": file_property.get("modDirectories", []),
                    "useLegacyModDirectory": bool(file_property.get("useLegacyModDirectory", False)),
                    "index": file_property.get("index", -1),
                    "portGroup": file_property.get("portGroup", ""),
                    "value": display_value,
                })
            bridge_effects.append(
                {
                    "instanceId": item.get("instanceId", -1),
                    "name": name,
                    "uri": uri,
                    "category": cat_name,
                    "color": cat_color,
                    "enabled": enabled,
                    "bypassed": not enabled,
                    "controls": controls,
                    "fileProperties": file_properties,
                    "pathProperties": path_properties,
                }
            )

        self.bridge._effect_blocks = bridge_effects
        effect_by_id = {int(effect["instanceId"]): effect for effect in bridge_effects}

        lane1, lane2, lane3, lane4 = [], [], [], []
        split_seen = False
        for top_item in raw_items:
            uri = top_item.get("uri", "")
            if uri == "uri://two-play/pipedal/pedalboard#Split":
                split_seen = True
                lane2.extend(
                    effect_by_id[int(v.get("instanceId", -1))]
                    for v in top_item.get("topChain", [])
                    if int(v.get("instanceId", -1)) in effect_by_id
                )
                lane4.extend(
                    effect_by_id[int(v.get("instanceId", -1))]
                    for v in top_item.get("bottomChain", [])
                    if int(v.get("instanceId", -1)) in effect_by_id
                )
            elif split_seen:
                lane3.extend(
                    [effect_by_id[int(top_item.get("instanceId", -1))]]
                    if int(top_item.get("instanceId", -1)) in effect_by_id
                    else []
                )
            else:
                lane1.extend(
                    [effect_by_id[int(top_item.get("instanceId", -1))]]
                    if int(top_item.get("instanceId", -1)) in effect_by_id
                    else []
                )

        self.bridge._lane1 = lane1[:8]
        self.bridge._lane2 = lane2[:8]
        self.bridge._lane3 = lane3[:8]
        self.bridge._lane4 = lane4[:8]
        self._build_recursive_signal_tree(raw_items, effect_by_id)
        self.bridge._sig_full_state_updated.emit()
        if not sys.platform.startswith("linux"):
            print(f"[PiPedal] Loaded {len(bridge_effects)} live effect blocks onto pedalboard")

    def _build_recursive_signal_tree(self, items: List[Dict[str, Any]], effect_by_id: Dict[int, Dict[str, Any]]):
        def _exact_extent(chain_list):
            if not chain_list:
                return (34.0, 34.0)
            max_a, max_b = 34.0, 34.0
            for sub in chain_list:
                if isinstance(sub, dict) and sub.get("uri") == SPLIT_URI:
                    ta, tb = _exact_extent(sub.get("topChain", []) or [])
                    ba, bb = _exact_extent(sub.get("bottomChain", []) or [])
                    to = tb + 14.0
                    bo = ba + 14.0
                    max_a = max(max_a, to + ta)
                    max_b = max(max_b, bo + bb)
            return (max_a, max_b)
        """Lay out the pedalboard signal tree as a pure recursive chain of nodes/wires.

        Geometry is produced exactly once here; QML only paints the resulting
        polylines with rounded corners.
        """
        nodes: List[Dict[str, Any]] = []
        wires: List[Dict[str, Any]] = []

        # ---- Layout constants -------------------------------------------------
        BLOCK_W = 68
        BLOCK_H = 68
        CAP_W = 46
        CAP_H = 74
        SPLIT_D = 42
        H_GAP = 26
        FORK_LEAD = 20
        BRANCH_PAD = 24
        V_GAP = 92
        ADD_H = 44
        INNER_GAP = 26.0

        SPLIT_URI = "uri://two-play/pipedal/pedalboard#Split"
        EMPTY_URI = "uri://two-play/pipedal/pedalboard#Empty"

        # ---- Collect split blocks so the parameter editor can select them ----
        def collect_splits(chain: List[Dict[str, Any]]) -> None:
            for item in chain:
                if item.get("uri") != SPLIT_URI:
                    continue
                inst_id = int(item.get("instanceId", -1))
                cv_map = {cv.get("key"): float(cv.get("value", 0.0)) for cv in item.get("controlValues", [])}
                split_type = cv_map.get("splitType", 0.0)
                if split_type < 1:
                    mode, label = "A/B", "A"
                elif split_type < 2:
                    mode, label = "MIX", "MIX"
                else:
                    mode, label = "L/R", "L/R"

                controls = [
                    {"symbol": "splitType", "name": "Type", "value": split_type, "min": 0.0, "max": 2.0, "default": 0.0, "isInteger": True, "isEnum": True, "isToggle": False, "scalePoints": [{"value": 0.0, "label": "A/B"}, {"value": 1.0, "label": "MIX"}, {"value": 2.0, "label": "L/R"}], "unit": ""},
                    {"symbol": "select", "name": "Select", "value": cv_map.get("select", 0.0), "min": 0.0, "max": 1.0, "default": 0.0, "isInteger": True, "isEnum": True, "isToggle": True, "scalePoints": [{"value": 0.0, "label": "A"}, {"value": 1.0, "label": "B"}], "unit": ""},
                    {"symbol": "mix", "name": "Mix", "value": cv_map.get("mix", 0.0), "min": -1.0, "max": 1.0, "default": 0.0, "isInteger": False, "isEnum": False, "isToggle": False, "scalePoints": [], "unit": ""},
                    {"symbol": "panL", "name": "Pan Top", "value": cv_map.get("panL", 0.0), "min": -1.0, "max": 1.0, "default": 0.0, "isInteger": False, "isEnum": False, "isToggle": False, "scalePoints": [], "unit": ""},
                    {"symbol": "volL", "name": "Vol Top", "value": cv_map.get("volL", 0.0), "min": -60.0, "max": 12.0, "default": 0.0, "isInteger": False, "isEnum": False, "isToggle": False, "scalePoints": [], "unit": "dB"},
                    {"symbol": "panR", "name": "Pan Bottom", "value": cv_map.get("panR", 0.0), "min": -1.0, "max": 1.0, "default": 0.0, "isInteger": False, "isEnum": False, "isToggle": False, "scalePoints": [], "unit": ""},
                    {"symbol": "volR", "name": "Vol Bottom", "value": cv_map.get("volR", 0.0), "min": -60.0, "max": 12.0, "default": 0.0, "isInteger": False, "isEnum": False, "isToggle": False, "scalePoints": [], "unit": "dB"},
                ]
                block = {
                    "instanceId": inst_id,
                    "name": "Signal Split",
                    "uri": item.get("uri", ""),
                    "category": "SIGNAL SPLITTER",
                    "color": "#94A3B8",
                    "enabled": bool(item.get("isEnabled", True)),
                    "bypassed": not bool(item.get("isEnabled", True)),
                    "controls": controls,
                    "fileProperties": [],
                    "pathProperties": {},
                    "splitMode": mode,
                    "splitLabel": label,
                }
                if inst_id not in effect_by_id:
                    effect_by_id[inst_id] = block
                collect_splits(item.get("topChain", []) or [])
                collect_splits(item.get("bottomChain", []) or [])

        collect_splits(items)
        for split_id, block in effect_by_id.items():
            if block.get("category") == "SIGNAL SPLITTER" and all(
                existing.get("instanceId") != split_id for existing in self.bridge._effect_blocks
            ):
                self.bridge._effect_blocks.append(block)

        # ---- Recursive measure helper for nested splits ------------------------
        def measure_chain(items_to_measure: List[Dict[str, Any]]) -> Tuple[float, float]:
            """Measure top and bottom extents of a chain.

            Returns (top_extent, bot_extent) where each extent is the vertical space
            from the split node to the end of that branch.
            Normal effect blocks contribute (BLOCK_H / 2.0, BLOCK_H / 2.0).
            Empty chains return (BLOCK_H / 2.0, BLOCK_H / 2.0).

            For PedalboardSplitItem:
                top_t, top_b = measure_chain(item.get("topChain", []) or [])
                bot_t, bot_b = measure_chain(item.get("bottomChain", []) or [])
                top_offset = top_b + INNER_GAP / 2.0
                bot_offset = bot_t + INNER_GAP / 2.0
                return (top_t + top_offset, bot_b + bot_offset)
            """
            # Empty chains return equal extents
            if not items_to_measure:
                return (BLOCK_H / 2.0, BLOCK_H / 2.0)

            top_extent = 0.0
            bot_extent = 0.0

            for item in items_to_measure:
                uri = item.get("uri", "")
                inst_id = int(item.get("instanceId", -1))

                if inst_id < 0 or uri == EMPTY_URI:
                    continue

                if uri == SPLIT_URI:
                    # Recursively measure nested split branches
                    top_t, top_b = measure_chain(item.get("topChain", []) or [])
                    bot_t, bot_b = measure_chain(item.get("bottomChain", []) or [])

                    top_offset = top_b + INNER_GAP / 2.0
                    bot_offset = bot_t + INNER_GAP / 2.0

                    return (top_t + top_offset, bot_b + bot_offset)
                else:
                    # Normal effect block returns equal extents
                    return (BLOCK_H / 2.0, BLOCK_H / 2.0)

            # If no valid items found, return empty extents
            return (0.0, 0.0)

        # ---- Helpers ----------------------------------------------------------
        next_slot_id = [-1000]

        def add_slot(start_x: float, end_x: float, center_y: float, chain_id: int, index: int) -> None:
            """Invisible tap target covering an open span of wire."""
            width = max(int(round(end_x - start_x)), 20)
            nodes.append({
                "instanceId": next_slot_id[0],
                "kind": "addSlot",
                "x": int(round(start_x)),
                "y": int(round(center_y - ADD_H / 2.0)),
                "width": width,
                "height": ADD_H,
                "label": "",
                "active": True,
                "chainId": str(chain_id),
                "index": int(index),
            })
            next_slot_id[0] -= 1

        def count_effects(chain: List[Dict[str, Any]]) -> int:
            total = 0
            for item in chain:
                uri = item.get("uri", "")
                inst_id = int(item.get("instanceId", -1))
                if inst_id < 0 or uri == EMPTY_URI:
                    continue
                if uri == SPLIT_URI:
                    total += count_effects(item.get("topChain", []) or [])
                    total += count_effects(item.get("bottomChain", []) or [])
                else:
                    total += 1
            return total

        def _resolve_chain_items(items: List[Dict[str, Any]], chain_id: int) -> List[Dict[str, Any]]:
            """Resolve items for a given chain_id.
            
            For chain_id == "main" (or -1), return the root items list.
            For chain_id like "split:<id>:top", locate the split by id and return topChain.
            For chain_id like "split:<id>:bottom", locate the split by id and return bottomChain.
            """
            if chain_id < 0:
                return items
            
            # Parse chain_id format: "split:<split_id>:top" or "split:<split_id>:bottom"
            parts = str(chain_id).split(":")
            if len(parts) != 3 or parts[0] != "split":
                return items
            
            split_id_str, direction = parts[1], parts[2]
            try:
                split_id = int(split_id_str)
            except ValueError:
                return items
            
            # Find the split node by instanceId
            for item in items:
                if isinstance(item, dict) and int(item.get("instanceId", -1)) == split_id:
                    if direction == "top":
                        return item.setdefault("topChain", [])
                    elif direction == "bottom":
                        return item.setdefault("bottomChain", [])
            
            # Split not found, return original items
            return items

        def branch_active(cv_map: Dict[str, float], top: bool) -> bool:
            split_type = cv_map.get("splitType", 0.0)
            if split_type < 1:
                selected = cv_map.get("select", 0.0)
                return selected < 0.5 if top else selected >= 0.5
            return True

        def layout_chain(chain: List[Dict[str, Any]], start_x: float, center_y: float, active: bool, chain_id: int) -> float:
            """Recursively lay out one chain; returns the x of its trailing wire end."""
            prev_x = float(start_x)
            effect_index = 0

            for item in chain:
                uri = item.get("uri", "")
                inst_id = int(item.get("instanceId", -1))
                if inst_id < 0 or uri == EMPTY_URI:
                    continue

                if uri == SPLIT_URI:
                    cv_map = {cv.get("key"): float(cv.get("value", 0.0)) for cv in item.get("controlValues", [])}
                    split_type = cv_map.get("splitType", 0.0)
                    selected = cv_map.get("select", 0.0)
                    if split_type < 1:
                        label, mode = ("A" if selected < 0.5 else "B"), "A/B"
                    elif split_type < 2:
                        label, mode = "MIX", "MIX"
                    else:
                        label, mode = "L/R", "L/R"

                    split_x = prev_x + H_GAP
                    wires.append({
                        "name": "split-in",
                        "points": [{"x": prev_x, "y": center_y}, {"x": split_x, "y": center_y}],
                        "active": bool(active),
                    })
                    add_slot(prev_x, split_x, center_y, chain_id, effect_index)

                    nodes.append({
                        "instanceId": inst_id,
                        "kind": "split",
                        "x": int(round(split_x)),
                        "y": int(round(center_y - SPLIT_D / 2.0)),
                        "width": SPLIT_D,
                        "height": SPLIT_D,
                        "label": label,
                        "splitMode": mode,
                        "active": True,
                        "chainId": str(chain_id),
                        "index": effect_index,
                    })

                    fork_x = split_x + SPLIT_D + FORK_LEAD
                    branch_start_x = fork_x + BRANCH_PAD
                    
                    # Extract chain items before measuring (fix: define top_items/bottom_items before use)
                    top_items = item.get("topChain", []) or []
                    bottom_items = item.get("bottomChain", []) or []
                    top_t, top_b = _exact_extent(top_items)
                    bot_t, bot_b = _exact_extent(bottom_items)
                    top_offset = top_b + 14.0
                    bot_offset = bot_t + 14.0
                    top_y = center_y - top_offset
                    bot_y = center_y + bot_offset

                    top_active = bool(active) and branch_active(cv_map, True)
                    bot_active = bool(active) and branch_active(cv_map, False)

                    top_end_x = layout_chain(top_items, branch_start_x, top_y, top_active, f"split:{inst_id}:top")
                    bot_end_x = layout_chain(bottom_items, branch_start_x, bot_y, bot_active, f"split:{inst_id}:bottom")
                    rejoin_x = max(top_end_x, bot_end_x) + BRANCH_PAD
                    rejoin_out_x = rejoin_x + FORK_LEAD

                    wires.append({
                        "name": "top-branch-fork",
                        "points": [
                            {"x": split_x + SPLIT_D, "y": center_y},
                            {"x": fork_x, "y": center_y},
                            {"x": fork_x, "y": top_y},
                            {"x": branch_start_x, "y": top_y},
                        ],
                        "active": top_active,
                    })
                    wires.append({
                        "name": "top-branch-rejoin",
                        "points": [
                            {"x": top_end_x, "y": top_y},
                            {"x": rejoin_x, "y": top_y},
                            {"x": rejoin_x, "y": center_y},
                            {"x": rejoin_out_x, "y": center_y},
                        ],
                        "active": top_active,
                    })
                    wires.append({
                        "name": "bottom-branch-fork",
                        "points": [
                            {"x": split_x + SPLIT_D, "y": center_y},
                            {"x": fork_x, "y": center_y},
                            {"x": fork_x, "y": bot_y},
                            {"x": branch_start_x, "y": bot_y},
                        ],
                        "active": bot_active,
                    })
                    wires.append({
                        "name": "bottom-branch-rejoin",
                        "points": [
                            {"x": bot_end_x, "y": bot_y},
                            {"x": rejoin_x, "y": bot_y},
                            {"x": rejoin_x, "y": center_y},
                            {"x": rejoin_out_x, "y": center_y},
                        ],
                        "active": bot_active,
                    })

                    add_slot(top_end_x, rejoin_x, top_y, f"split:{inst_id}:top", count_effects(top_items))
                    add_slot(bot_end_x, rejoin_x, bot_y, f"split:{inst_id}:bottom", count_effects(bottom_items))

                    prev_x = rejoin_out_x
                    # CRITICAL: no horizontal wire at center_y between split_x and rejoin_x.
                    continue

                block = effect_by_id.get(inst_id)
                if block is None:
                    continue

                x = prev_x + H_GAP
                # Wire should be active if the chain/branch is active, regardless of individual block bypassed state
                node_active = bool(active)
                wires.append({
                    "name": "chain",
                    "points": [{"x": prev_x, "y": center_y}, {"x": x, "y": center_y}],
                    "active": node_active,
                })
                nodes.append({
                    "instanceId": inst_id,
                    "kind": "effect",
                    "x": int(round(x)),
                    "y": int(round(center_y - BLOCK_H / 2.0)),
                    "width": BLOCK_W,
                    "height": BLOCK_H,
                    "label": block.get("name", "Effect"),
                    "category": block.get("category", "Utility"),
                    "color": block.get("color", "#94A3B8"),
                    "active": node_active,
                    "bypassed": bool(block.get("bypassed", False)),
                    "chainId": str(chain_id),
                    "index": effect_index,
                })
                prev_x = x + BLOCK_W
                effect_index += 1

            if str(chain_id) in ("main", "0", "") and effect_index > 0:
                add_slot(prev_x, prev_x + H_GAP, center_y, "main", effect_index)
            return prev_x

        # ---- Build the tree ---------------------------------------------------
        center_y = 0.0
        nodes.append({
            "instanceId": -10,
            "kind": "capsule",
            "x": 0.0,
            "y": center_y - CAP_H / 2.0,
            "width": CAP_W,
            "height": CAP_H,
            "label": "IN",
            "active": True,
            "chainId": -1,
            "index": 0,
        })

        main_end_x = layout_chain(items, CAP_W, center_y, True, "main")
        out_x = main_end_x + H_GAP
        wires.append({
            "name": "out",
            "points": [{"x": main_end_x, "y": center_y}, {"x": out_x, "y": center_y}],
            "active": True,
        })
        add_slot(main_end_x, out_x, center_y, -1, count_effects(items))
        nodes.append({
            "instanceId": -11,
            "kind": "capsule",
            "x": out_x,
            "y": center_y - CAP_H / 2.0,
            "width": CAP_W,
            "height": CAP_H,
            "label": "OUT",
            "active": True,
            "chainId": -1,
            "index": -1,
        })

        # ---- Normalize so drawing starts at (20, 20) --------------------------
        min_x = float("inf")
        min_y = float("inf")
        max_x_raw = float("-inf")
        max_y_raw = float("-inf")

        for node in nodes:
            nx = float(node["x"])
            ny = float(node["y"])
            min_x = min(min_x, nx)
            min_y = min(min_y, ny)
            max_x_raw = max(max_x_raw, nx + float(node["width"]))
            max_y_raw = max(max_y_raw, ny + float(node["height"]))
        for wire in wires:
            for point in wire["points"]:
                px = float(point["x"])
                py = float(point["y"])
                min_x = min(min_x, px)
                min_y = min(min_y, py)
                max_x_raw = max(max_x_raw, px)
                max_y_raw = max(max_y_raw, py)

        dx = 20.0 - min_x
        dy = 20.0 - min_y
        for node in nodes:
            node["x"] = int(round(float(node["x"]) + dx))
            node["y"] = int(round(float(node["y"]) + dy))
        for wire in wires:
            for point in wire["points"]:
                point["x"] = int(round(float(point["x"]) + dx))
                point["y"] = int(round(float(point["y"]) + dy))

        canvas_width = int(round(max_x_raw + dx + 20.0))
        canvas_height = int(round(max_y_raw + dy + 20.0))

        self.bridge._signal_nodes = nodes
        self.bridge._signal_wires = wires
        self.bridge._signal_canvas_width = canvas_width
        self.bridge._signal_canvas_height = canvas_height

    def _resolve_chain_items(self, items: List[Dict[str, Any]], chain_id: int) -> List[Dict[str, Any]]:
        """Resolve items for a given chain_id.
        
        For chain_id == "main" (or -1), return the root items list.
        For chain_id like "split:<id>:top", locate the split by id and return topChain.
        For chain_id like "split:<id>:bottom", locate the split by id and return bottomChain.
        """
        # Handle None, empty string, "main", or negative int as root/main trunk
        if chain_id is None:
            return items
        chain_id_str = str(chain_id).strip()
        if chain_id_str in ("", "main", "-1") or (isinstance(chain_id, int) and chain_id < 0):
            return items
        
        # Parse chain_id format: "split:<split_id>:top" or "split:<split_id>:bottom"
        parts = chain_id_str.split(":")
        if len(parts) != 3 or parts[0] != "split":
            return items
        
        split_id_str, direction = parts[1], parts[2]
        try:
            split_id = int(split_id_str)
        except ValueError:
            return items
        
        # Find the split node by instanceId
        for item in items:
            if isinstance(item, dict) and int(item.get("instanceId", -1)) == split_id:
                if direction == "top":
                    return item.setdefault("topChain", [])
                elif direction == "bottom":
                    return item.setdefault("bottomChain", [])
        
        # Split not found, return original items
        return items

    def _flatten_pedalboard_items(self, items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Recursively flatten pedalboard chains and filter out synthetic/empty items."""
        result = []
        for item in items:
            uri = item.get("uri", "")
            inst_id = item.get("instanceId", -1)

            # Skip synthetic controls (Input/Output have id < 0) and Empty placeholders
            if inst_id < 0 or uri == "uri://two-play/pipedal/pedalboard#Empty":
                continue

            if uri == "uri://two-play/pipedal/pedalboard#Split":
                top = item.get("topChain", [])
                bottom = item.get("bottomChain", [])
                result.extend(self._flatten_pedalboard_items(top))
                result.extend(self._flatten_pedalboard_items(bottom))
            else:
                result.append(item)
        return result

    # -----------------------------------------------------------------------
    # Tree Helpers (Class Methods on PiPedalAsyncClient)
    # -----------------------------------------------------------------------
    def _find_item_by_id(self, items: List[Dict[str, Any]], instance_id: int) -> Optional[Dict[str, Any]]:
        """Recursively search for an item by instance_id in topChain and bottomChain of Split blocks."""
        for item in items:
            if isinstance(item, dict) and item.get("instanceId") == instance_id:
                return item
            uri = item.get("uri", "")
            if uri == SPLIT_URI:
                top = item.get("topChain", []) or []
                bot = item.get("bottomChain", []) or []
                found_top = self._find_item_by_id(top, instance_id)
                if found_top is not None:
                    return found_top
                found_bot = self._find_item_by_id(bot, instance_id)
                if found_bot is not None:
                    return found_bot
        return None

    def _find_and_remove_item(self, items: List[Dict[str, Any]], instance_id: int) -> Optional[Dict[str, Any]]:
        """Recursively find and remove an item by instance_id from topChain and bottomChain of Split blocks."""
        for i, item in enumerate(items):
            if isinstance(item, dict) and item.get("instanceId") == instance_id:
                return items.pop(i)
            uri = item.get("uri", "")
            if uri == SPLIT_URI:
                top = item.get("topChain", []) or []
                bot = item.get("bottomChain", []) or []
                found_top = self._find_and_remove_item(top, instance_id)
                if found_top is not None:
                    return found_top
                found_bot = self._find_and_remove_item(bot, instance_id)
                if found_bot is not None:
                    return found_bot
        return None

    def _insert_item_before(self, items: List[Dict[str, Any]], target_instance_id: int, item_to_insert: Dict[str, Any]) -> bool:
        """Recursively insert an item before the one with target_instance_id in topChain and bottomChain of Split blocks."""
        for i, item in enumerate(items):
            if isinstance(item, dict) and item.get("instanceId") == target_instance_id:
                items.insert(i, item_to_insert)
                return True
            uri = item.get("uri", "")
            if uri == SPLIT_URI:
                top = item.get("topChain", []) or []
                bot = item.get("bottomChain", []) or []
                if self._insert_item_before(top, target_instance_id, item_to_insert):
                    return True
                if self._insert_item_before(bot, target_instance_id, item_to_insert):
                    return True
        # If not found in children, append to current list
        items.append(item_to_insert)
        return True

    # -----------------------------------------------------------------------
    # Outgoing JSON-RPC Requests & Fire-and-Forget Notifications
    # -----------------------------------------------------------------------
    def send_load_preset(self, preset_id: int):
        """Fire-and-forget preset load."""
        self._schedule_coro(self._send_notification("loadPreset", int(preset_id)))

    def save_current_preset(self):
        self._schedule_coro(self._send_notification("saveCurrentPreset", int(self.client_id)))

    def set_favorites(self, favorites: Dict[str, bool]):
        self._schedule_coro(self._send_notification("setFavorites", favorites))

    def send_set_item_enabled(self, instance_id: int, enabled: bool):
        """Fire-and-forget bypass toggle."""
        body = {
            "clientId": self.client_id,
            "instanceId": int(instance_id),
            "enabled": bool(enabled),
        }
        self._schedule_coro(self._send_notification("setPedalboardItemEnable", body))

    def send_set_control(self, instance_id: int, symbol: str, value: float):
        """Fire-and-forget real-time parameter change."""
        body = {
            "clientId": self.client_id,
            "instanceId": int(instance_id),
            "symbol": str(symbol),
            "value": float(value),
        }
        self._schedule_coro(self._send_notification("setControl", body))

    def request_file_list(self, instance_id: int, property_uri: str, file_property: Dict[str, Any], relative_path: str = ""):
        """Request the file list for a plugin path property."""
        self._schedule_coro(self._request_file_list(instance_id, property_uri, file_property, relative_path))

    def send_set_patch_property(self, instance_id: int, property_uri: str, value: str):
        """Set a PiPedal patch/path property using the schema-defined RPC."""
        body = {
            "instanceId": int(instance_id),
            "propertyUri": str(property_uri),
            "value": value,
        }
        self._schedule_coro(self._send_request_notification("setPatchProperty", body))

    def reorder_pedalboard(self, from_instance_id: int, to_instance_id: int):
        if from_instance_id == to_instance_id:
            return
        pedalboard = json.loads(json.dumps(self.raw_pedalboard))
        from_item = self._find_and_remove_item(pedalboard.get("items", []), int(from_instance_id))
        if from_item is None:
            return
        if not self._insert_item_before(pedalboard.get("items", []), int(to_instance_id), from_item):
            pedalboard.setdefault("items", []).append(from_item)
        body = {"clientId": self.client_id, "pedalboard": pedalboard}
        self._schedule_coro(self._send_notification("updateCurrentPedalboard", body))

    def switch_snapshot(self, snapshot_index: int):
        snapshots = self.raw_pedalboard.get("snapshots", []) or []
        if snapshot_index < 0 or snapshot_index >= len(snapshots):
            return
        self._schedule_coro(self._send_notification("setSnapshot", snapshot_index))

    def open_bank(self, bank_id: int):
        self._schedule_coro(self._send_notification("openBank", int(bank_id)))

    async def request_bank_presets(self, bank_id: int):
        try:
            presets = await self.request("requestBankPresets", {"bankInstanceId": int(bank_id)})
            if isinstance(presets, list):
                self.bridge._bank_presets = presets
                self.bridge.presetsListChanged.emit()
        except Exception as e:
            print(f"[PiPedal Error] {e!r}")

    def save_snapshot(self, snapshot_index: int):
        snapshots = json.loads(json.dumps(self.raw_pedalboard.get("snapshots", []) or []))
        while len(snapshots) < 6:
            snapshots.append(None)

        existing = snapshots[snapshot_index] if snapshot_index < len(snapshots) else None
        values = []
        for item in self._flatten_pedalboard_items(self.raw_pedalboard.get("items", [])):
            values.append({
                "instanceId": int(item.get("instanceId", -1)),
                "isEnabled": bool(item.get("isEnabled", True)),
                "controlValues": item.get("controlValues", []) or [],
                "lv2State": item.get("lv2State", [False, {}]),
                "pathProperties": item.get("pathProperties", {}) or {},
            })

        letter = chr(65 + snapshot_index)
        snapshots[snapshot_index] = {
            "name": existing.get("name", "") if isinstance(existing, dict) else "",
            "color": existing.get("color", "") if isinstance(existing, dict) else "",
            "isModified": False,
            "values": values,
        }
        if not snapshots[snapshot_index]["name"]:
            snapshots[snapshot_index]["name"] = f"Scene {letter}"

        body = {"snapshots": snapshots, "selectedSnapshot": snapshot_index}
        self._schedule_coro(self._send_notification("setSnapshots", body))

    def update_snapshot(self, snapshot_index: int, name: str, color: str):
        snapshots = json.loads(json.dumps(self.raw_pedalboard.get("snapshots", []) or []))
        while len(snapshots) < 6:
            snapshots.append(None)
        snapshot = snapshots[snapshot_index] if snapshot_index < len(snapshots) else None
        if not isinstance(snapshot, dict):
            snapshot = {"name": "", "color": "", "isModified": False, "values": []}
        snapshot["name"] = str(name).strip()
        snapshot["color"] = str(color).strip()
        snapshots[snapshot_index] = snapshot
        body = {"snapshots": snapshots, "selectedSnapshot": int(self.raw_pedalboard.get("selectedSnapshot", -1))}
        self._schedule_coro(self._send_notification("setSnapshots", body))

    def swap_snapshots(self, first_index: int, second_index: int):
        snapshots = json.loads(json.dumps(self.raw_pedalboard.get("snapshots", []) or []))
        while len(snapshots) < 6:
            snapshots.append(None)
        snapshots[first_index], snapshots[second_index] = snapshots[second_index], snapshots[first_index]
        body = {"snapshots": snapshots, "selectedSnapshot": int(self.raw_pedalboard.get("selectedSnapshot", -1))}
        self._schedule_coro(self._send_notification("setSnapshots", body))

    def copy_snapshot(self, source_index: int, target_index: int):
        snapshots = json.loads(json.dumps(self.raw_pedalboard.get("snapshots", []) or []))
        while len(snapshots) < 6:
            snapshots.append(None)
        if source_index < len(snapshots) and isinstance(snapshots[source_index], dict):
            snapshots[target_index] = json.loads(json.dumps(snapshots[source_index]))
        body = {"snapshots": snapshots, "selectedSnapshot": int(self.raw_pedalboard.get("selectedSnapshot", -1))}
        self._schedule_coro(self._send_notification("setSnapshots", body))

    def insert_plugin(self, plugin_uri: str, insertion_index: int):
        """Insert a plugin at the given index in the main chain or split branch.
        
        The insertion_index is interpreted as a flat index into the resolved chain items.
        """
        plugin = self.ui_plugins_by_uri.get(plugin_uri)
        if not plugin:
            return
        
        pedalboard = json.loads(json.dumps(self.raw_pedalboard))
        new_item = {
            "instanceId": int(pedalboard.get("nextInstanceId", 0)) + 1,
            "title": "",
            "uri": plugin_uri,
            "pluginName": plugin.get("name", "Effect"),
            "isEnabled": True,
            "controlValues": [
                {"key": ctrl.get("symbol", ""), "value": ctrl.get("default_value", 0.0)}
                for ctrl in plugin.get("controls", [])
                if ctrl.get("symbol")
            ],
            "pathProperties": {},
            "lv2State": [False, {}],
            "vstState": "",
        }
        pedalboard["nextInstanceId"] = int(new_item["instanceId"])
        
        # Resolve the target chain items using _resolve_chain_items
        resolved_chain = self._resolve_chain_items(pedalboard.get("items", []), insertion_index)
        
        # Insert at the correct position in the resolved chain
        if isinstance(resolved_chain, list):
            resolved_chain.insert(max(0, insertion_index), new_item)
        
        body = {"clientId": self.client_id, "pedalboard": pedalboard}
        self._schedule_coro(self._send_notification("updateCurrentPedalboard", body))

    def delete_block(self, instance_id: int):
        if not self.raw_pedalboard:
            return
        pedalboard = json.loads(json.dumps(self.raw_pedalboard))
        items = pedalboard.get("items", [])
        target_id = int(instance_id)
        def _pop_item(lst):
            for i, it in enumerate(lst):
                if not isinstance(it, dict):
                    continue
                if int(it.get("instanceId", -1)) == target_id:
                    return lst.pop(i)
                if it.get("uri") == SPLIT_URI:
                    got = _pop_item(it.get("topChain", []) or []) or _pop_item(it.get("bottomChain", []) or [])
                    if got is not None:
                        return got
            return None
        if _pop_item(items) is not None:
            body = {"clientId": self.client_id, "pedalboard": pedalboard}
            self._schedule_coro(self._send_notification("updateCurrentPedalboard", body))

    def drop_block(self, instance_id: int, target_chain_id: str, target_index: int):
        if not self.raw_pedalboard:
            return
        pedalboard = json.loads(json.dumps(self.raw_pedalboard))
        items = pedalboard.get("items", [])
        inst_id = int(instance_id)
        chain_str = str(target_chain_id or "main")
        def _find_item(lst, target_id):
            for it in lst:
                if not isinstance(it, dict):
                    continue
                if int(it.get("instanceId", -1)) == target_id:
                    return it
                if it.get("uri") == SPLIT_URI:
                    found = _find_item(it.get("topChain", []) or [], target_id) or _find_item(it.get("bottomChain", []) or [], target_id)
                    if found is not None:
                        return found
            return None
        def _contains_split(node, split_id):
            if not isinstance(node, dict) or node.get("uri") != SPLIT_URI:
                return False
            if int(node.get("instanceId", -1)) == split_id:
                return True
            return any(_contains_split(c, split_id) for c in (node.get("topChain", []) or []) + (node.get("bottomChain", []) or []))
        moving_item = _find_item(items, inst_id)
        if moving_item is None:
            return
        if chain_str.startswith("split:"):
            parts = chain_str.split(":")
            if len(parts) == 3 and parts[1].isdigit() and _contains_split(moving_item, int(parts[1])):
                return
        def _find_chain(lst, cid):
            if cid in ("main", "0", ""):
                return lst
            for it in lst:
                if not isinstance(it, dict):
                    continue
                iid = it.get("instanceId")
                if cid == f"split:{iid}:top":
                    return it.setdefault("topChain", [])
                if cid == f"split:{iid}:bottom":
                    return it.setdefault("bottomChain", [])
                if it.get("uri") == SPLIT_URI:
                    found = _find_chain(it.get("topChain", []) or [], cid)
                    if found is not None:
                        return found
                    found = _find_chain(it.get("bottomChain", []) or [], cid)
                    if found is not None:
                        return found
            return None
        target_chain = _find_chain(items, chain_str)
        if target_chain is None:
            return
        idx = int(target_index)
        src_idx = next((i for i, x in enumerate(target_chain) if isinstance(x, dict) and int(x.get("instanceId", -1)) == inst_id), -1)
        def _pop_item(lst, target_id):
            for i, it in enumerate(lst):
                if not isinstance(it, dict):
                    continue
                if int(it.get("instanceId", -1)) == target_id:
                    return lst.pop(i)
                if it.get("uri") == SPLIT_URI:
                    got = _pop_item(it.get("topChain", []) or [], target_id)
                    if got is not None:
                        return got
                    got = _pop_item(it.get("bottomChain", []) or [], target_id)
                    if got is not None:
                        return got
            return None
        moving = _pop_item(items, inst_id)
        if moving is None:
            return
        if src_idx != -1 and src_idx < idx:
            idx -= 1
        idx = max(0, min(idx, len(target_chain)))
        target_chain.insert(idx, moving)
        body = {"clientId": self.client_id, "pedalboard": pedalboard}
        self._schedule_coro(self._send_notification("updateCurrentPedalboard", body))
    def _schedule_coro(self, coro):
        """Safely schedule an async coroutine on the background event loop."""
        if self.loop and self.loop.is_running():
            asyncio.run_coroutine_threadsafe(coro, self.loop)

    async def _send_notification(self, method: str, body: Any = None):
        """Send a JSON notification message (no reply expected)."""
        if not self.ws:
            return
        if body is None:
            msg = [{"message": method}]
        else:
            msg = [{"message": method}, body]

        raw_json = json.dumps(msg)
        tx_preview = raw_json[:300] + ("..." if len(raw_json) > 300 else "")
        if not sys.platform.startswith("linux"):
            print(f"[WS TX] {tx_preview}")

        try:
            await self.ws.send(raw_json)
        except Exception as e:
            logger.warning(f"Failed to send notification {method}: {e}")

    async def _send_request_notification(self, method: str, body: Any):
        """Send a request-shaped RPC through the normal notification channel."""
        try:
            await self.request(method, body)
        except Exception as e:
            print(f"[PiPedal Error] {e!r}")

    async def _request_file_list(self, instance_id: int, property_uri: str, file_property: Dict[str, Any], relative_path: str = ""):
        """Resolve a PiPedal file-property request and publish it to QML."""
        try:
            payload = {
                "relativePath": relative_path or "",
                "fileProperty": file_property,
            }
            files_result = await self.request("requestFileList2", payload)
            print(f"[FileList RX] {json.dumps(files_result, ensure_ascii=False)}")

            if isinstance(files_result, dict):
                entries = files_result.get("files", [])
            elif isinstance(files_result, list):
                entries = files_result
            else:
                entries = []

            if not isinstance(entries, list):
                entries = []

            parsed_entries = []
            parsed_entries.append({
                "displayName": "<none>",
                "pathname": "",
                "isDirectory": False,
                "isProtected": True,
            })
            for entry in entries:
                if isinstance(entry, dict):
                    if entry.get("isDirectory", False):
                        parsed_entries.append(entry)
                        continue
                    pathname = str(entry.get("pathname") or entry.get("path") or "")
                    lower_path = pathname.lower()
                    if lower_path.endswith((".nam", ".json", ".aidax", ".wav", ".flac")):
                        parsed_entries.append(entry)
                elif isinstance(entry, str):
                    lower_path = entry.lower()
                    if lower_path.endswith((".nam", ".json", ".aidax", ".wav", ".flac")):
                        parsed_entries.append({
                            "displayName": entry,
                            "pathname": entry,
                            "isDirectory": False,
                            "isProtected": False,
                        })

            self.bridge._sig_file_list_updated.emit(int(instance_id), str(property_uri), parsed_entries)
        except Exception as e:
            print(f"[PiPedal Error] {e!r}")

    async def request(self, method: str, args: Any = None) -> Any:
        """Send an exact PiPedal JSON-RPC request and await its reply."""
        if not self.ws:
            raise ConnectionError("WebSocket is not connected")

        self.req_id += 1
        code = self.req_id

        header: Dict[str, Any] = {"message": method, "replyTo": code}
        if args is not None:
            msg = [header, args]
        else:
            msg = [header]

        raw_json = json.dumps(msg)
        tx_preview = raw_json[:300] + ("..." if len(raw_json) > 300 else "")
        if not sys.platform.startswith("linux"):
            print(f"[WS TX] {tx_preview}")

        future = self.loop.create_future()
        self.pending_requests[code] = future

        await self.ws.send(raw_json)
        timeout = 60.0 if method in ("plugins", "currentPedalboard", "pluginClasses", "getPresets", "getBankIndex") else 15.0
        try:
            return await asyncio.wait_for(future, timeout=timeout)
        except asyncio.TimeoutError:
            self.pending_requests.pop(code, None)
            raise TimeoutError(f"PiPedal RPC timed out: {method} (replyTo={code}, timeout={timeout:.0f}s)")
