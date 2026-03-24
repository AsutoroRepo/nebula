# Changelog

All notable changes to Nebula Killsay are documented here.
Format follows [Keep a Changelog](https://keepachangelog.com/en/1.0.0/).

---

## [3.0.0] — 2024-12-01

### Added
- Complete UI rewrite — new Nebula dark theme with animated starfield
- Three additional UI themes: Crimson, Arctic, Midnight
- Per-weapon message configuration (knife, AWP, pistol, grenade, Zeus)
- Kill streak detection with escalating messages and streak badge
- Headshot detection via `player.state.headshots` diff
- Death message support (health drop to 0 detection)
- Round win / loss messages
- Per-map message overrides (Pro)
- Milestone kill messages — fire a message every N kills (Pro)
- Message pack import / export as `.json` (Pro)
- Message variables: `{weapon}` `{streak}` `{kills}` `{map}` `{hs}`
- Message randomiser — comma-separated or multi-line, round-robin or random
- Session stats panel — KPM, HS%, best streak
- Kill history feed — last 8 events in sidebar
- F9 global hotkey to toggle automation without alt-tabbing
- Auto-update checker
- Onboarding wizard — auto-detects CS2 path, writes GSI config
- GSI connection timeout indicator (60s auto-disconnect)
- Session log export to `.txt`
- System tray minimise with restore/exit menu
- SteamID locking — ignores spectated players' stats
- Cooldown system to prevent message spam

### Changed
- Migrated from polling-based detection to GSI (Game State Integration)
- Settings now persist across sessions via `killsay_settings.json`

### Removed
- Legacy AutoHotkey dependency

---

## [2.1.0] — 2024-06-15

### Added
- Basic kill detection via match stats polling
- Default kill message configuration
- Minimal tray icon

### Fixed
- Message not firing when CS2 window not focused

---

## [2.0.0] — 2024-03-01

### Added
- Initial public release
- Flask-based local server
- F13 virtual key message delivery
- Basic UI via pywebview
