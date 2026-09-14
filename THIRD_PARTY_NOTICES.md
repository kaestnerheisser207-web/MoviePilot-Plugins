# Third-party code and design attribution

## H&R Assistant — InfinityPacer

Source: https://github.com/InfinityPacer/MoviePilot-Plugins/tree/a009a8c7031fe989cd4d3c8c421fe6f7396ca3de/plugins.v3/hitandrun

Revision: `a009a8c7031fe989cd4d3c8c421fe6f7396ca3de`.
License: GNU General Public License v3.0; see this repository's `LICENSE` and the upstream license at https://github.com/InfinityPacer/MoviePilot-Plugins/blob/a009a8c7031fe989cd4d3c8c421fe6f7396ca3de/LICENSE.

The reduced `TorrentHistory`, `TorrentTask`, and `HNRStatus` definitions in `tasks.py`, and the download-event/history/state-management approach in the plugin entrypoint, are adapted from the H&R Assistant. This plugin preserves original HR metadata separately from station-confirmed personal status. The comparison in `calculation.py` and the defaults in `calculation_rules.json` are also adapted from the same upstream methods and rules (144h global duration, 24h additional time, ratio 99, with site overrides). When explicitly enabled, calculated clearance may authorize deletion. It uses actual downloader seeding counters, never the upstream elapsed-since-download timer or configured deadline calculation, and does not install or run the H&R Assistant's tag/notification automation.

## CHDBits H&R Monitor — cmmchina

Source: https://github.com/cmmchina/chdbits-hnr-monitor/tree/627e67098fc6aa97912897ba95e13f53281a0b82/src/hnr_monitor

Revision: `627e67098fc6aa97912897ba95e13f53281a0b82`.
License: MIT. The full upstream copyright/license notice is retained in `plugins.v3/cloudbackupcleanup/vendor/chd_hnr/LICENSE`.

`vendor/chd_hnr/parser.py` and `models.py` are included unchanged from this revision. `config.py` contains the upstream `ParserConfig` dataclass, excluding unrelated monitoring/notification configuration. The adapter calls the existing table and record parser, supplies exact CHDBits/HDHome column mappings, and adds validation needed for deletion decisions. It preserves conflicting records instead of applying the monitor's first-record deduplication, rejects generated fallback IDs, and treats missing/ambiguous data as unknown. No upstream notification or independent background service is started.
