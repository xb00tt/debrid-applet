# To do

Everything from the UX review is built; Torbox, Real-Debrid and AllDebrid are verified against the live APIs.
Nothing planned.

Notes for later:

- Adding another service (Premiumize etc.) is one client class in `debrid_api.py`
  (`account`, `torrents`, `add_magnet`, `add_torrent_file`, `add_link`, `links`, `delete`), a key field in
  Settings and a line in `build_tabs()`. Only worth doing with an account to test against.
- AllDebrid hoster-link unlock: request path verified live (service errors come through, same endpoint powers
  the file panel), a successful unlock of a real hoster link just hasn't been observed yet.
