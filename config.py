"""
PiPedal Quad Cortex Touchscreen UI Configuration
Fixed Resolution: 800x480 (Hosyond 7-inch IPS DSI capacitive touchscreen)
"""

import os
import sys

# Network configuration
# Automatically use localhost on the Raspberry Pi (Linux), or QuadPi.local when testing from PC
_DEFAULT_HOST: str = "127.0.0.1" if sys.platform.startswith("linux") else "QuadPi.local"
PI_HOST: str = os.environ.get("PI_HOST", _DEFAULT_HOST)
PI_PORT: int = int(os.environ.get("PI_PORT", 80))

# Display configuration (Strict 800x480)
SCREEN_WIDTH: int = 800
SCREEN_HEIGHT: int = 480

# Developer hot-reloading for rapid desktop QML styling (disabled on Pi)
HOT_RELOAD: bool = not sys.platform.startswith("linux")