import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import qs.Commons
import qs.Ui

Panel {
  id: root
  moduleName: "goldbarth.scenula"
  manageIpc: false

  property var anchorItem: null
  property var hostWidget: null
  property var service: null
  readonly property var barIdentity: hostWidget || root
  readonly property color foreground: bar ? bar.foreground : Color.foreground
  readonly property color dim: Qt.darker(foreground, 1.45)
  readonly property color urgent: bar ? bar.urgent : Color.urgent
  readonly property string fontFamily: bar ? bar.fontFamily : Style.font.family
  property string addError: ""
  property bool hiddenExpanded: false

  readonly property var groupLabels: ({ setting: "Setting", subject: "Subject", mood: "Mood",
                                        light: "Light", color: "Color" })
  readonly property var intervalOptions: [5, 10, 15, 30, 45, 60, 90, 120, 180, 360, 720, 1440]
    .map(function(m) {
      return { value: String(m), label: m < 60 ? m + " min" : (m / 60) + " h" }
    })

  function open() {
    if (service) {
      service.nowEpoch = Date.now()
      service.refreshStatus()
    }
    controller.show()
  }
  function close() { controller.hide() }
  function toggle() { opened ? close() : open() }

  function switchPanel(direction) {
    if (bar && typeof bar.switchPanelFrom === "function")
      return bar.switchPanelFrom(barIdentity, direction)
    return false
  }

  function submitArtist() {
    if (!service) return
    root.addError = service.addArtist(artistField.text)
    if (!root.addError) artistField.text = ""
  }

  function artistError(name) {
    var errors = service && service.sync.artistErrors ? service.sync.artistErrors : {}
    return errors[name] || ""
  }

  function themeOptions() {
    if (!service) return []
    return service.themes.map(function(t) {
      return { value: t.name, label: t.name + " (" + t.count + ")",
               description: root.groupLabels[t.group] || t.group }
    })
  }

  function paletteOptions() {
    if (!service) return []
    return [{ value: "", label: "None", description: "Pick by theme only" }].concat(
      service.palettes.map(function(p) {
        return { value: p.id, label: p.label, description: p.light ? "Light" : "Dark" }
      }))
  }

  function enabledThemes() {
    if (!service) return []
    return service.themes
      .map(function(t) { return t.name })
      .filter(function(n) { return service.disabledThemes.indexOf(n) === -1 })
  }

  KeyboardPanel {
    id: panel
    anchorItem: root.anchorItem
    owner: root.barIdentity
    bar: root.bar
    open: root.opened
    focusTarget: keyCatcher
    contentWidth: panel.fittedContentWidth(Style.space(520))
    contentHeight: panel.fittedContentHeight(content.implicitHeight, Style.space(760))

    PanelKeyCatcher {
      id: keyCatcher
      anchors.fill: parent
      onCloseRequested: root.close()
      onTabRequested: function(direction) { root.switchPanel(direction) }
      onTextKey: function(text) {
        if (!root.service) return
        if (text === "n" || text === "N") root.service.applyNext()
        else if (text === "b" || text === "B") root.service.applyBack()
        else if ((text === "p" || text === "P") && root.service.enabled) root.service.togglePaused()
      }

      Flickable {
        anchors.fill: parent
        contentWidth: width
        contentHeight: content.implicitHeight
        clip: true
        boundsBehavior: Flickable.StopAtBounds
        interactive: contentHeight > height
        ScrollBar.vertical: ScrollBar { policy: ScrollBar.AsNeeded }

        ColumnLayout {
          id: content
          width: parent.width
          spacing: Style.space(12)

          PanelHero {
            Layout.fillWidth: true
            title: root.service ? root.service.themeLabel() : "Scenula"
            meta: !root.service ? "Service unavailable"
              : [root.service.layoutLabel(), root.service.paletteLabel(), root.service.nextText()]
                  .filter(function(part) { return part !== "" }).join(" · ")
            foreground: root.foreground
            fontFamily: root.fontFamily
            iconComponent: Component {
              Text {
                text: "󰸉"
                textFormat: Text.PlainText
                color: root.foreground
                font.family: root.fontFamily
                font.pixelSize: Style.font.display
              }
            }
          }

          // Current triptych, decoded only while the panel is open.
          Image {
            id: preview
            Layout.fillWidth: true
            Layout.preferredHeight: implicitWidth > 0 ? width * implicitHeight / implicitWidth : width * 0.42
            visible: root.service && root.service.current !== null
            source: root.opened && root.service && root.service.current
              ? Util.fileUrl(root.service.current.path) : ""
            sourceSize.width: 1040
            fillMode: Image.PreserveAspectFit
            asynchronous: true
            cache: false
          }

          Repeater {
            model: root.service && root.service.current ? root.service.current.images : []
            delegate: RowLayout {
              id: imageRow
              required property var modelData
              readonly property bool hidden: root.service !== null && root.service.isHidden(modelData.key)
              Layout.fillWidth: true
              spacing: Style.space(8)

              Text {
                Layout.fillWidth: true
                text: "󰌹  " + imageRow.modelData.title + "  ·  " + imageRow.modelData.artist
                textFormat: Text.PlainText
                elide: Text.ElideRight
                font.strikeout: imageRow.hidden
                color: titleMouse.containsMouse ? root.foreground : root.dim
                font.family: root.fontFamily
                font.pixelSize: Style.font.bodySmall
                MouseArea {
                  id: titleMouse
                  anchors.fill: parent
                  hoverEnabled: true
                  cursorShape: Qt.PointingHandCursor
                  onClicked: Qt.openUrlExternally(imageRow.modelData.link)
                }
              }

              Button {
                iconText: String.fromCodePoint(0xf04e1)  // swap
                tooltipText: "Replace this image, keep the others"
                verticalPadding: 0  // keep the image list as dense as plain text rows
                enabled: root.service !== null && !root.service.busy
                opacity: enabled ? 1 : 0.5
                foreground: root.foreground
                accent: Color.accent
                fontFamily: root.fontFamily
                onClicked: root.service.replaceImage(imageRow.modelData.key)
              }

              Button {
                iconText: String.fromCodePoint(imageRow.hidden ? 0xf0208 : 0xf0209)  // eye / eye-off
                tooltipText: imageRow.hidden ? "Allow in future collages again"
                                             : "Hide from future collages (stays until the next change)"
                verticalPadding: 0
                foreground: root.foreground
                accent: Color.accent
                fontFamily: root.fontFamily
                onClicked: root.service.setHidden(imageRow.modelData.key, !imageRow.hidden)
              }
            }
          }

          RowLayout {
            Layout.fillWidth: true
            spacing: Style.space(8)

            Button {
              text: "Back"
              iconText: String.fromCodePoint(0xf004d)  // arrow left
              tooltipText: "Show the previous collage again"
              bordered: true
              enabled: root.service !== null && root.service.backState.available && !root.service.busy
              opacity: enabled ? 1 : 0.5  // the kit's Button has no disabled look
              foreground: root.foreground
              accent: Color.accent
              fontFamily: root.fontFamily
              onClicked: root.service.applyBack()
            }

            Button {
              Layout.fillWidth: true
              text: root.service && root.service.busy ? "Switching…" : "Next wallpaper"
              iconText: "󰑐"
              bordered: true
              enabled: root.service && !root.service.busy
              foreground: root.foreground
              accent: Color.accent
              fontFamily: root.fontFamily
              onClicked: root.service.applyNext()
            }

            // Holds the current collage; only meaningful while rotation is on.
            Button {
              readonly property bool paused: root.service !== null && root.service.paused
              visible: root.service !== null && root.service.enabled
              Layout.fillWidth: true
              text: paused ? "Resume" : "Pause"
              iconText: String.fromCodePoint(paused ? 0xf040a : 0xf03e4)  // play / pause
              tooltipText: paused ? "Resume rotation; the next change follows after a full interval"
                                  : "Keep this collage until you resume"
              bordered: true
              foreground: root.foreground
              accent: Color.accent
              fontFamily: root.fontFamily
              onClicked: root.service.togglePaused()
            }

            // Turns into a cancel button while a sync runs.
            Button {
              readonly property bool syncing: root.service !== null && root.service.syncing
              readonly property bool cancelling: root.service !== null && root.service.cancelling
              Layout.fillWidth: true
              text: cancelling ? "Stopping…" : syncing ? "Cancel sync" : "Sync now"
              iconText: syncing ? "󰜺" : "󰓦"
              bordered: true
              enabled: root.service !== null && !cancelling
              foreground: root.foreground
              accent: Color.accent
              fontFamily: root.fontFamily
              onClicked: syncing ? root.service.cancelSync() : root.service.startSync()
            }
          }

          // A previous collage exists but cannot be shown on this screen or library.
          Text {
            Layout.fillWidth: true
            visible: root.service !== null && root.service.backState.exists && !root.service.backState.available
            text: root.service ? "Back unavailable: " + root.service.backState.reason + "." : ""
            textFormat: Text.PlainText
            color: root.dim
            font.family: root.fontFamily
            font.pixelSize: Style.font.caption
            wrapMode: Text.WordWrap
          }

          Text {
            Layout.fillWidth: true
            text: root.service ? root.service.syncText() : ""
            textFormat: Text.PlainText
            color: root.dim
            font.family: root.fontFamily
            font.pixelSize: Style.font.caption
            wrapMode: Text.WordWrap
          }

          Text {
            Layout.fillWidth: true
            visible: text !== ""
            text: {
              if (!root.service) return ""
              var lines = []
              if (root.service.lastError) lines.push(root.service.lastError)
              var errors = root.service.sync.errors || []
              for (var i = 0; i < Math.min(errors.length, 3); i++) lines.push(errors[i])
              if (errors.length > 3) lines.push("… and " + (errors.length - 3) + " more")
              return lines.join("\n")
            }
            textFormat: Text.PlainText
            color: root.urgent
            font.family: root.fontFamily
            font.pixelSize: Style.font.caption
            wrapMode: Text.WordWrap
          }

          PanelSeparator { Layout.fillWidth: true; foreground: root.foreground }

          PanelSectionHeader {
            Layout.fillWidth: true
            text: "ROTATION"
            foreground: root.foreground
            fontFamily: root.fontFamily
          }

          Toggle {
            Layout.fillWidth: true
            label: "Rotate automatically"
            description: "Replaces the theme background with rotating ArtStation collages."
            checked: root.service ? root.service.enabled : false
            foreground: root.foreground
            accent: Color.accent
            fontFamily: root.fontFamily
            enabled: root.service !== null
            onClicked: root.service.setEnabled(!root.service.enabled)
          }

          RowLayout {
            Layout.fillWidth: true
            spacing: Style.space(8)

            Dropdown {
              Layout.fillWidth: true
              label: "Interval"
              value: root.service ? String(root.service.intervalMinutes) : "30"
              options: root.intervalOptions
              foreground: root.foreground
              fontFamily: root.fontFamily
              onChanged: function(value) { root.service.setIntervalMinutes(value) }
            }

            MultiSelect {
              Layout.fillWidth: true
              label: "Themes"
              options: root.themeOptions()
              values: root.enabledThemes()
              placeholderText: "Search themes…"
              noSelectionText: "None"
              emptyText: "No themes yet"
              foreground: root.foreground
              fontFamily: root.fontFamily
              onChanged: function(values) { root.service.setEnabledThemes(values) }
            }
          }

          Text {
            Layout.fillWidth: true
            text: "Deselected themes are never rolled. Their images can still show up through other themes."
            textFormat: Text.PlainText
            color: root.dim
            font.family: root.fontFamily
            font.pixelSize: Style.font.caption
            wrapMode: Text.WordWrap
          }

          SearchableDropdown {
            Layout.fillWidth: true
            label: "Palette"
            value: root.service ? root.service.palette : ""
            options: root.paletteOptions()
            placeholderText: "Search palettes…"
            foreground: root.foreground
            fontFamily: root.fontFamily
            onChanged: function(value) { root.service.setPalette(value) }
          }

          Text {
            Layout.fillWidth: true
            text: root.service ? root.service.paletteText() : ""
            textFormat: Text.PlainText
            color: root.dim
            font.family: root.fontFamily
            font.pixelSize: Style.font.caption
            wrapMode: Text.WordWrap
          }

          // Hidden images, collapsed to one line; each can be allowed again.
          Text {
            Layout.fillWidth: true
            visible: root.service !== null && root.service.hiddenList.length > 0
            text: (root.hiddenExpanded ? "▾ " : "▸ ") + (root.service ? root.service.hiddenList.length : 0)
              + " hidden " + (root.service && root.service.hiddenList.length === 1 ? "image" : "images")
            textFormat: Text.PlainText
            color: hiddenMouse.containsMouse ? root.foreground : root.dim
            font.family: root.fontFamily
            font.pixelSize: Style.font.caption
            MouseArea {
              id: hiddenMouse
              anchors.fill: parent
              hoverEnabled: true
              cursorShape: Qt.PointingHandCursor
              onClicked: root.hiddenExpanded = !root.hiddenExpanded
            }
          }

          Repeater {
            model: root.hiddenExpanded && root.service ? root.service.hiddenList : []
            delegate: RowLayout {
              id: hiddenRow
              required property var modelData
              Layout.fillWidth: true
              spacing: Style.space(8)

              Text {
                Layout.fillWidth: true
                text: hiddenRow.modelData.title + "  ·  " + hiddenRow.modelData.artist
                textFormat: Text.PlainText
                elide: Text.ElideRight
                color: hiddenTitleMouse.containsMouse && hiddenRow.modelData.link ? root.foreground : root.dim
                font.family: root.fontFamily
                font.pixelSize: Style.font.bodySmall
                MouseArea {
                  id: hiddenTitleMouse
                  anchors.fill: parent
                  hoverEnabled: true
                  cursorShape: hiddenRow.modelData.link ? Qt.PointingHandCursor : Qt.ArrowCursor
                  onClicked: if (hiddenRow.modelData.link) Qt.openUrlExternally(hiddenRow.modelData.link)
                }
              }

              Button {
                iconText: String.fromCodePoint(0xf0208)  // eye
                tooltipText: "Allow in future collages again"
                verticalPadding: 0
                foreground: root.foreground
                accent: Color.accent
                fontFamily: root.fontFamily
                onClicked: root.service.setHidden(hiddenRow.modelData.key, false)
              }
            }
          }

          PanelSeparator { Layout.fillWidth: true; foreground: root.foreground }

          PanelSectionHeader {
            Layout.fillWidth: true
            text: "ARTISTS"
            foreground: root.foreground
            fontFamily: root.fontFamily
          }

          Repeater {
            model: root.service ? root.service.artists : []
            delegate: RowLayout {
              id: artistRow
              required property var modelData
              readonly property var stats: {
                var list = root.service ? root.service.artistStats : []
                for (var i = 0; i < list.length; i++) if (list[i].name === modelData) return list[i]
                return null
              }
              readonly property string error: root.artistError(modelData)
              Layout.fillWidth: true
              spacing: Style.space(8)

              Text {
                Layout.fillWidth: true
                text: artistRow.modelData
                textFormat: Text.PlainText
                elide: Text.ElideRight
                color: nameMouse.containsMouse ? Color.accent : root.foreground
                font.family: root.fontFamily
                font.pixelSize: Style.font.body
                MouseArea {
                  id: nameMouse
                  anchors.fill: parent
                  hoverEnabled: true
                  cursorShape: Qt.PointingHandCursor
                  onClicked: Qt.openUrlExternally("https://www.artstation.com/" + artistRow.modelData)
                }
              }
              Text {
                text: artistRow.error !== "" ? artistRow.error
                  : (artistRow.stats ? artistRow.stats.images + " images" : "new")
                textFormat: Text.PlainText
                color: artistRow.error !== "" ? root.urgent : root.dim
                font.family: root.fontFamily
                font.pixelSize: Style.font.bodySmall
              }
              Button {
                iconText: "󰆴"
                tooltipText: "Remove (deletes the downloaded images)"
                foreground: root.foreground
                accent: Color.accent
                fontFamily: root.fontFamily
                onClicked: root.service.removeArtist(artistRow.modelData)
              }
            }
          }

          RowLayout {
            Layout.fillWidth: true
            spacing: Style.space(8)

            TextField {
              id: artistField
              Layout.fillWidth: true
              placeholderText: "Username or ArtStation link"
              foreground: root.foreground
              font.family: root.fontFamily
              onTextChanged: root.addError = ""
              Keys.onPressed: function(event) {
                if (event.key === Qt.Key_Return || event.key === Qt.Key_Enter) {
                  root.submitArtist()
                  event.accepted = true
                } else if (event.key === Qt.Key_Escape) {
                  artistField.text = ""
                  keyCatcher.forceActiveFocus()
                  event.accepted = true
                }
              }
            }

            Button {
              text: "Add"
              iconText: "󰐕"
              bordered: true
              enabled: artistField.text.trim() !== ""
              foreground: root.foreground
              accent: Color.accent
              fontFamily: root.fontFamily
              onClicked: root.submitArtist()
            }
          }

          Text {
            Layout.fillWidth: true
            visible: root.addError !== ""
            text: root.addError
            textFormat: Text.PlainText
            color: root.urgent
            font.family: root.fontFamily
            font.pixelSize: Style.font.caption
          }

          Text {
            Layout.fillWidth: true
            text: "New artists are downloaded right away and tagged locally with " + (root.service && root.service.config.visionModel
              ? root.service.config.visionModel : "gemma3:12b") + ". Covers are skipped. Press N for the next wallpaper, B to go back, P to pause or resume."
            textFormat: Text.PlainText
            color: root.dim
            font.family: root.fontFamily
            font.pixelSize: Style.font.caption
            wrapMode: Text.WordWrap
          }
        }
      }
    }
  }
}
