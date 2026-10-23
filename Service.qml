import QtQuick
import Quickshell
import Quickshell.Io

// Schedules wallpaper changes and syncs. All real work happens in artwall.py;
// this service only decides *when* and mirrors its JSON status for the UI.
Item {
  id: root

  readonly property string home: Quickshell.env("HOME")
  readonly property string configDir: home + "/.config/omarchy/artstation-wallpaper"
  readonly property string configPath: configDir + "/config.json"
  readonly property string stateDir: home + "/.local/state/omarchy-artwall"
  readonly property string themeNamePath: home + "/.local/state/omarchy/current/theme.name"
  readonly property string backgroundLink: home + "/.local/state/omarchy/current/background"
  readonly property string script: decodeURIComponent(
    String(Qt.resolvedUrl("artwall.py")).replace(/^file:\/\//, ""))

  readonly property int syncMaxAgeMs: 24 * 3600 * 1000
  readonly property int retryDelayMs: 5 * 60 * 1000

  // Config (written only by this service; unknown keys are preserved).
  property bool configLoaded: false
  property var config: ({})
  // Off until the user switches it on: it replaces the theme background.
  readonly property bool enabled: config.enabled === true
  readonly property int intervalMinutes: Number(config.intervalMinutes) || 30
  readonly property var artists: Array.isArray(config.artists) ? config.artists : []
  readonly property var disabledThemes: Array.isArray(config.disabledThemes) ? config.disabledThemes : []

  // Status mirrored from `artwall.py status`.
  property bool statusLoaded: false
  property var status: ({})
  readonly property var current: status.state && status.state.current ? status.state.current : null
  readonly property var sync: status.sync || ({})
  readonly property var themes: status.themes || []
  readonly property var artistStats: status.artists || []
  readonly property bool syncing: syncProc.running || sync.running === true
  readonly property bool busy: nextProc.running

  property string lastError: ""
  property double nowEpoch: Date.now()
  property double retryAfter: 0
  property bool syncQueued: false
  property double lastAutoSync: 0
  property bool statusQueued: false
  property bool rerenderQueued: false
  property bool rerenderFull: false
  property double themeChangedAt: 0

  // Monitor names and sizes. A change (other monitor, resolution, rotation)
  // re-fits the wallpaper, e.g. triptych on 21:9 -> two portraits on 16:9.
  readonly property string screenSignature: Quickshell.screens
    .map(function(s) { return s.name + ":" + s.width + "x" + s.height }).join(",")
  onScreenSignatureChanged: screenDebounce.restart()

  // ---------- config ----------

  function applyConfigText(text) {
    var parsed = {}
    try { parsed = text && text.trim() ? JSON.parse(text) : {} }
    catch (e) { root.lastError = "Invalid config.json: " + e }
    root.config = parsed
    root.configLoaded = true
    Qt.callLater(root.reconcile)
  }

  function saveConfig(patch) {
    var next = {}
    for (var k in root.config) next[k] = root.config[k]
    for (var p in patch) next[p] = patch[p]
    root.config = next
    configFile.setText(JSON.stringify(next, null, 2) + "\n")
  }

  // Same rules as normalize_artist() in artwall.py.
  function normalizeArtist(value) {
    var v = String(value || "").trim().toLowerCase()
    var sub = v.match(/^(?:https?:\/\/)?([a-z0-9_-]+)\.artstation\.com/)
    if (sub && sub[1] !== "www") return sub[1]
    v = v.replace(/^(?:https?:\/\/)?(?:www\.)?artstation\.com\//, "")
    v = v.split("/")[0].split("?")[0]
    return /^[a-z0-9_-]+$/.test(v) ? v : ""
  }

  function addArtist(input) {
    var name = root.normalizeArtist(input)
    if (!name) return "Invalid username or link"
    if (root.artists.indexOf(name) !== -1) return name + " is already added"
    root.saveConfig({ artists: root.artists.concat([name]) })
    root.startSync()
    return ""
  }

  function removeArtist(name) {
    root.saveConfig({ artists: root.artists.filter(function(a) { return a !== name }) })
    root.startSync()
  }

  function setEnabled(value) {
    root.saveConfig({ enabled: value === true })
    if (value === true) {
      root.retryAfter = 0
      Qt.callLater(root.applyNext)
    }
  }

  function setIntervalMinutes(minutes) { root.saveConfig({ intervalMinutes: Number(minutes) }) }

  function setEnabledThemes(values) {
    // Keep disabled themes that are currently absent (e.g. artist removed).
    var known = root.themes.map(function(t) { return t.name })
    var disabled = root.disabledThemes.filter(function(t) { return known.indexOf(t) === -1 })
    for (var i = 0; i < known.length; i++)
      if (values.indexOf(known[i]) === -1) disabled.push(known[i])
    root.saveConfig({ disabledThemes: disabled })
  }

  // ---------- actions ----------

  function applyNext() {
    if (nextProc.running) return
    root.lastError = ""
    nextProc.running = true
  }

  function startSync() {
    if (syncProc.running) {
      root.syncQueued = true
      return
    }
    syncProc.running = true
  }

  function refreshStatus() {
    if (statusProc.running) {
      root.statusQueued = true
      return
    }
    statusProc.running = true
  }

  // ---------- scheduling ----------

  function lastChangeMs() { return root.current ? root.current.at * 1000 : 0 }

  function msUntilDue() {
    return root.intervalMinutes * 60000 - (Date.now() - root.lastChangeMs())
  }

  function minutesUntilNext() { return Math.max(0, Math.ceil(root.msUntilDue() / 60000)) }

  function needsSync() {
    if (root.artists.length === 0) return false
    var last = Number(root.sync.lastSync || 0) * 1000
    return Date.now() - last > root.syncMaxAgeMs
  }

  function reconcile() {
    root.nowEpoch = Date.now()
    if (!root.configLoaded || !root.statusLoaded) return
    // Auto-sync at most hourly so a failing network cannot cause a sync loop.
    if (root.needsSync() && !syncProc.running && Date.now() - root.lastAutoSync > 3600000) {
      root.lastAutoSync = Date.now()
      root.startSync()
    }
    if (root.enabled && !root.busy && Date.now() >= root.retryAfter && root.msUntilDue() <= 0)
      root.applyNext()
    root.armTimer()
  }

  // Wake exactly when the next change is due; never spin while off.
  function armTimer() {
    if (!root.enabled) {
      scheduleTimer.stop()
      return
    }
    var wait = Math.max(root.msUntilDue(), root.retryAfter - Date.now())
    scheduleTimer.interval = Math.max(10000, Math.min(wait, 3600000))
    scheduleTimer.restart()
  }

  // Re-apply the current selection. `full` re-renders unconditionally (theme
  // switch: new gutter color); otherwise only when the screen size changed.
  function rerender(full) {
    if (full) root.rerenderFull = true
    if (rerenderProc.running) {
      root.rerenderQueued = true
      return
    }
    rerenderProc.command = ["python3", "-I", root.script, "rerender"]
      .concat(root.rerenderFull ? [] : ["--if-changed"])
    rerenderProc.lastFull = root.rerenderFull
    root.rerenderFull = false
    rerenderProc.running = true
  }

  // Omarchy writes theme.name before it sets the new theme's background. Wait
  // until the background has left our collage (or a few seconds passed, for
  // switches that keep the background), then re-apply over it.
  function onThemeChanged() {
    if (!root.enabled || !root.current) return
    root.themeChangedAt = Date.now()
    themePoll.restart()
  }

  // ---------- status text helpers for the UI ----------

  function layoutLabel() {
    return root.current && root.current.layoutLabel ? root.current.layoutLabel : ""
  }

  function themeLabel() {
    if (!root.current) return "No wallpaper yet"
    return root.current.themes.length ? root.current.themes.join(" + ") : "Random"
  }

  function syncText() {
    if (root.syncing) {
      var phase = { start: "Starting", feeds: "Fetching feeds", download: "Downloading images",
                    tagging: "Tagging" }[root.sync.phase] || "Syncing"
      return phase + (root.sync.total ? " " + root.sync.done + "/" + root.sync.total : "") + "…"
    }
    var last = Number(root.sync.lastSync || 0)
    if (!last) return "Never synced"
    var hours = Math.floor((root.nowEpoch / 1000 - last) / 3600)
    return (root.status.images || 0) + " images · " + root.themes.length + " themes · synced "
      + (hours < 1 ? "< 1 h ago" : hours + " h ago")
  }

  function nextText() {
    if (!root.enabled) return "Automatic rotation off"
    if (root.busy) return "Switching…"
    var m = root.minutesUntilNext()
    return "Next change " + (m > 0 ? "in " + m + " min" : "now")
  }

  // ---------- files and processes ----------

  FileView {
    id: configFile
    path: root.configPath
    watchChanges: true
    printErrors: false
    atomicWrites: true
    onLoaded: root.applyConfigText(text())
    onLoadFailed: root.applyConfigText("")
    onFileChanged: reload()
  }

  FileView {
    id: themeNameFile
    path: root.themeNamePath
    watchChanges: true
    printErrors: false
    onFileChanged: { reload(); root.onThemeChanged() }
  }

  Timer {
    id: themePoll
    interval: 500
    repeat: true
    onTriggered: if (!backgroundProc.running) backgroundProc.running = true
  }

  Process {
    id: backgroundProc
    command: ["readlink", "-f", root.backgroundLink]
    stdout: StdioCollector { id: backgroundOut; waitForEnd: true }
    onExited: {
      var replaced = String(backgroundOut.text).indexOf("/omarchy-artwall/") === -1
      if (replaced || Date.now() - root.themeChangedAt > 15000) {
        themePoll.stop()
        root.rerender(true)
      }
    }
  }

  // Watch the backend's state so CLI runs show up live.
  FileView {
    id: stateFile
    path: root.stateDir + "/state.json"
    watchChanges: true
    printErrors: false
    onFileChanged: { reload(); statusDebounce.restart() }
  }

  FileView {
    id: syncFile
    path: root.stateDir + "/sync.json"
    watchChanges: true
    printErrors: false
    onFileChanged: { reload(); statusDebounce.restart() }
  }

  Timer {
    id: statusDebounce
    interval: 300
    onTriggered: root.refreshStatus()
  }

  Process {
    id: initProc
    // Watchers need existing files.
    command: ["sh", "-c", "mkdir -p \"$1\" \"$2\" && touch \"$2/state.json\" \"$2/sync.json\"",
              "sh", root.configDir, root.stateDir]
    onExited: {
      stateFile.reload()
      syncFile.reload()
      root.refreshStatus()
      // The monitor may have changed while the shell was not running.
      screenDebounce.restart()
    }
  }

  // Hotplug and mode switches emit several geometry changes; act once they settle.
  Timer {
    id: screenDebounce
    interval: 2000
    onTriggered: root.rerender(false)
  }

  Timer {
    id: rerenderRetry
    interval: 2000
    onTriggered: root.rerender(rerenderProc.lastFull)
  }

  Process {
    id: rerenderProc
    property bool lastFull: false
    stderr: StdioCollector { id: rerenderErr; waitForEnd: true }
    onExited: function(code) {
      if (code === 3) {
        rerenderRetry.restart()  // a change was rendering; try again shortly
      } else if (code !== 0) {
        root.lastError = String(rerenderErr.text || "Re-applying the wallpaper failed")
          .trim().split("\n").pop()
      }
      if (root.rerenderQueued) {
        root.rerenderQueued = false
        root.rerender(false)
      }
      root.refreshStatus()
    }
  }

  Process {
    id: statusProc
    command: ["python3", "-I", root.script, "status"]
    stdout: StdioCollector { id: statusOut; waitForEnd: true }
    onExited: function(code) {
      if (code === 0) {
        try {
          root.status = JSON.parse(statusOut.text)
          root.statusLoaded = true
        } catch (e) {
          root.lastError = "Unreadable status: " + e
        }
      }
      if (root.statusQueued) {
        root.statusQueued = false
        root.refreshStatus()
      }
      Qt.callLater(root.reconcile)
    }
  }

  Process {
    id: nextProc
    command: ["python3", "-I", root.script, "next"]
    stderr: StdioCollector { id: nextErr; waitForEnd: true }
    onExited: function(code) {
      if (code === 0 || code === 3) {
        root.retryAfter = 0
      } else {
        // Typically "not enough images yet": retry later instead of spinning.
        root.lastError = String(nextErr.text || "Change failed").trim().split("\n").pop()
        root.retryAfter = Date.now() + root.retryDelayMs
      }
      root.refreshStatus()
    }
  }

  Process {
    id: syncProc
    command: ["python3", "-I", root.script, "sync"]
    stderr: StdioCollector { id: syncErr; waitForEnd: true }
    onExited: function(code) {
      if (code !== 0 && code !== 3)
        root.lastError = String(syncErr.text || "Sync failed").trim().split("\n").pop()
      root.retryAfter = 0
      if (root.syncQueued) {
        root.syncQueued = false
        root.startSync()
      }
      root.refreshStatus()
    }
  }

  Timer {
    id: scheduleTimer
    repeat: false
    onTriggered: root.reconcile()
  }

  // Coarse heartbeat: daily sync check and fresh "in N min" texts.
  Timer {
    interval: 60000
    running: true
    repeat: true
    onTriggered: root.reconcile()
  }

  Component.onCompleted: initProc.running = true
}
