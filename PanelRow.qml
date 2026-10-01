import QtQuick
import QtQuick.Layouts
import qs.Commons
import qs.Ui

// One row of the card's list.
//
// Taken out of MusicPanel.qml on 2026-09-24: that file was 3,009 lines, and
// this is the part with no logic in it at all -- it draws whatever the row
// object says and hands clicks back. `panel` is the MusicPanel it belongs to.
Item {
  id: rowItem
  // The MusicPanel this row belongs to: its colours, its fonts, and where a
  // click goes. Everything this file used to reach through `root`.
  required property var panel
  required property real listWidth       // the list's own width, for wrapping
  required property var modelData
  required property int index

  readonly property var row: modelData
  readonly property bool isHeader: row.type === "header"
  readonly property bool isInfo: row.type === "info" || row.type === "key"
  readonly property bool isError: row.type === "errorrow"
  readonly property bool isKey: row.type === "key"
  readonly property bool isLyric: row.type === "lyric"
  readonly property bool isPara: isLyric && row.data && row.data.para === true
  readonly property bool isTip: row.type === "tip"
  readonly property bool lyricNow: isLyric && row.data.idx >= 0 && row.data.idx === panel.lyricIndex
  readonly property bool hot: index === panel.selectedIndex
  readonly property bool busy: row.key !== undefined && row.key === panel.busyKey

  width: listWidth
  // Lyric lines, tips and problems wrap, so their rows grow with the text.
  height: isHeader ? Style.space(26)
    : (isLyric || isTip || isError ? Math.max(Style.space(22), titleText.implicitHeight + Style.space(8))
       : Style.spacing.popupRowHeight)

  BorderSurface {
    id: rowFill
    anchors.fill: parent
    visible: !rowItem.isHeader && !rowItem.isInfo
    radius: Style.spacing.labelGap
    color: rowItem.hot ? Style.hoverFillFor(panel.fg, panel.accent)
           : (rowItem.row.current ? Style.selectedFillFor(panel.fg, panel.accent) : "transparent")
    borderSpec: rowItem.hot ? Border.controlSpec("hover-cursor", panel.fg, panel.accent) : Border.none()
  }

  // How far he has got through this one, drawn as the row filling from the
  // left rather than said in numbers (his idea, 2026-09-25). Quiet on
  // purpose: it stands in place of "45%", it is not there to be pleasing.
  // Nothing is created for a row with no progress, which is most of them.
  Loader {
    active: Number(rowItem.row.part) > 0
    anchors.left: parent.left
    anchors.top: parent.top
    anchors.bottom: parent.bottom
    width: parent.width * Math.min(1, Math.max(0, Number(rowItem.row.part) || 0))
    sourceComponent: Rectangle {
      radius: Style.spacing.labelGap
      color: panel.fg
      opacity: rowItem.hot ? 0.10 : 0.06
    }
  }

  MouseArea {
    anchors.fill: parent
    hoverEnabled: true
    enabled: !rowItem.isHeader && !rowItem.isInfo
    cursorShape: Qt.PointingHandCursor
    acceptedButtons: Qt.LeftButton | Qt.MiddleButton
    onEntered: panel.selectedIndex = rowItem.index
    onClicked: function(mouse) {
      panel.activate(rowItem.row, mouse.button === Qt.MiddleButton ? "next" : "replace",
                    (mouse.modifiers & Qt.ShiftModifier) !== 0)
    }
  }

  RowLayout {
    anchors.fill: parent
    anchors.leftMargin: Style.space(8)
    anchors.rightMargin: Style.space(4)
    spacing: Style.space(8)

    Text {
      textFormat: Text.PlainText
      visible: !rowItem.isHeader && !rowItem.isInfo
      Layout.preferredWidth: Style.space(16)
      horizontalAlignment: Text.AlignHCenter
      text: rowItem.busy ? "󰦖" : (rowItem.row.glyph || "")
      color: rowItem.row.current ? panel.accent : panel.dim
      font.family: panel.fontFamily
      font.pixelSize: Style.font.body

      RotationAnimator on rotation {
        running: rowItem.busy
        from: 0; to: 360; duration: 900
        loops: Animation.Infinite
      }
    }

    Text {
      id: titleText
      // A book's paragraph is styled (its words escaped in paraHtml);
      // every other row is plain text.
      textFormat: rowItem.isPara ? Text.StyledText : Text.PlainText
      Layout.fillWidth: true
      text: rowItem.isPara ? panel.paraHtml(rowItem.row.data.idx, panel.activeP, panel.activeS,
                                           panel.activeW, panel.highlight)
                           : (rowItem.row.title || "")
      lineHeight: rowItem.isPara ? 1.15 : 1.0
      color: rowItem.isError ? Color.urgent
             : rowItem.isPara ? (rowItem.lyricNow ? (panel.highlight === "paragraph" ? panel.accent : panel.fg)
                                                  : panel.dim)
             : rowItem.isTip ? (rowItem.row.quiet ? panel.dim : panel.fg)
             : rowItem.isLyric ? (rowItem.lyricNow ? panel.accent : panel.dim)
             : rowItem.isKey ? panel.fg : (rowItem.isHeader || rowItem.isInfo || rowItem.row.quiet) ? panel.faint
             : (rowItem.row.current ? panel.accent : panel.fg)
      font.family: panel.fontFamily
      font.pixelSize: rowItem.isHeader ? Style.font.caption
                      : rowItem.isLyric && panel.svc.settings.lyricsSize === "large" ? Style.font.subtitle
                      : Style.font.bodySmall
      font.bold: rowItem.isHeader || (rowItem.lyricNow && !rowItem.isPara) || (rowItem.isTip && !rowItem.row.quiet)
      font.capitalization: rowItem.isHeader ? Font.AllUppercase : Font.MixedCase
      font.italic: rowItem.isInfo && !rowItem.isKey
      wrapMode: rowItem.isLyric || rowItem.isTip || rowItem.isError ? Text.WordWrap : Text.NoWrap
      elide: rowItem.isLyric || rowItem.isTip || rowItem.isError ? Text.ElideNone : Text.ElideRight
    }

    Text {
      textFormat: Text.PlainText
      visible: text !== ""
      Layout.maximumWidth: rowItem.listWidth * 0.4
      text: rowItem.row.sub || ""
      color: panel.faint
      font.family: panel.fontFamily
      font.pixelSize: Style.font.caption
      elide: Text.ElideRight
    }

    PanelActionButton {
      visible: (rowItem.row.action || "") !== ""
               && (rowItem.isHeader || rowItem.hot || rowItem.row.actionOn === true)
      iconText: rowItem.row.action || ""
      tooltipText: rowItem.row.actionTip || ""
      size: Style.space(20)
      fontSize: Style.font.bodySmall
      foreground: rowItem.row.actionOn || rowItem.row.spinning ? panel.accent : panel.dim
      onClicked: panel.rowAction(rowItem.row)

      RotationAnimator on rotation {
        running: rowItem.row.spinning === true
        from: 0; to: 360; duration: 900
        loops: Animation.Infinite
        // Settle upright when it stops, not wherever it was.
        onRunningChanged: if (!running) parent.rotation = 0
      }
    }

    // Up to three more small actions on hover (Up next: ↑ ↓ ✕).
    PanelActionButton {
      readonly property var ex: rowItem.row.extras ? rowItem.row.extras[0] : null
      visible: ex !== null && ex !== undefined && ex.on && rowItem.hot
      iconText: ex ? ex.icon : ""
      tooltipText: ex ? ex.tip : ""
      size: Style.space(20)
      fontSize: Style.font.bodySmall
      foreground: panel.dim
      onClicked: panel.rowExtra(rowItem.row, ex.id)
    }
    PanelActionButton {
      readonly property var ex: rowItem.row.extras ? rowItem.row.extras[1] : null
      visible: ex !== null && ex !== undefined && ex.on && rowItem.hot
      iconText: ex ? ex.icon : ""
      tooltipText: ex ? ex.tip : ""
      size: Style.space(20)
      fontSize: Style.font.bodySmall
      foreground: panel.dim
      onClicked: panel.rowExtra(rowItem.row, ex.id)
    }
    PanelActionButton {
      readonly property var ex: rowItem.row.extras ? rowItem.row.extras[2] : null
      visible: ex !== null && ex !== undefined && ex.on && rowItem.hot
      iconText: ex ? ex.icon : ""
      tooltipText: ex ? ex.tip : ""
      size: Style.space(20)
      fontSize: Style.font.bodySmall
      foreground: panel.dim
      hoverColor: Color.urgent
      onClicked: panel.rowExtra(rowItem.row, ex.id)
    }

    Text {
      textFormat: Text.PlainText
      visible: rowItem.row.chevron === true
      text: "󰅂"
      color: panel.faint
      font.family: panel.fontFamily
      font.pixelSize: Style.font.bodySmall
    }
  }
}
