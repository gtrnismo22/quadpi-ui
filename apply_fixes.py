qml_text = open("Main.qml", encoding="utf-8").read()

# 1. Restore a 160ms hold interval and track actual drag movement (`hasMoved`)
qml_text = qml_text.replace("interval: 75", "interval: 160", 1)
qml_text = qml_text.replace(
    "property bool dragged: false",
    "property bool dragged: false\n                                    property bool hasMoved: false",
    1,
)

# 2. Reset `hasMoved` on press and lift, and set it only when moved > 10px
qml_text = qml_text.replace(
    "nodeRoot.lifting = true;\n                                        dragged = true;",
    "nodeRoot.lifting = true;\n                                        dragged = true;\n                                        hasMoved = false;",
    1,
)
qml_text = qml_text.replace(
    "dragged = false;\n                                            blockHoldTimer.start();",
    "dragged = false;\n                                            hasMoved = false;\n                                            blockHoldTimer.start();",
    1,
)
qml_text = qml_text.replace(
    "var point = mapToItem(treeContainer, mouse.x, mouse.y);",
    "if (Math.hypot(mouse.x - pressX, mouse.y - pressY) > 10)\n                                            hasMoved = true;\n                                        var point = mapToItem(treeContainer, mouse.x, mouse.y);",
    1,
)

# 3. Only drop if lifted AND moved; otherwise open the parameter editor
qml_text = qml_text.replace(
    "var wasLifting = nodeRoot.lifting;",
    "var wasLifting = nodeRoot.lifting;\n                                        var didMove = hasMoved;",
    1,
)
qml_text = qml_text.replace(
    "if (!wasLifting) {",
    "if (!wasLifting || !didMove) {",
    1,
)

open("Main.qml", "w", encoding="utf-8").write(qml_text)
print("[OK] Main.qml updated: stationary taps open the parameter editor; drags > 10px reorder blocks.")