import QtQuick
import Quickshell
import Quickshell.Io
import qs.Commons
import qs.Ui

BarWidget {
  id: root
  moduleName: "rushi.songbook"

  property bool opened: false

  // One icon, no text: a record for music, a tower for radio, YouTube's
  // mark, a book for an audiobook; dim when idle.
  readonly property string musicIcon: setting("icon", "󰦚")
  readonly property string radioIcon: setting("radioIcon", "󰐹")
  readonly property string youtubeIcon: setting("youtubeIcon", "󰗃")
  readonly property string bookIcon: setting("bookIcon", "󰂺")
  readonly property string kindIcon: card.isRadio ? radioIcon : card.kind === "youtube" ? youtubeIcon
                                     : card.kind === "book" ? bookIcon : musicIcon

  // State comes from CardService's MPD watcher, not from MPRIS: no live
  // player objects are touched here at all.

  // The text dancer on the bar is a setting (off unless switched on in the
  // card's settings). Fixed width, so the bar does not shuffle.
  readonly property bool showDancer: card.barDancer && card.playing
  readonly property string widestFrame: {
    var w = ""
    var f = card.danceFrames
    for (var i = 0; i < f.length; i++) if (f[i].length > w.length) w = f[i]
    return w
  }

  // Settings → Bar → Song title beside the icon: a short, still title.
  // It never scrolls, so it redraws only when the song changes.
  readonly property string shortTitle: {
    if (card.settings.barTitle !== true || card.stopped) return ""
    var t = card.isRadio ? (card.title || card.station) : card.title
    t = String(t || "").trim()
    return t.length > 24 ? t.slice(0, 23).trim() + "…" : t
  }

  TextMetrics {
    id: danceMetrics
    font.family: root.bar ? root.bar.fontFamily : Style.font.family
    font.pixelSize: Style.font.body
    text: root.widestFrame
  }

  function open() { opened = true }
  function close() { opened = false }
  function toggle() { opened = !opened }
  function closeForPopoutSwitch() { opened = false }

  function runBarAction(action) {
    if (action === "next") card.skip(1)
    else if (action === "rmpc")
      Quickshell.execDetached(["omarchy-launch-or-focus-tui", "--app-id=rmpc", "rmpc"])
    else if (action === "card") root.toggle()
    else card.togglePlay()
  }

  function tooltip() {
    if (!card.reachable) return "MPD is not running"
    if (card.isRadio)
      return (card.station || "Radio") + (card.title !== "" ? "\n" + card.title : "")
    if (card.title === "") return "Nothing playing"
    return card.title + (card.artist !== "" ? "\n" + card.artist : "")
  }

  visible: true
  implicitWidth: pill.implicitWidth
  implicitHeight: pill.implicitHeight


  CardService {
    id: card
    panelOpen: root.opened
    onBeatCountChanged: if (root.showDancer) { root.popped = true; unpop.restart() }
  }

  // On each beat the icon pops: bigger at once, back 0.12 s later. Two
  // steps, not a smooth animation: a smooth one redrew the whole bar at the
  // screen's refresh rate (165 Hz here) for 0.2 s every beat, and with the
  // dancer on the shell went from 2% to 17% of a core (measured 2026-09-22).
  property bool popped: false
  Timer {
    id: unpop
    interval: 120
    onTriggered: root.popped = false
  }

  WidgetButton {
    id: pill
    anchors.fill: parent
    bar: root.bar
    text: root.showDancer ? card.danceFrame
          : root.kindIcon + (root.shortTitle !== "" ? "  " + root.shortTitle : "")
    fixedWidth: root.showDancer ? Math.ceil(danceMetrics.width) + 14 : -1
    active: root.opened
    dimmed: !card.playing
    horizontalMargin: 7
    tooltipText: root.opened ? "" : root.tooltip()

    // Middle and right click are set in the card's Shortcuts view.
    onPressed: function(button) {
      if (button === Qt.MiddleButton) root.runBarAction(card.settings.barMiddle || "playPause")
      else if (button === Qt.RightButton) root.runBarAction(card.settings.barRight || "rmpc")
      else root.toggle()
    }

    onWheelMoved: function(delta) {
      if (delta > 0) card.skip(-1)
      else card.skip(1)
    }

    scale: root.popped ? 1.12 : 1.0
  }

  // `omarchy-shell songbook toggle` -- bind it to a key if you like.
  IpcHandler {
    target: "songbook"
    function toggle(): void { root.toggle() }
    function open(): void { root.open() }
    function close(): void { root.close() }
    // Same as clicking the art square: art, then each dance style.
    function art(): void { card.cycleArt() }
    // The same next/previous as the card's buttons: on the radio, the next
    // station of its list. Handy for a Hyprland key binding, and for tests.
    function next(): void { card.skip(1) }
    function previous(): void { card.skip(-1) }
    // Show the song notification now (Settings → Notify), for a test.
    function notify(): void { card.sendNotification() }
    // The sleep icon's next step (off, 15, 30, 60 min, end of song or chapter).
    function sleep(): void { card.cycleSleep() }
    // The terminal reader: the book playing, else the last one read.
    function reader(): void { card.openReader(card.status.book ? card.status.book.folder : "") }
    // Same as clicking the name of what plays: open where it lives.
    function playing(): void {
      root.open()
      Qt.callLater(function() { panel.goToPlaying() })
    }
    function sleepState(): string { return card.sleepLabel }
    // Open on one of the bottom views: home, queue or settings.
    // A screen that needs something to show: `songbook show novel <book path>`,
    // `songbook show folder <name>`. Same check as `view`.
    function show(name: string, arg: string): void {
      if (!panel.knownView(name)) return
      root.open()
      Qt.callLater(function() { panel.go(name, arg) })
    }
    function view(name: string): void {
      // Any screen the card has, by name: `omarchy-shell songbook view setRadio`.
      // It was five of them; every screen can be opened now, which makes them
      // scriptable and lets a change to one be checked without clicking
      // through the card by hand (2026-09-24).
      if (!panel.knownView(name)) return
      root.open()
      Qt.callLater(function() { panel.showTop(name) })
    }
    function views(): string { return panel.allViews().join(" ") }
    // What the card is showing: the screen's name and how many rows are on
    // it. For saying where a problem is, and for checking that a screen the
    // card should be able to draw is not silently drawing nothing.
    function probe(): string {
      // Rows, and the size of the first one. Counting rows alone was not
      // enough: a row with no width still counts, draws every word on top of
      // every other and cannot be clicked -- which is what happened when the
      // list delegate was moved to its own file (2026-09-24).
      return panel.view + " rows=" + panel.rows.length + " " + panel.rowSize()
    }
    // The words on the screen, one row a line: for checking that a row a
    // change should have added is really there. probe() counts rows; this
    // says what they say.
    function titles(): string {
      var out = []
      for (var i = 0; i < panel.rows.length; i++) {
        var r = panel.rows[i]
        out.push((r.title || r.text || "") + (r.sub ? " · " + r.sub : ""))
      }
      return out.join("\n")
    }
    // For testing: act on the row with this key exactly as a mouse click
    // does, so checks go through the same path a person's click does.
    function press(key: string): void {
      var i = panel.indexOfKey(key)
      if (i >= 0) panel.activate(panel.rows[i], "replace")
    }
    // The same for the row's small button on the right (› opens a folder
    // or a book without playing it), so a test can look without playing.
    function pressAction(key: string): void {
      var i = panel.indexOfKey(key)
      if (i >= 0) panel.rowAction(panel.rows[i])
    }
    function help(): void {
      root.open()
      Qt.callLater(function() { panel.go("keys") })
    }
  }

  Connections {
    target: root.bar
    ignoreUnknownSignals: true

    function onActivePopoutChanged() {
      if (root.opened && root.bar && root.bar.activePopout
          && root.bar.activePopout !== root) root.close()
    }
  }

  KeyboardPanel {
    id: popup
    anchorItem: root
    bar: root.bar
    owner: root
    open: root.opened
    focusTarget: panel
    contentWidth: popup.fittedContentWidth(Style.space(360))
    contentHeight: popup.fittedContentHeight(panel.implicitHeight)

    MusicPanel {
      id: panel
      anchors.fill: parent
      bar: root.bar
      svc: card
      opened: root.opened
      onCloseRequested: root.close()
    }
  }
}
