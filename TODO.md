# To do

Everything from the UX review is built. One idea left:

- **More services** (AllDebrid, Premiumize). `debrid_api.py` clients share one interface
  (`account`, `torrents`, `add_magnet`, `add_torrent_file`, `add_link`, `links`, `delete`),
  so a third tab is a new client class, a Settings field and a line in `build_tabs()`.
  Needs an account on the service to test against.
