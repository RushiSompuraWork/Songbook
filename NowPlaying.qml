import QtQuick
import QtQuick.Layouts
import Quickshell
import qs.Commons
import qs.Ui

// What is playing, at the top of the card: the art square (and the dancer),
// the title, the time, the buttons, and the line that says what the card is
// doing. Taken out of MusicPanel.qml on 2026-09-24, where it sat among the
// logic; it draws and it calls back, and holds no state of its own.
//
// `panel` is the MusicPanel it belongs to.
ColumnLayout {
  id: nowPlaying
  required property var panel
  spacing: Style.space(10)

RowLayout {
  Layout.fillWidth: true
  spacing: Style.space(10)

  // The art square: album art, or (click) the dancer, or (click) art
  // again. With no art it shows the kind of thing playing.
  BorderSurface {
    id: artBox
    Layout.preferredWidth: Style.space(64)
    Layout.preferredHeight: Style.space(64)
    radius: Style.spacing.labelGap
    // No frame: the art and the dancer sit straight on the card. A faint
    // fill only when there is nothing else to show.
    color: artBox.dance || artBox.hasArt ? "transparent" : Style.normalFillFor(panel.fg, panel.accent)
    borderSpec: Border.none()

    readonly property bool dance: panel.svc && panel.svc.showDance
    readonly property bool hasArt: panel.svc && panel.svc.art !== ""

    Image {
      anchors.fill: parent
      visible: !artBox.dance && artBox.hasArt && status === Image.Ready
      // Always a local file written by card.py, never a web address.
      source: artBox.hasArt ? "file://" + panel.svc.art : ""
      sourceSize.width: Style.space(128)
      sourceSize.height: Style.space(128)
      fillMode: Image.PreserveAspectCrop
      asynchronous: true
    }

    Text {
      id: dancer
      textFormat: Text.PlainText
      anchors.fill: parent
      anchors.margins: Style.space(4)
      visible: artBox.dance
      text: panel.svc ? (panel.svc.playing ? panel.svc.danceFrame : "(￣ー￣)zz") : ""
      color: panel.accent
      font.family: panel.fontFamily
      font.pixelSize: Style.font.subtitle
      fontSizeMode: Text.HorizontalFit
      minimumPixelSize: 8
      horizontalAlignment: Text.AlignHCenter
      verticalAlignment: Text.AlignVCenter

      // Pops on the beat in two steps, as on the bar (see BarWidget):
      // a smooth animation redraws at the screen's full refresh rate.
      property bool popped: false
      scale: popped ? 1.15 : 1.0

      Timer {
        id: cardUnpop
        interval: 120
        onTriggered: parent.popped = false
      }
    }

    Text {
      textFormat: Text.PlainText
      anchors.centerIn: parent
      visible: !artBox.dance && !artBox.hasArt
      // No cover: what is playing says what it is.
      text: panel.svc ? panel.kindGlyph(panel.svc.kind) : "󰝚"
      color: panel.svc && panel.svc.playing ? panel.accent : panel.fg
      font.family: panel.fontFamily
      font.pixelSize: Style.font.displayLarge
    }

    MouseArea {
      anchors.fill: parent
      cursorShape: Qt.PointingHandCursor
      onClicked: if (panel.svc) panel.svc.cycleArt()
    }

    Connections {
      target: panel.svc
      function onBeatCountChanged() {
        if (!artBox.dance) return
        dancer.popped = true
        cardUnpop.restart()
      }
    }
  }

  ColumnLayout {
    Layout.fillWidth: true
    spacing: Style.space(3)

    Text {
      textFormat: Text.PlainText
      Layout.fillWidth: true
      text: {
        if (!panel.svc || !panel.svc.reachable) return "MPD is not running"
        if (panel.svc.isRadio) return panel.svc.station || "Radio"
        return panel.svc.title || "Nothing playing"
      }
      color: panel.fg
      font.family: panel.fontFamily
      font.pixelSize: Style.font.subtitle
      font.bold: true
      elide: Text.ElideRight

      // The name leads to where it lives: its folder, its book, the
      // station list it came from.
      MouseArea {
        anchors.fill: parent
        enabled: panel.svc && panel.svc.queueLength > 0
        cursorShape: enabled ? Qt.PointingHandCursor : Qt.ArrowCursor
        onClicked: panel.goToPlaying()
      }
    }

    Text {
      textFormat: Text.PlainText
      Layout.fillWidth: true
      text: !panel.svc ? "" : (panel.svc.isRadio
        ? (panel.svc.title !== "" ? panel.svc.title : "Live")
        : (panel.svc.currentIsFav ? "󰓎  " : "") + panel.svc.artist)
      visible: text !== ""
      color: panel.dim
      font.family: panel.fontFamily
      font.pixelSize: Style.font.bodySmall
      elide: Text.ElideRight
    }
  }

  // The one extra control: keep the song the radio is playing.
  PanelActionButton {
    visible: panel.svc && panel.svc.title !== "" && (panel.svc.isRadio || panel.svc.kind === "youtube")
    iconText: "󰇚"
    tooltipText: "Save this song (Ctrl+Space)"
    foreground: panel.dim
    onClicked: panel.saveCurrent()
  }
}

// seek
RowLayout {
  Layout.fillWidth: true
  visible: panel.svc && !panel.svc.isRadio && panel.svc.duration > 0
  spacing: Style.space(6)

  Text {
    textFormat: Text.PlainText
    text: panel.clock(panel.svc ? panel.svc.partPosition : 0) || "0:00"
    color: panel.dim
    font.family: panel.fontFamily
    font.pixelSize: Style.font.caption
  }

  PanelSlider {
    Layout.fillWidth: true
    bar: panel.bar
    minimum: 0
    maximum: Math.max(1, panel.svc ? panel.svc.partDuration : 1)
    step: 1
    integer: true
    value: panel.svc ? panel.svc.partPosition : 0
    fillColor: panel.accent
    onReleased: function(v) { if (panel.svc) panel.svc.seekPart(v) }
  }

  Text {
    textFormat: Text.PlainText
    text: panel.clock(panel.svc ? panel.svc.partDuration : 0)
    color: panel.dim
    font.family: panel.fontFamily
    font.pixelSize: Style.font.caption
  }
}

// transport
RowLayout {
  Layout.fillWidth: true
  spacing: Style.space(6)

  // Sleep: one icon, each click the next step (off, 15, 30, 60 min,
  // after this song; in a book, the end of this chapter).
  Button {
    iconText: "󰒲"
    foreground: panel.svc && panel.svc.sleepStep !== 0 ? panel.accent : panel.faint
    iconSize: Style.font.icon
    tooltipText: panel.svc ? panel.svc.sleepLabel : ""
    onClicked: panel.svc.cycleSleep()
  }

  Item { Layout.fillWidth: true }

  // In a book the row is: chapter back, ⟲, play, ⟳, chapter ahead (his
  // layout, 2026-09-22): hearing a sentence again, or skipping a little,
  // is what a long book needs most, so those sit next to play.
  // Otherwise: shuffle, previous, play, next, repeat.
  readonly property bool bookMode: panel.svc && panel.svc.kind === "book"

  Button {
    iconText: "󰒮"
    foreground: panel.fg
    iconSize: Style.font.iconLarge
    visible: parent.bookMode
    tooltipText: "Chapter back"
    onClicked: panel.svc.skip(-1)
  }
  Button {
    iconText: "󰒟"
    foreground: panel.svc && panel.svc.randomOn ? panel.accent : panel.fg
    iconSize: Style.font.iconLarge
    visible: panel.svc && !panel.svc.isRadio && !parent.bookMode
    tooltipText: "Shuffle"
    onClicked: panel.svc.toggleRandom()
  }
  Button {
    iconText: parent.bookMode ? "󰑟" : "󰒮"
    foreground: panel.fg
    iconSize: Style.font.iconLarge
    enabled: panel.svc && (!panel.svc.isRadio || panel.svc.queueLength > 1 || panel.svc.stars.length > 1)
    tooltipText: parent.bookMode ? "Back " + (panel.svc.settings.bookSkipBack || 10) + " s" : ""
    onClicked: parent.bookMode ? panel.svc.seekBy(-(panel.svc.settings.bookSkipBack || 10))
                               : panel.svc.skip(-1)
  }
  Button {
    iconText: panel.svc && panel.svc.playing ? "󰏤" : "󰐊"
    foreground: panel.fg
    iconSize: Style.font.iconLarge
    onClicked: panel.svc.togglePlay()
  }
  Button {
    iconText: parent.bookMode ? "󰈑" : "󰒭"
    foreground: panel.fg
    iconSize: Style.font.iconLarge
    enabled: panel.svc && (!panel.svc.isRadio || panel.svc.queueLength > 1 || panel.svc.stars.length > 1)
    tooltipText: parent.bookMode ? "Ahead " + (panel.svc.settings.bookSkipForward || 30) + " s" : ""
    onClicked: parent.bookMode ? panel.svc.seekBy(panel.svc.settings.bookSkipForward || 30)
                               : panel.svc.skip(1)
  }
  Button {
    iconText: "󰑖"
    foreground: panel.svc && panel.svc.repeatOn ? panel.accent : panel.fg
    iconSize: Style.font.iconLarge
    visible: panel.svc && !panel.svc.isRadio && !parent.bookMode
    tooltipText: "Repeat"
    onClicked: panel.svc.toggleRepeat()
  }
  Button {
    iconText: "󰒭"
    foreground: panel.fg
    iconSize: Style.font.iconLarge
    visible: parent.bookMode
    tooltipText: "Chapter ahead"
    onClicked: panel.svc.skip(1)
  }

  Item { Layout.fillWidth: true }

  Button {
    iconText: "󰒓"
    foreground: panel.topView === "settings" ? panel.accent : panel.faint
    iconSize: Style.font.icon
    tooltipText: "Settings"
    onClicked: panel.topView === "settings" ? panel.goHome() : panel.showTop("settings")
  }
}

// download progress, or a short message; a problem gets a Copy button
RowLayout {
  Layout.fillWidth: true
  visible: statusText.text !== ""
  spacing: Style.space(4)

Text {
  id: statusText
  textFormat: Text.PlainText
  Layout.fillWidth: true
  text: {
    if (panel.message !== "") return panel.message
    // Reading a novel aloud: how far the chapter has got.
    if (panel.svc && panel.svc.speakingNow)
      return "󰔊  Speaking " + (panel.svc.speaking.title || "") + " · "
             + Math.round(Number(panel.svc.speaking.percent || 0)) + "%  ("
             + (panel.svc.speaking.done || 0) + " of " + (panel.svc.speaking.total || 0)
             + " sentences)"
    if (panel.dlActive) return "󰇚  " + panel.dl.title + "  " + Math.round(Number(panel.dl.percent || 0)) + "%"
    if (panel.dlRecent && panel.dl.state === "done") return "󰄬  Saved to " + panel.dl.folder
    if (panel.dlRecent && panel.dl.state === "failed") return "Download failed" + (panel.dl.error ? ": " + panel.dl.error : "")
    return ""
  }
  color: (panel.message !== "" && panel.messageIsError) || (panel.message === "" && panel.dl.state === "failed")
         ? Color.urgent : panel.dim
  font.family: panel.fontFamily
  font.pixelSize: Style.font.caption
  // Long problems wrap, so all of it can be read before copying.
  wrapMode: panel.problemText !== "" ? Text.WordWrap : Text.NoWrap
  maximumLineCount: 4
  elide: Text.ElideRight
}

Button {
  visible: panel.problemText !== ""
  iconText: "󰆏"
  foreground: panel.faint
  iconSize: Style.font.icon
  tooltipText: "Copy the message"
  onClicked: panel.copyText(panel.problemText)
}
}
}
