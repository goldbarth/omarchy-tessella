import QtQuick
import Quickshell
import Quickshell.Io

// Schedules wallpaper changes and syncs. All real work happens in tessella.py;
// this service only decides *when* and mirrors its JSON status for the UI.
Item {
  id: root

  readonly property string home: Quickshell.env("HOME")
  readonly property string configDir: home + "/.config/omarchy/tessella"
  readonly property string configPath: configDir + "/config.json"
  readonly property string stateDir: home + "/.local/state/tessella"
  readonly property string themeNamePath: home + "/.local/state/omarchy/current/theme.name"
  readonly property string backgroundLink: home + "/.local/state/omarchy/current/background"
  readonly property string script: decodeURIComponent(
    String(Qt.resolvedUrl("tessella.py")).replace(/^file:\/\//, ""))

  readonly property string migrator: decodeURIComponent(
    String(Qt.resolvedUrl("migrate-to-scenula.sh")).replace(/^file:\/\//, ""))

  readonly property int syncMaxAgeMs: 24 * 3600 * 1000
  readonly property int retryDelayMs: 5 * 60 * 1000

  // Config (written only by this service; unknown keys are preserved).
  property bool configLoaded: false
  property var config: ({})
  // Off until the user switches it on: it replaces the theme background.
  readonly property bool enabled: config.enabled === true
  // Pause holds the current collage without switching rotation off. Resuming
  // starts a fresh interval at resumedAt (epoch seconds) instead of changing at once.
  readonly property bool paused: config.paused === true
  readonly property double resumedAt: Number(config.resumedAt) || 0
  readonly property int intervalMinutes: Number(config.intervalMinutes) || 30
  readonly property var artists: Array.isArray(config.artists) ? config.artists : []
  readonly property var disabledThemes: Array.isArray(config.disabledThemes) ? config.disabledThemes : []
  // "artist/id" keys excluded from future collages; undone from the panel.
  readonly property var hiddenImages: Array.isArray(config.hiddenImages) ? config.hiddenImages : []
  // Palette preset id from palettes.json; "" picks by theme only.
  readonly property string palette: typeof config.palette === "string" ? config.palette : ""

  // Status mirrored from `tessella.py status`.
  property bool statusLoaded: false
  property var status: ({})
  readonly property var current: status.state && status.state.current ? status.state.current : null
  readonly property var sync: status.sync || ({})
  readonly property var themes: status.themes || []
  readonly property var artistStats: status.artists || []
  readonly property var palettes: status.palettes || []
  // Active preset with its close-match count, or null.
  readonly property var paletteStatus: status.palette || null
  readonly property int pendingColors: Number(status.pendingColors) || 0
  // Hidden images with titles for the undo list.
  readonly property var hiddenList: status.hidden || []
  readonly property bool syncing: syncProc.running || sync.running === true
  // Set by cancelSync() until the sync has actually stopped.
  property bool cancelling: false
  onSyncingChanged: if (!syncing) cancelling = false
  readonly property bool busy: nextProc.running || backProc.running || replaceProc.running
  // Whether the previous collage can be shown again, and why not.
  readonly property var backState: status.back || ({ available: false, exists: false, reason: "" })

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

  // Same rules as normalize_artist() in tessella.py.
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
    // Switching rotation on or off always ends a pause.
    root.saveConfig({ enabled: value === true, paused: false })
    if (value === true) {
      root.retryAfter = 0
      Qt.callLater(root.applyNext)
    }
  }

  function setPaused(value) {
    if (value === root.paused) return
    root.saveConfig(value === true ? { paused: true }
                                   : { paused: false, resumedAt: Math.floor(Date.now() / 1000) })
    Qt.callLater(root.reconcile)
  }

  function togglePaused() { root.setPaused(!root.paused) }

  function isHidden(key) { return root.hiddenImages.indexOf(key) !== -1 }

  // Hiding keeps the current collage; the image is skipped from the next pick on.
  function setHidden(key, hidden) {
    if (!key || root.isHidden(key) === hidden) return
    root.saveConfig({ hiddenImages: hidden
      ? root.hiddenImages.concat([key])
      : root.hiddenImages.filter(function(k) { return k !== key }) })
    root.refreshStatus()
  }

  function setPalette(id) {
    root.saveConfig({ palette: String(id || "") })
    // Matching needs every image's colors; sync analyzes the missing ones.
    if (id && root.pendingColors > 0) root.startSync()
    root.refreshStatus()
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

  // Runs in its own terminal: the migration replaces this plugin, so it must
  // outlive the shell process that loaded it.
  function migrateToScenula() {
    Quickshell.execDetached(["omarchy-launch-floating-terminal-with-presentation",
                             "bash '" + root.migrator.replace(/'/g, "'\\''") + "'"])
  }

  function applyNext() {
    if (nextProc.running) return
    root.lastError = ""
    nextProc.running = true
  }

  function applyBack() {
    if (root.busy || !root.backState.available) return
    root.lastError = ""
    backProc.running = true
  }

  // Swap one image of the current collage; the others stay.
  function replaceImage(key) {
    if (root.busy || !key) return
    root.lastError = ""
    replaceProc.command = ["python3", "-I", root.script, "replace", String(key)]
    replaceProc.running = true
  }

  function startSync() {
    if (syncProc.running) {
      root.syncQueued = true
      return
    }
    syncProc.running = true
  }

  // The sync may also run from a terminal, so stop it by PID, not via syncProc.
  function cancelSync() {
    if (!root.syncing || root.cancelling) return
    root.syncQueued = false
    root.cancelling = true
    cancelProc.running = true
  }

  function refreshStatus() {
    if (statusProc.running) {
      root.statusQueued = true
      return
    }
    statusProc.running = true
  }

  // ---------- scheduling ----------

  function lastChangeMs() {
    return Math.max(root.current ? root.current.at * 1000 : 0, root.resumedAt * 1000)
  }

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
    if (root.enabled && !root.paused && !root.busy && Date.now() >= root.retryAfter && root.msUntilDue() <= 0)
      root.applyNext()
    root.armTimer()
  }

  // Wake exactly when the next change is due; never spin while off or paused.
  function armTimer() {
    if (!root.enabled || root.paused) {
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

  // Palette of the current collage, with a note when it had to be loosened.
  function paletteLabel() {
    var p = root.current ? root.current.palette : null
    if (!p) return ""
    return p.label + ({ relaxed: " (relaxed)", none: " (ignored)" }[p.match] || "")
  }

  function paletteText() {
    if (!root.palette) return "No palette: images are picked by theme only."
    var lines = []
    var s = root.paletteStatus
    if (s) lines.push(s.matches + " images match " + s.label + " closely.")
    else lines.push("This palette is no longer available.")
    var p = root.current ? root.current.palette : null
    if (p && p.id === root.palette) {
      if (p.match === "relaxed")
        lines.push("Too few close matches, so this collage uses the next-best images.")
      else if (p.match === "none")
        lines.push("Too few matches for a varied collage, so this one ignores the palette.")
    } else if (s) {
      lines.push("Applies from the next change.")
    }
    if (root.pendingColors > 0)
      lines.push(root.pendingColors + " images still need a color analysis; sync picks them up.")
    return lines.join(" ")
  }

  function themeLabel() {
    if (!root.current) return "No wallpaper yet"
    return root.current.themes.length ? root.current.themes.join(" + ") : "Random"
  }

  function syncText() {
    if (root.syncing) {
      if (root.cancelling) return "Stopping sync…"
      var phase = { start: "Starting", feeds: "Fetching feeds", download: "Downloading images",
                    colors: "Analyzing colors", tagging: "Tagging" }[root.sync.phase] || "Syncing"
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
    if (root.paused) return "Rotation paused"
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
      var replaced = String(backgroundOut.text).indexOf("/tessella/") === -1
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
    // Watchers need existing files. Without config.json, a config written later
    // (by hand or a script) goes unnoticed and the next saveConfig() overwrites it.
    command: ["sh", "-c",
              "mkdir -p \"$1\" \"$2\" && touch \"$1/config.json\" \"$2/state.json\" \"$2/sync.json\"",
              "sh", root.configDir, root.stateDir]
    onExited: {
      configFile.reload()
      stateFile.reload()
      syncFile.reload()
      root.refreshStatus()
      // The monitor may have changed while the shell was not running.
      screenDebounce.restart()
      renameNotice.running = true
    }
  }

  // Reaches users who never open the panel. At most once a day.
  Process {
    id: renameNotice
    command: ["sh", "-c",
              "marker=\"$1/scenula-notice\"; "
              + "[ -n \"$(find \"$marker\" -mmin -1440 2>/dev/null)\" ] && exit 0; "
              + "touch \"$marker\" && notify-send -a Tessella \"Tessella is now Scenula\" "
              + "\"Open Tessella in the bar and choose Migrate to Scenula to keep getting updates.\"",
              "sh", root.stateDir]
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
    id: backProc
    command: ["python3", "-I", root.script, "back"]
    stderr: StdioCollector { id: backErr; waitForEnd: true }
    onExited: function(code) {
      if (code !== 0 && code !== 3)
        root.lastError = String(backErr.text || "Back failed").trim().split("\n").pop()
      root.refreshStatus()
    }
  }

  Process {
    id: replaceProc
    stderr: StdioCollector { id: replaceErr; waitForEnd: true }
    onExited: function(code) {
      // E.g. "No other image fits this slot": the collage stays as it is.
      if (code !== 0 && code !== 3)
        root.lastError = String(replaceErr.text || "Replace failed").trim().split("\n").pop()
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

  Process {
    id: cancelProc
    command: ["python3", "-I", root.script, "cancel"]
    // Covers "no sync running" (stale status): nothing will flip `syncing`.
    onExited: root.refreshStatus()
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
