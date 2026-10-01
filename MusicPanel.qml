import QtQuick
import QtQuick.Layouts
import Quickshell
import Quickshell.Io
import qs.Commons
import qs.Ui

// The music card: now playing on top, then one search box and one list.
//
// The list is the whole UI. What it shows depends on `view` (home, a folder,
// the world of stations, search results, the queue, or "save to which
// folder"), and every row is a plain JS object built by buildRows(). The
// ListView never sees a live QObject, and rebuilds are debounced, because
// Repeaters fed live object lists are what crashed the shell before (Omarchy
// issues #11202 and #11755).
//
// Every Text is PlainText: station names and video titles come from
// strangers, and the default AutoText would render "<img src=...>" in one as
// rich text and fetch it.
FocusScope {
  id: root

  property var bar: null
  property var svc: null
  property bool opened: false
  signal closeRequested()

  readonly property color fg: bar ? bar.foreground : Color.foreground
  readonly property color accent: Color.accent
  readonly property color dim: Util.alpha(fg, 0.55)
  readonly property color faint: Util.alpha(fg, 0.35)
  readonly property string fontFamily: bar ? bar.fontFamily : Style.font.family

  implicitHeight: column.implicitHeight

  // ---- view state ----------------------------------------------------------

  property string view: "home"         // home folder world stations search queue pick
  property var viewArg: null
  property var history: []

  property var discover: []
  property bool discoverLoading: false
  property string discoverError: ""
  property var world: null
  property var folderSongs: []
  property var folderDirs: []            // subfolders: artist → album → songs
  property var stationList: []
  property bool stationsLoading: false
  property var queue: []
  property var favSongs: []
  property var setupChecks: []
  property var loginBrowsers: []          // browsers with a sign-in found on disk
  property string loginNote: ""           // result of "Check the sign-in"
  property bool loginChecking: false
  property bool resetArmed: false
  property var importSongs: []           // the song list being looked at
  property var importChosen: []          // one tick per song
  property string importPath: ""
  property var pendingImport: null       // {songs} or {playlist} waiting for a folder
  property var pendingMove: []           // Inbox files waiting for a folder
  property int tourStep: 0
  property var lyricsData: null
  property bool lyricsLoading: false
  property int lyricIndex: -1

  property string query: ""
  property int searchToken: 0
  property var searchSongs: []
  property var searchStations: []
  property var searchVideos: []
  property var linkInfo: null
  property bool localLoading: false
  property bool ytLoading: false

  property string pendingDownload: ""  // url or search text waiting for a folder
  property string pendingLabel: ""
  property bool namingFolder: false

  property string busyKey: ""
  property string message: ""

  property var rows: []
  property string builtView: ""
  property string pendingKey: ""          // select this row after the next rebuild
  property int selectedIndex: -1

  function go(v, arg) {
    history = history.concat([{ view: view, arg: viewArg }])
    view = v
    viewArg = arg === undefined ? null : arg
    selectedIndex = -1
    enter()
  }

  function back() {
    namingFolder = false
    if (history.length === 0) { view = "home"; viewArg = null; enter(); return }
    var h = history[history.length - 1]
    history = history.slice(0, history.length - 1)
    view = h.view
    viewArg = h.arg
    selectedIndex = -1
    enter()
  }

  // Library, Up next and Settings are top-level: switching to one starts a
  // fresh history, so ← from inside it goes back to the Library.
  function showTop(v) {
    history = v === "home" ? [] : [{ view: "home", arg: null }]
    view = v
    viewArg = null
    namingFolder = false
    selectedIndex = -1
    enter()
  }

  // Every screen the card can show, so one can be opened by name.
  readonly property var viewNames: ["home", "folder", "book", "novel", "world", "stations",
                                    "search", "queue", "favs", "lyrics", "inbox", "importhelp",
                                    "importlist", "pick", "tour", "keys", "ytMine", "work"]
  // "12m", "1h05", "45s" -- short enough to sit at the end of a row without
  // pushing the title out (his ask, 2026-09-25).
  function shortClock(seconds) {
    var n = Math.round(Number(seconds) || 0)
    if (n <= 0) return ""
    if (n < 60) return n + "s"
    if (n < 3600) return Math.round(n / 60) + "m"
    var h = Math.floor(n / 3600), m = Math.round((n % 3600) / 60)
    return m > 0 ? h + "h" + (m < 10 ? "0" : "") + m : h + "h"
  }

  function jobGlyph(kind) {
    return kind === "speak" ? "󰔊" : kind === "download" ? "󰇚" : kind === "import" ? "󰚇"
         : kind === "install" ? "󰏔" : kind === "retest" ? "󰐷" : "󰦖"
  }

  function allViews() { return viewNames.concat(settingsViews) }
  // How big the rows on screen actually are, for `songbook probe`.
  function rowSize() {
    var it = list.itemAtIndex(0)
    if (!it)
      return "w=? h=?"
    return "w=" + Math.round(it.width) + " h=" + Math.round(it.height)
  }
  function knownView(name) { return allViews().indexOf(name) >= 0 }

  SettingsRows {
    id: settingsRows
    panel: root
  }

  readonly property string topView: view === "queue" ? "queue" : view === "lyrics" ? "lyrics"
    : (settingsViews.indexOf(view) >= 0 ? "settings" : "home")
  readonly property var settingsViews: ["settings", "setPlayback", "setLibrary", "setRadio",
                                        "setYoutube", "setBar", "setHelp", "setPrivacy",
                                        "setLyrics", "setHidden", "setCountries", "setGenres",
                                        "starsImport", "setBookFolders", "setReadAloud", "setVoice",
                                        "setup", "account"]

  function goHome() {
    history = []
    view = "home"
    viewArg = null
    namingFolder = false
    selectedIndex = -1
    enter()
  }

  // Fetch whatever the new view needs, then draw it.
  function enter() {
    if (view === "home" && discover.length === 0) loadDiscover()
    else if (view === "world" && !world) loadWorld()
    else if (view === "folder") loadFolder(viewArg)
    else if (view === "stations") loadStations(viewArg)
    else if (view === "queue") loadQueue()
    else if (view === "favs") loadFavs()
    else if (view === "settings") { svc.loadSettings(); loadAccount(); loadRadioInfo() }
    else if (view === "setRadio") loadRadioInfo()
    else if (view === "setYoutube") loadAccount()
    else if (view === "setPrivacy") loadSavedSize()
    else if ((view === "setCountries" || view === "setGenres") && !world) loadWorld()
    else if (view === "starsImport") loadStarFiles()
    else if (view === "ytMine") loadYtMine()
    else if (view === "setBookFolders" && viewArg) loadPickDirs(viewArg)
    else if (view === "setReadAloud" || view === "setVoice") svc.loadVoices()
    else if (view === "work") svc.loadWork(function() { rebuild() })
    else if (view === "book") loadBook(viewArg)
    else if (view === "novel") {
      ticked = ({}); tickedCount = 0; tickAnchor = -1
      svc.loadNovel(viewArg, function() { rebuild() })
    }
    else if (view === "setup") loadSetup()
    else if (view === "account") loadAccount()
    else if (view === "lyrics") loadLyrics()
    else if (view === "inbox") svc.loadInbox()
    rebuild()
  }

  function reset() {
    query = ""
    searchField.text = ""
    pendingDownload = ""
    goHome()
    keys.forceActiveFocus()
  }

  onOpenedChanged: {
    if (!opened) return
    reset()
    // First open ever: the four-tip tour (Skip, or Esc, ends it for good).
    if (svc && svc.settingsLoaded && svc.settings.tourDone !== true) {
      tourStep = 0
      showTop("tour")
    } else if (svc && svc.settings.lyricsAuto === true && svc.playing && !svc.isRadio) {
      // Settings → Lyrics → Open with the card: straight onto the words.
      showTop("lyrics")
    }
  }

  // ---- loading -------------------------------------------------------------

  function loadDiscover() {
    if (!svc || discoverLoading) return
    discoverLoading = true
    rebuild()
    svc.call(["discover"], function(d) {
      discoverLoading = false
      discover = d && d.stations ? d.stations : []
      // Nobody asked for this list, so a failure is a quiet line in it,
      // not a red message.
      discoverError = d && d.error ? String(d.error) : ""
      rebuild()
    })
  }

  function loadWorld() {
    svc.call(["world"], function(d) {
      if (d && !d.error) world = d
      else flash(d ? d.error : "Radio Browser did not answer", true)
      rebuild()
    })
  }

  function loadFolder(name) {
    folderSongs = []
    folderDirs = []
    svc.call(["songs", name], function(d) {
      folderSongs = d && d.songs ? d.songs : []
      folderDirs = (d && d.dirs ? d.dirs : []).concat(d && d.texts ? d.texts : [])
      rebuild()
    })
  }

  // Folders inside one, for choosing Book folders at any depth.
  property var pickDirs: []
  function loadPickDirs(folder) {
    pickDirs = []
    svc.call(["songs", folder], function(d) {
      pickDirs = d && d.dirs ? d.dirs : []
      rebuild()
    })
  }

  // Putting a voice engine in place (Settings → Read aloud → 󰇚).
  //
  // The install writes where it has got to twice a second; the card reads
  // that file rather than asking a process for it every two seconds, so the
  // figure moves as smoothly as the download does and costs nothing when
  // nothing is installing (2026-09-27).
  property var voiceInstall: ({})
  FileView {
    id: voiceWatch
    path: root.svc ? root.svc.stateDir + "/voices-install.json" : ""
    watchChanges: true
    printErrors: false
    onFileChanged: reload()
    onLoaded: {
      var was = root.voiceInstall.state
      try { root.voiceInstall = JSON.parse(text()) } catch (e) { return }
      if (root.voiceInstall.state === "done" && was !== "done") root.svc.loadVoices()
      root.svc.loadWork()
      if (root.view === "setReadAloud") root.rebuild()
    }
    function restart() { reload() }
  }

  // A novel's chapters he has ticked to be read aloud, by chapter number.
  property var ticked: ({})
  property int tickedCount: 0
  property int tickAnchor: -1           // the last chapter ticked, for shift
  function countTicks() {
    var n = 0
    for (var k in ticked) n++
    tickedCount = n
  }
  function tick(i) {
    var t = ticked
    if (t[i]) delete t[i]; else t[i] = true
    ticked = t
    tickAnchor = t[i] ? i : -1
    countTicks()
  }
  // Shift on a second chapter ticks everything between it and the last one
  // ticked (his ask, 2026-09-23), skipping any that are already spoken or
  // already waiting -- those are not work to ask for again.
  function tickTo(i) {
    if (tickAnchor < 0 || tickAnchor === i) { tick(i); return }
    var lo = Math.min(tickAnchor, i), hi = Math.max(tickAnchor, i)
    var chs = svc.novel.chapters || [], t = ticked, added = 0
    for (var n = 0; n < chs.length; n++) {
      var c = chs[n]
      if (c.index < lo || c.index > hi) continue
      if (c.state === "ready" || c.state === "queued" || c.state === "making") continue
      if (!t[c.index]) { t[c.index] = true; added++ }
    }
    ticked = t
    tickAnchor = i
    countTicks()
    flash(added > 0 ? "󰄲  " + added + (added === 1 ? " chapter ticked" : " chapters ticked")
                    : "Nothing to tick there")
  }
  function tickedList() {
    var out = []
    for (var k in ticked) out.push(Number(k))
    out.sort(function(a, b) { return a - b })
    return out
  }
  // Start making what is ticked. Ticking on its own never starts anything:
  // it is a list of what he wants, and this is the "go".
  function startTicked() {
    var want = tickedList()
    if (!want.length) { flash("Tick a chapter first · shift ticks a whole run"); return }
    svc.speakChapters(svc.novel.path, want, function(res) {
      ticked = ({}); tickedCount = 0; tickAnchor = -1
      rebuild()
      if (res) flash("󰔊  " + want.length + (want.length === 1 ? " chapter" : " chapters")
                     + " added · the first one is being made now")
    })
  }
  function tickedWork() {
    var chs = svc.novel.chapters || [], work = 0
    for (var i = 0; i < chs.length; i++) if (ticked[chs[i].index]) work += Number(chs[i].work || 0)
    return work
  }

  property var bookData: null
  function loadBook(folder) {
    svc.call(["book", folder], function(d) {
      bookData = d && !d.error ? d : null
      if (d && d.error) flash(d.error, true)
      rebuild()
    })
  }

  function baseName(path) {
    var p = String(path || "")
    return p.slice(p.lastIndexOf("/") + 1)
  }

  function loadStations(arg, fresh) {
    if (!arg || !arg.by) {
      // Asked for the station list with nothing to list (opening the screen
      // by name, with no country or genre chosen). Found by the sweep that
      // opens every screen, 2026-09-24.
      stationList = []
      stationsLoading = false
      rebuild()
      return
    }
    stationList = []
    stationsLoading = true
    stationsAt = 0
    svc.call(["stations", arg.by, arg.value, fresh ? "fresh" : ""], function(d) {
      stationsLoading = false
      stationList = d && d.stations ? d.stations : []
      stationsAt = d && d.at ? d.at : 0
      if (d && d.error) flash(d.error, true)
      rebuild()
      var shown = arg
      testStations(stationList, function(sorted) {
        if (view !== "stations" || viewArg !== shown) return   // left the list meanwhile
        stationList = sorted
        rebuild()
      })
    })
  }

  // ---- station speed ---------------------------------------------------------
  //
  // card.py tests how fast each station starts (probe-stations); the list
  // shows straight away and re-sorts once, when the results are in: good,
  // ok, untested, slow, not working, and by reviews within each.

  property int stationsTesting: 0
  property int stationsUntested: 0         // how many the running test covers
  property real stationsAt: 0              // when the shown list was fetched
  property var radioInfo: ({})             // for Settings: how often, when, a test running
  property bool rescanning: false
  property bool clearArmed: false
  property string savedSize: ""

  function loadSavedSize() {
    svc.call(["saved-size"], function(d) {
      var b = d && d.bytes ? d.bytes : 0
      savedSize = b >= 1048576 ? (b / 1048576).toFixed(1) + " MB" : Math.round(b / 1024) + " KB"
      rebuild()
    })
  }

  Timer {
    id: clearDisarm
    interval: 4000
    onTriggered: { root.clearArmed = false; root.rebuild() }
  }

  // "today", "yesterday", "3 days ago"
  function ago(secs) {
    if (!secs) return ""
    var days = Math.floor((Date.now() / 1000 - secs) / 86400)
    return days <= 0 ? "today" : days === 1 ? "yesterday" : days + " days ago"
  }

  function loadRadioInfo() {
    svc.call(["radio-info"], function(d) {
      if (d && !d.error) radioInfo = d
      rebuild()
    })
  }

  // While a test of every station runs, the Settings row counts along.
  Timer {
    interval: 2000
    repeat: true
    running: root.opened && (root.view === "setRadio" || root.view === "settings")
             && root.radioInfo.retest !== undefined && root.radioInfo.retest.state === "running"
    onTriggered: root.loadRadioInfo()
  }
  readonly property var tierRank: ({ "good": 0, "ok": 1, "": 2, "slow": 3, "bad": 4 })

  function rankStations(list, quality) {
    // The same order and hiding as card.py rank(), for lists re-sorted here.
    var hide = svc.settings.radioHide || "none"
    var byQuality = svc.settings.radioQuality === true
    var out = []
    for (var i = 0; i < list.length; i++) {
      var s = Object.assign({}, list[i])
      if (quality[s.url] !== undefined) s.tier = quality[s.url]
      if (s.tier === "bad" && hide !== "none") continue
      if (s.tier === "slow" && hide === "slow") continue
      out.push(s)
    }
    out.sort(function(a, b) {
      var t = tierRank[a.tier || ""] - tierRank[b.tier || ""]
      if (t !== 0) return t
      if (byQuality && (b.bitrate || 0) !== (a.bitrate || 0)) return (b.bitrate || 0) - (a.bitrate || 0)
      if ((b.votes || 0) !== (a.votes || 0)) return (b.votes || 0) - (a.votes || 0)
      return (b.clicks || 0) - (a.clicks || 0)
    })
    return out
  }

  function testStations(list, done) {
    var urls = []
    for (var i = 0; i < list.length; i++) if (!list[i].tier) urls.push(list[i].url)
    if (urls.length === 0) return
    stationsUntested = urls.length
    stationsTesting++
    rebuild()
    svc.call(["probe-stations", JSON.stringify(urls)], function(d) {
      stationsTesting = Math.max(0, stationsTesting - 1)
      if (d && d.quality) done(rankStations(list, d.quality))
      else rebuild()
    })
  }

  // Signal-strength icon and a plain word for how fast a station starts.
  function tierGlyph(t) {
    return t === "good" ? "󰤨" : t === "ok" ? "󰤥" : t === "slow" ? "󰤟" : t === "bad" ? "󰤮" : "󰐹"
  }
  function tierWord(t) {
    return t === "good" ? "fast" : t === "ok" ? "ok" : t === "slow" ? "slow"
         : t === "bad" ? "not working" : ""
  }

  // The list a station row belongs to, in the order shown, so playing it
  // queues its neighbours and next/previous move between them.
  function stationListFor(url) {
    var lists = view === "stations" ? [stationList]
              : view === "search" ? [searchStations]
              : [discover, svc.stars]
    for (var i = 0; i < lists.length; i++)
      for (var j = 0; j < lists[i].length; j++)
        if (lists[i][j].url === url) return lists[i]
    return []
  }

  function loadFavs() {
    svc.call(["fav-songs"], function(d) {
      favSongs = d && d.songs ? d.songs : []
      rebuild()
    })
  }

  // Lyrics come from card.py (LRCLIB, cached); the shell never fetches.
  function loadLyrics() {
    lyricsLoading = true
    lyricIndex = -1
    rebuild()
    svc.call(["lyrics"], function(d) {
      lyricsLoading = false
      lyricsData = d
      prepareText(d && d.text ? d.text : null)
      rebuild()
    })
  }

  // ---- reading along (booktext.py): paragraphs of sentences of [word, t]

  property var textParas: []
  property bool textSynced: false
  property var flatT: []          // every word's start, in order
  property var flatP: []          // ... its paragraph
  property var flatS: []          // ... its sentence in that paragraph
  property var flatW: []          // ... its place in that sentence
  property int activeP: -1
  property int activeS: -1
  property int activeW: -1
  readonly property string highlight: svc ? (svc.settings.textHighlight || "sentence") : "sentence"

  function prepareText(text) {
    activeP = activeS = activeW = -1
    textParas = text ? text.paras : []
    textSynced = !!(text && text.synced)
    var t = [], p = [], s = [], w = []
    for (var i = 0; i < textParas.length; i++)
      for (var j = 0; j < textParas[i].length; j++)
        for (var k = 0; k < textParas[i][j].length; k++) {
          var at = Number(textParas[i][j][k][1])
          if (at < 0) continue
          t.push(at); p.push(i); s.push(j); w.push(k)
        }
    flatT = t; flatP = p; flatS = s; flatW = w
  }

  function paraStart(i) {
    var para = textParas[i]
    for (var j = 0; para && j < para.length; j++)
      for (var k = 0; k < para[j].length; k++)
        if (Number(para[j][k][1]) >= 0) return Number(para[j][k][1])
    return -1
  }

  function esc(text) {
    return String(text).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
  }

  // One paragraph as styled text: everything escaped (the words come from
  // any file), the sentence or word being read in the accent colour.
  function paraHtml(i, p, s, w, mode) {
    var para = textParas[i] || []
    var parts = []
    for (var j = 0; j < para.length; j++) {
      var words = para[j].map(function(x) { return esc(x[0]) })
      if (i === p && mode === "sentence" && j === s)
        parts.push("<font color=\"" + accent + "\"><b>" + words.join(" ") + "</b></font>")
      else if (i === p && mode === "word" && j === s && w >= 0 && w < words.length) {
        words[w] = "<font color=\"" + accent + "\"><b>" + words[w] + "</b></font>"
        parts.push(words.join(" "))
      } else parts.push(words.join(" "))
    }
    return parts.join(" ")
  }

  Timer {
    interval: root.highlight === "word" ? 120 : 250
    repeat: true
    running: root.opened && root.view === "lyrics" && root.textSynced && root.svc.playing
    onTriggered: {
      var pos = root.svc.position + 0.15 - Number(root.svc.settings.lyricsOffset || 0) / 1000
      var t = root.flatT
      var lo = 0, hi = t.length - 1, found = -1
      while (lo <= hi) {                    // the last word that has started
        var mid = (lo + hi) >> 1
        if (t[mid] <= pos) { found = mid; lo = mid + 1 } else hi = mid - 1
      }
      var p = found >= 0 ? root.flatP[found] : -1
      var s = found >= 0 ? root.flatS[found] : -1
      var w = found >= 0 ? root.flatW[found] : -1
      if (p !== root.activeP) {
        root.activeP = p
        root.lyricIndex = p
        if (p >= 0) list.positionViewAtIndex(p + 1, ListView.Center)
      }
      root.activeS = s
      root.activeW = w
    }
  }

  readonly property bool lyricsSynced: !!(lyricsData && lyricsData.lyrics
                                          && lyricsData.lyrics.synced.length > 0 && svc && !svc.isRadio)

  // Follow the song: highlight the line being sung and keep it in view.
  Timer {
    interval: 250
    repeat: true
    running: root.opened && root.view === "lyrics" && root.lyricsSynced && root.svc.playing
    onTriggered: {
      var lines = root.lyricsData.lyrics.synced
      // Settings → Lyrics → Timing: a positive offset shows lines later.
      var pos = root.svc.position + 0.3 - Number(root.svc.settings.lyricsOffset || 0) / 1000
      var idx = -1
      for (var i = 0; i < lines.length && lines[i].t <= pos; i++) idx = i
      if (idx !== root.lyricIndex) {
        root.lyricIndex = idx
        if (idx >= 0) list.positionViewAtIndex(idx + 1, ListView.Center)
      }
    }
  }

  // YouTube sign-in: which browsers have one, and a check that it works.
  function loadAccount() {
    svc.call(["yt-browsers"], function(d) {
      loginBrowsers = d && d.browsers ? d.browsers : []
      rebuild()
    })
  }

  function browserName(id) {
    for (var i = 0; i < loginBrowsers.length; i++)
      if (loginBrowsers[i].id === id) return loginBrowsers[i].name
    return id
  }

  function chooseLogin(id) {
    loginNote = ""
    svc.call(["set-setting", "youtubeLogin", JSON.stringify(id)], function(d) {
      if (d && d.settings) svc.settings = d.settings
      if (d && d.error) { flash(d.error, true); return }
      rebuild()
      if (id !== "") checkLogin()
      else flash("Signed out: YouTube is used without an account")
    })
  }

  function checkLogin() {
    loginChecking = true
    loginNote = ""
    busyKey = "logincheck"
    rebuild()
    svc.call(["yt-login-test"], function(d) {
      loginChecking = false
      if (busyKey === "logincheck") busyKey = ""
      loginNote = d && d.note ? String(d.note) : "No answer"
      // Said out loud too: the note alone sits small at the bottom of the list.
      if (d && d.signedIn) flash("Signed in · Your YouTube is on the Library page")
      else flash(loginNote, true)
      rebuild()
    })
  }

  function loadSetup() {
    setupChecks = []
    svc.call(["setup-check"], function(d) {
      setupChecks = d && d.checks ? d.checks : []
      rebuild()
    })
  }

  function loadQueue() {
    svc.call(["queue"], function(d) {
      queue = d && d.queue ? d.queue : []
      rebuild()
    })
  }

  function isLink(text) {
    return /^https?:\/\/(www\.|m\.|music\.)?(youtube\.com|youtu\.be)\//.test(text)
  }

  // The searches still running; a new keystroke stops them, so an old
  // YouTube search neither wastes CPU nor counts against the rate limit.
  property var searchProcs: []

  function runSearch() {
    var q = query.trim()
    var token = ++searchToken
    for (var i = 0; i < searchProcs.length; i++) svc.cancel(searchProcs[i])
    searchProcs = []
    searchSongs = []
    searchStations = []
    searchVideos = []
    linkInfo = null
    if (q === "") { if (view === "search") back(); return }
    if (view !== "search") go("search")

    if (isLink(q)) {
      localLoading = true
      searchProcs = [svc.call(["link", q], function(d) {
        if (token !== searchToken) return
        localLoading = false
        linkInfo = d && !d.error ? d : null
        if (d && d.error) flash(d.error, true)
        rebuild()
      })]
      rebuild()
      return
    }

    localLoading = true
    ytLoading = true
    var local = svc.call(["search", q], function(d) {
      if (token !== searchToken) return
      localLoading = false
      searchSongs = d && d.songs ? d.songs : []
      searchStations = d && d.stations ? d.stations : []
      rebuild()
      testStations(searchStations, function(sorted) {
        if (token !== searchToken) return
        searchStations = sorted
        rebuild()
      })
    })
    var yt = svc.call(["ytsearch", q], function(d) {
      if (token !== searchToken) return
      ytLoading = false
      searchVideos = d && d.videos ? d.videos : []
      rebuild()
    })
    searchProcs = [local, yt]
    rebuild()
  }

  Timer {
    id: resetDisarm
    interval: 4000
    onTriggered: { root.resetArmed = false; root.rebuild() }
  }

  Timer {
    id: lyricsReload
    interval: 1500
    onTriggered: root.loadLyrics()
  }

  Timer {
    id: searchDebounce
    interval: 400
    onTriggered: root.runSearch()
  }

  // ---- rows ------------------------------------------------------------------

  function header(text, action, actionTip, id) {
    return { type: "header", title: text, action: action || "", actionTip: actionTip || "",
             data: { id: id || "" } }
  }
  function info(text) { return { type: "info", title: text } }
  // A problem inside a list: click, or the copy icon, puts the whole text on
  // the clipboard, to search online or hand to an AI.
  function errorRow(text) {
    return { type: "errorrow", key: "errorrow:" + text.slice(0, 40), glyph: "󰀦", title: text,
             sub: "", action: "󰆏", actionTip: "Copy the message", actionOn: true,
             data: { text: text } }
  }
  function backRow(text) { return { type: "back", key: "back", glyph: "󰁍", title: text } }

  // A folder the card recognised as an audiobook (books.py): click carries
  // on where it was left, › lists the chapters.
  function bookRow(f) {
    // A book to read only (a novel, no sound): click opens the reader.
    // How far through it he is, drawn as the row filling rather than said in
    // numbers. It comes from the library, so it is the same after a restart.
    if (f.textOnly)
      return { type: "book", key: "book:" + f.name, glyph: "󰂽", title: f.label || f.name,
               sub: "to read", action: "󰅂", actionTip: "Chapters · read aloud",
               part: Number(f.part || 0), data: f }
    return { type: "book", key: "book:" + f.name, glyph: "󰂺", title: f.label || f.name,
             sub: f.finished > 0 ? f.finished + " of " + f.chapters + " finished"
                                 : f.chapters + " chapters",
             action: "󰅂", actionTip: "Chapters", part: Number(f.part || 0), data: f,
             // Every book can open in the reader (full screen, its chapters and
             // time); with no text yet it says how to add some.
             extras: [{ id: "read", icon: "󰂽", tip: f.text ? "Read along (opens the reader)"
                                                        : "Open in the reader (no text yet)", on: true }] }
  }

  function folderRow(f) {
    if (f.book && view !== "pick") return bookRow(f)
    return { type: "folder", key: "folder:" + f.name, glyph: f.shelf ? "󱉟" : "󰉋", title: f.label || f.name,
             sub: f.shelf ? "books" : f.count ? String(f.count) : "", action: view === "pick" ? "" : "󰅂",
             actionTip: f.shelf ? "Open" : "Show songs", data: f }
  }

  function songRow(s, type) {
    var fav = svc && svc.isFav(s.file)
    return { type: type, key: type + ":" + s.file, glyph: "󰎈", title: s.title,
             sub: clock(s.duration), action: fav ? "󰓎" : "󰓒", actionOn: fav,
             actionTip: fav ? "Unstar (" + (binds.star || "") + ")" : "Star (" + (binds.star || "") + ")",
             extras: [{ id: "add", icon: "󰐕", tip: "Add to Up next (" + (binds.addToQueue || "") + ")",
                        on: true }], data: s }
  }

  // One icon per kind of thing, wherever it shows: the art square with no
  // cover, the bar, Up next.
  function kindGlyph(kind) {
    return kind === "radio" ? "󰐹" : kind === "youtube" ? "󰗃" : kind === "book" ? "󰂺" : "󰝚"
  }

  function queueKind(q) {
    var f = String(q.file)
    if (q.source || f.indexOf("/yt/") >= 0 || f.indexOf("googlevideo") >= 0) return "youtube"
    if (!isLocal(q.file)) return "radio"
    var book = svc.status.book
    if (book && String(q.file).indexOf(book.folder + "/") === 0) return "book"
    return "music"
  }

  function isLocal(file) { return String(file || "").indexOf("://") < 0 }

  function stationRow(s) {
    var starred = svc && svc.isStarred(s.url)
    var speed = tierWord(s.tier || "")
    var where = (speed ? speed + " · " : "") + s.country + (s.tags ? " · " + s.tags : "")
    return { type: "station", key: "station:" + s.url, glyph: tierGlyph(s.tier || ""), title: s.name,
             sub: where, action: starred ? "󰓎" : "󰓒", actionOn: starred,
             actionTip: starred ? "Unstar (Alt+Space)" : "Star (Alt+Space)", data: s }
  }

  function videoRow(v) {
    return { type: "video", key: "video:" + v.url, glyph: "󰗃", title: v.title,
             sub: v.artist, action: "󰇚", actionTip: "Download (Ctrl+Space)", data: v }
  }

  // The shortcut guide. Plain rows, nothing to click; ? or the last row on
  // the home list opens it.
  // The first-run tour: four tips, shown once, Skip at any time.
  readonly property var tourTips: [
    ["󰍉", "Type to search", "Your songs, radio stations and YouTube in one list. Paste a YouTube link to play it."],
    ["󰉋", "Click to play", "A folder plays all of it, a song plays alone; + adds more to Up next."],
    ["󰓎", "Star and keep", (binds.star || "Alt+Space") + " stars a song or station, "
       + (binds.download || "Ctrl+Space") + " downloads. ☾ is a sleep timer, ⚙ holds settings."],
    ["󰚇", "Bring your songs", "Settings (⚙) → Import songs: music files and song lists from "
       + "Spotify, YouTube or your phone, dropped in Downloads."],
    ["󰌌", "Your keys", "Press " + (binds.guide || "?") + " for every shortcut; any of them can be changed there."]
  ]

  // The three steps of importing, shown when there is nothing to import yet
  // and behind "How importing works" otherwise.
  function importSteps() {
    return [
      { type: "tip", key: "how1", glyph: "󰇚", title: "1. Put it in your Downloads folder" },
      { type: "tip", key: "how1b", glyph: "", quiet: true,
        title: "Music files (mp3, flac, m4a…), or a song list. Files sent from your phone with LocalSend land there by themselves. Added something with the card open? ↻ at the top of Import songs looks again." },
      { type: "tip", key: "how2", glyph: "󰲸", title: "2. A song list is just a file of song names" },
      { type: "tip", key: "how2b", glyph: "", quiet: true,
        title: "A .txt with one \"Artist - Song\" per line works. From Spotify: exportify.app → Export (CSV). From YouTube: takeout.google.com → YouTube → playlists. For almost any app: tunemymusic.com → your playlist → Export to file." },
      { type: "tip", key: "how3", glyph: "󰐊", title: "3. Open it here" },
      { type: "tip", key: "how3b", glyph: "", quiet: true,
        title: "Click the list, untick what you don't want, then Play it or Download it into a folder. Each song is found on YouTube, a few seconds apart. Music files move into a folder you pick." },
      { type: "tip", key: "how4", glyph: "󰋗", title: "Where to find this again" },
      { type: "tip", key: "how4b", glyph: "", quiet: true,
        title: "Settings (⚙) → Import songs, the first row." }
    ]
  }

  function finishTour() {
    svc.setSetting("tourDone", true)
    tourStep = 0
    goHome()
  }

  // The keys that stay put: moving around the card.
  readonly property var fixedKeys: [
    ["Type anything", "search"],
    ["↑  ↓", "move"],
    ["Enter", "play now (a song plays alone)"],
    ["Middle click a row", "play next"],
    ["→", "open a folder"],
    ["←  Backspace", "go back"],
    ["Song title", "click: up next"],
    ["Art square", "click: next dance"],
    ["Esc", "back, then close"]
  ]
  function tilde(path) {
    var home = Quickshell.env("HOME")
    return path && home && path.indexOf(home) === 0 ? "~" + path.slice(home.length) : path
  }

  function settingRow(key, title, value) {
    return { type: "setting", key: "setting:" + key, glyph: "󰒓", title: title,
             sub: value, data: { key: key } }
  }

  // A row in a pick-several list (hidden folders, genres, countries).
  function listToggleRow(key, item, title, on, onWord, glyph) {
    return { type: "listtoggle", key: "lt:" + key + ":" + item, glyph: on ? "󰄬" : glyph,
             title: title, sub: on ? onWord : "", quiet: !on, data: { key: key, item: item } }
  }

  function keyRow(k) { return { type: "key", title: k[0], sub: k[1] } }

  function navRow(title, glyph, sub, target) {
    return { type: "nav", key: "nav:" + title, glyph: glyph, title: title, sub: sub || "",
             chevron: true, data: target }
  }

  function buildRows() {
    var r = []
    var i
    if (!svc) return r

    if (view === "home") {
      var folders = svc.home.folders || []
      // Setup problems first: nothing else works until they are fixed.
      if (svc.setupProblems.length > 0) {
        r.push(header("Setup"))
        for (i = 0; i < svc.setupProblems.length; i++)
          r.push({ type: "key", title: "󰀦  " + svc.setupProblems[i].name,
                   sub: svc.setupProblems[i].fix })
      }
      if (svc.importing) {
        var job = svc.importJob
        r.push({ type: "importprogress", key: "importprogress", glyph: "󰦖",
                 title: (job.mode === "play" ? "Queueing " : "Downloading ") + job.done
                        + " of " + job.total + (job.current ? " · " + job.current : ""),
                 sub: job.failed > 0 ? job.failed + " not found" : "",
                 action: "󰅖", actionTip: "Stop", actionOn: true })
      }
      r.push(header("Your music"))
      var favCount = svc.favorites.length
      r.push({ type: "favfolder", key: "favfolder", glyph: "󰓎", title: "Favorites",
               sub: favCount > 0 ? String(favCount) : "star with Alt+Space",
               action: favCount > 0 ? "󰅂" : "", actionTip: "Show songs" })
      if (folders.length === 0)
        r.push(info("No music yet: add folders to " + (tilde(svc.musicDir) || "your music folder")
                    + ", or search and download"))
      for (i = 0; i < folders.length; i++)
        if (!folders[i].hidden) r.push(folderRow(folders[i]))
      if (svc.settings.youtubeLogin)
        r.push(navRow("Your YouTube", "󰗃", "liked · playlists", { view: "ytMine" }))

      if (svc.stars.length > 0) {
        r.push(header("Starred"))
        for (i = 0; i < svc.stars.length; i++) r.push(stationRow(svc.stars[i]))
      }

      r.push(header("Discover", "󰑐", "New stations", "discover"))
      r[r.length - 1].spinning = discoverLoading
      if (discoverLoading) r.push(info("Tuning in somewhere new…"))
      else if (discover.length === 0)
        r.push(discoverError !== "" ? errorRow(discoverError.replace(/[?.]*$/, "") + " · ↻ to retry")
                                    : info("No stations right now: try ↻"))
      for (i = 0; i < discover.length; i++) r.push(stationRow(discover[i]))
      r.push(navRow("World", "󰖟", "genres · countries", { view: "world" }))
      r.push({ type: "nav", key: "nav:keys", glyph: "󰌌", title: "Shortcuts", sub: "?",
               chevron: true, quiet: true, data: { view: "keys" } })
    } else if (view === "favs") {
      r.push(backRow("Favorites"))
      if (favSongs.length === 0) r.push(info("Nothing starred yet"))
      for (i = 0; i < favSongs.length; i++) r.push(songRow(favSongs[i], "favsong"))
    } else if (view === "folder") {
      // Subfolders first (click plays everything inside, › opens), then the
      // folder's own songs (click plays that song alone).
      r.push(backRow(baseName(viewArg)))
      for (i = 0; i < folderDirs.length; i++) r.push(folderRow(folderDirs[i]))
      for (i = 0; i < folderSongs.length; i++) r.push(songRow(folderSongs[i], "song"))
      if (folderDirs.length === 0 && folderSongs.length === 0) r.push(info("Nothing here yet"))
    } else if (view === "book") {
      r.push(backRow(baseName(viewArg)))
      if (!bookData) r.push(info("Loading…"))
      else {
        r.push({ type: "readrow", key: "readrow", glyph: "󰂽",
                 title: bookData.text ? "Read along" : "Open in the reader",
                 sub: bookData.text ? "opens the reader" : "no text yet", data: { folder: viewArg } })
        var gray = svc.settings.bookGraySkipped === true
        var chs = bookData.chapters || []
        for (i = 0; i < chs.length; i++) {
          var ch = chs[i]
          // ✓ only when he reached the end. Part way through says nothing:
          // the row fills as far as he got (his choice, 2026-09-25).
          var done = ch.state === "finished"
          var greyed = done || (gray && ch.state === "skipped")
          r.push({ type: "chapter", key: "chapter:" + ch.id,
                   glyph: ch.current ? "󰐊" : done ? "󰄬" : "󰂺",
                   title: ch.title,
                   part: Number(ch.part || 0),
                   sub: shortClock(ch.length),
                   quiet: greyed && !ch.current, current: ch.current,
                   action: ch.part > 0 ? "󰑓" : "",
                   actionTip: "Forget where I got to",
                   data: { folder: viewArg, id: ch.id } })
        }
      }
    } else if (view === "work") {
      r.push(backRow("What the card is doing"))
      var wk = svc.work || {}
      var run = wk.running || [], wait = wk.waiting || []
      if (run.length === 0 && wait.length === 0)
        r.push(info("Nothing is running"))
      if (run.length > 0) r.push(header("Now"))
      for (i = 0; i < run.length; i++) {
        var jb = run[i]
        r.push({ type: "job", key: "job:" + jb.id, glyph: jobGlyph(jb.kind),
                 title: jb.title, sub: jb.percent >= 0 ? Math.round(jb.percent) + "%" : "",
                 current: true,
                 action: jb.stop ? "󰓛" : "", actionTip: "Stop this",
                 data: { stop: jb.stop || "", id: jb.id } })
        if (jb.detail) r.push(info(jb.detail))
      }
      if (wait.length > 0) r.push(header("Waiting"))
      for (i = 0; i < wait.length && i < 60; i++) {
        var wj = wait[i]
        r.push({ type: "queued", key: "queued:" + wj.id, glyph: jobGlyph(wj.kind),
                 title: wj.place + "  " + wj.title, sub: wj.detail || "", quiet: true,
                 action: "󰅖", actionTip: "Take it off the queue",
                 data: { path: wj.path || "", chapter: wj.chapter } })
      }
    } else if (view === "novel") {
      r.push(backRow(svc.novel.title || baseName(viewArg)))
      if (svc.novelLoading && !(svc.novel.chapters || []).length) r.push(info("Loading…"))
      else {
        var nv = svc.novel, nchs = nv.chapters || []
        var sp = svc.speaking || {}
        var busyNow = String(sp.state || "") === "making" && String(sp.path || "") === String(nv.path)
        // What the queue is doing, and the two buttons for it.
        // Always here, so ticking a chapter does not shift the list under
        // the cursor, and the way to start the work is never hidden.
        var mins = Math.max(1, Math.round(tickedWork() / 60))
        r.push({ type: "speakmake", key: "speakmake", glyph: "󰔊",
                 title: tickedCount > 0
                        ? "Read " + tickedCount + (tickedCount === 1 ? " chapter" : " chapters")
                          + " aloud"
                        : "Read chapters aloud",
                 sub: tickedCount > 0
                      ? "about " + mins + " min of work · " + (nv.voice || nv.engine || "no voice yet")
                      : "tick chapters below, then press this · shift ticks a whole run",
                 quiet: tickedCount === 0,
                 action: tickedCount > 0 ? "󰅖" : "", actionTip: "Clear the ticks", data: {} })
        if (busyNow || Number(nv.waiting || 0) > 0) {
          r.push({ type: "speakstop", key: "speakstop", glyph: "󰓛",
                   title: busyNow ? "Making chapter " + (Number(sp.chapter || 0) + 1)
                                  + " · " + Math.round(Number(sp.percent || 0)) + "%"
                                  : Number(nv.waiting) + " waiting",
                   sub: busyNow && Number(nv.waiting || 0) > 0
                        ? Number(nv.waiting) + " waiting behind it · click to stop everything"
                        : "click to stop", data: {} })
        }
        for (i = 0; i < nchs.length; i++) {
          var nc = nchs[i]
          var on = ticked[nc.index] === true
          var glyph = nc.state === "making" ? "󰔊" : nc.state === "ready" ? "󰎆"
                    : nc.state === "queued" ? "󰄲" : nc.finished ? "󰄬" : on ? "󰄲" : "󰄰"
          // How long it is, in a word. What used to be here -- "about 12:05 ·
          // 2 min to make" -- was cut off to "(about 12:05 · 2 min)", which
          // said nothing (his report, 2026-09-25).
          var sub = nc.state === "making" ? Math.round(Number(sp.percent || 0)) + "%"
                  : nc.state === "queued" ? "waiting"
                  : shortClock(nc.seconds)
          r.push({ type: "novelchapter", key: "novelchapter:" + nc.index,
                   glyph: glyph, title: (nc.index + 1) + "  " + nc.title, sub: sub,
                   quiet: nc.finished && nc.state !== "ready",
                   current: nc.state === "making",
                   // How far into it he has got, drawn as the row filling up.
                   part: nc.state === "making" ? Number(sp.percent || 0) / 100 : Number(nc.part || 0),
                   action: "󰐊",
                   actionTip: nc.state === "ready" ? "Play this chapter"
                              : "Read it aloud and start playing",
                   // The reader on hover, where a book's own row has it,
                   // rather than a row of its own at the top.
                   extras: [{ id: "read", icon: "󰂽", tip: "Open this chapter in the reader",
                              on: true }],
                   data: { path: nv.path, index: nc.index, state: nc.state } })
        }
        if (!nchs.length) r.push(info("No chapters in that book"))
      }
    } else if (view === "world") {
      r.push(backRow("World"))
      if (!world) { r.push(info("Loading…")); return r }
      r.push(header("Genres"))
      var hideG = svc.settings.hiddenGenres || []
      for (i = 0; i < world.tags.length; i++) {
        var t = world.tags[i]
        if (hideG.indexOf(t) >= 0) continue
        r.push(navRow(t, "󰝚", "", { view: "stations", by: "tag", value: t, label: t }))
      }
      r.push(header("Countries"))
      for (i = 0; i < world.countries.length; i++) {
        var c = world.countries[i]
        r.push(navRow(c.name, "󰇧", String(c.count),
                      { view: "stations", by: "country", value: c.code, label: c.name }))
      }
    } else if (view === "stations") {
      r.push(backRow((viewArg && viewArg.label) || "Stations"))
      if (!stationsLoading)
        r.push(header(stationsAt ? "Kept from " + ago(stationsAt) : "Stations", "󰑐",
                      "Get a fresh list and test it", "refreshlist"))
      if (stationsTesting > 0 && !stationsLoading)
        r.push(info("Testing " + stationsUntested + (stationsUntested === 1 ? " station" : " stations")
                    + " not tested yet…"))
      if (stationsLoading) r.push(info("Loading stations…"))
      else if (stationList.length === 0) r.push(info("No stations found"))
      for (i = 0; i < stationList.length; i++) r.push(stationRow(stationList[i]))
    } else if (view === "search") {
      if (linkInfo) {
        if (linkInfo.type === "playlist")
          r.push({ type: "link", key: "link", glyph: "󰐑", title: "Play playlist",
                   sub: linkInfo.count + " songs · " + linkInfo.title, data: linkInfo,
                   extras: [{ id: "saveall", icon: "󰇚", tip: "Save all to a folder", on: true }] })
        else
          r.push(videoRow(linkInfo))
      } else if (localLoading && isLink(query.trim())) {
        r.push(info("Reading the link…"))
      } else {
        if (searchSongs.length > 0) r.push(header("Your songs"))
        for (i = 0; i < searchSongs.length; i++) r.push(songRow(searchSongs[i], "found"))
        if (searchStations.length > 0) r.push(header("Stations"))
        for (i = 0; i < searchStations.length; i++) r.push(stationRow(searchStations[i]))
        r.push(header("YouTube"))
        if (ytLoading) r.push(info("Searching YouTube…"))
        else if (searchVideos.length === 0) r.push(info("Nothing on YouTube"))
        for (i = 0; i < searchVideos.length; i++) r.push(videoRow(searchVideos[i]))
      }
    } else if (view === "queue") {
      // Tidy Up next: the playing song, then what follows, each with move
      // and remove on hover (or Alt+↑/↓ and Delete).
      var start = Math.max(0, svc.songPos)
      var left = Math.max(0, queue.length - start)
      // One click clears everything after the playing song.
      var after = Math.max(0, left - 1)
      r.push(header(left > 0 ? "Up next · " + left + (left === 1 ? " song" : " songs") : "Up next",
                    after > 0 ? "󰩺" : "", "Clear the rest (" + (binds.clearQueue || "") + ")", "clear"))
      if (queue.length === 0) r.push(info("Nothing queued. Play a folder, a station or a search result."))
      for (i = start; i < queue.length; i++) {
        var q = queue[i]
        var qFav = svc.isFav(q.file)
        r.push({ type: "queue", key: "queue:" + i, glyph: i === svc.songPos ? "󰐊" : kindGlyph(queueKind(q)),
                 title: q.title, sub: clock(q.duration), current: i === svc.songPos, data: q,
                 action: isLocal(q.file) ? (qFav ? "󰓎" : "󰓒") : "", actionOn: qFav,
                 actionTip: qFav ? "Unstar (Alt+Space)" : "Star (Alt+Space)",
                 extras: [
                   { id: "up", icon: "󰁝", tip: "Move up (Alt+↑)", on: i > start },
                   { id: "down", icon: "󰁅", tip: "Move down (Alt+↓)", on: i < queue.length - 1 },
                   { id: "remove", icon: "󰅖", tip: "Remove (Delete)", on: true }
                 ] })
      }
    } else if (view === "lyrics") {
      if (lyricsLoading || !lyricsData) r.push(info("Looking for lyrics…"))
      else if (lyricsData.text) {
        r.push(header((lyricsData.artist ? lyricsData.artist + " · " : "") + (lyricsData.title || ""),
                      svc.kind === "book" ? "󰊓" : "", "Read full screen (opens the reader)", "reader"))
        for (i = 0; i < textParas.length; i++)
          r.push({ type: "lyric", key: "para:" + i, title: "",
                   data: { idx: i, t: paraStart(i), para: true } })
        r.push(info("Text from " + (lyricsData.text.source === "tag" ? "the file's tags"
                    : "the ." + lyricsData.text.source + " file")
                    + (textSynced ? "" : " · not timed, so nothing follows")))
      }
      else if (!lyricsData.lyrics) {
        // A book with no text still opens full screen in the reader.
        if (svc.kind === "book")
          r.push(header((lyricsData.artist ? lyricsData.artist + " · " : "") + (lyricsData.title || ""),
                        "󰊓", "Open in the reader", "reader"))
        r.push(info(lyricsData.why || "No lyrics found"))
      }
      else {
        var L = lyricsData.lyrics
        r.push(header((lyricsData.artist ? lyricsData.artist + " · " : "") + (lyricsData.title || "")))
        if (lyricsSynced) {
          for (i = 0; i < L.synced.length; i++)
            r.push({ type: "lyric", key: "lyric:" + i, title: L.synced[i].line || "♪",
                     data: { idx: i, t: L.synced[i].t } })
        } else {
          var plain = String(L.plain || "").split("\n")
          for (i = 0; i < plain.length; i++)
            r.push({ type: "lyric", key: "lyric:" + i, title: plain[i] || " ",
                     data: { idx: -1, t: -1 } })
        }
        r.push(info("Lyrics from LRCLIB"))
      }
    } else if (settingsRows.rows(view) !== null) {
      // The settings screens live in SettingsRows.qml.
      return settingsRows.rows(view)
    } else if (view === "inbox") {
      r.push(backRow("Import songs"))
      var lists = svc.inbox.lists || [], files = svc.inbox.files || []
      // ↻ looks in Downloads again; it turns while it looks.
      r.push(header("From your Downloads folder", "󰑐", "Look again", "refreshinbox"))
      r[r.length - 1].spinning = svc.inboxLoading
      if (lists.length + files.length === 0) {
        r.push(info("Nothing waiting yet"))
        r = r.concat(importSteps())
      } else {
        r.push(navRow("How importing works", "󰋗", "", { view: "importhelp" }))
      }
      if (lists.length > 0) r.push(header("Song lists"))
      for (i = 0; i < lists.length; i++)
        r.push({ type: "inboxlist", key: "inboxlist:" + lists[i].path, glyph: "󰲸",
                 title: lists[i].name, sub: "open", chevron: true, data: lists[i],
                 extras: [{ id: "dismiss", icon: "󰅖", tip: "Not a song list: hide it", on: true }] })
      if (files.length > 0)
        r.push(header("Music files", "󰉗", "Move them all into a folder", "moveall"))
      for (i = 0; i < files.length; i++)
        r.push({ type: "inboxfile", key: "inboxfile:" + files[i].path, glyph: "󰎈",
                 title: files[i].name, sub: "move in", data: files[i],
                 extras: [{ id: "dismiss", icon: "󰅖", tip: "Leave it in Downloads", on: true }] })
      if (lists.length + files.length > 0)
        r.push({ type: "tip", key: "howshort", glyph: "", quiet: true,
                 title: "Song lists (.txt, .csv, .m3u) and music files that arrive in Downloads show here for two weeks. ✕ hides one." })
    } else if (view === "importhelp") {
      r.push(backRow("How importing works"))
      r = r.concat(importSteps())
    } else if (view === "importlist") {
      r.push(backRow(baseName(importPath)))
      var chosen = 0
      for (i = 0; i < importChosen.length; i++) if (importChosen[i]) chosen++
      if (importSongs.length === 0) r.push(info("Reading the list…"))
      else {
        r.push({ type: "importplay", key: "importplay", glyph: "󰐊",
                 title: "Play the chosen songs", sub: chosen + " songs" })
        r.push({ type: "importdownload", key: "importdownload", glyph: "󰇚",
                 title: "Download into a folder…", sub: "a few seconds apart" })
        r.push(header(chosen + " of " + importSongs.length + " chosen", "󰄲",
                      "Tick or untick all", "tickall"))
        for (i = 0; i < importSongs.length; i++) {
          var sg = importSongs[i]
          r.push({ type: "importsong", key: "importsong:" + i, glyph: importChosen[i] ? "󰄲" : "󰄱",
                   title: (sg.artist ? sg.artist + " – " : "") + (sg.title || "YouTube link"),
                   sub: sg.url ? "link" : "", data: { idx: i } })
        }
      }
    } else if (view === "tour") {
      var tip = tourTips[Math.min(tourStep, tourTips.length - 1)]
      r.push(header("Welcome · " + (tourStep + 1) + " of " + tourTips.length, "󰒭", "Skip the tour",
                    "skiptour"))
      r.push({ type: "tip", key: "tip:" + tourStep, glyph: tip[0], title: tip[1] })
      r.push({ type: "tip", key: "tipdetail:" + tourStep, glyph: "", title: tip[2], quiet: true })
      r.push({ type: "tournext", key: "tournext", glyph: "󰅂",
               title: tourStep < tourTips.length - 1 ? "Next" : "Start listening" })
    } else if (view === "pick") {
      r.push(backRow(choosingDefault ? "Downloads go into…" : pendingMove.length > 0 ? "Move into…" : "Save to…"))
      if (choosingDefault) {
        var dfNow = svc.settings.downloadFolder || ""
        r.push({ type: "askeach", key: "askeach", glyph: dfNow === "" ? "󰄬" : "󰋗",
                 title: "Ask each time", current: dfNow === "" })
      } else r.push(info(pendingLabel))
      if (pendingMove.length === 0 && !choosingDefault)
        r.push({ type: "folder", key: "folder:Favorites", glyph: "󰓎", title: "Favorites",
                 sub: "and star it", data: { name: "Favorites" } })
      if (!choosingDefault)
        r.push({ type: "newfolder", key: "newfolder", glyph: "󰉗", title: "New folder" })
      var fs = svc.home.folders || []
      for (i = 0; i < fs.length; i++) r.push(folderRow(fs[i]))
    }
    return r
  }

  // Debounced, and always a fresh array of plain objects.
  Timer {
    id: rebuildTimer
    interval: 75
    onTriggered: {
      var keyBefore = root.pendingKey !== "" ? root.pendingKey
        : (root.selectedIndex >= 0 && root.selectedIndex < root.rows.length
           ? root.rows[root.selectedIndex].key : "")
      root.pendingKey = ""
      // Where he had scrolled to. Handing the ListView a new model puts it
      // back at the top, and a list that refreshes itself (a book being read
      // aloud) would jump to the top every two seconds, losing the place
      // scrolled to each time (reported 2026-09-23).
      var sameList = root.builtView === root.view + "|" + root.viewArg
      // The row at the top of the window, by key: rows come and go (a chapter
      // being made, a button appearing), so a pixel offset would drift.
      var topAt = sameList ? list.indexAt(Style.space(8), list.contentY + 1) : -1
      var topKey = (topAt >= 0 && topAt < root.rows.length) ? root.rows[topAt].key : ""
      root.rows = root.buildRows()
      root.selectedIndex = root.indexOfKey(keyBefore)
      if (root.focusChapter && root.view === "book") {
        for (var c = 0; c < root.rows.length; c++)
          if (root.rows[c].type === "chapter" && root.rows[c].current) { root.focusKey = root.rows[c].key; root.focusChapter = false }
      }
      if (root.focusKey !== "") {
        var at = root.indexOfKey(root.focusKey)
        if (at >= 0) {
          root.selectedIndex = at
          root.focusKey = ""
          Qt.callLater(function() { list.positionViewAtIndex(at, ListView.Center) })
        }
      }
      // A new list starts at the top; the book's text jumps back to the
      // paragraph being read.
      if (root.view === "lyrics" && root.activeP >= 0)
        Qt.callLater(function() { list.positionViewAtIndex(root.activeP + 1, ListView.Center) })
      // A different view starts at its top, not where the last one was
      // scrolled to (the Import row sat hidden above the Library).
      if (!sameList) {
        root.builtView = root.view + "|" + root.viewArg
        list.positionViewAtBeginning()
      } else if (topKey !== "" && root.focusKey === "") {
        // The same list again: put it back where he had it.
        var back = root.indexOfKey(topKey)
        if (back > 0)
          Qt.callLater(function() { list.positionViewAtIndex(back, ListView.Beginning) })
      }
    }
  }
  function rebuild() { rebuildTimer.restart() }

  Connections {
    target: root.svc
    // While chapters are being made, the list shows how far it has got.
    function onSpeakingChanged() { if (root.view === "novel") root.rebuild() }
    function onWorkChanged() { if (root.view === "work") root.rebuild() }
    function onNovelChanged() { if (root.view === "novel") root.rebuild() }
    function onHomeChanged() {
      if (root.view === "favs") root.loadFavs()
      root.rebuild()
    }
    function onErrorRaised(message) { root.flash(message, true) }
    function onNoticeRaised(message) { root.flash(message, false) }
    function onSongPosChanged() { if (root.view === "queue") root.rebuild() }
    function onQueueLengthChanged() { if (root.view === "queue") root.loadQueue() }
    function onQueueEdited() { if (root.view === "queue") root.loadQueue() }
    function onSettingsChanged() { root.rebuild() }
    function onCurrentFileChanged() {
      if (root.view === "lyrics") root.loadLyrics()
      else if (root.view === "book") root.loadBook(root.viewArg)
    }
    function onInboxChanged() { root.rebuild() }
    function onInboxLoadingChanged() { root.rebuild() }
    function onImportJobChanged() { if (root.view === "home") root.rebuild() }
    function onSetupProblemsChanged() { root.rebuild() }
    function onLibraryChanged() { if (root.view === "folder") root.loadFolder(root.viewArg) }
    function onTitleChanged() {
      // A new chapter inside one .m4b is a new title, not a new file.
      if (root.view === "lyrics" && root.svc.kind === "book") lyricsReload.restart()
      if (root.view === "lyrics" && root.svc.isRadio) lyricsReload.restart()
    }
  }

  function indexOfKey(key) {
    if (!key) return -1
    for (var i = 0; i < rows.length; i++) if (rows[i].key === key) return i
    return -1
  }

  function selectable(row) {
    return row && row.type !== "header" && row.type !== "info" && row.type !== "key"
      && row.type !== "tip"
  }

  function moveSelection(dir) {
    var i = selectedIndex
    for (var n = 0; n < rows.length; n++) {
      i = i + dir
      if (i < 0 || i >= rows.length) return
      if (selectable(rows[i])) {
        selectedIndex = i
        list.positionViewAtIndex(i, ListView.Contain)
        return
      }
    }
  }

  // ---- actions ---------------------------------------------------------------

  // A row to pick out once a list has loaded (it may arrive after the
  // first draw, so every rebuild tries until it is there).
  property string focusKey: ""
  property bool focusChapter: false

  function goToPlaying() {
    var cur = svc.current || {}
    var file = String(cur.file || "")
    var target = null, key = ""
    var chapter = false
    if (svc.kind === "book" && svc.status.book) {
      target = { view: "book", arg: svc.status.book.folder }
      chapter = true
    } else if (svc.kind === "music" && isLocal(file) && file !== "") {
      target = { view: "folder", arg: dirOf(file) }
      key = "song:" + file
      if (target.arg === "") target = { view: "home", arg: null }
    } else if (svc.playingFrom && ["stations", "home", "favs", "folder"].indexOf(svc.playingFrom.view) >= 0) {
      target = { view: svc.playingFrom.view, arg: svc.playingFrom.arg }
      key = "station:" + file
    } else {
      showTop("queue")                // a search or a link: Up next has it
      return
    }
    history = target.view === "home" ? [] : [{ view: "home", arg: null }]
    view = target.view
    viewArg = target.arg
    namingFolder = false
    selectedIndex = -1
    focusKey = key                    // after the view changes, which clears it
    focusChapter = chapter
    enter()
  }

  function dirOf(file) {
    var p = String(file || "")
    var slash = p.lastIndexOf("/")
    return slash > 0 ? p.slice(0, slash) : ""
  }

  function activate(row, mode, shift) {
    // Already working on this very row: a second click would start the whole
    // thing again beside the first, which is what several clicks on a
    // YouTube playlist did (2026-09-24).
    if (row && row.key !== undefined && row.key !== "" && row.key === busyKey)
      return
    if (!row || !selectable(row)) return
    mode = mode || "replace"
    var d = row.data
    if (row.type === "back") back()
    else if (row.type === "folder") {
      if (view === "pick") saveInto(d.name)
      else if (mode === "next" || d.shelf) go("folder", d.name)   // a shelf of books opens
      else svc.playFolder(d.name)
    }
    // A song on its own plays on its own; the folder row plays the folder.
    else if (row.type === "song" || row.type === "found" || row.type === "favsong") {
      if (mode === "next" || mode === "end") {
        svc.addSong(d.file, mode)
        flash(mode === "next" ? "Plays next" : "Added to Up next")
      }
      else svc.addSong(d.file, "replace")
    }
    else if (row.type === "favfolder") {
      if (mode === "next" || svc.favorites.length === 0) go("favs")
      else svc.playFavorites()
    }

    else if (row.type === "station" || row.type === "video" || row.type === "link") {
      busyKey = row.key
      rebuild()
      // Stations and YouTube both take a moment; spin the row meanwhile.
      var done = function(res) {
        busyKey = ""
        rebuild()
        if (res && res.more > 0) flash("Playing · adding " + res.more + " more in the background")
      }
      var m = mode === "end" ? "end" : mode
      if (m === "replace") svc.playingFrom = { view: view, arg: viewArg, url: d.url }
      if (row.type === "station") svc.playStation(d, m, done, m === "replace" ? stationListFor(d.url) : [])
      else svc.playYoutube(d.url, m, done)
    }
    else if (row.type === "job") {
      if (d.stop) svc.stopWork(d.stop, function() { rebuild(); flash("Stopped") })
    }
    else if (row.type === "queued") {
      svc.dropChapters(d.path, [d.chapter], function() {
        svc.loadWork(function() { rebuild() })
        flash("Taken off the queue")
      })
    }
    else if (row.type === "lyric") { if (d.t >= 0) svc.seekTo(d.t) }
    else if (row.type === "setting") toggleSetting(d.key)
    else if (row.type === "listtoggle") toggleListItem(d.key, d.item)
    else if (row.type === "voicepick") {
      if (!d.ready) { flash("Not installed yet · " + row.sub.replace("not installed · ", ""), true); return }
      svc.setSetting("voiceEngine", d.id)
      svc.setSetting("voiceName", "")
      svc.loadVoices()
    }
    else if (row.type === "voicename") { svc.setSetting("voiceName", d.name); back() }
    else if (row.type === "speakstop") svc.stopSpeaking(function(res) {
      flash(res && Number(res.dropped || 0) > 0
            ? "Stopped · " + res.dropped + " taken off the queue" : "Stopped")
    })
    // Tick a chapter, or play one that is already spoken.
    else if (row.type === "novelchapter") {
      // Shift ticks a run of chapters, whatever this one is.
      if (shift && d.state !== "making") { tickTo(d.index); rebuild() }
      else if (d.state === "ready") svc.playSpoken(d.path, d.index, function(res) {
        if (res && res.chapter) flash("󰎆  " + res.chapter)
      })
      else if (d.state === "queued") svc.dropChapters(d.path, [d.index], function() {
        flash("Taken off the queue")
      })
      else if (d.state !== "making") {
        tick(d.index)
        flash(tickedCount > 0
              ? tickedCount + (tickedCount === 1 ? " chapter ticked" : " chapters ticked")
                + " · press Read aloud to start"
              : "Nothing ticked")
        rebuild()
      }
    }
    // The play button on a chapter that has not been made: make it and start
    // playing as soon as there is something to play (his choice, 2026-09-25).
    else if (row.type === "novelchapterplay") {
      svc.speakChapters(d.path, [d.index], function() { rebuild() }, true)
      flash("Reading it aloud · playing in about half a minute")
    }
    // Make the ticked ones: they join the queue the reader shares.
    else if (row.type === "speakmake") startTicked()
    else if (row.type === "book" && d.textOnly) svc.openReader(d.file || d.name)
    else if (row.type === "readrow") svc.openReader(d.folder)
    else if (row.type === "book") {
      svc.playBook(d.name, "", function(res) {
        if (res && res.chapter) flash("󰂺  " + res.chapter + (res.at > 5 ? " · from " + clock(res.at) : ""))
      })
    }
    else if (row.type === "chapter") {
      svc.playBook(d.folder, d.id, function() { loadBook(d.folder) })
    }

    else if (row.type === "errorrow") copyText(d.text)
    else if (row.type === "askeach") { svc.setSetting("downloadFolder", ""); choosingDefault = false; back() }
    else if (row.type === "starsexport") {
      svc.call(["stars-export"], function(res) {
        if (res && res.ok) flash(res.count + " stations saved to " + tilde(res.path))
        else flash(res ? res.error : "Could not export", true)
      })
    }
    else if (row.type === "starsfile") {
      svc.call(["stars-import", d.path], function(res) {
        if (res && res.ok) {
          svc.loadHome()
          flash(res.added > 0 ? res.added + " stations starred" : "Nothing new in " + d.name)
          back()
        } else flash(res ? res.error : "Could not import", true)
      })
    }
    else if (row.type === "ytlist") {
      // A playlist takes a while to start: list it, resolve its first song,
      // then fill the rest in the background. Say so and spin the row, as a
      // single video already did -- without that, clicking it again looked
      // like the way to make something happen, and every click started
      // another one (his report, 2026-09-24).
      busyKey = row.key
      rebuild()
      svc.playYoutube(d.url, mode === "next" || mode === "end" ? mode : "replace",
                      function(res) {
                        busyKey = ""
                        rebuild()
                        if (res && res.error) return
                        flash(res && res.more > 0
                              ? "Playing " + d.title + " · adding " + res.more + " more"
                              : "Playing " + d.title)
                      })
      flash("Starting " + d.title + "…")
    }
    else if (row.type === "loginpick") chooseLogin(d.id)
    else if (row.type === "rescan") {
      if (rescanning) return
      rescanning = true
      rebuild()
      svc.call(["rescan"], function(res) {
        rescanning = false
        if (res && res.error) flash(String(res.error), true)
        else {
          // The list is reloaded here, not left to MPD's own report: MPD says
          // nothing at all about a folder of novels, so without this the
          // button could never find one (2026-09-30).
          svc.loadHome()
          flash("Looked again · " + (res && res.folders !== undefined ? res.folders : "0")
                + " folders · new songs appear as MPD finishes")
        }
        rebuild()
      })
    }
    else if (row.type === "clearsaved") {
      // Two clicks, like Reset, so a stray one does not clear anything.
      if (!clearArmed) { clearArmed = true; clearDisarm.restart(); rebuild(); return }
      clearArmed = false
      svc.call(["clear-saved"], function() {
        flash("Cleared: covers and lyrics are fetched again when needed")
        loadSavedSize()
      })
    }
    else if (row.type === "radioretest") {
      if ((radioInfo.retest || {}).state === "running") return
      svc.workStarted()
      svc.call(["radio-retest"], function(d) {
        flash(d && d.total ? "Testing " + d.total + " stations in the background"
                           : "No station lists kept yet: open a genre or country first")
        loadRadioInfo()
      })
    }
    else if (row.type === "logincheck") { if (!loginChecking) checkLogin() }
    else if (row.type === "binding") { captureAction = d.id; rebuild(); keys.forceActiveFocus() }
    else if (row.type === "importprogress") svc.importStop()
    else if (row.type === "inboxlist") openImport(d.path)
    else if (row.type === "inboxfile") askMove([d.path], d.name)
    else if (row.type === "importsong") {
      var next = importChosen.slice()
      next[d.idx] = !next[d.idx]
      importChosen = next
      rebuild()
    }
    else if (row.type === "importplay") startImport("play", "")
    else if (row.type === "importdownload") {
      pendingImport = { songs: chosenSongs(), path: importPath }
      pendingLabel = chosenSongs().length + " songs from " + baseName(importPath)
      go("pick")
    }
    else if (row.type === "tip") { }
    else if (row.type === "tournext") {
      if (tourStep < tourTips.length - 1) { tourStep++; rebuild() } else finishTour()
    }
    else if (row.type === "tourrow") { tourStep = 0; showTop("tour") }
    else if (row.type === "resetall") {
      // Two clicks, so a stray one does not wipe the settings.
      if (!resetArmed) { resetArmed = true; resetDisarm.restart(); rebuild(); return }
      resetArmed = false
      svc.resetSettings()
      flash("Settings and shortcuts are back to the defaults")
    }
    else if (row.type === "barbinding") {
      var order = ["playPause", "next", "rmpc", "card"]
      var nowAt = order.indexOf(svc.settings[d.id])
      svc.setSetting(d.id, order[(nowAt + 1) % order.length])
    }
    else if (row.type === "nav") {
      // Genre and country rows open a station list; every other nav row
      // names the view it opens. (Only three were listed before, so
      // "Import songs" opened an empty station list.)
      if (d.view === "stations") go("stations", d)
      else go(d.view)
    }
    else if (row.type === "queue") svc.playAt(d.pos)
    else if (row.type === "newfolder") startNaming()
  }

  // A pick-several setting: hidden folders and genres list what is hidden
  // (the row shows "shown" when it is not), countries list what is chosen.
  function toggleListItem(key, item) {
    var list = (svc.settings[key] || []).slice()
    var at = list.indexOf(item)
    if (at >= 0) list.splice(at, 1)
    else {
      if (key === "radioCountries" && list.length >= 10) { flash("Ten countries at most", true); return }
      list.push(item)
    }
    svc.setSetting(key, list)
    if (key === "radioCountries") discover = []     // the next Home asks again
  }

  // Your YouTube (signed in): the Liked list and his playlists.
  property var ytMine: []
  property bool ytMineLoading: false
  property string ytMineError: ""
  function loadYtMine() {
    ytMineLoading = true
    ytMineError = ""
    svc.call(["yt-mine"], function(d) {
      ytMineLoading = false
      ytMine = d && d.lists ? d.lists : []
      ytMineError = d && d.error ? String(d.error) : ""
      rebuild()
    })
  }

  // Playlists in Downloads that starred stations can come in from.
  property var starFiles: []
  function loadStarFiles() {
    svc.call(["stars-files"], function(d) {
      starFiles = d && d.files ? d.files : []
      rebuild()
    })
  }

  // How much of the machine the voice may use, in words rather than a number
  // of cores: what he is choosing is how much of the desk it takes.
  function coresWord(n) {
    return n <= 1 ? "gentle" : n >= 4 ? "fast" : "normal"
  }

  // Settings flip (on/off) or step (fade: off, 3, 6, 10 s) in place.
  function toggleSetting(key) {
    if (key === "musicDir") { startEditingMusicDir(); return }
    if (key === "youtubeLogin") { go("account"); return }
    var cycles = {
      downloadFormat: ["mp3", "opus"], downloadQuality: ["best", "small"],
      radioHide: ["none", "bad", "slow"], youtubeQuality: ["best", "saver"],
      spokenAhead: [1, 2, 3, 5, 8, 10],
      meaningLanguage: ["en", "hi", "gu", "mr", "bn", "ta", "te", "ur", "es", "fr", "de"],
      searchResults: [6, 10, 15], danceStyle: [-1, 0, 1, 2, 3, 4, 5, 6]
    }
    if (key === "downloadFolder") { choosingDefault = true; go("pick"); return }
    cycles.folderSort = ["name", "recent"]
    cycles.notifyShows = ["full", "text", "title"]
    cycles.lyricsOffset = [0, 500, 1000, -1000, -500]
    cycles.lyricsSize = ["normal", "large"]
    cycles.voiceSpeed = [100, 125, 150, 175, 200, 50, 75]
    cycles.voiceCores = [2, 4, 1]
    cycles.textHighlight = ["sentence", "word", "paragraph"]
    cycles.bookSkipBack = [10, 15, 30]
    cycles.bookSkipForward = [30, 10]
    cycles.bookRewind = [10, 30, 0, 5]
    if (cycles[key] !== undefined) {
      var list = cycles[key]
      var cur = svc.settings[key]
      svc.setSetting(key, list[(list.indexOf(cur) + 1) % list.length])
      return
    }
    if (key === "radioRetestDays") {
      svc.setSetting(key, (svc.settings.radioRetestDays || 5) === 5 ? 10 : 5)
      return
    }
    var st = svc.settings
    if (key === "youtubeRelay") {
      svc.setSetting(key, st.youtubeRelay === false)
      flash(st.youtubeRelay === false ? "YouTube goes through the steady relay again"
                                      : "YouTube plays direct links (may stop partway)")
      return
    }
    if (key === "crossfade") {
      var steps = [0, 3, 6, 10]
      svc.setSetting(key, steps[(steps.indexOf(st.crossfade) + 1) % steps.length])
    } else {
      svc.setSetting(key, !st[key])
    }
  }

  // Editing the music folder: the path goes in the search box; Enter saves,
  // which edits mpd.conf (a dated backup is kept) and restarts MPD. The
  // notice row says so before anything happens.
  property bool editingMusicDir: false


  function startEditingMusicDir() {
    editingMusicDir = true
    rebuild()
    searchField.text = tilde(svc.musicDir)
    searchField.forceActiveFocus()
    searchField.selectAll()
  }

  function stopEditingMusicDir() {
    editingMusicDir = false
    searchField.text = ""
    rebuild()
    keys.forceActiveFocus()
  }

  function saveMusicDir(path) {
    if (path === "" || path === tilde(svc.musicDir)) { stopEditingMusicDir(); return }
    flash("Saving… MPD restarts for a moment")
    svc.setMusicDir(path, function(d) {
      if (d && d.error) { flash(d.error, true); return }
      stopEditingMusicDir()
      flash(d && d.note ? d.note : "Music folder changed; MPD rescanned it")
      svc.loadHome()
    })
  }

  function rowExtra(row, id) {
    if (row && id === "read" && row.type === "novelchapter") {
      // A chapter of a novel: open the reader on the book, at that chapter.
      svc.call(["reader-open", row.data.path], function(d) {
        if (d && d.error) flash(d.error, true)
        else flash("Opening the reader…")
      })
      return
    }
    if (row && id === "read") { svc.openReader(row.data.name); return }
    if (row && id === "dismiss") { svc.inboxDone(row.data.path); return }
    if (row && id === "saveall") {
      pendingImport = { playlist: row.data.url }
      pendingLabel = "All " + (row.data.count || "") + " songs of " + (row.data.title || "the playlist")
      go("pick")
      return
    }
    if (row && id === "add") {
      svc.addSong(row.data.file, "end")
      flash("Added to Up next")
      return
    }
    if (!row || row.type !== "queue") return
    var pos = row.data.pos
    if (id === "remove") svc.queueDelete(pos)
    else if (id === "up" && pos > Math.max(0, svc.songPos)) svc.queueMove(pos, pos - 1)
    else if (id === "down" && pos < queue.length - 1) svc.queueMove(pos, pos + 1)
  }

  function rowAction(row) {
    if (!row) return
    if (row.type === "errorrow") { copyText(row.data.text); return }
    if (row.type === "voicepick") {
      if (row.data.ready) return
      if (row.data.setup) {
        svc.call(["voice-install", row.data.id], function(d) {
          if (d && d.error) { errorRaised(String(d.error)); return }
          voiceWatch.reload()
          svc.workStarted()
        })
        voiceInstall = { state: "working", note: "Starting…" }
        rebuild()
      } else copyText(row.data.install)
      return
    }
    if (row.type === "listtoggle" && row.data.key === "bookFolders") {
      go("setBookFolders", row.data.item)
      return
    }
    if (row.type === "book" && row.data.textOnly) {
      go("novel", row.data.file || row.data.name)
      return
    }
    if (row.type === "book") { go("book", row.data.name); return }
    if (row.type === "speakmake") { ticked = ({}); tickedCount = 0; tickAnchor = -1; rebuild(); return }
    if (row.type === "job") {
      if (row.data.stop) svc.stopWork(row.data.stop, function() { rebuild(); flash("Stopped") })
      return
    }
    if (row.type === "queued") {
      svc.dropChapters(row.data.path, [row.data.chapter], function() {
        svc.loadWork(function() { rebuild() })
      })
      return
    }
    if (row.type === "novelchapter") {
      if (row.data.state === "ready")
        svc.playSpoken(row.data.path, row.data.index, function(res) {
          if (res && res.chapter) flash("󰎆  " + res.chapter)
        })
      else if (row.data.state !== "making") {
        svc.speakChapters(row.data.path, [row.data.index], function() { rebuild() }, true)
        flash("Reading it aloud · playing in about half a minute")
      }
      return
    }
    if (row.type === "chapter") {
      svc.call(["book-forget", row.data.folder, row.data.id], function() { loadBook(row.data.folder) })
      return
    }
    if (row.type === "importprogress") { svc.importStop(); return }
    if (row.type === "station") svc.star(row.data)
    else if (row.type === "song" || row.type === "found" || row.type === "favsong"
             || row.type === "queue") svc.favToggle(row.data.file)
    else if (row.type === "favfolder") go("favs")
    else if (row.type === "video") askFolder(row.data.url, row.data.title)
    else if (row.type === "folder") go("folder", row.data.name)
    else if (row.type === "header" && row.data.id === "discover") loadDiscover()
    else if (row.type === "header" && row.data.id === "reader")
      svc.openReader(svc.status.book ? svc.status.book.folder : "")
    else if (row.type === "header" && row.data.id === "refreshlist") loadStations(viewArg, true)
    else if (row.type === "header" && row.data.id === "refreshinbox") svc.loadInbox()
    else if (row.type === "header" && row.data.id === "clear") svc.queueClearAfter()
    else if (row.type === "header" && row.data.id === "skiptour") finishTour()
    else if (row.type === "header" && row.data.id === "tickall") {
      var any = importChosen.indexOf(false) >= 0
      importChosen = importSongs.map(function() { return any })
      rebuild()
    }
    else if (row.type === "header" && row.data.id === "moveall") {
      var fl = svc.inbox.files || []
      askMove(fl.map(function(f) { return f.path }), fl.length + " music files")
    }
    else if (row.type === "header" && row.data.id === "resetkeys") {
      svc.setSetting("keys", svc.defaultKeys)
      svc.setSetting("barMiddle", "playPause")
      svc.setSetting("barRight", "rmpc")
      flash("Shortcuts are back to the defaults")
    }
  }

  // ---- shortcuts: every one can be changed in the Shortcuts view ----------
  //
  // An event becomes a combination like "Ctrl+Space" or "Alt+Up"; the
  // action whose saved combination matches runs. Shift is dropped for
  // symbols ("?" is Shift+/ on most keyboards) and kept for letters.

  readonly property var keyNames: ({
    [Qt.Key_Space]: "Space", [Qt.Key_Return]: "Return", [Qt.Key_Enter]: "Return",
    [Qt.Key_Delete]: "Delete", [Qt.Key_Backspace]: "Backspace", [Qt.Key_Tab]: "Tab",
    [Qt.Key_Up]: "Up", [Qt.Key_Down]: "Down", [Qt.Key_Left]: "Left", [Qt.Key_Right]: "Right",
    [Qt.Key_Home]: "Home", [Qt.Key_End]: "End", [Qt.Key_PageUp]: "PageUp",
    [Qt.Key_PageDown]: "PageDown", [Qt.Key_Insert]: "Insert"
  })

  function comboOf(event) {
    var k = event.key
    var name = ""
    if (keyNames[k] !== undefined) name = keyNames[k]
    else if (k >= Qt.Key_F1 && k <= Qt.Key_F12) name = "F" + (k - Qt.Key_F1 + 1)
    else if ((k >= Qt.Key_A && k <= Qt.Key_Z) || (k >= Qt.Key_0 && k <= Qt.Key_9))
      name = String.fromCharCode(k)
    else if (event.text && event.text.length === 1 && event.text > " ") name = event.text
    if (name === "") return ""
    var symbol = name.length === 1 && !/[A-Za-z0-9]/.test(name)
    var mods = []
    if (event.modifiers & Qt.ControlModifier) mods.push("Ctrl")
    if (event.modifiers & Qt.AltModifier) mods.push("Alt")
    if ((event.modifiers & Qt.ShiftModifier) && !symbol) mods.push("Shift")
    if (event.modifiers & Qt.MetaModifier) mods.push("Meta")
    return mods.concat([name]).join("+")
  }

  // Named "binds", not "keys": `keys` is the id of the key-handling Item.
  readonly property var binds: svc && svc.settings.keys ? svc.settings.keys : ({})
  function matches(action, event) { return binds[action] !== undefined && binds[action] === comboOf(event) }

  // In the search box only chords with Ctrl, Alt or Meta act; plain keys
  // (Space, Delete, letters) keep typing.
  function isChord(event) {
    return (event.modifiers & (Qt.ControlModifier | Qt.AltModifier | Qt.MetaModifier)) !== 0
  }

  // Runs the action bound to this key, if any. Returns true when handled.
  function shortcut(event, inField) {
    var row = selectedIndex >= 0 ? rows[selectedIndex] : null
    if (inField && !isChord(event)) return false
    if (matches("download", event)) {
      if (row && row.type === "video") askFolder(row.data.url, row.data.title)
      else saveCurrent()
    } else if (matches("star", event)) {
      if (row && row.type === "station") svc.star(row.data)
      else if (row && (row.type === "song" || row.type === "found" || row.type === "favsong"
                       || (row.type === "queue" && isLocal(row.data.file)))) svc.favToggle(row.data.file)
      else if (row && row.type === "video") starOnline(row.data.url, row.data.title)
      else starCurrent()
    } else if (matches("playNext", event)) {
      activate(row, "next")
    } else if (matches("addToQueue", event)) {
      activate(row, "end")
    } else if (matches("clearQueue", event)) {
      svc.queueClearAfter()
    } else if (view === "queue" && row && row.type === "queue"
               && (matches("remove", event) || matches("moveUp", event) || matches("moveDown", event))) {
      var id = matches("remove", event) ? "remove" : (matches("moveUp", event) ? "up" : "down")
      if (id !== "remove") pendingKey = "queue:" + (row.data.pos + (id === "up" ? -1 : 1))
      rowExtra(row, id)
    } else if (!inField && matches("playPause", event)) {
      svc.togglePlay()
    } else if (!inField && matches("guide", event)) {
      if (view !== "keys") go("keys")
    } else {
      return false
    }
    return true
  }

  // Changing a shortcut: pick a row, then press the new keys.
  property string captureAction: ""

  function capture(event) {
    if (event.key === Qt.Key_Escape) { captureAction = ""; rebuild(); return }
    if ([Qt.Key_Control, Qt.Key_Alt, Qt.Key_Shift, Qt.Key_Meta, Qt.Key_Super_L,
         Qt.Key_Super_R, Qt.Key_AltGr].indexOf(event.key) >= 0) return
    var combo = comboOf(event)
    if (combo === "") return
    var last = combo.split("+").pop()
    if (combo.indexOf("+") < 0 && /^[A-Za-z0-9]$/.test(last)) {
      flash("A letter alone would stop you typing it: add Ctrl, Alt or Shift")
      return
    }
    var reserved = ["Up", "Down", "Left", "Right", "Return", "Backspace", "Escape"]
    if (combo.indexOf("+") < 0 && reserved.indexOf(combo) >= 0) {
      flash(combo + " moves around the card; pick another")
      return
    }
    for (var a in binds) {
      if (a !== captureAction && binds[a] === combo) {
        flash(combo + " is already " + actionLabel(a))
        return
      }
    }
    var next = {}
    for (var b in binds) next[b] = binds[b]
    next[captureAction] = combo
    captureAction = ""
    svc.setSetting("keys", next)
  }

  readonly property var keyActions: [
    ["download", "download song"], ["star", "star song or station"],
    ["playNext", "play next"], ["addToQueue", "add to end of up next"],
    ["remove", "remove from up next"], ["moveUp", "move up in up next"],
    ["moveDown", "move down in up next"], ["clearQueue", "clear the rest of up next"],
    ["playPause", "play / pause"], ["guide", "this list"]
  ]
  function actionLabel(id) {
    for (var i = 0; i < keyActions.length; i++) if (keyActions[i][0] === id) return keyActions[i][1]
    return id
  }
  readonly property var barActionNames: ({ playPause: "play / pause", next: "next song",
                                           rmpc: "open rmpc", card: "open the card" })

  // Nothing selected: star what is playing. A station stars the station; a
  // song from your folders goes into Favorites; a YouTube song is
  // downloaded into Favorites, because its link would expire.
  function starCurrent() {
    if (!svc || !svc.current) { flash("Select a song or station to star it"); return }
    if (svc.isRadio)
      svc.star({ uuid: "", name: svc.station || "Radio", url: svc.current.file, country: "", tags: "" })
    else if (svc.kind === "youtube" && svc.source !== "") starOnline(svc.source, svc.title)
    else svc.favToggle(svc.current.file)
  }

  function starOnline(url, title) {
    svc.startDownload(url, "Favorites")
  }

  // Save what is playing now: a YouTube stream by its page, a radio song by
  // searching YouTube for the title the station sends.
  function saveCurrent() {
    if (!svc || svc.title === "") return
    if (svc.kind === "youtube" && svc.source !== "") askFolder(svc.source, svc.title)
    else if (svc.isRadio) askFolder(svc.title, svc.title)
    else flash("This song is already in your music")
  }

  function askFolder(source, label) {
    pendingDownload = source
    pendingLabel = label
    // Settings → Library → Save into: one folder for every download, when
    // it still exists; otherwise ask as before.
    var df = svc.settings.downloadFolder || ""
    var fs = svc.home.folders || []
    var there = df === "Favorites"
    for (var i = 0; i < fs.length; i++) if (fs[i].name === df) there = true
    if (df !== "" && there) { saveInto(df); return }
    go("pick")
  }

  // Settings → Library → Save into: the pick list chooses the default.
  property bool choosingDefault: false
  onViewChanged: {
    if (view !== "pick") choosingDefault = false
    focusKey = ""                     // a row wanted in another list is not wanted here
    focusChapter = false
  }

  function saveInto(folder) {
    namingFolder = false
    if (choosingDefault) {
      choosingDefault = false
      svc.setSetting("downloadFolder", folder)
      back()
      return
    }
    if (pendingMove.length > 0) {
      var paths = pendingMove
      pendingMove = []
      for (var i = 0; i < paths.length; i++) svc.inboxMove(paths[i], folder)
      flash(paths.length === 1 ? "Moved into " + folder : paths.length + " files moved into " + folder)
    } else if (pendingImport) {
      var job = pendingImport
      pendingImport = null
      svc.importStart("download", folder,
                      job.playlist ? "playlist:" + job.playlist : JSON.stringify(job.songs))
      if (job.path) svc.inboxDone(job.path)
      flash("Downloading into " + folder + ", one song every few seconds")
    } else if (pendingDownload !== "") {
      svc.startDownload(pendingDownload, folder)
      pendingDownload = ""
    } else return
    goHome()
  }

  function askMove(paths, label) {
    pendingMove = paths
    pendingLabel = label
    go("pick")
  }

  function openImport(path) {
    importPath = path
    importSongs = []
    importChosen = []
    go("importlist")
    svc.call(["import-read", path], function(d) {
      if (d && d.error) { flash(d.error, true); return }
      importSongs = d && d.songs ? d.songs : []
      importChosen = importSongs.map(function() { return true })
      rebuild()
    })
  }

  function chosenSongs() {
    return importSongs.filter(function(s, i) { return importChosen[i] })
  }

  function startImport(mode, folder) {
    var songs = chosenSongs()
    if (songs.length === 0) { flash("Tick at least one song"); return }
    svc.importStart(mode, folder, JSON.stringify(songs))
    svc.inboxDone(importPath)
    flash(mode === "play" ? "Playing as each song is found, one every few seconds" : "")
    goHome()
  }

  function startNaming() {
    namingFolder = true
    searchField.text = ""
    searchField.forceActiveFocus()
  }

  // A short line under the controls. Only errors are red; "Added to Up
  // next" and the like are plain.
  property bool messageIsError: false
  function flash(text, isError) {
    message = String(text || "")
    messageIsError = isError === true
    messageTimer.interval = messageIsError ? 15000 : 5000   // time to copy it
    messageTimer.restart()
  }

  Timer {
    id: messageTimer
    interval: 5000
    onTriggered: root.message = ""
  }

  // Copy a problem, with what produced it, so it can be pasted into a search
  // or given to an AI as it is.
  function copyText(text) {
    var full = "rushi.songbook (Omarchy music card): " + String(text || "")
    Quickshell.execDetached(["wl-copy", "--", full])
    message = "󰄬  Copied"
    messageIsError = false
    messageTimer.interval = 2500
    messageTimer.restart()
  }

  readonly property string problemText: message !== "" && messageIsError ? message
    : (message === "" && dlRecent && dl.state === "failed")
      ? "Download failed: " + (dl.title || "") + (dl.error ? " · " + dl.error : "") : ""

  function clock(secs) {
    var t = Math.max(0, Math.floor(Number(secs) || 0))
    if (t === 0) return ""
    var m = Math.floor(t / 60)
    var s = t % 60
    return m + ":" + (s < 10 ? "0" : "") + s
  }

  // ---- download banner ---------------------------------------------------------

  readonly property var dl: svc ? svc.download : ({})
  readonly property bool dlActive: dl.state === "starting" || dl.state === "downloading"
  property bool dlRecent: false
  property real dlSeenAt: 0
  // The file is re-read every second, so react only to a download that
  // finished just now, once.
  onDlChanged: {
    var at = Number(dl.at || 0)
    if ((dl.state === "done" || dl.state === "failed") && at !== dlSeenAt) {
      dlSeenAt = at
      if (Date.now() / 1000 - at < 30) { dlRecent = true; dlTimer.restart() }
      // A download into Favorites stars the song when it lands.
      if (svc) svc.loadHome()
    }
  }
  Timer { id: dlTimer; interval: 8000; onTriggered: root.dlRecent = false }

  // ---- keyboard ------------------------------------------------------------------

  Item {
    id: keys
    anchors.fill: parent
    focus: true

    Keys.onPressed: function(event) {
      var row = root.selectedIndex >= 0 ? root.rows[root.selectedIndex] : null
      var shift = (event.modifiers & Qt.ShiftModifier) !== 0
      if (root.captureAction !== "") {
        root.capture(event)
      } else if (root.shortcut(event, false)) {
        // one of the changeable shortcuts
      } else if (event.key === Qt.Key_Escape) {
        if (root.view === "tour") root.finishTour()
        else if (root.view === "search") searchField.text = ""
        else if (root.view !== "home") root.back()
        else root.closeRequested()
      } else if (event.key === Qt.Key_Down) {
        root.moveSelection(1)
      } else if (event.key === Qt.Key_Up) {
        var before = root.selectedIndex
        root.moveSelection(-1)
        if (root.selectedIndex === before) {
          root.selectedIndex = -1
          searchField.forceActiveFocus()
        }
      } else if (event.key === Qt.Key_Left || event.key === Qt.Key_Backspace) {
        if (root.view !== "home") root.back()
      } else if (event.key === Qt.Key_Right) {
        if (row && row.type === "folder") root.rowAction(row)
        else if (row && row.type === "nav") root.activate(row)
      } else if (event.key === Qt.Key_Return || event.key === Qt.Key_Enter) {
        root.activate(row, "replace", (event.modifiers & Qt.ShiftModifier) !== 0)
      } else if (event.text === "/") {
        searchField.forceActiveFocus()
      } else if (event.text === "q") {
        root.closeRequested()
      } else if (event.text && event.text.length === 1 && event.text > " ") {
        // Any other key starts a search, so typing just works.
        searchField.forceActiveFocus()
        searchField.insert(searchField.length, event.text)
      } else {
        return
      }
      event.accepted = true
    }
  }

  // ---- layout --------------------------------------------------------------------

  ColumnLayout {
    id: column
    anchors.left: parent.left
    anchors.right: parent.right
    anchors.top: parent.top
    spacing: Style.space(10)

    // now playing
    NowPlaying {
      panel: root
      Layout.fillWidth: true
    }

    RowLayout {
      Layout.fillWidth: true
      spacing: Style.space(4)

    TextField {
      id: searchField
      Layout.fillWidth: true
      placeholderText: root.editingMusicDir ? "Music folder path, then Enter"
        : (root.namingFolder ? "Name the new folder, then Enter" : "Search or paste a link · ? keys")
      foreground: root.fg
      font.family: root.fontFamily

      onTextChanged: {
        if (root.namingFolder || root.editingMusicDir) return
        root.query = text
        searchDebounce.restart()
      }

      Keys.onPressed: function(event) {
        var entry = root.namingFolder || root.editingMusicDir
        var enter = event.key === Qt.Key_Return || event.key === Qt.Key_Enter
        if (root.captureAction !== "") {
          root.capture(event)
        } else if (!entry && root.shortcut(event, true)) {
          // a chord (Ctrl/Alt) shortcut works while typing too
        } else if (!entry && text === "" && root.matches("guide", event)) {
          if (root.view !== "keys") root.go("keys")
        } else if (event.key === Qt.Key_Escape) {
          if (root.editingMusicDir) root.stopEditingMusicDir()
          else if (text !== "") text = ""
          else { root.namingFolder = false; keys.forceActiveFocus() }
        } else if (event.key === Qt.Key_Down && !entry) {
          // Arrow into the list, where the shortcuts act on the selected row.
          keys.forceActiveFocus()
          root.moveSelection(1)
        } else if (enter && root.editingMusicDir) {
          root.saveMusicDir(text.trim())
        } else if (enter && root.namingFolder) {
          var name = text.trim()
          if (name !== "") root.saveInto(name)
          text = ""
        } else if (enter) {
          if (root.selectedIndex < 0) root.moveSelection(1)
          root.activate(root.rows[root.selectedIndex], "replace")
        } else {
          return
        }
        event.accepted = true
      }
    }

      // The bottom part's views: Library, Lyrics, Up next.
      PanelActionButton {
        iconText: "󰝚"
        tooltipText: "Library"
        foreground: root.topView === "home" ? root.accent : root.faint
        onClicked: root.goHome()
      }
      PanelActionButton {
        iconText: "󰗚"
        tooltipText: "Lyrics"
        foreground: root.topView === "lyrics" ? root.accent : root.faint
        onClicked: root.showTop("lyrics")
      }
      PanelActionButton {
        iconText: "󰲸"
        tooltipText: "Up next"
        foreground: root.topView === "queue" ? root.accent : root.faint
        onClicked: root.showTop("queue")
      }
    }

    // Whatever the card is busy with, in sight the whole time it runs: what
    // kind of work, how far it has got, and how many others are behind it.
    // Click it for the list. Asked for 2026-09-25: one place that always
    // says what kind of work is running.
    BorderSurface {
      id: workBar
      Layout.fillWidth: true
      Layout.preferredHeight: Style.space(30)
      visible: root.svc && root.svc.working && root.view !== "work"
      radius: Style.spacing.labelGap
      color: Style.normalFillFor(root.fg, root.accent)
      borderSpec: Border.none()

      readonly property var first: (root.svc && root.svc.work.running
                                    && root.svc.work.running.length > 0)
                                   ? root.svc.work.running[0] : null
      readonly property int pct: workBar.first ? Number(workBar.first.percent) : -1

      // How far along, drawn behind the words rather than as a bar of its own.
      Rectangle {
        anchors.left: parent.left
        anchors.top: parent.top
        anchors.bottom: parent.bottom
        width: workBar.pct >= 0 ? parent.width * Math.min(1, workBar.pct / 100) : 0
        radius: Style.spacing.labelGap
        color: Style.selectedFillFor(root.fg, root.accent)
        Behavior on width { NumberAnimation { duration: 400; easing.type: Easing.OutCubic } }
      }

      RowLayout {
        anchors.fill: parent
        anchors.leftMargin: Style.space(10)
        anchors.rightMargin: Style.space(8)
        spacing: Style.space(8)

        Text {
          textFormat: Text.PlainText
          text: "󰦖"
          color: root.accent
          font.family: root.fontFamily
          font.pixelSize: Style.font.body
          RotationAnimator on rotation {
            running: workBar.visible
            from: 0; to: 360; duration: 1400
            loops: Animation.Infinite
          }
        }
        Text {
          textFormat: Text.PlainText
          Layout.fillWidth: true
          elide: Text.ElideRight
          text: root.svc ? root.svc.workLine : ""
          color: root.fg
          font.family: root.fontFamily
          font.pixelSize: Style.font.bodySmall
        }
        Text {
          textFormat: Text.PlainText
          text: " show "
          color: root.dim
          font.family: root.fontFamily
          font.pixelSize: Style.font.bodySmall
        }
      }

      MouseArea {
        anchors.fill: parent
        cursorShape: Qt.PointingHandCursor
        onClicked: root.go("work")
      }
    }

    // Ticking chapters and starting the work are two different things, and
    // the button used to be the first row of the list -- so it scrolled out
    // of sight the moment he scrolled down to tick chapter 60, and he could
    // not tell whether anything had started (his report, 2026-09-23). It is
    // pinned above the list now, in sight wherever he has scrolled to.
    BorderSurface {
      id: tickBar
      Layout.fillWidth: true
      Layout.preferredHeight: Style.space(34)
      visible: root.view === "novel"
               && (root.tickedCount > 0 || tickBar.making || tickBar.waiting > 0)
      radius: Style.spacing.labelGap
      color: Style.normalFillFor(root.fg, root.accent)
      borderSpec: Border.none()

      readonly property var sp: root.svc ? (root.svc.speaking || ({})) : ({})
      readonly property bool making: String(sp.state || "") === "making"
      readonly property int waiting: Number(sp.waiting || 0)
      readonly property int mins: Math.max(1, Math.round(root.tickedWork() / 60))

      RowLayout {
        anchors.fill: parent
        anchors.leftMargin: Style.space(10)
        anchors.rightMargin: Style.space(6)
        spacing: Style.space(8)

        Text {
          textFormat: Text.PlainText
          text: tickBar.making ? "󰔊" : "󰄲"
          color: root.accent
          font.family: root.fontFamily
          font.pixelSize: Style.font.body
        }

        Text {
          textFormat: Text.PlainText
          Layout.fillWidth: true
          elide: Text.ElideRight
          text: root.tickedCount > 0
                ? root.tickedCount + (root.tickedCount === 1 ? " chapter ticked · about "
                                                             : " chapters ticked · about ")
                  + tickBar.mins + " min of work"
                : tickBar.making
                  ? "Making chapter " + (Number(tickBar.sp.chapter || 0) + 1) + " · "
                    + Math.round(Number(tickBar.sp.percent || 0)) + "%"
                    + (tickBar.waiting > 0 ? " · " + tickBar.waiting + " waiting" : "")
                  : tickBar.waiting + " waiting"
          color: root.fg
          font.family: root.fontFamily
          font.pixelSize: Style.font.body
        }

        // Start the work on what is ticked. Nothing happens until this.
        Text {
          textFormat: Text.PlainText
          visible: root.tickedCount > 0
          text: " Read aloud "
          color: root.accent
          font.family: root.fontFamily
          font.pixelSize: Style.font.body
          MouseArea {
            anchors.fill: parent
            anchors.margins: -Style.space(4)
            cursorShape: Qt.PointingHandCursor
            onClicked: root.startTicked()
          }
        }

        Text {
          textFormat: Text.PlainText
          text: root.tickedCount > 0 ? " Clear " : " Stop "
          color: root.dim
          font.family: root.fontFamily
          font.pixelSize: Style.font.body
          MouseArea {
            anchors.fill: parent
            anchors.margins: -Style.space(4)
            cursorShape: Qt.PointingHandCursor
            onClicked: {
              if (root.tickedCount > 0) {
                root.ticked = ({}); root.tickedCount = 0; root.tickAnchor = -1
                root.rebuild()
              } else {
                root.svc.stopSpeaking(function(res) {
                  root.flash(res && Number(res.dropped || 0) > 0
                             ? "Stopped · " + res.dropped + " taken off the queue" : "Stopped")
                })
              }
            }
          }
        }
      }
    }

    ListView {
      id: list
      Layout.fillWidth: true
      Layout.preferredHeight: Math.min(contentHeight, Style.space(300))
      clip: true
      model: root.rows
      boundsBehavior: Flickable.StopAtBounds
      currentIndex: root.selectedIndex

      delegate: PanelRow {
        panel: root
        listWidth: list.width
      }
    }
  }
}
