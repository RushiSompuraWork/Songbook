import QtQuick
import Quickshell
import Quickshell.Io

// State layer for the music card. Every action is one call to
// scripts/card.py, which answers with one line of JSON; nothing here talks to
// MPD, Radio Browser or yt-dlp directly.
//
// Status is pushed, not polled: `card.py watch` waits on MPD's idle
// command and prints a line whenever something changes. One long-lived
// process at ~0.05% CPU replaced a fresh process every second (~4%). If it
// ever dies, a slow poll covers until it is back.
Item {
  id: root

  visible: false
  width: 0
  height: 0

  property bool panelOpen: false

  readonly property string script: Qt.resolvedUrl("scripts/card.py").toString().replace("file://", "")
  readonly property string stateDir: Quickshell.env("HOME") + "/.local/state/rushi.songbook"

  property var status: ({})
  property var home: ({ folders: [], stars: [], favorites: [], last: {} })
  property var download: ({})
  property bool reachable: true

  readonly property string playState: String(status.state || "stop")
  readonly property bool playing: playState === "play"
  readonly property bool stopped: playState === "stop"
  readonly property string kind: String(status.kind || "music")
  readonly property bool isRadio: kind === "radio"
  readonly property var current: status.current || null
  readonly property string title: current ? String(current.title || "") : ""
  readonly property string artist: current ? String(current.artist || "") : ""
  readonly property string station: String(status.station || "")
  // The YouTube page a playing stream came from, for "save this song".
  readonly property string source: current ? String(current.source || "") : ""
  readonly property real duration: Number(status.duration || 0)
  readonly property int songPos: status.song === undefined ? -1 : Number(status.song)
  readonly property int queueLength: Number(status.length || 0)
  readonly property bool randomOn: status.random === true
  readonly property bool repeatOn: status.repeat === true
  readonly property var stars: home.stars || []

  // MPD reports elapsed only when a poll lands, so tick it forward locally.
  property real elapsedDrift: 0
  readonly property real position: Math.min(duration > 0 ? duration : 1e9,
                                             Number(status.elapsed || 0) + elapsedDrift)

  function isStarred(url) {
    for (var i = 0; i < stars.length; i++) if (stars[i].url === url) return true
    return false
  }

  // ---- calling the helper -------------------------------------------------

  Component {
    id: callComponent

    Process {
      id: proc
      property var callback: null
      property bool gone: false          // destroyed once, from whichever side
      running: false
      stdout: StdioCollector {
        waitForEnd: true
        onStreamFinished: {
          var data = null
          var lines = String(text || "").trim().split("\n")
          try {
            data = JSON.parse(lines[lines.length - 1])
          } catch (e) {
            data = { error: "no answer from card.py" }
          }
          var cb = proc.callback
          proc.callback = null
          if (cb) cb(data)
          if (!proc.gone) {
            proc.gone = true
            Qt.callLater(function() { proc.destroy() })
          }
        }
      }
    }
  }

  function call(args, callback) {
    var p = callComponent.createObject(root, {
      command: ["python3", root.script].concat(args),
      callback: callback || null
    })
    p.running = true
    return p
  }

  // Stop a call whose answer is no longer wanted (an outdated search).
  // It has to be destroyed as well: a call that finishes destroys itself, but
  // a cancelled one used to be left behind, and a search cancels on every
  // keystroke (found 2026-09-24).
  function cancel(p) {
    if (!p || p.gone)
      return
    p.callback = null
    if (p.running)
      p.running = false
    p.gone = true
    Qt.callLater(function() { p.destroy() })
  }

  // Run an action, then refresh so the card reflects it straight away.
  function act(args, after) {
    call(args, function(data) {
      if (data && data.error) root.errorRaised(String(data.error))
      // The watcher reports the change itself; only poll without it.
      if (!watchProc.running) refresh()
      if (after) after(data)
    })
  }

  signal errorRaised(string message)
  signal noticeRaised(string message)     // news that is not a failure
  signal libraryChanged()                 // MPD finished a rescan

  // ---- status -------------------------------------------------------------

  property bool statusBusy: false

  function refresh() {
    if (statusBusy) return
    statusBusy = true
    call(["status"], function(data) {
      statusBusy = false
      root.applyStatus(data)
    })
  }

  // ---- album art: card.py saves it locally, QML only loads files ----------

  property string art: ""
  property string artFor: ""
  readonly property string currentFile: current ? String(current.file || "") : ""

  onCurrentFileChanged: {
    newSongStyle()
    if (currentFile === artFor) return
    artFor = currentFile
    art = ""
    if (currentFile === "") return
    var want = currentFile
    call(["art"], function(d) {
      if (root.artFor === want && d && d.art) root.art = d.art
    })
  }

  // ---- the dancer: text frames stepped on each beat MPD's audio makes -----
  //
  // Each style alternates key poses and in-between frames: a beat moves
  // to the in-between frame at once and to the next pose a moment later, so
  // one step reads as a movement rather than a jump. Half-width characters
  // keep every frame narrow.

  readonly property var danceStyles: [
    ["\\(･ω･)/", "-(･ω･)-", "┌(･ω･)┘", "|(･ω･)|", "└(･ω･)┐", "-(･ω･)-"],
    ["ʕ･ᴥ･ʔ", "ʕ･ᴥ･ʔﾉ", "ヽʕ･ᴥ･ʔﾉ", "ヽʕ･ᴥ･ʔ"],
    ["(=^･ω･^=)", "(=^･ω･^=)ﾉ", "ヽ(=^･ω･^=)ﾉ", "ヽ(=^･ω･^=)"],
    ["(･ω･ )", "( ･ω･ )", "( ･ω･)", "( ･ω･ )"],
    ["☆(･ω･) ", "✧(･ω･)✧", " (･ω･)☆", "✧(･ω･)✧"],
    ["_(･ω･)_", "(･ω･)", "¯\\(°ω°)/¯", "(･ω･)"],
    ["♪(･ω･) ", "♪(･ω･)♫", " (･ω･)♫", "♫(･ω･)♪"]
  ]

  property bool showDance: false          // the art square: image or dancer
  property int styleIndex: 0
  property int songStyle: 0               // picked fresh for every song
  property int step: 0
  property int beatCount: 0
  property real lastBeat: 0
  readonly property var danceFrames: danceStyles[styleIndex]
  readonly property string danceFrame: danceFrames[step % danceFrames.length]
  // Only listen while someone can see a dancer: in the card, or on the bar
  // when that setting is on.
  readonly property bool barDancer: settings.barDancer === true
  readonly property bool dancing: playing && ((panelOpen && showDance) || barDancer)

  // A new song, a new style. Only a real change of song counts: the
  // current song briefly reads empty between status polls.
  property string styledFile: ""
  property int stylesShown: 0
  function newSongStyle() {
    if (currentFile === "" || currentFile === styledFile) return
    styledFile = currentFile
    notifyTimer.restart()
    // Settings → Bar → Dancer style: one style always, or a new one each song.
    var fixed = Number(settings.danceStyle)
    var pick = Math.floor(Math.random() * danceStyles.length)
    if (pick === songStyle) pick = (pick + 1) % danceStyles.length
    if (fixed >= 0 && fixed < danceStyles.length) pick = fixed
    songStyle = pick
    styleIndex = pick
    step = 0
  }

  // A style picked in Settings shows at once, not only from the next song.
  readonly property int chosenStyle: settings.danceStyle === undefined ? -1 : Number(settings.danceStyle)
  onChosenStyleChanged: {
    if (chosenStyle < 0 || chosenStyle >= danceStyles.length) return
    songStyle = chosenStyle
    styleIndex = chosenStyle
    step = 0
  }

  // The art square cycles: art, this song's style, every other style in
  // turn, then back to the art.
  function cycleArt() {
    if (!showDance) {
      styleIndex = songStyle
      step = 0
      stylesShown = 1
      showDance = true
      return
    }
    if (stylesShown >= danceStyles.length) { showDance = false; return }
    styleIndex = (styleIndex + 1) % danceStyles.length
    stylesShown++
    step = 0
  }

  function onBeat() {
    lastBeat = Date.now()
    stepOnce()
  }

  function stepOnce() {
    // Land on the in-between frame now, the next pose shortly after.
    if (step % 2 === 0) step++
    beatCount++
    tweenTimer.restart()
  }

  Timer {
    id: tweenTimer
    interval: 110
    onTriggered: if (root.step % 2 === 1) root.step++
  }

  Process {
    id: beatProc
    command: ["python3", Qt.resolvedUrl("scripts/beat.py").toString().replace("file://", "")]
    running: false
    // If it stops while the dancer is still on screen, start it again at
    // once instead of waiting for the slow net above.
    onRunningChanged: if (!running && root.dancing && root.feedState === "on") beatAgain.restart()
    stdout: SplitParser {
      onRead: function(line) { root.onBeat() }
    }
  }

  // MPD's beat feed (its fifo output) only runs while a dancer is on screen:
  // left on, it costs MPD as much again as playing (card.py beat-feed).
  // One change at a time, so an "off" can never land after a later "on".
  property string feedState: ""           // what MPD was last told
  property bool feedBusy: false
  function syncFeed() {
    var want = dancing ? "on" : "off"
    if (feedBusy || want === feedState) return
    feedBusy = true
    if (want === "off") beatProc.running = false
    call(["beat-feed", want], function(d) {
      root.feedBusy = false
      root.feedState = want
      if (want === "on") beatProc.running = root.dancing
      root.syncFeed()                    // catch a change made meanwhile
    })
  }

  // Start the listener while the dancer is on screen and stop it otherwise;
  // restart it if it ever exits on its own. The first tick also switches
  // the feed off at start-up when no dancer shows.
  // Every 2 s for ever was 43,000 wake-ups a day to notice something that
  // already reports itself: the dancer coming and going raises
  // onDancingChanged, and the listener stopping raises onRunningChanged.
  // What is left is a slow net under both (2026-09-24).
  Timer {
    interval: 30000
    repeat: true
    running: true
    triggeredOnStart: true
    onTriggered: {
      root.syncFeed()
      if (root.feedState === "on" && root.dancing !== beatProc.running) beatProc.running = root.dancing
    }
  }
  Timer {
    id: beatAgain
    interval: 1000
    onTriggered: if (root.dancing && root.feedState === "on") beatProc.running = true
  }

  onDancingChanged: {
    syncFeed()
    if (feedState === "on" && dancing !== beatProc.running) beatProc.running = dancing
  }

  // Quiet passages (talk radio, a soft intro) have no beats; sway gently.
  Timer {
    interval: 700
    repeat: true
    running: root.dancing
    onTriggered: if (Date.now() - root.lastBeat > 1500) root.stepOnce()
  }

  // ---- settings (extras.py keeps them) -------------------------------------

  property var settings: ({ barDancer: false, notify: false, pauseOnUnplug: true,
                            crossfade: 0, tourDone: false, keys: defaultKeys,
                            barMiddle: "playPause", barRight: "rmpc",
                            youtubeRelay: true, youtubeLogin: "", radioRetestDays: 5,
                            consume: false, shuffleFolders: false, downloadFormat: "mp3",
                            downloadQuality: "best", sponsorblock: false, radioHide: "none",
                            radioQuality: false, youtubeQuality: "best", searchResults: 6,
                            danceStyle: -1, radioReportPlays: true, softPause: false,
                            downloadFolder: "", hiddenFolders: [], bookFolders: [],
                            folderSort: "name", radioCountries: [], hiddenGenres: [],
                            youtubeMusic: false, barTitle: false, notifyShows: "full",
                            lyricsAuto: false, lyricsOffset: 0, lyricsSize: "normal",
                            bookGraySkipped: false, textHighlight: "sentence",
                            voiceEngine: "", voiceName: "", voiceSpeed: 100, voiceCores: 2, voiceCommand: "",
                            spokenFolder: "Spoken", spokenAheadOn: false, spokenAhead: 2, spokenTidy: false, meaningLanguage: "en", meaningSource: "auto",
                            bookSkipBack: 10, bookSkipForward: 30, bookRewind: 10, sleepFade: true })

  property bool settingsLoaded: false
  function loadSettings() {
    call(["settings"], function(d) {
      if (d && d.settings) { root.settings = d.settings; root.settingsLoaded = true }
    })
  }

  function setSetting(key, value, done) {
    call(["set-setting", key, JSON.stringify(value)], function(d) {
      if (d && d.settings) root.settings = d.settings
      else if (d && d.error) root.errorRaised(String(d.error))
      // Which folders are books shows on the Library page: redraw it.
      if (key === "bookFolders") root.loadHome()
      if (done) done(d)
    })
  }

  // Where MPD keeps the music, for the settings view.
  property string musicDir: ""
  Component.onCompleted: {
    loadSettings()
    call(["config"], function(d) { if (d && d.musicDir) root.musicDir = d.musicDir })
  }

  // ---- sleep timer: one icon, each click the next step ---------------------
  //
  // off → 15 → 30 → 60 minutes → after this song → off. Minutes are timed
  // here (the shell outlives the card being open); "after this song" is
  // MPD's single-oneshot mode, which stops at the end of the current song.

  readonly property var sleepSteps: [0, 15, 30, 60, -1]
  property int sleepStep: 0
  property real sleepAt: 0
  property real sleepNow: Date.now()
  readonly property int sleepMinutes: sleepSteps[sleepStep]
  // In a book, "after this song" is the end of this chapter: MPD's single
  // oneshot when each chapter is a file, a timer to the chapter's end inside
  // one .m4b (where the file is the whole book).
  readonly property bool chapterSleep: sleepMinutes < 0 && kind === "book" && partLength > 0
  readonly property string sleepLabel: {
    if (sleepStep === 0) return "Sleep timer: off"
    if (sleepMinutes < 0) return kind === "book" ? "Sleep: end of this chapter" : "Sleep: after this song"
    var left = Math.max(0, Math.ceil((sleepAt - sleepNow) / 60000))
    return "Sleep in " + left + " min"
  }

  function cycleSleep() {
    var wasSongEnd = sleepMinutes < 0
    sleepStep = (sleepStep + 1) % sleepSteps.length
    if (wasSongEnd) act(["single", "0"])
    if (sleepMinutes > 0) sleepAt = Date.now() + sleepMinutes * 60000
    else if (sleepMinutes < 0 && kind === "book" && partLength > 0) aimChapterEnd()
    else if (sleepMinutes < 0) act(["single", "oneshot"])
    sleepNow = Date.now()
  }

  // Time to the end of the chapter playing, from MPD's own clock; aimed
  // again whenever play resumes or the chapter changes.
  property real statusAt: 0               // when the watcher last reported
  function aimChapterEnd() {
    // With the fade on, it starts 10 s early so the sound is gone as the
    // chapter ends, not partway into the next one.
    var fade = settings.sleepFade === false ? 0 : 10
    // MPD's elapsed is from its last report; playing has gone on since.
    var now = Number(status.elapsed || 0) + (playing ? (Date.now() - statusAt) / 1000 : 0)
    sleepAt = Date.now() + Math.max(0, partStart + partLength - fade - 0.5 - now) * 1000
  }
  onPartStartChanged: if (chapterSleep && playing) aimChapterEnd()
  onPlayingChanged: if (chapterSleep && playing) aimChapterEnd()

  // The end of the sleep timer: a 10 s fade-out, unless turned off in
  // Settings (card.py sleep-fade reads it).
  function sleepNowPlease() {
    sleepStep = 0
    if (playing) act(["sleep-fade"])
  }

  // Where the station or video playing was picked ({view, arg, url}), so
  // a click on its name can go back there (MusicPanel.goToPlaying).
  property var playingFrom: null

  // ⟲ and ⟳ in a book (Settings → Library → Audiobooks).
  function seekBy(secs) { act(["seek-by", String(Math.round(secs))]) }

  Timer {
    interval: root.chapterSleep ? 500 : 5000
    repeat: true
    running: root.sleepStep !== 0
    onTriggered: {
      root.sleepNow = Date.now()
      if ((root.sleepMinutes > 0 || root.chapterSleep) && root.playing
          && root.sleepNow >= root.sleepAt)
        root.sleepNowPlease()
      else if (root.sleepMinutes > 0 && root.sleepNow >= root.sleepAt) root.sleepStep = 0
    }
  }

  // "After this song" ends when MPD stops by itself.
  onPlayStateChanged: if (sleepMinutes < 0 && playState === "stop") sleepStep = 0

  readonly property var defaultKeys: ({
    download: "Ctrl+Space", star: "Alt+Space", playNext: "Shift+Return",
    addToQueue: "Ctrl+Return", remove: "Delete", moveUp: "Alt+Up", moveDown: "Alt+Down",
    clearQueue: "Ctrl+Delete", playPause: "Space", guide: "?"
  })

  function resetSettings() {
    call(["reset-settings"], function(d) { if (d && d.settings) root.settings = d.settings })
  }

  function setMusicDir(path, done) {
    call(["set-music-dir", path], function(d) {
      if (d && d.musicDir) root.musicDir = d.musicDir
      if (done) done(d)
    })
  }

  // ---- notification when the song changes (a setting, off by default) -------
  //
  // Only while the card is closed: with it open you can see the song. A
  // radio station's own song changes count too. The text is escaped,
  // because notification daemons read markup and titles come from strangers.

  Timer {
    id: notifyTimer
    interval: 900            // let the album art arrive first
    onTriggered: root.sendNotification()
  }
  onTitleChanged: if (isRadio && title !== "") notifyTimer.restart()

  function escapeMarkup(text) {
    return String(text).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
  }

  function sendNotification() {
    if (settings.notify !== true || !playing || panelOpen) return
    var head = isRadio ? (title || station) : title
    if (head === "") return
    // Settings → Bar and notifications → The notification shows.
    var shows = settings.notifyShows || "full"
    var body = shows === "title" ? "" : isRadio ? station : artist
    var args = ["notify-send", "-a", "Music", "-u", "low", "-t", "4000"]
    if (art !== "" && shows === "full") args = args.concat(["-i", art])
    Quickshell.execDetached(args.concat(["--", escapeMarkup(head), escapeMarkup(body)]))
  }

  // Pause when headphones unplug (a setting, on by default) runs inside the
  // watcher process and reads the setting itself; see extras.unplug_loop.

  // ---- Up next editing ------------------------------------------------------

  signal queueEdited()
  function queueDelete(pos) { act(["queue-delete", String(pos)], function() { root.queueEdited() }) }
  function queueClearAfter() { act(["queue-clear-after"], function() { root.queueEdited() }) }
  function queueMove(from, to) { act(["queue-move", String(from), String(to)], function() { root.queueEdited() }) }

  function loadHome() {
    call(["home"], function(data) {
      if (data && !data.error) root.home = data
    })
  }

  function applyStatus(data) {
    if (!data || data.error) {
      root.reachable = false
      return
    }
    root.reachable = true
    if (Number(data.elapsed || 0) !== Number(root.status.elapsed || 0)) root.elapsedDrift = 0
    root.statusAt = Date.now()
    root.status = data
    if (data.favoritesChanged) loadHome()
    if (data.libraryChanged) { loadHome(); root.libraryChanged() }
  }

  Process {
    id: watchProc
    command: ["python3", root.script, "watch"]
    running: true
    stdout: SplitParser {
      onRead: function(line) {
        var d = null
        try { d = JSON.parse(line) } catch (e) { return }
        // Besides status, the watcher reports what it did on its own.
        if (d.paused) root.noticeRaised("Paused: headphones unplugged")
        else if (d.recovered) root.noticeRaised(String(d.recovered))
        // The music folder changed. It has to be its own line, not a status:
        // applyStatus replaces root.status wholesale, so a partial one would
        // blank the title and the controls (2026-09-30).
        else if (d.foldersChanged) root.loadHome()
        else root.applyStatus(d)
      }
    }
    // The card is blind without it, so notice the moment it goes rather than
    // on the next tick. A second's pause first: were it to fail on start-up
    // this would otherwise spin (2026-09-24).
    onRunningChanged: if (!running) watchAgain.restart()
  }

  Timer {
    id: watchAgain
    interval: 1000
    onTriggered: {
      if (!watchProc.running) {
        watchProc.running = true
        root.refresh()
      }
    }
  }

  // And a slow net under that, in case it ever stops without saying so.
  // It used to run every 5 s for ever -- 17,000 wake-ups a day to check a
  // property that now reports itself.
  Timer {
    interval: 30000
    repeat: true
    running: true
    onTriggered: {
      if (!watchProc.running) {
        watchProc.running = true
        root.refresh()
      }
    }
  }

  Timer {
    interval: 250
    repeat: true
    running: root.panelOpen && root.playing && !root.isRadio
    onTriggered: root.elapsedDrift += 0.25
  }

  onPanelOpenChanged: {
    if (panelOpen) { loadHome(); loadInbox(); checkSetupOnce() }
    // Changing a timer's interval does not cut short the wait already begun,
    // so opening the card would otherwise sit on the slow one for up to 15 s
    // before asking what is running.
    workTimer.restart()
    loadWork()
  }

  // ---- download progress: card.py writes it, we watch the file -------------

  // A watch cannot see a file that does not exist yet, so while the card is
  // open also re-read it once a second -- but only while there is something
  // to read about. Anything that starts a download or an import says so
  // (workStarted), and one begun elsewhere shows up on the work tick, so
  // this no longer re-reads two files a second for a card sitting idle
  // (2026-09-27).
  Timer {
    interval: 1000
    repeat: true
    running: root.panelOpen && root.work && root.work.busy
    onTriggered: { downloadFile.reload(); importFile.reload() }
  }

  // ---- Inbox and imports ------------------------------------------------------

  property var importJob: ({})
  readonly property bool importing: importJob.state === "running"
  property var inbox: ({ lists: [], files: [] })
  readonly property int inboxCount: (inbox.lists || []).length + (inbox.files || []).length

  FileView {
    id: importFile
    path: root.stateDir + "/import.json"
    watchChanges: true
    printErrors: false
    onFileChanged: { reload(); root.loadWork() }
    onLoaded: {
      try { root.importJob = JSON.parse(text()) } catch (e) {}
    }
  }

  property bool inboxLoading: false
  function loadInbox() {
    inboxLoading = true
    call(["inbox"], function(d) {
      if (d && d.lists) root.inbox = d
      // Long enough to see it turn, even when the answer is instant.
      inboxDoneTimer.restart()
    })
  }
  Timer { id: inboxDoneTimer; interval: 400; onTriggered: root.inboxLoading = false }
  function inboxDone(path) { call(["inbox-done", path], function() { root.loadInbox() }) }
  function inboxMove(path, folder, done) {
    call(["inbox-move", path, folder], function(d) {
      if (d && d.error) root.errorRaised(String(d.error))
      root.loadInbox()
      if (done) done(d)
    })
  }
  function importStart(mode, folder, source, done) {
    call(["import-start", mode, folder, source], function(d) {
      if (d && d.error) root.errorRaised(String(d.error))
      importFile.reload()
      root.workStarted()
      if (done) done(d)
    })
  }
  function importStop() { call(["import-stop"], function() { importFile.reload() }) }

  // ---- setup problems, checked once per session when the card first opens ----

  property var setupProblems: []
  property bool setupChecked: false
  function checkSetupOnce() {
    if (setupChecked) return
    setupChecked = true
    call(["setup-check"], function(d) {
      var bad = []
      var checks = d && d.checks ? d.checks : []
      // The beat feed is optional; only report what stops the card working.
      for (var i = 0; i < checks.length; i++)
        if (!checks[i].ok && checks[i].name !== "Beat feed") bad.push(checks[i])
      root.setupProblems = bad
    })
  }

  // Work begun somewhere else -- chapters ticked in the terminal reader, a
  // download from the command line -- would otherwise wait for the slow tick
  // to be noticed. These files change the moment it starts, so the card asks
  // then instead of asking over and over in case (2026-09-27).
  FileView {
    id: spokenFile
    path: root.stateDir + "/spoken.json"
    watchChanges: true
    printErrors: false
    onFileChanged: { reload(); root.loadWork() }
  }

  FileView {
    id: downloadFile
    path: root.stateDir + "/download.json"
    watchChanges: true
    printErrors: false
    onFileChanged: { reload(); root.loadWork() }
    onLoaded: {
      try { root.download = JSON.parse(text()) } catch (e) {}
    }
  }

  // ---- actions ------------------------------------------------------------

  function togglePlay() {
    if (stopped) act(["resume"])
    else act(["pause"])
  }
  function next()      { act(["next"]) }
  function previous()  { act(["previous"]) }
  // Next/previous from anywhere in the card or bar: on the radio, the next
  // station in its list (see stepStation); otherwise the next song.
  function skip(dir) {
    if (isRadio && stepStation(dir)) return
    // In a book, the next chapter where it was left, also inside one .m4b.
    if (kind === "book") { act(["book-step", dir > 0 ? "next" : "previous"]); return }
    if (dir > 0) next()
    else previous()
  }
  // The time bar's part: inside one .m4b book the chapter playing (card.py
  // status "book"), otherwise the whole song.
  readonly property real partStart: status.book ? Number(status.book.start || 0) : 0
  readonly property real partLength: status.book ? Number(status.book.length || 0) : 0
  readonly property real partPosition: Math.max(0, position - partStart)
  readonly property real partDuration: partLength > 0 ? partLength : duration
  function seekPart(s) { seekTo(partStart + s) }
  function seekTo(s)   { act(["seek", String(Math.max(0, Math.round(s)))]) }
  function playAt(pos) { act(["playpos", String(pos)]) }
  function toggleRandom() { act(["toggle", "random"]) }
  function toggleRepeat() { act(["toggle", "repeat"]) }

  function playFolder(name, startFile) {
    act(["play-folder", name, startFile || ""], loadHome)
  }
  function addSong(file, mode) { act(["add-song", file, mode]) }
  // What is being read aloud right now. It used to have a poller of its own,
  // asking every 2 s for something `jobs` already reports -- two processes a
  // second between them, about 5% of a core spent starting Python
  // (2026-09-27). It is read out of `work` now, which is asked once.
  property var speaking: ({})
  readonly property bool speakingNow: String(speaking.state || "") === "making"
  function readAloudFrom(work) {
    var run = (work && work.running) || []
    for (var i = 0; i < run.length; i++)
      if (run[i].kind === "speak")
        return { state: "making", title: run[i].title, book: run[i].detail,
                 percent: run[i].percent, chapter: run[i].chapter,
                 waiting: (work.waiting || []).length }
    return { state: "", waiting: (work && work.waiting ? work.waiting.length : 0) }
  }

  // Everything the card is busy with (jobs.py): what is running, what is
  // waiting, and one line saying so. Asked for often enough to feel live
  // while the card is open, and slowly while it is shut so the bar's
  // indicator is still right (his ask, 2026-09-25).
  property var work: ({ busy: false, line: "", running: [], waiting: [], count: 0, queued: 0 })
  readonly property bool working: work.busy === true
  readonly property string workLine: String(work.line || "")
  function loadWork(done) {
    call(["jobs"], function(d) {
      if (!d || d.error) { if (done) done(d); return }
      var wasReady = String(root.speaking.state || "")
      root.work = d
      root.speaking = root.readAloudFrom(d)
      // A chapter that has just become ready changes the chapter list.
      if (root.novelPath && wasReady === "making" && root.speaking.state !== "making")
        root.loadNovel(root.novelPath)
      if (done) done(d)
    })
  }
  // Only while he is looking at it. Nothing on the bar shows this, so asking
  // every fifteen seconds with the card shut was 5,760 processes a day for
  // something nobody could see (2026-09-27).
  //
  // And only fast while there is something to watch. Two seconds is right
  // for a progress figure that is moving; with nothing running the answer is
  // the same every time, and asking for it was 30 processes a minute for as
  // long as the card stood open (measured 2026-09-27: 24 starts a minute of
  // an idle card). The wait drops back to two seconds the moment anything
  // starts, because the card asks again itself after every command it sends.
  Timer {
    id: workTimer
    interval: root.work && root.work.busy ? 2000 : 10000
    repeat: true
    running: root.panelOpen
    triggeredOnStart: true
    onTriggered: root.loadWork()
  }
  // Something has just been asked for: look now, and keep the quick wait
  // while it runs. Without this the slow idle wait could hold a new job off
  // the screen for ten seconds.
  function workStarted() {
    workTimer.restart()
    loadWork()
  }
  function stopWork(which, done) {
    if (!which) return
    call([which], function(d) { root.loadWork(done) })
  }

  // A novel's chapters, with what has been read aloud already and what is
  // waiting (speak.py book-chapters). His design, 2026-09-23: chapters are
  // ticked here, in the card, and the queue makes them one at a time.
  property var novel: ({ chapters: [], title: "", path: "", ready: 0, waiting: 0 })
  property string novelPath: ""
  property bool novelLoading: false
  function loadNovel(path, done) {
    if (!path) return
    root.novelPath = path
    root.novelLoading = true
    call(["book-chapters", path], function(d) {
      root.novelLoading = false
      if (d && d.error) { root.errorRaised(String(d.error)); return }
      if (d) root.novel = d
      if (done) done(d)
    })
  }
  // Tick chapters to be read aloud. They join the one queue the reader
  // shares, so both show the same thing.
  // `listen`: the first of them starts playing as soon as its first piece is
  // ready, about half a minute, instead of when the whole chapter is made.
  function speakChapters(path, list, done, listen) {
    if (!path || !list || !list.length) return
    call(["speak", path, list.join(","), listen ? "listen" : ""], function(d) {
      if (d && d.error) { root.errorRaised(String(d.error)); return }
      root.workStarted()
      root.loadNovel(path, done)
    })
  }
  function dropChapters(path, list, done) {
    call(["speak-drop", path, (list || []).join(",")], function(d) {
      if (d && d.error) { root.errorRaised(String(d.error)); return }
      root.loadNovel(path, done)
    })
  }
  function playSpoken(path, chapter, done) {
    act(["play-spoken", path, String(chapter)], function(d) {
      if (done) done(d)
    })
  }
  function stopSpeaking(done) {
    call(["speak-stop"], function(d) {
      if (root.novelPath) root.loadNovel(root.novelPath)
      if (done) done(d)
    })
  }

  // Which voice engines are installed, and the chosen one's voices.
  property var voiceInfo: ({ engines: [], voices: [], engine: "", voice: "" })
  function loadVoices() {
    call(["voices"], function(d) {
      if (d && !d.error) root.voiceInfo = d
    })
  }

  // The terminal reader (reader.py) on a book, or on the last one read.
  function openReader(target) {
    call(["reader-open", target || ""], function(d) {
      if (d && d.error) root.errorRaised(String(d.error))
    })
  }

  // An audiobook: from where it was left, or from a chapter (books.py).
  function playBook(folder, chapterId, done) {
    act(["play-book", folder, chapterId || ""], function(d) { loadHome(); if (done) done(d) })
  }

  // With the list it was picked from, the whole list is queued (card.py
  // play-station), so next/previous move between those stations.
  function playStation(station, mode, done, list) {
    var args = ["play-station", JSON.stringify(station), mode || "replace"]
    if (list && list.length > 1) {
      var plain = []
      for (var i = 0; i < list.length && i < 60; i++)
        plain.push({ url: list[i].url, name: list[i].name, uuid: list[i].uuid || "" })
      args.push(JSON.stringify(plain))
    }
    act(args, done)
  }

  function playYoutube(url, mode, done) {
    act(["play-youtube", url, mode || "replace"], done)
  }

  function star(station) {
    call(["star", JSON.stringify(station)], function(data) {
      if (!data || data.error) return
      var h = root.home
      root.home = { folders: h.folders || [], stars: data.stars || [],
                    favorites: h.favorites || [], last: h.last || {} }
    })
  }

  // ---- favorite songs: an MPD playlist, see card.py -----------------------

  readonly property var favorites: home.favorites || []
  readonly property bool currentIsFav: current !== null && isFav(String(current.file || ""))

  function isFav(file) { return favorites.indexOf(file) >= 0 }

  function favToggle(file) {
    call(["fav-toggle", file], function(data) {
      if (!data || data.error) { if (data) root.errorRaised(String(data.error)); return }
      var h = root.home
      root.home = { folders: h.folders || [], stars: h.stars || [],
                    favorites: data.favorites || [], last: h.last || {} }
    })
  }

  function playFavorites(startFile) { act(["play-favorites", startFile || ""]) }

  // On the radio, prev/next go to the station before or after in the list
  // it was played from (queued by playStation), in the order shown, even
  // with shuffle on. A lone station falls back to walking the starred ones.
  function stepStation(dir) {
    var pos = Number(status.song)
    if (queueLength > 1 && pos >= 0) {
      playAt((pos + dir + queueLength) % queueLength)
      return true
    }
    if (stars.length < 2) return false
    var at = 0
    for (var i = 0; i < stars.length; i++) if (stars[i].name === station) at = i
    playStation(stars[(at + dir + stars.length) % stars.length])
    return true
  }

  function startDownload(source, folder) {
    act(["download", source, folder], function(d) { root.workStarted(); loadHome() })
  }
}
