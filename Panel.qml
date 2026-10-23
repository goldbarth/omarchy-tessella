import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import qs.Commons
import qs.Ui

Panel {
  id: root
  moduleName: "goldbarth.tessella"
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
        if ((text === "n" || text === "N") && root.service) root.service.applyNext()
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
            title: root.service ? root.service.themeLabel() : "Tessella"
            meta: !root.service ? "Service unavailable"
              : (root.service.layoutLabel() ? root.service.layoutLabel() + " · " : "") + root.service.nextText()
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
            delegate: Text {
              required property var modelData
              Layout.fillWidth: true
              text: "󰌹  " + modelData.title + "  ·  " + modelData.artist
              textFormat: Text.PlainText
              elide: Text.ElideRight
              color: titleMouse.containsMouse ? root.foreground : root.dim
              font.family: root.fontFamily
              font.pixelSize: Style.font.bodySmall
              MouseArea {
                id: titleMouse
                anchors.fill: parent
                hoverEnabled: true
                cursorShape: Qt.PointingHandCursor
                onClicked: Qt.openUrlExternally(modelData.link)
              }
            }
          }

          RowLayout {
            Layout.fillWidth: true
            spacing: Style.space(8)

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
              ? root.service.config.visionModel : "gemma3:12b") + ". Covers are skipped. Press N for the next wallpaper."
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
