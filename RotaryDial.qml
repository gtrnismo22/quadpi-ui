import QtQuick
import QtQuick.Shapes

Item {
    id: root

    property real value: 0.0
    property real minimumValue: 0.0
    property real maximumValue: 1.0
    property real defaultValue: 0.5
    property bool isInteger: false
    property bool isLogarithmic: false
    property string unit: ""
    property string label: ""
    property int instanceId: -1
    property string symbol: ""
    property color accentColor: "#00E5FF"
    property bool compact: false

    property bool isDragging: false

    signal valueModified(real newValue)

    implicitWidth: compact ? 48 : 120
    implicitHeight: compact ? 48 : 150

    // Range calculation & normalized fraction (0.0 to 1.0)
    readonly property real span: Math.max(0.0001, maximumValue - minimumValue)
    readonly property real displayValue: value
    readonly property real fraction: maximumValue === minimumValue
        ? 0.0
        : Math.max(0.0, Math.min(1.0, (displayValue - minimumValue) / (maximumValue - minimumValue)))

    // Formatted value display string
    function formatValue(val) {
        if (isInteger || Math.abs(Math.round(val) - val) < 0.0001) {
            return "" + Math.round(val);
        } else {
            var decPlaces = Math.abs(maximumValue - minimumValue) < 10.0 ? 1 : 1;
            return val.toFixed(decPlaces);
        }
    }

    Column {
        anchors.fill: parent
        spacing: 2

        // Parameter Name Header (Crisp Uppercase)
        Text {
            width: parent.width
            height: root.compact ? 0 : 18
            visible: !root.compact
            text: root.label.toUpperCase()
            color: "#8E8E9F"
            font.pixelSize: 11
            font.weight: Font.DemiBold
            font.capitalization: Font.AllUppercase
            horizontalAlignment: Text.AlignHCenter
            verticalAlignment: Text.AlignVCenter
            elide: Text.ElideRight
        }

        // Circular Rotary Potentiometer (Shape Arcs + Cap)
        Item {
            id: dialContainer
            width: root.compact ? 48 : 86
            height: root.compact ? 48 : 86
            anchors.horizontalCenter: parent.horizontalCenter

            readonly property real centerPt: width / 2
            readonly property real strokeW: 6
            readonly property real arcRadius: (width - strokeW) / 2

            // Background Inactive Arc Track (270° from 135° to 405°)
            Shape {
                id: bgShape
                anchors.fill: parent
                layer.enabled: true
                layer.smooth: true

                ShapePath {
                    strokeColor: "#26262E"
                    strokeWidth: dialContainer.strokeW
                    fillColor: "transparent"
                    capStyle: ShapePath.RoundCap

                    PathAngleArc {
                        centerX: dialContainer.centerPt
                        centerY: dialContainer.centerPt
                        radiusX: dialContainer.arcRadius
                        radiusY: dialContainer.arcRadius
                        startAngle: 135
                        sweepAngle: 270
                    }
                }
            }

            // Foreground Active Glowing Value Arc
            Shape {
                id: fgShape
                anchors.fill: parent
                layer.enabled: true
                layer.smooth: true

                ShapePath {
                    strokeColor: root.accentColor
                    strokeWidth: dialContainer.strokeW
                    fillColor: "transparent"
                    capStyle: ShapePath.RoundCap

                    PathAngleArc {
                        centerX: dialContainer.centerPt
                        centerY: dialContainer.centerPt
                        radiusX: dialContainer.arcRadius
                        radiusY: dialContainer.arcRadius
                        startAngle: 135
                        sweepAngle: root.fraction * 270
                    }
                }
            }

            // Center Dark Elevated Knob Cap
            Rectangle {
                id: knobCap
                width: root.compact ? 38 : 64
                height: root.compact ? 38 : 64
                radius: width / 2
                anchors.centerIn: parent
                color: "#141418"
                border.color: dragArea.pressed ? root.accentColor : "#2E2E38"
                border.width: dragArea.pressed ? 2 : 1

                // Subtle inner metallic bevel ring
                Rectangle {
                    width: parent.width - 8
                    height: parent.height - 8
                    radius: width / 2
                    anchors.centerIn: parent
                    color: "#181820"
                    border.color: "#22222C"
                    border.width: 1

                    // Rotational Indicator Notch
                    Item {
                        anchors.fill: parent
                        rotation: -135 + (root.fraction * 270)

                        Rectangle {
                            width: 3
                            height: 10
                            radius: 1.5
                            color: dragArea.pressed ? root.accentColor : "#FFFFFF"
                            anchors.horizontalCenter: parent.horizontalCenter
                            anchors.top: parent.top
                            anchors.topMargin: 4
                        }
                    }
                }
            }

            // Interactive Touch MouseArea for Vertical Drag & Double-Tap Reset
            MouseArea {
                id: dragArea
                anchors.fill: parent
                hoverEnabled: false // Prevent sticky hover on capacitive touchscreen

                property real dragStartY: 0.0
                property real dragStartNorm: 0.0
                property real pendingValue: root.value

                Timer {
                    id: parameterThrottle
                    interval: 30
                    repeat: false
                    onTriggered: {
                        root.valueModified(dragArea.pendingValue);
                    }
                }

                onPressed: function(mouse) {
                    root.isDragging = true;
                    if (bridge) bridge.beginControlDrag(root.instanceId, root.symbol);
                    var globalPoint = dragArea.mapToItem(null, mouse.x, mouse.y);
                    dragStartY = globalPoint.y;
                    dragStartNorm = root.maximumValue === root.minimumValue
                        ? 0.0
                        : Math.max(0.0, Math.min(1.0,
                            (root.value - root.minimumValue) /
                            (root.maximumValue - root.minimumValue)));
                }

                onPositionChanged: function(mouse) {
                    if (!pressed) return;
                    var globalPoint = dragArea.mapToItem(null, mouse.x, mouse.y);
                    var dy = dragStartY - globalPoint.y; // Dragging UP increases value
                    var sensitivity = 150.0;
                    var norm = dragStartNorm + (dy / sensitivity);
                    norm = Math.max(0.0, Math.min(1.0, norm));
                    var rawSpan = root.maximumValue - root.minimumValue;
                    var rawVal = root.minimumValue + norm * rawSpan;
                    var clampedVal = Math.max(root.minimumValue, Math.min(root.maximumValue, rawVal));

                    if (root.isInteger) {
                        clampedVal = Math.round(clampedVal);
                    }

                    if (clampedVal !== root.value) {
                        root.value = clampedVal;
                        pendingValue = clampedVal;
                        if (!parameterThrottle.running) {
                            parameterThrottle.start();
                        }
                    }
                }

                onReleased: function(mouse) {
                    parameterThrottle.stop();
                    root.valueModified(root.value);
                    root.isDragging = false;
                    if (bridge) bridge.endControlDrag();
                }

                onDoubleClicked: function() {
                    // Double-tap resets to default value
                    root.value = root.defaultValue;
                    root.valueModified(root.defaultValue);
                }

                onCanceled: function() {
                    root.isDragging = false;
                    if (bridge) bridge.endControlDrag();
                }
            }
        }

        // Value Readout Pill
        Rectangle {
            width: parent.width - 16
            height: 24
            visible: !root.compact
            radius: 4
            color: "#101014"
            border.color: "#22222A"
            border.width: 1
            anchors.horizontalCenter: parent.horizontalCenter

            Text {
                anchors.centerIn: parent
                text: root.formatValue(root.value)
                color: dragArea.pressed ? root.accentColor : "#F0F0F5"
                font.pixelSize: 12
                font.weight: Font.DemiBold
                horizontalAlignment: Text.AlignHCenter
                verticalAlignment: Text.AlignVCenter
            }
        }

        Text {
            width: parent.width
            height: 14
            visible: !root.compact && root.unit !== "" && root.unit.toLowerCase() !== "none"
            text: root.unit
            color: "#777785"
            font.pixelSize: 9
            font.weight: Font.DemiBold
            horizontalAlignment: Text.AlignHCenter
            verticalAlignment: Text.AlignVCenter
        }
    }
}
