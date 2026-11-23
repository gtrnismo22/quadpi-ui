# Fixes for Wire Bypassed Shading and Power Button Behavior

## Summary
Fixed two issues in `pipedal_client.py`:
1. **Wire bypassed shading**: Wires were incorrectly dimmed when individual effect blocks were bypassed. Now wires remain active (bright) regardless of individual block bypassed state, only dimming when the entire chain/branch is inactive.
2. **Power button behavior**: Added re-triggering of signal tree rebuild after toggling bypass to ensure QML receives updated wire states.

## Changes Made

### File: `pipedal_client.py`

#### Change 1: Wire active state logic (Line ~1603)
**Before:**
```python
node_active = bool(active) and bool(block.get("enabled", True))
```

**After:**
```python
# Wire should be active if the chain/branch is active, regardless of individual block bypassed state
node_active = bool(active)
```

**Reason:** Wires should reflect the active/inactive state of the chain/branch, not individual effect blocks. An effect can be bypassed (enabled=False) but still part of an active chain, so wires connecting to it should remain bright.

#### Change 2: Signal tree rebuild after toggleBypass (Line ~624-626)
**Added:**
```python
# Update the wire active state for this block's chain position
# by re-triggering signal tree rebuild to ensure wires reflect correct active state
self._build_recursive_signal_tree(self.raw_pedalboard.get("items", []))
```

**Reason:** When bypass is toggled, the local state (`target_item["enabled"]` and `target_item["bypassed"]`) is updated immediately. However, the wire tree needs to be rebuilt to ensure QML receives the correct wire active states. This ensures that:
- Wires are recalculated with the new chain structure
- QML's `signalTreeChanged` signal triggers a repaint of the wire canvas
- The power button color and effect opacity update correctly

## How It Works

### Wire Color Logic in QML (Main.qml line 410)
```qml
ctx.strokeStyle = wires[i].active ? "#E4E4E7" : "#3F3F46";
```
- Bright wire (`#E4E4E7`): Chain/branch is active (`wires[i].active == True`)
- Dim wire (`#3F3F46`): Chain/branch is inactive

### Effect Block States
- `modelData.active`: Controls opacity and text color (dimmed when inactive)
- `modelData.bypassed`: Controls border color and icon (gray when bypassed)
- Wires use only the chain-level `active` state, not individual block states

## Testing Recommendations
1. Toggle bypass on an effect in an active chain - wire should remain bright
2. Toggle bypass on all effects in a chain - wires should dim when entire chain becomes inactive
3. Verify power button color changes correctly between bypassed/enabled states
4. Check that signal tree updates propagate correctly after each toggle
