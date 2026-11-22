# Project Rules: PiPedal Quad Cortex Touchscreen UI

## Target Hardware & Environment
- **Target Device:** Raspberry Pi 4 (ARM64, Raspberry Pi OS, Openbox X11 kiosk, 4GB RAM).
- **Display:** Hosyond 7-inch IPS DSI Capacitive Touchscreen, fixed resolution **800x480** (~133 PPI).
- **Development Workflow:** Built and tested on a desktop PC connecting remotely to `ws://<PI_IP>:80`, then deployed to the Pi connecting locally to `ws://127.0.0.1:80`.

## Tech Stack (Strict)
- **Language:** Python 3.11+
- **UI Framework:** `PySide6` (`QtQuick` / `QML`, `QtQuick.Controls`, `QtQuick.Shapes`) — NO Qt Widgets, NO webviews/Chromium.
- **Networking:** `websockets` running on a background `asyncio` thread bridged to QML via a `QObject` controller with Qt `Signal` / `Property` / `Slot` bindings.
- **Protocol Source of Truth:** All JSON-RPC request/response formats and state structures MUST match the TypeScript definitions in `schema_ref/*.tsx`.

## Touchscreen & Visual Design Rules
- **Aesthetic:** Neural DSP Quad Cortex inspired — pitch-black background (`#0A0A0C`), dark elevated cards (`#141418`), subtle 1px borders (`#26262E`), crisp sans-serif typography, glowing category accent rings.
- **Touch Ergonomics (800x480):**
  - Minimum interactive hit target: **48x48px**.
  - Set `hoverEnabled: false` on all controls so capacitive touch taps do not stick in hover states.
  - Disable all ripple/bounce animations; prioritize instant visual feedback (<16ms frame time).
