"""
PiPedal Quad Cortex Touchscreen UI Configuration
Fixed Resolution: 800x480 (Hosyond 7-inch IPS DSI capacitive touchscreen)
"""

import os

# Network configuration
# Set PI_HOST to your Raspberry Pi IP when testing remotely from PC
# e.g., os.environ.get("PI_HOST", "192.168.1.100") OG: 127.0.0.1
PI_HOST: str = os.environ.get("PI_HOST", "QuadPi.local")
PI_PORT: int = int(os.environ.get("PI_PORT", 80))

# Display configuration (Strict 800x480)
SCREEN_WIDTH: int = 800
SCREEN_HEIGHT: int = 480

# Developer hot-reloading for rapid desktop QML styling
HOT_RELOAD: bool = True
