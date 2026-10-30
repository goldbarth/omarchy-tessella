// Isolated test host. Uses the installed registry, UI types, service and widget.
import QtQuick
import Quickshell
import Quickshell.Io
import "services"

ShellRoot {
  id: root
  property var config: ({})
  property var activeManifest: {
    // Register both dependencies; isEnabled() reads config through a callback.
    var configDependency = root.config
    var plugins = registry.installedPlugins
    for (var id in plugins)
      if (registry.isEnabled(id)) return plugins[id]
    return null
  }

  PluginRegistry {
    id: registry
    shellConfigProvider: function() { return root.config }
    shellConfigMutator: function(mutator) {
      var next = JSON.parse(JSON.stringify(root.config))
      mutator(next)
      root.config = next
      configFile.setText(JSON.stringify(next))
    }
  }

  FileView {
    id: configFile
    path: Quickshell.env("HOME") + "/.config/omarchy/shell.json"
    watchChanges: true
    onLoaded: { root.config = JSON.parse(text()); registry.rescan() }
    onFileChanged: reload()
  }

  QtObject {
    id: api
    function serviceFor(id) {
      return serviceLoader.loadedId === id ? serviceLoader.item : null
    }
  }
  QtObject {
    id: bar
    property var shell: api
    property color foreground: "white"
    property color barForeground: "white"
    property color urgent: "red"
    property string fontFamily: "monospace"
    property bool vertical: false
    property bool foregroundAnimationEnabled: false
    property int barSize: 32
    property string position: "top"
    function hideTooltip(item) { }
    function registerClickTarget(item) { }
    function unregisterClickTarget(item) { }
  }

  Loader {
    id: serviceLoader
    property string loadedId: ""
    source: root.activeManifest ? registry.entryPointUrl(root.activeManifest, "service") : ""
    onSourceChanged: loadedId = ""
    onLoaded: loadedId = root.activeManifest.id
  }
  Loader {
    id: widgetLoader
    source: root.activeManifest ? registry.entryPointUrl(root.activeManifest, "barWidget") : ""
    onLoaded: item.bar = bar
  }

  IpcHandler {
    target: "shell"
    function rescanPlugins(): void { configFile.reload(); registry.rescan() }
    function listPlugins(): string {
      return JSON.stringify(Object.keys(registry.installedPlugins).map(function(id) {
        return { id: id, enabled: registry.isEnabled(id), firstParty: false,
                 name: registry.installedPlugins[id].name,
                 kinds: registry.installedPlugins[id].kinds }
      }))
    }
    function enablePlugin(id: string, placementJson: string): string {
      if (!registry.installedPlugins[id]) return "unknown"
      return registry.setEnabled(id, true, JSON.parse(placementJson)) ? "ok" : registry.lastEnableError
    }
    function setPluginEnabled(id: string, enabled: string): string {
      return registry.setEnabled(id, enabled === "true") ? "ok" : registry.lastEnableError
    }
  }

  IpcHandler {
    target: "test"
    function report(): string {
      var service = serviceLoader.item
      var panelLoaded = widgetLoader.item && widgetLoader.item.children.some(function(child) {
        return child.item && child.item.moduleName === "goldbarth.scenula"
      })
      return JSON.stringify({ ids: Object.keys(registry.installedPlugins),
        active: root.activeManifest ? root.activeManifest.id : "",
        widgetLoaded: widgetLoader.status === Loader.Ready,
        widgetReady: widgetLoader.item ? widgetLoader.item.ready : false,
        panelLoaded: panelLoaded || false,
        initialized: service ? service.initialized : false,
        loaded: service ? service.configLoaded && service.statusLoaded : false,
        current: service ? service.current : null,
        error: service ? service.lastError : "", config: service ? service.config : null })
    }
  }
}
