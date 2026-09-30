# Downstream port audit

Upstream candidate: `95ee74db646895c0f42d10eff7c56ee8c3d531d1`.
Original downstream tip: `e6bc662b879959ce23e8ca428d299f863f158ef4`.
Inventory: complete diff `936dd7346f..e6bc662b87` (30 files; both custom commits).

| Original behavior | Current implementation / evidence |
| --- | --- |
| Empty Chat Completions and Anthropic lifecycle frames do not keep a stalled model alive | Ported to `_StreamingApiCall._count_chunk` and `_accept_chat_chunk`; preserves current Relay, single-writer fences, refusal and reasoning-details replay. `test_streaming.py` progress predicates and streaming integration. |
| Generated `delta.images` data URLs become MEDIA files | Ported to current streaming accumulator with capped payload, supported MIME types, deduplication and profile-aware output; integration test reads actual output bytes. |
| Namespaced Responses tool calls remain qualified | Ported to shared `_response_tool_call` (both function/custom calls), rather than restoring old normalization loop. Namespace regression tests. |
| Codex heartbeat watchdog and activity | Upstream `_codex_event_has_content`, phase-aware `last_progress_ts`, watchdog state and request fencing retained. Activity now touches only progress or completed items/terminal events. |
| Missing Electron previews avoid rejected IPC noise | Ported structured ENOENT/ENOTDIR response + preload compatibility. Current Windows/WSL path bridge and sensitive-path protections retained. Hardening regression test. |
| Landed patch diff overrides stale outer tool error | Ported to toolStatus, both row and preview-outcome consumers; explicit structured failures still win. Fallback-model regression test. |
| Already-applied patches are real no-ops | Upstream already counts declared change hunks and validates already-applied replacements; retained atomic overlay, Add/Move overwrite guards and context-only rejection. Ported no-write return, empty modified-file list, no-change metadata and file-state guard. Parser/already-applied suites. |
| AI limits provider discovery independent of provider name | Ported custom-provider enumeration; key_env uses current profile-aware get_secret_str, not process environment. Gateway RPC is profile-scoped and has generated Python/TypeScript/OpenRPC contracts. Discovery/RPC tests. |
| AI limits sidebar, usage and rolling charged throughput | Wired sidebar to ai_limits.get RPC, mapping GPT/Spark limits and usage rows; no hardcoded provider or renderer API-key exposure. Polling serialized; unavailable/stale/throughput UI tests. |
| provider_runtime config getter | Superseded deliberately by ai_limits.get; renderer no longer requires provider credentials, so restoring a secret-exporting config getter would add an unused exposure. |
| i18n sidebar label | Preserved all original locales and types; regenerated committed desktop key catalog. |
| simple-git warning suppression and build hook | Preserved idempotent patch script, root command and prebuild hook without reverting newer buildSourceDesktop pipeline. Patch executed twice successfully. |

No old upstream whole-file replacements were accepted. The resulting delta against upstream is additive and focused; fork ancestry is recorded only after this complete inventory and port. Live installation, AppData configuration and application restart are excluded.
