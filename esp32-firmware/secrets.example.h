/* =====================================================================
 *  secrets.example.h — TEMPLATE. Safe to commit.
 *  ---------------------------------------------------------------
 *  Copy this file to `secrets.h` and fill in your own values:
 *
 *      cp secrets.example.h secrets.h
 *
 *  `secrets.h` is gitignored and must NEVER be committed. The sketch
 *  picks it up automatically if it exists; if it does not, the
 *  placeholders in Section 1.1 are used and the build still succeeds.
 * ===================================================================== */

#pragma once

/* 2.4 GHz only — the ESP32 cannot see 5 GHz networks at all.
 * On an iPhone Personal Hotspot you must enable
 * Settings -> Personal Hotspot -> "Maximize Compatibility". */
#define WIFI_SSID      "YOUR_WIFI_SSID"
#define WIFI_PASSWORD  "YOUR_WIFI_PASSWORD"

/* The laptop running laptop-server/main.py.
 * Check it with:  ipconfig getifaddr en0
 * Must be on the same subnet as the board. */
#define SERVER_HOST    "192.168.1.100"
