# To do

Everything from the UX review is built; Torbox, Real-Debrid and AllDebrid are verified against the live APIs.

- **Premiumize** would be the next service. Same recipe as AllDebrid: one client class in `debrid_api.py`
  (`account`, `torrents`, `add_magnet`, `add_torrent_file`, `add_link`, `links`, `delete`), a key field in
  Settings, a line in `build_tabs()`. Needs an account to test against; Premiumize offers no trial.
- AllDebrid: `.torrent` upload and hoster-link unlock are implemented and unit-tested but not yet tried live.
