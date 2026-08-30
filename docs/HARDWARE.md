# Hardware Reference

Supplied by the project owner; every value was measured on the physical board rather
than copied from a datasheet. This is the authority for pin assignments and for the
quirks below — where this file and the firmware disagree, this file is right.

## Board

| | |
|---|---|
| Module | ESP32-WROOM-32 (DevKit v1) |
| PSRAM | **None.** ~320 KB DRAM; **135–165 KB free at runtime** |
| Arduino core | 3.3.11 verified |
| Serial | `/dev/cu.usbserial-0001` @ 115200 |
| Cores | 0 = audio tasks, 1 = `loop()` |

## Pin map (all confirmed working)

| Peripheral | Signal | GPIO | Notes |
|---|---|---|---|
| LED / light panel | PWM | **23** | LEDC ch 0, 5 kHz, 8-bit (0–255) |
| Button | input | **19** | `INPUT_PULLUP`, active LOW |
| BH1750 #A | SDA / SCL | **21 / 22** | ADDR→GND, address `0x23` |
| BH1750 #B | SDA / SCL | **21 / 22** | ADDR→3V3, address `0x5C` |
| INMP441 mic | SCK / WS / SD | **32 / 33 / 13** | I2S_NUM_0 |
| MAX98357A amp | BCLK / LRC / DIN | **26 / 25 / 27** | I2S_NUM_1 |

Free: 2, 4, 5, 12, 14, 15, 16, 17, 18, 34, 35, 36, 39.
Never use: 6–11 (SPI flash, prevents boot), 34–39 (input only), 0/2/12/15 (strapping).

## Three things that will break the board

**1. No `analogRead()` anywhere.** The legacy I2S driver registers ESP-IDF's old ADC
driver; `analogRead()` registers the new one. Mixing them aborts at boot on core 3.x
with `ADC: CONFLICT! driver_ng is not allowed to be used with the legacy driver`. Use
I2C or a digital pin instead.

**2. MCLK must be explicitly unset on both I2S peripherals.** A zero-initialised
`i2s_pin_config_t` leaves `mck_io_num` at 0, so both peripherals try to route their
master clock onto GPIO 0 and whichever installs second fails with
`i2s_set_pin: mclk config failed`. Cost a debugging session; it is why the speaker was
silent at first.

**3. `loop()` must never block.** Audio lives on core 0 tasks so a long tone, a stalled
stream or a dead dashboard cannot delay light control, sensors or the button. A board
that answers ping but refuses a WebSocket connection has a stuck `loop()` — ICMP is
handled by the WiFi stack's own task, so that asymmetry is a useful diagnostic.

## Audio facts (measured)

- **Mic:** 32-bit I2S slots → int16 via `>> 15`. A **DC-blocking high-pass is
  required**: the INMP441 idles near **−22000**, not 0, and that offset otherwise
  dominates RMS and pegs the level meter at 100% in a silent room. Measured idle RMS
  fell from ~20150 to ~1350 once the filter was added.
- **INMP441 L/R pin appears to be floating.** The slot carrying data has been observed
  changing between boots, so it is auto-detected. Tying L/R to GND would make it
  deterministic.
- **Speaker:** wire format is 16 kHz / 16-bit / mono, but the **DAC runs at 32 kHz**
  with 2× linear interpolation. At 16 kHz a 5 kHz tone gets 3.2 samples per cycle and
  the staircase is audible as buzz. **APLL enabled** for a jitter-free clock.
- **MAX98357A `SD` pin must float or be high.** Tied to GND it is in shutdown and
  silent no matter what the firmware does. Floating = mono (L+R)/2.

## Memory — why recording must stream

16 kHz × 16-bit = **32 KB/s**. With ~150 KB free heap, a 5 s clip (160 KB) fails to
allocate. The device must never hold a whole clip: at most a small FIFO, with the
receiver assembling the file.

## Sensors

BH1750 continuous high-res conversion takes ~120 ms; polling faster just re-reads the
same value. Lux → percent should be **logarithmic** (1 lx = 0%, 1000 lx = 100%) — lux
spans four orders of magnitude and a linear map pins every indoor reading near zero.
Use a deadband so the light does not hunt.

## Measured values

| Quantity | Value |
|---|---|
| Mic idle RMS (quiet room) | ~900–1400 |
| Mic recording peak | 15287 / 32767 |
| BH1750 A / B indoor | 8.3 lx / 0.8 lx, up to 25 / 455 lx |
| Free heap running | 135–164 KB |
| Stream throughput, strong link (−62 dBm) | 40 KB/s (needs 31.2) |
| Stream throughput, weak link (−79 dBm) | 17.5 KB/s — stutters |

**Link quality is the limiting factor** for streaming audio; light, sensors and
recording are unaffected.

## Other quirks

- **The always-on red LED is the power indicator**, hardwired to 3V3. No GPIO controls
  it. The controllable LED is on GPIO 23.
- **WiFi is 2.4 GHz only.** The ESP32 cannot see 5 GHz SSIDs.
- **WiFi credentials are compile-time constants**; changing networks needs a reflash.

## Relationship to this project's firmware

This reference was written against a different firmware — `CheckForESP_Firmware`, in
which the **ESP32 hosts a WebSocket server on port 81** and a browser connects directly
to it. This project's firmware is the opposite: the ESP32 is a **client** that dials
out to the laptop server, because the laptop is where Whisper, the ML model and TTS
run, and the ESP32 is specified as a pure I/O device with no local decision-making
(architectural decisions #1 and #3).

Every hardware fact above still applies. The transport direction and the wire protocol
do not — see `laptop-server/protocol.py` for this project's schema.
