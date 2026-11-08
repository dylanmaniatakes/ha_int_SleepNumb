# ASCII Controller Transport — what changed and how to test it
_2026-11-08. Written for humans, not lawyers. No upstream PR yet — this push is for
installation and testing in your own Home Assistant first._

## The short version
Sleep Number's bankruptcy means the SleepIQ cloud is dying. This release gives the
integration a **local, cloud-free path to the 360-gen bed controller** — the same one the
SleepIQ phone app uses when you're standing next to the bed. It was reverse-engineered from
the app itself and then **verified live against Dylan's bed**: every frame the integration
now sends was captured from the real app session and matches byte-for-byte.

## What you get after installing
With a Bluetooth adapter or ESPHome Bluetooth Proxy in range of the bed (the same range
limit the phone app has — about 8 ft), and a BLE address configured on the integration
entry:

- **Sleep number works locally** — the slider sets firmness over Bluetooth (`PSNS`)
  and follows the pump until it settles. Works even with the cloud fully dead.
- **Presets work locally** — Flat / Snore etc. go straight to the controller (`ASTP`).
  Note: on this bed the base motor moves BOTH sides together; "sides" only matter for
  firmness and presence.
- **Under-bed light** — a real HA `light` entity: brightness slider maps to the
  controller's off/low/medium/high words. The fixture is one shared light (the wire has no
  side argument).
- **Foot warmer (heater)** — a per-side select: Off / Low / Medium / High, one per sleeper.
  The hub's own default timer (120 min) applies, same as the app.
- **Live pressure sensors, per side, enabled by default** — the controller streams
  mattress pressure readings (`SNCG`). Your right side read 45→65→50 live during testing.
- **Local presence ("in bed")** — the controller refuses a direct presence query
  (`LBPG` answers `FAIL:2`), and we now know the cloud's "detection" is just server-side
  inference from those same pressure readings. So the integration does that inference
  locally: `pressure above the learned empty-bed level = in bed`. It learns the empty
  baseline automatically (slow minimum per side) while the bed runs; overnight it
  self-calibrates. The cloud still provides presence as a fallback when BLE is out of range.
- **Clock sync** — the integration sets the hub's time once per session, same as the app.

Nothing about the older MCR pump path or the cloud path was removed — the transport order
is now: **ASCII controller → legacy MCR → cloud**, degrading cleanly feature by feature.

## How the new transport works
Each conversation with the bed is a short GATT session — connect to the controller
(advertising as "Smart bed 07C27D", service `09d23fae-…`), write one framed ASCII command
to the UART characteristic, read the reply back from the same characteristic, disconnect.
One session per job, never two simultaneous connections (a Bluetooth Proxy has exactly
one GATT slot). Dashboard state comes back in a single batched round trip (the `BAMG`
multi-query — 14 reads in one packet, matching what the app itself does).

Response grammar, decoded: `PASS:` + value for success (bare ack for sets, value/JSON for
reads, JSON arrays for batches), `FAIL:<code>` for refusals. Codes are the app's own
StatusCode enum (`INVALID_BAMKEY`=19, protocol failures 16–21, format failures 32+).

## Files changed
- `custom_components/sleepnumber_pro/bamkey_ascii.py` — **new**: framing
  (`fUzIoN` + length + ASCII + CRC32, length includes the CRC), parser, SNCG/BAMG
  response handling, snapshot mapping, presence helper.
- `custom_components/sleepnumber_pro/bluetooth.py` — added the ASCII session layer +
  one-time detection (probe = light status; a FAIL/timeout means "not this generation"
  and the old MCR path keeps working).
- `custom_components/sleepnumber_pro/coordinator.py` — the status coordinator prefers the
  ASCII plane and applies per-side pressure/presence onto sleepers; the settings
  coordinator polls the foot-warmer state (slow lane, 5 min).
- `custom_components/sleepnumber_pro/number.py` — sleep-number writes now go
  `PSNS` + settle-wait over the ASCII plane when available.
- `custom_components/sleepnumber_pro/select.py` — presets route to `ASTP` when available;
  "favorite" recalls the stored sleep number. Unknown preset spellings try candidate
  words before giving up.
- `custom_components/sleepnumber_pro/light.py` — **new**: the under-bed light entity.
- `custom_components/sleepnumber_pro/foot_warm.py` — **new**: per-side foot-warmer selects.
- `custom_components/sleepnumber_pro/__init__.py` — Light platform registered; the
  ASCII capability flag gates the new platforms; info log on first detection.
- `custom_components/sleepnumber_pro/sensor.py` — pressure sensors enabled by default
  (they now have a live local source).
- `custom_components/sleepnumber_pro/strings.json` — names for the new entities.
- `tests/test_ascii_snapshot.py` — **new**: 10 tests replaying the real wire responses
  (batch mapping, short batches, FAIL tolerance, presence thresholds, frame round-trips).

## Installing (HA OS: File editor / Samba; Core: copy the tree)
1. Back up or note your current `custom_components/sleepnumber_pro/` version.
2. Copy this whole `custom_components/sleepnumber_pro/` over the old one (it is additive;
   your existing config entries, entities and entities' registry IDs are unchanged).
3. Restart Home Assistant.
4. Confirm in **Settings → Integrations** that Sleep Number reloaded cleanly, then look at
   a bed device: you should see `Under-bed light`, per-sleeper `Foot warmer` selects, and
   live `Pressure` sensors. The log line
   `360-gen controller answered on the ASCII bamkey plane` confirms local mode.
5. **First night**: leave it running — the pressure baselines self-learn for presence.

## BLE prerequisite
The integration needs to *reach* the controller: a BT adapter on the HA host in range, or
an ESPHome Bluetooth Proxy near the bed. The config flow accepts a BLE address
(`CONF_BLE_ADDRESS`); the existing proxy guidance in `docs/BLUETOOTH.md` applies.
RSSI of about −80 is marginal; keep the proxy within ~15 ft of the bed.

## Known limits (v1)
- Baselines reset when HA restarts; ~1 h of running re-learns them (fine overnight).
- The foot-warm timer is fixed at the hub's default (120 min) — no timer entity yet.
- `watch_tv` / `zero_g` preset word spellings are unverified (candidates tried in order).
- The controller must be re-detected after integration reload (cheap; automatic).
- Presence from pressure is a threshold call — fast sit-downs may flicker until the
  baseline stabilizes; tune the learned-minimum behavior later if needed.

## Reverse-engineering provenance
Everything above was derived from the SleepIQ Android app (APK
`8b0ab928…6474`) via static decompile (jadx) + live HCI snoop captures over adb, and
cross-checked against the in-repo `mcr.py` implementation that previous fork authors
verified against BAM firmware 0.4.1d9. Two radios confirmed: the pump hub (advertising
`ffffd1fd`, binary MCR) and the 360-gen controller ("Smart bed 07C27D", `09d23fae`, ASCII
`fUzIoN` frames). The full 158-key command vocabulary, wire grammar and capture notes live
in the RE workspace (not part of this repo per project hygiene).
