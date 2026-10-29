import QtQuick
import Quickshell.Io
import qs.Ui

BarWidget {
  id: root
  moduleName: "goldbarth.tessella"

  readonly property var service: bar && bar.shell ? bar.shell.serviceFor(root.moduleName) : null
  readonly property bool ready: service !== null
  readonly property bool opened: panelLoader.item ? panelLoader.item.opened === true : false

  // BarWidget does not derive its size from children.
  implicitWidth: button.implicitWidth
  implicitHeight: button.implicitHeight

  function open() { if (panelLoader.item) panelLoader.item.open() }
  function close() { if (panelLoader.item) panelLoader.item.close() }
  function toggle() { if (panelLoader.item) panelLoader.item.toggle() }

  function injectPanel() {
    var target = panelLoader.item
    if (!target) return
    target.bar = root.bar
    target.anchorItem = button
    target.hostWidget = root
    target.service = root.service
  }

  onBarChanged: injectPanel()
  onServiceChanged: injectPanel()

  Loader {
    id: panelLoader
    active: true
    source: Qt.resolvedUrl("Panel.qml")
    visible: false
    onLoaded: {
      root.injectPanel()
      Qt.callLater(root.injectPanel)
    }
  }

  IpcHandler {
    target: root.moduleName

    function toggle(): void { root.toggle() }
    function open(): void { root.open() }
    function close(): void { root.close() }
    function next(): void { if (root.ready) root.service.applyNext() }
    function pause(): void { if (root.ready) root.service.setPaused(true) }
    function resume(): void { if (root.ready) root.service.setPaused(false) }
    function togglePause(): void { if (root.ready) root.service.togglePaused() }
    function sync(): void { if (root.ready) root.service.startSync() }
    function cancel(): void { if (root.ready) root.service.cancelSync() }
    function status(): string {
      if (!root.ready) return "service unavailable"
      return "enabled=" + root.service.enabled
        + " paused=" + root.service.paused
        + " layout=\"" + root.service.layoutLabel() + "\""
        + " themes=\"" + root.service.themeLabel() + "\""
        + " next=\"" + root.service.nextText() + "\""
        + " sync=\"" + root.service.syncText() + "\""
        + (root.service.lastError ? " error=\"" + root.service.lastError + "\"" : "")
    }
  }

  WidgetButton {
    id: button
    anchors.fill: parent
    bar: root.bar
    text: "󰸉"
    dimmed: !root.ready || !root.service.enabled || root.service.paused
    active: root.ready && root.service.lastError !== ""
    tooltipText: !root.ready ? "Tessella unavailable"
      : root.service.themeLabel() + " · " + root.service.nextText()
    onPressed: function(code) {
      if (code === Qt.MiddleButton) { if (root.ready) root.service.applyNext() }
      else root.toggle()
    }
  }
}
