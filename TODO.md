# To do

Open ideas, none blocking.

- **More services** (AllDebrid, Premiumize). `debrid_api.py` clients share one interface, so a third tab is mostly a new client class plus a Settings field.
- **Torbox "stop seeding" lag.** Torbox acknowledges the operation but can keep reporting "uploading (no peers)" for a long time; nothing to fix client-side, but a tooltip explaining that would spare a double-take.
- **Persist the filter dropdown** (state filter resets to All on restart; may or may not be wanted).
