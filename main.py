"""
Main Application Entry Point for PiPedal Touchscreen UI
Initializes PySide6, registers the PiPedalBridge, loads Main.qml,
and provides live hot-reloading for rapid UI development.
"""

from __future__ import annotations

import os
import signal
import sys
from pathlib import Path

from PySide6.QtCore import QFileSystemWatcher, QObject, QTimer, QUrl
from PySide6.QtGui import QGuiApplication, QIcon
from PySide6.QtQml import QQmlApplicationEngine

import config
from pipedal_client import PiPedalAsyncClient, PiPedalBridge


class QmlReloader(QObject):
    """
    Watches all .qml files in the project directory using QFileSystemWatcher.
    When any .qml file is saved, it clears the QML component cache and reloads
    Main.qml in-place without dropping the active WebSocket connection.
    """

    def __init__(self, engine: QQmlApplicationEngine, main_qml_path: Path):
        super().__init__()
        self.engine = engine
        self.main_qml_path = main_qml_path
        self.watcher = QFileSystemWatcher(self)

        # Debounce timer to avoid multiple reloads on atomic writes/file saves
        self.debounce_timer = QTimer(self)
        self.debounce_timer.setSingleShot(True)
        self.debounce_timer.setInterval(100)
        self.debounce_timer.timeout.connect(self._do_reload)

        self._setup_watcher()

    def _setup_watcher(self):
        project_dir = self.main_qml_path.parent
        self.watcher.addPath(str(project_dir))

        # Add all .qml files in the directory
        for qml_file in project_dir.glob("*.qml"):
            self.watcher.addPath(str(qml_file.resolve()))

        self.watcher.fileChanged.connect(self._on_file_changed)
        self.watcher.directoryChanged.connect(self._on_dir_changed)

    def _on_file_changed(self, path: str):
        print(f"[HotReload] File changed: {os.path.basename(path)}")
        # Some editors delete & recreate files on save; re-add the file to watcher
        if os.path.exists(path) and path not in self.watcher.files():
            self.watcher.addPath(path)
        self.debounce_timer.start()

    def _on_dir_changed(self, path: str):
        # Refresh watched .qml files in directory
        project_dir = Path(path)
        for qml_file in project_dir.glob("*.qml"):
            qpath = str(qml_file.resolve())
            if qpath not in self.watcher.files():
                self.watcher.addPath(qpath)
        self.debounce_timer.start()

    def _do_reload(self):
        print("[HotReload] Clearing QML component cache & reloading UI in-place...")
        try:
            self.engine.clearComponentCache()

            # Destroy current root window objects
            for root_obj in self.engine.rootObjects():
                root_obj.deleteLater()

            # Load updated Main.qml
            self.engine.load(QUrl.fromLocalFile(str(self.main_qml_path)))
            print("[HotReload] Reload complete! WebSocket connection preserved.")
        except Exception as e:
            print(f"[HotReload] Error during reload: {e}", file=sys.stderr)


def main():
    # Handle Ctrl+C cleanly in terminal
    signal.signal(signal.SIGINT, signal.SIG_DFL)

    # Enable high DPI scaling if running on high-res monitors
    os.environ["QT_QUICK_CONTROLS_STYLE"] = "Basic"

    app = QGuiApplication(sys.argv)
    app.setApplicationName("PiPedal Quad Cortex Touchscreen UI")
    app.setOrganizationName("PiPedal")

    # 1. Instantiate the QObject bridge
    bridge = PiPedalBridge()

    # 2. Start the background asyncio WebSocket client
    async_client = PiPedalAsyncClient(bridge=bridge, host=config.PI_HOST, port=config.PI_PORT)
    bridge.set_async_client(async_client)
    async_client.start()

    # 3. Initialize QQmlApplicationEngine and register bridge
    engine = QQmlApplicationEngine()
    engine.rootContext().setContextProperty("bridge", bridge)

    main_qml_path = Path(__file__).parent.resolve() / "Main.qml"
    if not main_qml_path.exists():
        print(f"Error: Main.qml not found at {main_qml_path}", file=sys.stderr)
        sys.exit(1)

    # 4. Load initial Main.qml
    engine.load(QUrl.fromLocalFile(str(main_qml_path)))

    if not engine.rootObjects():
        print("Error: Failed to load Main.qml. Check QML syntax.", file=sys.stderr)
        sys.exit(1)

    # 5. Enable Hot-Reloading if configured
    reloader = None
    if config.HOT_RELOAD:
        print(f"[HotReload] Active! Watching for QML changes in {main_qml_path.parent}")
        reloader = QmlReloader(engine, main_qml_path)

    # Execute Qt Event Loop
    exit_code = app.exec()

    # Clean shutdown
    async_client.stop()
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
