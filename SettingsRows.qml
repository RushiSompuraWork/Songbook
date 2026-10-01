import QtQuick

// The card's settings screens, and the pages reached from them.
//
// Taken out of MusicPanel.qml on 2026-09-24, where one buildRows() built
// every screen the card has. This one builds nothing else and knows nothing
// else; `panel` is the MusicPanel, for its row makers and its state.
//
// rows(view) returns the rows for one of these screens, or null when the
// screen is not one of its own.
QtObject {
  id: settingsRows
  required property var panel

  // Choosing a word in a book says what it means, in this language. The few
  // he is likely to want by name, anything else by its code.
  function languageName(code) {
    var known = { en: "English", hi: "Hindi", gu: "Gujarati", mr: "Marathi",
                  bn: "Bengali", ta: "Tamil", te: "Telugu", ur: "Urdu",
                  es: "Spanish", fr: "French", de: "German", pt: "Portuguese",
                  it: "Italian", ja: "Japanese", zh: "Chinese", ru: "Russian" }
    return known[code] || code
  }

  function rows(view) {
    var r = []
    var i
    var sl = panel.svc.settings
if (view === "settings") {
  // Settings is a list of sections; each opens its own page. A section
  // row says what is set inside, so most checks need no click.
  var st = panel.svc.settings
  r.push(panel.header("Settings"))
  // First, so a new user finds it: bringing songs in from elsewhere.
  r.push(panel.navRow("Import songs", "󰚇",
                panel.svc.inboxCount > 0 ? panel.svc.inboxCount + " waiting"
                                   : "from Spotify, YouTube, your phone…",
                { view: "inbox" }))
  r.push(panel.navRow("Playback", "󰐊",
                (st.crossfade > 0 ? "fade " + st.crossfade + " s" : "no fade")
                + (st.pauseOnUnplug ? " · pause on unplug" : ""), { view: "setPlayback" }))
  r.push(panel.navRow("Library", "󰉋", tilde(panel.svc.musicDir) || "music folder", { view: "setLibrary" }))
  r.push(panel.navRow("Radio", "󰐹", "keep " + (st.radioRetestDays || 5) + " days"
                + (radioInfo.lastTested ? " · tested " + panel.ago(radioInfo.lastTested) : ""),
                { view: "setRadio" }))
  r.push(panel.navRow("YouTube", "󰗃",
                (st.youtubeLogin ? "signed in" : "not signed in")
                + (st.youtubeRelay === false ? " · direct" : " · steady"), { view: "setYoutube" }))
  r.push(panel.navRow("Read aloud", "󰔊",
                st.voiceEngine ? st.voiceEngine + (st.voiceName ? " · " + st.voiceName : "")
                               : "for novels · pick a voice", { view: "setReadAloud" }))
  r.push(panel.navRow("Lyrics", "󰎈",
                (st.lyricsAuto ? "opens with the card" : "size " + (st.lyricsSize || "normal"))
                + (st.lyricsOffset ? " · " + (st.lyricsOffset > 0 ? "+" : "") + st.lyricsOffset / 1000 + " s" : ""),
                { view: "setLyrics" }))
  r.push(panel.navRow("Bar and notifications", "󰍜",
                "dancer " + (st.barDancer ? "on" : "off") + " · notify " + (st.notify ? "on" : "off"),
                { view: "setBar" }))
  r.push(panel.navRow("Privacy and data", "󰒃",
                "plays reported " + (st.radioReportPlays === false ? "off" : "on"), { view: "setPrivacy" }))
  r.push(panel.navRow("Shortcuts", "󰌌", "?", { view: "keys" }))
  r.push(panel.navRow("Help and reset", "󰋗", "setup check · tour · reset", { view: "setHelp" }))
} else if (view === "setPlayback") {
  var sp = panel.svc.settings
  r.push(panel.backRow("Playback"))
  r.push(panel.settingRow("crossfade", "Fade between songs", sp.crossfade > 0 ? sp.crossfade + " s" : "off"))
  r.push(panel.settingRow("pauseOnUnplug", "Pause when headphones unplug", sp.pauseOnUnplug ? "on" : "off"))
  r.push(panel.settingRow("sleepFade", "Sleep timer fades out", sp.sleepFade === false ? "off" : "over 10 s"))
  r.push(panel.settingRow("consume", "Remove songs from Up next once played", sp.consume ? "on" : "off"))
  r.push(panel.settingRow("shuffleFolders", "Shuffle a folder when it starts", sp.shuffleFolders ? "on" : "off"))
  r.push(panel.settingRow("softPause", "Fade out on pause, in on play", sp.softPause ? "on" : "off"))
  r.push(panel.info("The sleep timer is the ☾ beside the play buttons"))
} else if (view === "setLibrary") {
  r.push(panel.backRow("Library"))
  var sl = panel.svc.settings
  r.push(panel.settingRow("musicDir", "Music folder", tilde(panel.svc.musicDir) || "from mpd.conf"))
  if (panel.editingMusicDir)
    r.push(panel.info("Enter saves: mpd.conf is edited (a backup is kept) and MPD restarts."))
  r.push({ type: "rescan", key: "rescan", glyph: "󰑐", title: "Look for new songs and books now",
           sub: rescanning ? "looking…" : "" })
  r.push(panel.settingRow("folderSort", "Sort folders", sl.folderSort === "recent" ? "last played first" : "by name"))
  var nHidden = (sl.hiddenFolders || []).length
  r.push(panel.navRow("Hide folders", "󰘓", nHidden > 0 ? nHidden + " hidden" : "none hidden",
                { view: "setHidden" }))
  r.push(panel.header("Downloads"))
  r.push(panel.settingRow("downloadFolder", "Save into", sl.downloadFolder ? sl.downloadFolder : "ask each time"))
  r.push(panel.settingRow("downloadFormat", "Format",
                    sl.downloadFormat === "opus" ? "Opus, as YouTube sends it" : "MP3, plays everywhere"))
  if (sl.downloadFormat !== "opus")
    r.push(panel.settingRow("downloadQuality", "Quality",
                      sl.downloadQuality === "small" ? "smaller files" : "best"))
  r.push(panel.settingRow("sponsorblock", "Cut talk and intros that are not music",
                    sl.sponsorblock ? "on" : "off"))
  r.push(panel.navRow("Import songs", "󰚇", panel.svc.inboxCount > 0 ? panel.svc.inboxCount + " waiting" : "",
                { view: "inbox" }))
  r.push(panel.header("Audiobooks"))
  var bf = sl.bookFolders || []
  r.push(panel.navRow("Book folders", "󰂺", bf.length > 0 ? bf.join(", ") : "none chosen",
                { view: "setBookFolders", arg: "" }))
  // "A chapter counts as heard after 30 s / 1 min" was here. A tick now
  // means he reached the end, and the row fills to say how far he got, so
  // there is nothing left to guess at (2026-09-25).
  r.push(panel.settingRow("bookGraySkipped", "Gray out chapters passed over", sl.bookGraySkipped ? "on" : "off"))
  r.push(panel.settingRow("meaningLanguage", "Word meanings in",
                          languageName(sl.meaningLanguage || "en")))
  var ms = sl.meaningSource || "auto"
  r.push(panel.settingRow("meaningSource", "Look meanings up",
                    ms === "online" ? "online only"
                  : ms === "offline" ? "offline only · never sent away"
                  : "online when connected, else offline"))
  r.push(panel.settingRow("bookSkipBack", "󰑟 goes back", (sl.bookSkipBack || 10) + " s"))
  r.push(panel.settingRow("bookSkipForward", "󰈑 goes ahead", (sl.bookSkipForward || 30) + " s"))
  r.push(panel.settingRow("bookRewind", "After a pause of 5 min, go back",
                    sl.bookRewind ? sl.bookRewind + " s" : "off"))
} else if (view === "setRadio") {
  var days = panel.svc.settings.radioRetestDays || 5
  r.push(panel.backRow("Radio"))
  r.push(panel.settingRow("radioRetestDays", "Keep radio lists and speed tests", "for " + days + " days"))
  var rt = radioInfo.retest || {}
  r.push({ type: "radioretest", key: "radioretest", glyph: "󰤨", title: "Test radio speed now",
           sub: rt.state === "running" ? "testing " + (rt.done || 0) + " of " + (rt.total || 0) + "…"
              : radioInfo.lastTested ? "last " + panel.ago(radioInfo.lastTested) : "" })
  var hideNow = panel.svc.settings.radioHide || "none"
  r.push(panel.settingRow("radioHide", "Hide stations",
                    hideNow === "bad" ? "not working" : hideNow === "slow" ? "slow and not working" : "none"))
  r.push(panel.settingRow("radioQuality", "Prefer higher-quality streams",
                    panel.svc.settings.radioQuality ? "on" : "off"))
  var lean = panel.svc.settings.radioCountries || []
  r.push(panel.navRow("Discover leans towards", "󰇧", lean.length > 0 ? lean.join(", ") : "anywhere",
                { view: "setCountries" }))
  var hg = (panel.svc.settings.hiddenGenres || []).length
  r.push(panel.navRow("Genres on the World page", "󰝚", hg > 0 ? hg + " hidden" : "all shown",
                { view: "setGenres" }))
  r.push(panel.header("Starred stations"))
  r.push({ type: "starsexport", key: "starsexport", glyph: "󰈝", title: "Export to Downloads",
           sub: panel.svc.stars.length + (panel.svc.stars.length === 1 ? " station" : " stations") })
  r.push(panel.navRow("Import from Downloads", "󰈠", ".m3u playlist", { view: "starsImport" }))
} else if (view === "setBookFolders") {
  var here = panel.viewArg || ""
  var chosenB = panel.svc.settings.bookFolders || []
  r.push(panel.backRow(here === "" ? "Book folders" : panel.baseName(here)))
  if (here === "") {
    r.push(panel.info("Every folder inside a chosen one opens as a book"))
    for (i = 0; i < chosenB.length; i++)
      r.push(listToggleRow("bookFolders", chosenB[i], chosenB[i], true, "book folder", "󰂺"))
    r.push(panel.header("Your music"))
  }
  var dirs = here === "" ? (panel.svc.home.folders || []) : pickDirs
  for (i = 0; i < dirs.length; i++) {
    var dn = dirs[i].name
    var tr = listToggleRow("bookFolders", dn, dirs[i].label || panel.baseName(dn), chosenB.indexOf(dn) >= 0,
                           "book folder", "󰉋")
    tr.key = "ltd:" + dn
    tr.action = "󰅂"
    tr.actionTip = "Folders inside"
    r.push(tr)
  }
  if (dirs.length === 0 && here !== "") r.push(panel.info("No folders inside"))
} else if (view === "setCountries") {
  r.push(panel.backRow("Discover leans towards"))
  r.push(panel.info("Two of three Discover picks come from these"))
  if (!panel.world) r.push(panel.info("Loading…"))
  else {
    var chosenC = panel.svc.settings.radioCountries || []
    // Chosen ones first, then the rest by name as the World page has them.
    var cs = panel.world.countries.filter(function(c) { return chosenC.indexOf(c.code) >= 0 })
      .concat(panel.world.countries.filter(function(c) { return chosenC.indexOf(c.code) < 0 }))
    for (i = 0; i < cs.length; i++)
      r.push(listToggleRow("radioCountries", cs[i].code, cs[i].name, chosenC.indexOf(cs[i].code) >= 0,
                           "leaning", "󰇧"))
  }
} else if (view === "setGenres") {
  r.push(panel.backRow("Genres on the World page"))
  if (!panel.world) r.push(panel.info("Loading…"))
  else {
    var hiddenG = panel.svc.settings.hiddenGenres || []
    var gs = panel.world.genres || panel.world.tags
    for (i = 0; i < gs.length; i++)
      r.push(listToggleRow("hiddenGenres", gs[i], gs[i], hiddenG.indexOf(gs[i]) < 0, "shown", "󰝚"))
  }
} else if (view === "setHidden") {
  r.push(panel.backRow("Hide folders"))
  r.push(panel.info("Hidden folders still play from search and Up next"))
  var hf = panel.svc.settings.hiddenFolders || []
  var allF = panel.svc.home.folders || []
  for (i = 0; i < allF.length; i++)
    r.push(listToggleRow("hiddenFolders", allF[i].name, allF[i].name, hf.indexOf(allF[i].name) < 0,
                         "shown", "󰉋"))
} else if (view === "starsImport") {
  r.push(panel.backRow("Import starred stations"))
  if (panel.starFiles.length === 0) r.push(panel.info("No .m3u playlist in Downloads"))
  for (i = 0; i < panel.starFiles.length; i++)
    r.push({ type: "starsfile", key: "starsfile:" + panel.starFiles[i].path, glyph: "󰈠",
             title: panel.starFiles[i].name, sub: "add its stations", data: panel.starFiles[i] })
  r.push(panel.info("Stations already starred are skipped"))
} else if (view === "ytMine") {
  r.push(panel.backRow("Your YouTube"))
  if (ytMineLoading) r.push(panel.info("Asking YouTube…"))
  else if (ytMineError !== "") r.push(panel.errorRow(ytMineError))
  for (i = 0; i < ytMine.length; i++)
    r.push({ type: "ytlist", key: "ytlist:" + ytMine[i].url, glyph: i === 0 ? "󰋑" : "󰲸",
             title: ytMine[i].title, sub: ytMine[i].count ? String(ytMine[i].count) : "",
             data: ytMine[i] })
} else if (view === "setYoutube") {
  var sy = panel.svc.settings
  r.push(panel.backRow("YouTube"))
  r.push(panel.settingRow("youtubeLogin", "YouTube sign-in",
                    sy.youtubeLogin ? "from " + browserName(sy.youtubeLogin) : "off"))
  r.push(panel.settingRow("youtubeRelay", "Steady YouTube playback", sy.youtubeRelay === false ? "off" : "on"))
  r.push(panel.settingRow("youtubeQuality", "Streaming quality",
                    sy.youtubeQuality === "saver" ? "data saver" : "best"))
  r.push(panel.settingRow("searchResults", "Results per search", String(sy.searchResults || 6)))
  r.push(panel.settingRow("youtubeMusic", "Search YouTube Music", sy.youtubeMusic ? "songs only" : "off"))
} else if (view === "setBar") {
  var sb = panel.svc.settings
  r.push(panel.backRow("Bar and notifications"))
  r.push(panel.settingRow("barDancer", "Dancer on the bar", sb.barDancer ? "on" : "off"))
  var ds = Number(sb.danceStyle)
  r.push(panel.settingRow("danceStyle", "Dancer style",
                    ds >= 0 && ds < panel.svc.danceStyles.length ? panel.svc.danceStyles[ds][0] : "a new one each song"))
  r.push(panel.settingRow("barTitle", "Song title beside the icon", sb.barTitle ? "on" : "off"))
  r.push(panel.settingRow("notify", "Notify when the song changes", sb.notify ? "on" : "off"))
  if (sb.notify)
    r.push(panel.settingRow("notifyShows", "The notification shows",
                      sb.notifyShows === "title" ? "title only"
                      : sb.notifyShows === "text" ? "title and artist" : "cover, title and artist"))
  r.push(panel.header("Clicks on the bar icon"))
  r.push({ type: "key", title: "Left click", sub: "open the card" })
  r.push({ type: "barbinding", key: "bar:barMiddle", glyph: "󰍽", title: "Middle click",
           sub: panel.barActionNames[sb.barMiddle] || "", data: { id: "barMiddle" } })
  r.push({ type: "barbinding", key: "bar:barRight", glyph: "󰍽", title: "Right click",
           sub: panel.barActionNames[sb.barRight] || "", data: { id: "barRight" } })
} else if (view === "setReadAloud") {
  r.push(panel.backRow("Read aloud"))
  r.push(panel.info("A novel is spoken into your music folder and then plays like an audiobook"))
  var engines = panel.svc.voiceInfo.engines || []
  var chosen = panel.svc.settings.voiceEngine || panel.svc.voiceInfo.engine || ""
  for (i = 0; i < engines.length; i++) {
    var en = engines[i]
    r.push({ type: "voicepick", key: "voice:" + en.id,
             glyph: en.id === chosen ? "󰄬" : en.ready ? "󰔊" : "󰅖",
             title: en.name, sub: en.ready ? en.about : en.install,
             current: en.id === chosen, quiet: !en.ready,
             // Kokoro needs no root: the card can put it in place itself.
             action: !en.ready && en.setup ? "󰇚" : !en.ready ? "󰆏" : "",
             actionTip: !en.ready && en.setup ? "Install it (about 2 GB)" : "Copy how to install it",
             data: { id: en.id, ready: en.ready, setup: en.setup, install: en.install } })
  }
  if (panel.voiceInstall.state === "working")
    r.push({ type: "info", key: "voiceinstall", glyph: "󰇚",
             title: panel.voiceInstall.note || "Installing…",
             sub: panel.voiceInstall.doing || "",
             // Filling from the left, like everything else that is part way
             // through; a 340 MB download needs to look like it is moving.
             part: Math.max(0, Math.min(1, Number(panel.voiceInstall.percent || 0) / 100)) })
  else if (panel.voiceInstall.state === "error") {
    r.push(panel.errorRow(panel.voiceInstall.note || "The install did not finish"))
    r.push(panel.info("Press 󰇚 to carry on — it starts from where it stopped"))
  }
  r.push(panel.header("How it reads"))
  r.push(panel.navRow("Voice", "󰗋", panel.svc.settings.voiceName || "the engine's own", { view: "setVoice" }))
  r.push(panel.settingRow("voiceSpeed", "Speed", (panel.svc.settings.voiceSpeed || 100) + "%"))
  r.push(panel.settingRow("voiceCores", "How hard it works",
                          panel.coresWord(panel.svc.settings.voiceCores || 2)))
  var sa = panel.svc.settings
  r.push(panel.settingRow("spokenAheadOn", "Keep chapters ready ahead",
                          sa.spokenAheadOn ? "on" : "off"))
  if (sa.spokenAheadOn)
    r.push(panel.settingRow("spokenAhead", "How many ahead", String(sa.spokenAhead || 2)))
  r.push(panel.settingRow("spokenTidy", "Remove a chapter once it is heard",
                          sa.spokenTidy ? "on" : "off"))
  if (sa.spokenTidy)
    r.push(panel.info("They can always be read aloud again"))
  r.push(panel.info("Gentle leaves the rest of the machine alone; a chapter still gets made "
                    + "faster than it is listened to"))
  r.push(panel.info("Spoken books are kept in " + (panel.svc.settings.spokenFolder || "Spoken")))
  r.push(panel.info("Tick chapters in a book to have them read aloud: open it from the Library"))
  if (panel.svc.speakingNow || Number(panel.svc.speaking.waiting || 0) > 0)
    r.push({ type: "speakstop", key: "speakstop", glyph: "󰓛",
             title: panel.svc.speakingNow ? "Stop speaking " + (panel.svc.speaking.title || "")
                                    : "Stop · " + panel.svc.speaking.waiting + " waiting",
             sub: panel.svc.speakingNow
                  ? Math.round(Number(panel.svc.speaking.percent || 0)) + "% of chapter "
                    + (Number(panel.svc.speaking.chapter || 0) + 1)
                    + (Number(panel.svc.speaking.waiting || 0) > 0
                       ? " · " + panel.svc.speaking.waiting + " waiting" : "")
                  : "nothing is being made now" })
} else if (view === "setVoice") {
  r.push(panel.backRow("Voice"))
  var vs = panel.svc.voiceInfo.voices || []
  if (vs.length === 0) r.push(panel.info("This engine has no list of voices; it uses its own"))
  r.push({ type: "voicename", key: "voicename:", glyph: !panel.svc.settings.voiceName ? "󰄬" : "󰗋",
           title: "The engine's own", current: !panel.svc.settings.voiceName, data: { name: "" } })
  for (i = 0; i < vs.length && i < 200; i++)
    r.push({ type: "voicename", key: "voicename:" + vs[i], glyph: vs[i] === panel.svc.settings.voiceName ? "󰄬" : "󰗋",
             title: panel.baseName(String(vs[i])), sub: vs[i] === panel.svc.settings.voiceName ? "in use" : "",
             current: vs[i] === panel.svc.settings.voiceName, data: { name: vs[i] } })
} else if (view === "setLyrics") {
  var sly = panel.svc.settings
  r.push(panel.backRow("Lyrics"))
  r.push(panel.settingRow("lyricsAuto", "Open the card on the lyrics", sly.lyricsAuto ? "while a song plays" : "off"))
  var off = Number(sly.lyricsOffset || 0)
  r.push(panel.settingRow("lyricsOffset", "Timing",
                    off === 0 ? "as sent" : (off > 0 ? "lines " + off / 1000 + " s later"
                                                     : "lines " + (-off / 1000) + " s earlier")))
  r.push(panel.settingRow("lyricsSize", "Text size", sly.lyricsSize === "large" ? "large" : "normal"))
  var hl = sly.textHighlight || "sentence"
  r.push(panel.settingRow("textHighlight", "A book lights up", hl === "word" ? "each word"
                    : hl === "paragraph" ? "the paragraph" : "the sentence"))
} else if (view === "setPrivacy") {
  r.push(panel.backRow("Privacy and data"))
  r.push(panel.settingRow("radioReportPlays", "Tell Radio Browser what I play",
                    panel.svc.settings.radioReportPlays === false ? "off" : "on"))
  r.push(panel.info("Only a play count is sent"))
  r.push({ type: "clearsaved", key: "clearsaved", glyph: "󰃢",
           title: "Clear saved covers, lyrics and radio lists",
           sub: clearArmed ? "click again to confirm" : savedSize, current: clearArmed })
  r.push(panel.info("Keeps settings, stars and Favorites"))
} else if (view === "setHelp") {
  r.push(panel.backRow("Help and reset"))
  r.push(panel.navRow("Setup check", "󰄬", "", { view: "setup" }))
  r.push({ type: "tourrow", key: "tourrow", glyph: "󰋗", title: "Show the tour again", sub: "" })
  r.push({ type: "resetall", key: "resetall", glyph: "󰑓", title: "Reset all settings",
           sub: resetArmed ? "click again to confirm" : "and shortcuts", current: resetArmed })
} else if (view === "account") {
  var chosen = panel.svc.settings.youtubeLogin || ""
  r.push(panel.backRow("YouTube sign-in"))
  r.push({ type: "tip", key: "login1", glyph: "󰗹",
           title: "Fewer \"are you a bot?\" stops, and age-restricted songs and your own playlists play" })
  r.push({ type: "tip", key: "login2", glyph: "󰌾", quiet: true,
           title: "The card never sees your password. It borrows the sign-in from a browser you are already signed in to, each time it asks YouTube, and keeps no copy." })
  r.push({ type: "tip", key: "login3", glyph: "󰀦", quiet: true,
           title: "YouTube can block an account it thinks is a program. Safest: sign that browser in to a spare Google account, not your main one." })
  r.push(panel.header("Use the sign-in from"))
  if (loginBrowsers.length === 0) r.push(panel.info("No browser with a sign-in was found"))
  for (i = 0; i < loginBrowsers.length; i++) {
    var b = loginBrowsers[i]
    r.push({ type: "loginpick", key: "loginpick:" + b.id, glyph: b.id === chosen ? "󰄬" : "󰖟",
             title: b.name, sub: b.id === chosen ? "in use" : "", current: b.id === chosen,
             data: { id: b.id } })
  }
  r.push({ type: "loginpick", key: "loginpick:", glyph: chosen === "" ? "󰄬" : "󰍃",
           title: "Don't sign in", sub: chosen === "" ? "in use" : "", current: chosen === "",
           data: { id: "" } })
  if (chosen !== "")
    r.push({ type: "logincheck", key: "logincheck", glyph: "󰑓", title: "Check the sign-in",
             sub: loginChecking ? "checking…" : loginNote })
} else if (view === "setup") {
  r.push(panel.backRow("Setup check"))
  if (setupChecks.length === 0) r.push(panel.info("Checking…"))
  for (i = 0; i < setupChecks.length; i++) {
    var c = setupChecks[i]
    r.push({ type: "key", title: (c.ok ? "󰄬  " : "󰅖  ") + c.name,
             sub: c.ok ? "ok" : c.fix })
  }
} else if (view === "keys") {
  r.push(panel.backRow("Shortcuts"))
  r.push(panel.header("Change any: pick it, press the new keys", "󰑓",
                "Reset shortcuts to the defaults", "resetkeys"))
  for (i = 0; i < keyActions.length; i++) {
    var ka = keyActions[i]
    var capturing = captureAction === ka[0]
    r.push({ type: "binding", key: "binding:" + ka[0], glyph: "󰌌",
             title: capturing ? "press the new keys…  (Esc cancels)" : (panel.binds[ka[0]] || ""),
             sub: ka[1], current: capturing, data: { id: ka[0] } })
  }
  r.push(panel.header("On the bar icon"))
  r.push({ type: "key", title: "Left click", sub: "open the card" })
  r.push({ type: "barbinding", key: "bar:barMiddle", glyph: "󰍽", title: "Middle click",
           sub: panel.barActionNames[panel.svc.settings.barMiddle] || "", data: { id: "barMiddle" } })
  r.push({ type: "barbinding", key: "bar:barRight", glyph: "󰍽", title: "Right click",
           sub: panel.barActionNames[panel.svc.settings.barRight] || "", data: { id: "barRight" } })
  r.push({ type: "key", title: "Scroll", sub: "previous / next" })
  r.push(panel.header("Fixed"))
  for (i = 0; i < panel.fixedKeys.length; i++) r.push(panel.keyRow(panel.fixedKeys[i]))
    } else {
      return null            // not one of this file's screens
    }
    return r
  }
}
