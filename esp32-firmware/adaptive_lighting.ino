/* =====================================================================
 *  ADAPTIVE LIGHTING — ESP32 firmware (single file)
 *  ---------------------------------------------------------------
 *  Board : ESP32 DevKit v1 (ESP32-WROOM-32, no PSRAM)
 *  Core  : Arduino-ESP32 3.x
 *
 *  This device is a pure I/O peripheral. It does no inference and makes no
 *  lighting decisions of its own: it reads its two light sensors, streams
 *  them to the laptop, records audio on request, and does what it is told.
 *  Whisper, the brightness model and TTS all live on the laptop.
 *
 *  TO RUN THIS YOU ONLY NEED TO EDIT SECTION 1.
 *
 *  LIBRARIES (Library Manager):
 *      "WebSockets"  by Markus Sattler   >= 2.4.1
 *      "ArduinoJson" by Benoit Blanchon  >= 7.0.0
 *      "BH1750"      by Christopher Laws
 *
 *  ---------------------------------------------------------------
 *  THREE THINGS THAT WILL BREAK THIS BOARD  (learned the hard way)
 *  ---------------------------------------------------------------
 *   1. NO analogRead() ANYWHERE. The I2S driver and Arduino's ADC register
 *      conflicting ESP-IDF drivers and the chip aborts during init with
 *      "ADC: CONFLICT! driver_ng is not allowed to be used with the legacy
 *      driver", boot-looping before setup() prints a thing. The light
 *      sensors are I2C precisely to avoid this. Use I2C or a digital pin.
 *   2. MCLK MUST BE EXPLICITLY UNSET on both I2S peripherals. Left at its
 *      default both peripherals try to route their master clock onto GPIO 0
 *      and whichever installs second fails. This is why the speaker was
 *      silent at first.
 *   3. loop() MUST NEVER BLOCK. Audio runs in tasks on core 0 so a long
 *      tone, a stalled stream or a dead laptop cannot delay light control,
 *      sensors or the button. A board that answers ping but refuses a
 *      WebSocket has a stuck loop() — ICMP is handled by the WiFi stack's
 *      own task, so that asymmetry is a useful diagnostic.
 *
 *  GPIO 6-11 are the SPI flash (never use). 34-39 are input only.
 * ===================================================================== */

#include <Arduino.h>
#include <WiFi.h>
#include <Wire.h>
#include <BH1750.h>
#include <WebSocketsClient.h>
#include <ArduinoJson.h>
#include <driver/i2s_std.h>
#include <freertos/FreeRTOS.h>
#include <freertos/task.h>
#include <freertos/stream_buffer.h>
#include <math.h>

#if ESP_ARDUINO_VERSION_MAJOR < 3
  #error "Requires ESP32 Arduino core 3.x. Update via Boards Manager."
#endif

/* =====================================================================
 * ======  SECTION 1 : CONFIGURATION  (edit this only)  ================
 * ===================================================================== */

/* ---- 1.1 Network ----
 * Station mode only: this device joins your 2.4 GHz network and dials out to
 * the laptop server. It does NOT create an access point, so it will never
 * appear in a WiFi list — read its IP off the serial monitor.
 * The ESP32 cannot see 5 GHz SSIDs at all.
 *
 * FILL THESE IN BEFORE FLASHING. They are placeholders in the repo so that no
 * real network credentials are ever committed. */
#define WIFI_SSID               "YOUR_WIFI_SSID"       // 2.4 GHz only
#define WIFI_PASSWORD           "YOUR_WIFI_PASSWORD"
#define WIFI_HOSTNAME           "esp32-adaptive-light"
#define WIFI_RETRY_INTERVAL_MS  5000UL

/* The laptop's LAN address. This is DHCP-assigned and WILL change when the
 * laptop moves network — check it with `ipconfig getifaddr en0` and re-flash.
 * The ESP32 must be on the SAME subnet: if this reads 192.168.1.x and the
 * board's own IP comes up 192.168.0.x, they cannot see each other.
 * The value below is a PLACEHOLDER — replace it with your own laptop IP. */
#define SERVER_HOST             "192.168.1.100"  // the laptop running main.py
#define SERVER_PORT             8000
#define SERVER_PATH             "/ws/device"

#define DEVICE_ID               "esp32-room-1"
#define FIRMWARE_VERSION        "2.0.0"

/* ---- 1.2 Pins ---- (all confirmed working on this board) */
#define LIGHT_PANEL_PIN         23      // PWM to the LED / panel driver
#define BUTTON_PIN              19      // to GND, internal pull-up, active LOW

#define SENSOR_I2C_SDA          21
#define SENSOR_I2C_SCL          22
#define SENSOR_A_ADDR           0x23    // ADDR -> GND   (indoor)
#define SENSOR_B_ADDR           0x5C    // ADDR -> 3V3   (outdoor)
#define SENSOR_I2C_HZ           100000

#define MIC_I2S_PORT            I2S_NUM_0
#define MIC_SCK_PIN             32
#define MIC_WS_PIN              33
#define MIC_SD_PIN              13

#define SPK_I2S_PORT            I2S_NUM_1
#define SPK_BCLK_PIN            26
#define SPK_LRC_PIN             25
#define SPK_DIN_PIN             27

/* ---- 1.3 Light panel ---- */
#define LIGHT_PWM_FREQ_HZ       5000
#define LIGHT_PWM_RESOLUTION    8       // duty 0..255
#define LIGHT_INVERT_OUTPUT     0       // 1 if your driver is active-LOW
#define LIGHT_RAMP_STEP         3       // duty units per tick
#define LIGHT_RAMP_INTERVAL_MS  8       // -> full sweep ~680 ms

/* ---- 1.4 Sensors ----
 * BH1750 continuous high-res conversion takes ~120 ms; polling faster just
 * re-reads the same value. */
#define SENSOR_SAMPLE_INTERVAL_MS 130
#define SENSOR_EMA_ALPHA          0.25f
#define TELEMETRY_INTERVAL_MS     1000  // how often readings go to the laptop

/* ---- 1.5 Microphone (INMP441) ----
 * MIC_CHANNEL_LEFT: 1 = L/R tied to GND (data on the LEFT slot),
 *                   0 = L/R tied high or floating (data on the RIGHT slot).
 * Measured on this board: RIGHT. The L/R pin appears to float, and the active
 * slot has been seen changing between boots, so this is only a starting guess
 * — micAutoDetectSlot() reinstalls on the other slot if this one is silent. */
#define MIC_CHANNEL_LEFT        0
#define MIC_SAMPLE_RATE         16000
#define MIC_READ_SAMPLES        256
#define MIC_DMA_BUF_COUNT       6
#define MIC_DMA_BUF_LEN         256
/* The INMP441 sends 24-bit data left-justified in a 32-bit slot. Strictly
 * correct is (raw >> 16); shifting less adds gain. RAISE if audio clips,
 * LOWER if it is too quiet. Measured peak here: 15287 of 32767. */
#define MIC_SAMPLE_SHIFT        15
/* The mic idles around -22000, not 0. Left in, that DC offset dominates the
 * signal and wastes most of the range Whisper sees. One-pole high-pass;
 * 0.995 @ 16 kHz is a ~13 Hz corner. Measured idle RMS fell 20150 -> 1350. */
#define MIC_DC_BLOCK_R          0.995f
#define MIC_LEVEL_FULLSCALE     5000.0f
#define MIC_LEVEL_NOISE_FLOOR   300.0f
#define MIC_LEVEL_EMA_ALPHA     0.25f

/* ---- 1.6 Recording ----
 * 16 kHz x 16-bit = 32 KB/s. With ~150 KB free heap and no PSRAM, buffering a
 * whole clip fails after 2-3 seconds. Audio is streamed as it is captured; the
 * laptop assembles it. On-device we never hold more than the FIFO. */
#define RECORDING_DURATION_MS   5000
#define RECORDING_MAX_MS        15000
#define MIC_STREAM_BUFFER_BYTES 8192
#define MIC_WS_CHUNK_BYTES      1024

/* ---- 1.7 Speaker (MAX98357A) ----
 * Two rates, deliberately. SPK_STREAM_RATE is the wire format the laptop
 * sends. SPK_I2S_RATE is what the DAC clocks at: at 16 kHz a 5 kHz tone gets
 * only 3.2 samples per cycle, and that staircase is audible as buzz. Streamed
 * PCM is linearly interpolated up on the fly. */
#define SPK_STREAM_RATE         16000
#define SPK_I2S_RATE            32000
#define SPK_UPSAMPLE            (SPK_I2S_RATE / SPK_STREAM_RATE)
#define SPK_DMA_BUF_COUNT       8
#define SPK_DMA_BUF_LEN         256
#define SPK_VOLUME              0.90f
/* Playback needs a cushion, not just a buffer: draining the moment the first
 * bytes land means every network hiccup empties it and the DMA clocks out
 * zeros. Bank PREROLL before making a sound; on underrun go quiet and refill
 * rather than machine-gunning fragments. One short pause beats distortion. */
#define PLAYBACK_BUFFER_BYTES   32768   // ~1.0 s
#define PLAYBACK_PREROLL_BYTES  10240   // ~320 ms banked before starting
#define PLAYBACK_REFILL_BYTES   6144    // ~190 ms to recover after underrun
#define FRAMES_PER_WRITE        128

/* ---- 1.8 Button ---- */
#define BUTTON_DEBOUNCE_MS      35
#define BUTTON_DOUBLECLICK_MS   350
#define BUTTON_LONGPRESS_MS     900

/* ---- 1.9 System ---- */
#define SERIAL_BAUD             115200
#define JSON_RX_MAX_BYTES       2048

/* =====================================================================
 * ======  SECTION 2 : TYPES + PROTOTYPES  =============================
 * =====================================================================
 * Explicit prototypes: the .ino preprocessor injects auto-generated ones
 * ABOVE the type definitions, which breaks any signature using them. */

enum ButtonEvent { BTN_NONE = 0, BTN_SINGLE_CLICK, BTN_DOUBLE_CLICK, BTN_LONG_PRESS };

void        lightInit();
void        lightUpdate();
void        setLightBrightness(int duty);
int         lightGetActual();
void        sensorInit();
void        sensorUpdate();
float       sensorLuxA();
float       sensorLuxB();
bool        sensorIsHealthy();
void        buttonInit();
ButtonEvent buttonUpdate();
bool        micInit();
void        micLaunchTask();
bool        micIsHealthy();
void        micStartStream();
void        micStopStream();
size_t      micReadStream(uint8_t* dst, size_t maxBytes);
bool        spkInit();
void        spkLaunchTask();
bool        playbackBegin(uint32_t sampleRate);
size_t      playbackWrite(const uint8_t* data, size_t len);
void        playbackEnd();
void        playbackAbort();
bool        spkIsHealthy();
bool        startRecording(uint32_t durationMs);
void        stopRecording();
void        recorderUpdate();
bool        isRecording();
bool        netSendAudioChunk(const uint8_t* pcm, size_t len);
static void runBrightnessSweep();

/* =====================================================================
 * ======  SECTION 3 : LIGHT PANEL  ====================================
 * ===================================================================== */

static int      g_lightTarget   = 0;   // where we want to be
static int      g_lightActual   = 0;   // what the pin outputs right now
static uint32_t g_lightLastRamp = 0;

static void lightWriteDuty(int duty) {
  duty = constrain(duty, 0, 255);
#if LIGHT_INVERT_OUTPUT
  ledcWrite(LIGHT_PANEL_PIN, 255 - duty);
#else
  ledcWrite(LIGHT_PANEL_PIN, duty);
#endif
}

void lightInit() {
  ledcAttach(LIGHT_PANEL_PIN, LIGHT_PWM_FREQ_HZ, LIGHT_PWM_RESOLUTION);
  g_lightTarget = g_lightActual = 0;
  lightWriteDuty(0);
}

/* Non-blocking ramp: brightness never jumps, it walks toward the target. */
void lightUpdate() {
  if (g_lightActual == g_lightTarget) return;
  uint32_t now = millis();
  if (now - g_lightLastRamp < LIGHT_RAMP_INTERVAL_MS) return;
  g_lightLastRamp = now;

  int delta = g_lightTarget - g_lightActual;
  int step  = (abs(delta) < LIGHT_RAMP_STEP) ? abs(delta) : LIGHT_RAMP_STEP;
  g_lightActual += (delta > 0) ? step : -step;
  lightWriteDuty(g_lightActual);
}

void setLightBrightness(int duty) { g_lightTarget = constrain(duty, 0, 255); }
int  lightGetActual()             { return g_lightActual; }

/* =====================================================================
 * ======  SECTION 4 : LIGHT SENSORS (2x BH1750 over I2C)  =============
 * ===================================================================== */

static BH1750   g_bhA(SENSOR_A_ADDR);
static BH1750   g_bhB(SENSOR_B_ADDR);
static bool     g_okA = false, g_okB = false;
static float    g_luxA = 0.0f, g_luxB = 0.0f;
static bool     g_liveA = false, g_liveB = false;
static uint32_t g_sensorLast = 0;

static float readSensorLux(BH1750& s, bool present) {
  if (!present) return -1.0f;
  float lux = s.readLightLevel();
  return (lux < 0.0f) ? -1.0f : lux;
}

void sensorInit() {
  Wire.begin(SENSOR_I2C_SDA, SENSOR_I2C_SCL, SENSOR_I2C_HZ);
  Wire.setTimeOut(50);      // a jammed bus must never hang the light loop

  /* Scan first: knowing WHAT answered beats guessing why it did not. A module
   * on the wrong pins, unpowered, or with a floating ADDR all look identical
   * from a failed begin() — but not from a scan. */
  int found = 0;
  Serial.print("[i2c] scanning: ");
  for (uint8_t addr = 1; addr < 127; addr++) {
    Wire.beginTransmission(addr);
    if (Wire.endTransmission() == 0) { Serial.printf("0x%02X ", addr); found++; }
  }
  if (!found) Serial.print("(nothing responded)");
  Serial.printf(" [%d device(s)]\n", found);

  g_okA = g_bhA.begin(BH1750::CONTINUOUS_HIGH_RES_MODE, SENSOR_A_ADDR, &Wire);
  g_okB = g_bhB.begin(BH1750::CONTINUOUS_HIGH_RES_MODE, SENSOR_B_ADDR, &Wire);
  Serial.printf("[sensor] A(indoor) @0x%02X: %s   B(outdoor) @0x%02X: %s\n",
                SENSOR_A_ADDR, g_okA ? "found" : "NOT FOUND",
                SENSOR_B_ADDR, g_okB ? "found" : "NOT FOUND");

  delay(180);               // let the first conversion complete
  float a = readSensorLux(g_bhA, g_okA);
  float b = readSensorLux(g_bhB, g_okB);
  if (a >= 0) { g_luxA = a; g_liveA = true; }
  if (b >= 0) { g_luxB = b; g_liveB = true; }
}

void sensorUpdate() {
  uint32_t now = millis();
  if (now - g_sensorLast < SENSOR_SAMPLE_INTERVAL_MS) return;
  g_sensorLast = now;

  float a = readSensorLux(g_bhA, g_okA);
  if (a >= 0) { g_luxA += SENSOR_EMA_ALPHA * (a - g_luxA); g_liveA = true; }
  else        { g_liveA = false; }

  float b = readSensorLux(g_bhB, g_okB);
  if (b >= 0) { g_luxB += SENSOR_EMA_ALPHA * (b - g_luxB); g_liveB = true; }
  else        { g_liveB = false; }
}

float sensorLuxA()      { return g_liveA ? g_luxA : -1.0f; }
float sensorLuxB()      { return g_liveB ? g_luxB : -1.0f; }
bool  sensorIsHealthy() { return g_liveA && g_liveB; }

/* =====================================================================
 * ======  SECTION 5 : BUTTON  =========================================
 * =====================================================================
 *  single click -> push to talk (5 s recording)
 *  double click -> ask the laptop to switch AUTO / MANUAL
 *  long press   -> report diagnostics over serial
 *  Fully non-blocking: no delay() anywhere on this path. */

enum BtnState { ST_IDLE, ST_DOWN, ST_WAIT_SECOND, ST_SECOND_DOWN, ST_WAIT_RELEASE };

static BtnState g_btnState    = ST_IDLE;
static bool     g_btnStable   = false;
static bool     g_btnLastRaw  = false;
static uint32_t g_btnChanged  = 0;
static uint32_t g_btnPressed  = 0;
static uint32_t g_btnReleased = 0;

static bool btnRawPressed() { return digitalRead(BUTTON_PIN) == LOW; }

void buttonInit() {
  pinMode(BUTTON_PIN, INPUT_PULLUP);
  g_btnState  = ST_IDLE;
  g_btnStable = g_btnLastRaw = btnRawPressed();
}

ButtonEvent buttonUpdate() {
  uint32_t now = millis();

  bool raw = btnRawPressed();
  if (raw != g_btnLastRaw) { g_btnLastRaw = raw; g_btnChanged = now; }
  if ((now - g_btnChanged) >= BUTTON_DEBOUNCE_MS && g_btnStable != g_btnLastRaw) {
    g_btnStable = g_btnLastRaw;
    if (g_btnStable) g_btnPressed = now; else g_btnReleased = now;
  }

  switch (g_btnState) {
    case ST_IDLE:
      if (g_btnStable) g_btnState = ST_DOWN;
      break;
    case ST_DOWN:
      if (g_btnStable && (now - g_btnPressed) >= BUTTON_LONGPRESS_MS) {
        g_btnState = ST_WAIT_RELEASE; return BTN_LONG_PRESS;
      }
      if (!g_btnStable) g_btnState = ST_WAIT_SECOND;
      break;
    case ST_WAIT_SECOND:
      if (g_btnStable) { g_btnState = ST_SECOND_DOWN; break; }
      if ((now - g_btnReleased) >= BUTTON_DOUBLECLICK_MS) {
        g_btnState = ST_IDLE; return BTN_SINGLE_CLICK;
      }
      break;
    case ST_SECOND_DOWN:
      if (!g_btnStable) { g_btnState = ST_IDLE; return BTN_DOUBLE_CLICK; }
      if ((now - g_btnPressed) >= BUTTON_LONGPRESS_MS) {
        g_btnState = ST_WAIT_RELEASE; return BTN_LONG_PRESS;
      }
      break;
    case ST_WAIT_RELEASE:
      if (!g_btnStable) g_btnState = ST_IDLE;
      break;
  }
  return BTN_NONE;
}

/* =====================================================================
 * ======  SECTION 6 : MICROPHONE (I2S capture task, core 0)  ==========
 * =====================================================================
 *  INMP441 -> I2S0 -> micTask (core 0) -> FIFO -> loop() -> WebSocket
 *  The capture task never blocks on the network: if the consumer falls
 *  behind, audio is dropped rather than stalling capture, because stalling
 *  I2S would corrupt timing everywhere downstream. */

static i2s_chan_handle_t    g_micChan      = NULL;
static bool                 g_micReady     = false;
static volatile bool        g_micStreaming = false;
static volatile float       g_micLevelEma  = 0.0f;
static volatile uint32_t    g_micDropped   = 0;
static StreamBufferHandle_t g_micFifo      = NULL;
static TaskHandle_t         g_micTask      = NULL;
static float                g_dcPrevX = 0.0f, g_dcPrevY = 0.0f;
static int32_t              g_micRaw[MIC_READ_SAMPLES];
static int16_t              g_micPcm[MIC_READ_SAMPLES];
static bool                 g_micUsesLeft  = MIC_CHANNEL_LEFT;

/* Split out from micInit() so the slot auto-detect can reinstall on the other
 * slot without duplicating the configuration. */
static bool micInstall(bool useLeft) {
  i2s_chan_config_t chanCfg = I2S_CHANNEL_DEFAULT_CONFIG(MIC_I2S_PORT, I2S_ROLE_MASTER);
  chanCfg.dma_desc_num  = MIC_DMA_BUF_COUNT;
  chanCfg.dma_frame_num = MIC_DMA_BUF_LEN;
  if (i2s_new_channel(&chanCfg, NULL, &g_micChan) != ESP_OK) return false;

  i2s_std_config_t cfg = {
    .clk_cfg  = I2S_STD_CLK_DEFAULT_CONFIG(MIC_SAMPLE_RATE),
    .slot_cfg = I2S_STD_PHILIPS_SLOT_DEFAULT_CONFIG(I2S_DATA_BIT_WIDTH_32BIT,
                                                    I2S_SLOT_MODE_MONO),
    .gpio_cfg = {
      // MCLK must be explicitly unused, or both I2S peripherals try to route
      // their master clock onto GPIO 0 and the second install fails.
      .mclk = I2S_GPIO_UNUSED,
      .bclk = (gpio_num_t)MIC_SCK_PIN,
      .ws   = (gpio_num_t)MIC_WS_PIN,
      .dout = I2S_GPIO_UNUSED,
      .din  = (gpio_num_t)MIC_SD_PIN,
      .invert_flags = { .mclk_inv = false, .bclk_inv = false, .ws_inv = false },
    },
  };
  cfg.slot_cfg.slot_mask = useLeft ? I2S_STD_SLOT_LEFT : I2S_STD_SLOT_RIGHT;

  if (i2s_channel_init_std_mode(g_micChan, &cfg) != ESP_OK) {
    i2s_del_channel(g_micChan); g_micChan = NULL; return false;
  }
  if (i2s_channel_enable(g_micChan) != ESP_OK) {
    i2s_del_channel(g_micChan); g_micChan = NULL; return false;
  }

  g_micUsesLeft = useLeft;
  g_dcPrevX = g_dcPrevY = 0.0f;
  return true;
}

static void micUninstall() {
  if (!g_micChan) return;
  i2s_channel_disable(g_micChan);
  i2s_del_channel(g_micChan);
  g_micChan = NULL;
}

/* One I2S read -> int16 PCM + RMS of that block. */
static size_t micCaptureBlock(float* rmsOut) {
  size_t bytesRead = 0;
  if (i2s_channel_read(g_micChan, g_micRaw, sizeof(g_micRaw), &bytesRead,
                       pdMS_TO_TICKS(100)) != ESP_OK) return 0;

  size_t n = bytesRead / sizeof(int32_t);
  double acc = 0;
  for (size_t i = 0; i < n; i++) {
    float x = (float)(g_micRaw[i] >> MIC_SAMPLE_SHIFT);
    // y[n] = x[n] - x[n-1] + R*y[n-1]   (DC blocker)
    float y = x - g_dcPrevX + MIC_DC_BLOCK_R * g_dcPrevY;
    g_dcPrevX = x; g_dcPrevY = y;

    int32_t v = (int32_t)y;
    if (v >  32767) v =  32767;
    if (v < -32768) v = -32768;
    g_micPcm[i] = (int16_t)v;
    acc += (double)v * (double)v;
  }
  *rmsOut = (n > 0) ? sqrt(acc / n) : 0.0f;
  return n;
}

/* Peak-to-peak spread on the current slot. A dead or wrong-slot mic returns
 * all zeros or a frozen value. */
static long micMeasure(double* rmsOut) {
  // Discard the first blocks: the DMA buffer was just zeroed and the INMP441
  // needs a few frames of bit clock before it drives real data.
  for (int b = 0; b < 3; b++) { float junk; micCaptureBlock(&junk); }

  int32_t minV = INT32_MAX, maxV = INT32_MIN;
  double rmsAcc = 0; int blocks = 0;
  for (int b = 0; b < 8; b++) {
    float rms = 0;
    size_t n = micCaptureBlock(&rms);
    if (n == 0) continue;
    for (size_t i = 0; i < n; i++) {
      if (g_micPcm[i] < minV) minV = g_micPcm[i];
      if (g_micPcm[i] > maxV) maxV = g_micPcm[i];
    }
    rmsAcc += rms; blocks++;
  }
  *rmsOut = blocks ? (rmsAcc / blocks) : 0.0;
  return blocks ? (long)(maxV - minV) : -1;
}

/* The INMP441 puts data on the LEFT slot only when its L/R pin is tied to
 * GND. Wired high — or left floating, as it appears to be on this board — it
 * transmits on the RIGHT slot instead and we read silence. Rather than fail on
 * a wiring detail, try the other slot and keep whichever works. */
static void micAutoDetectSlot() {
  double rms = 0;
  long spread = micMeasure(&rms);
  if (spread > 1) {
    Serial.printf("[mic] signal on %s slot (rms=%.0f spread=%ld)\n",
                  g_micUsesLeft ? "LEFT" : "RIGHT", rms, spread);
    return;
  }

  bool other = !g_micUsesLeft;
  Serial.printf("[mic] %s slot silent, trying %s\n",
                g_micUsesLeft ? "LEFT" : "RIGHT", other ? "LEFT" : "RIGHT");
  micUninstall();
  if (!micInstall(other)) {
    Serial.println("[mic] re-init on the other slot FAILED");
    g_micReady = false;
    return;
  }

  double rms2 = 0;
  long spread2 = micMeasure(&rms2);
  if (spread2 > 1) {
    Serial.printf("[mic] auto-switched to %s slot (rms=%.0f spread=%ld)\n",
                  other ? "LEFT" : "RIGHT", rms2, spread2);
    return;
  }
  Serial.println("[mic] silent on BOTH slots — check SD/VDD wiring");
}

bool micInit() {
  if (!micInstall(MIC_CHANNEL_LEFT)) return false;
  g_micFifo = xStreamBufferCreate(MIC_STREAM_BUFFER_BYTES, 1);
  if (!g_micFifo) { micUninstall(); return false; }
  g_micReady = true;
  micAutoDetectSlot();
  return g_micReady;
}

static void micTaskFn(void*) {
  for (;;) {
    float rms = 0;
    size_t n = micCaptureBlock(&rms);
    if (n == 0) { vTaskDelay(pdMS_TO_TICKS(5)); continue; }

    // Subtract the floor rather than gating on it, so a quiet room reads near
    // 0% instead of parking at whatever the noise floor maps to.
    float pct = 0.0f;
    if (rms > MIC_LEVEL_NOISE_FLOOR)
      pct = ((rms - MIC_LEVEL_NOISE_FLOOR) /
             (MIC_LEVEL_FULLSCALE - MIC_LEVEL_NOISE_FLOOR)) * 100.0f;
    if (pct > 100.0f) pct = 100.0f;
    g_micLevelEma += MIC_LEVEL_EMA_ALPHA * (pct - g_micLevelEma);

    if (g_micStreaming) {
      size_t want = n * sizeof(int16_t);
      size_t sent = xStreamBufferSend(g_micFifo, g_micPcm, want, 0);  // never blocks
      if (sent < want) g_micDropped += (want - sent);
    }
  }
}

void micLaunchTask() {
  if (g_micReady && !g_micTask)
    xTaskCreatePinnedToCore(micTaskFn, "micTask", 4096, NULL, 3, &g_micTask, 0);
}

bool   micIsHealthy()  { return g_micReady; }
void   micStartStream(){ if (g_micReady) { xStreamBufferReset(g_micFifo); g_micDropped = 0; g_micStreaming = true; } }
void   micStopStream() { g_micStreaming = false; }
size_t micReadStream(uint8_t* dst, size_t maxBytes) {
  return g_micReady ? xStreamBufferReceive(g_micFifo, dst, maxBytes, 0) : 0;
}

/* =====================================================================
 * ======  SECTION 7 : SPEAKER (I2S playback task, core 0)  ============
 * ===================================================================== */

static i2s_chan_handle_t    g_spkChan   = NULL;
static bool                 g_spkReady  = false;
static StreamBufferHandle_t g_playPcm   = NULL;
static TaskHandle_t         g_audioTask = NULL;
static volatile bool        g_playActive = false;
static volatile bool        g_playEnded  = false;
static volatile bool        g_playPrimed = false;
static volatile uint32_t    g_playUnderruns = 0;
static float                g_lastPlaySample = 0.0f;
static int16_t              g_spkOut[FRAMES_PER_WRITE * 2];   // interleaved L,R

bool spkInit() {
  i2s_chan_config_t chanCfg = I2S_CHANNEL_DEFAULT_CONFIG(SPK_I2S_PORT, I2S_ROLE_MASTER);
  chanCfg.dma_desc_num  = SPK_DMA_BUF_COUNT;
  chanCfg.dma_frame_num = SPK_DMA_BUF_LEN;
  if (i2s_new_channel(&chanCfg, &g_spkChan, NULL) != ESP_OK) return false;

  i2s_std_config_t cfg = {
    .clk_cfg  = I2S_STD_CLK_DEFAULT_CONFIG(SPK_I2S_RATE),
    .slot_cfg = I2S_STD_PHILIPS_SLOT_DEFAULT_CONFIG(I2S_DATA_BIT_WIDTH_16BIT,
                                                    I2S_SLOT_MODE_STEREO),
    .gpio_cfg = {
      .mclk = I2S_GPIO_UNUSED,          // see the note in micInstall()
      .bclk = (gpio_num_t)SPK_BCLK_PIN,
      .ws   = (gpio_num_t)SPK_LRC_PIN,
      .dout = (gpio_num_t)SPK_DIN_PIN,
      .din  = I2S_GPIO_UNUSED,
      .invert_flags = { .mclk_inv = false, .bclk_inv = false, .ws_inv = false },
    },
  };
  // APLL gives a properly synthesised audio clock. The default PLL divider
  // cannot hit audio rates exactly, and that jitter shows up as roughness on
  // high frequencies — precisely the ones with least headroom already.
  cfg.clk_cfg.clk_src = I2S_CLK_SRC_APLL;

  if (i2s_channel_init_std_mode(g_spkChan, &cfg) != ESP_OK) {
    i2s_del_channel(g_spkChan); g_spkChan = NULL; return false;
  }
  if (i2s_channel_enable(g_spkChan) != ESP_OK) {
    i2s_del_channel(g_spkChan); g_spkChan = NULL; return false;
  }

  g_playPcm = xStreamBufferCreate(PLAYBACK_BUFFER_BYTES, 1);
  if (!g_playPcm) { i2s_del_channel(g_spkChan); g_spkChan = NULL; return false; }

  g_spkReady = true;
  return true;
}

static void spkWriteFrames(size_t frames) {
  size_t written = 0;
  i2s_channel_write(g_spkChan, g_spkOut, frames * 2 * sizeof(int16_t),
                    &written, pdMS_TO_TICKS(200));
}

static void audioTaskFn(void*) {
  static uint8_t chunk[FRAMES_PER_WRITE * sizeof(int16_t)];

  for (;;) {
    if (!g_playActive) { vTaskDelay(pdMS_TO_TICKS(10)); continue; }

    size_t avail = xStreamBufferBytesAvailable(g_playPcm);

    /* --- waiting for the cushion to fill --- */
    if (!g_playPrimed) {
      size_t need = g_playUnderruns ? PLAYBACK_REFILL_BYTES : PLAYBACK_PREROLL_BYTES;
      if (avail >= need) {
        g_playPrimed = true;
      } else if (g_playEnded) {
        if (avail > 0) {
          g_playPrimed = true;               // short clip: play what we have
        } else {                             // genuinely finished
          g_playActive = false; g_playEnded = false;
        }
      } else {
        vTaskDelay(pdMS_TO_TICKS(5));        // still filling
      }
      continue;
    }

    /* --- primed: drain --- */
    // Read at most enough input that the upsampled result still fits g_spkOut.
    const size_t maxIn = (FRAMES_PER_WRITE / SPK_UPSAMPLE) * sizeof(int16_t);
    size_t got = xStreamBufferReceive(g_playPcm, chunk, maxIn, 0);
    if (got >= sizeof(int16_t)) {
      size_t nIn = got / sizeof(int16_t);
      const int16_t* in = (const int16_t*)chunk;
      size_t out = 0;
      for (size_t i = 0; i < nIn; i++) {
        float cur = (float)in[i];
        // Interpolate between the previous sample and this one, so inserted
        // samples follow the waveform instead of repeating it — repeating
        // would just re-create the staircase we are removing.
        for (int k = 0; k < SPK_UPSAMPLE; k++) {
          float t = (float)(k + 1) / SPK_UPSAMPLE;
          int32_t v = (int32_t)((g_lastPlaySample + (cur - g_lastPlaySample) * t)
                                * SPK_VOLUME);
          if (v >  32767) v =  32767;
          if (v < -32768) v = -32768;
          g_spkOut[out * 2]     = (int16_t)v;
          g_spkOut[out * 2 + 1] = (int16_t)v;
          out++;
        }
        g_lastPlaySample = cur;
      }
      spkWriteFrames(out);                   // blocks until the DMA has room
    } else if (g_playEnded) {
      g_playActive = false; g_playEnded = false; g_playPrimed = false;
    } else {
      g_playPrimed = false;                  // underrun -> re-bank a cushion
      g_playUnderruns++;
    }
  }
}

void spkLaunchTask() {
  if (g_spkReady && !g_audioTask)
    xTaskCreatePinnedToCore(audioTaskFn, "audioTask", 4096, NULL, 2, &g_audioTask, 0);
}

bool playbackBegin(uint32_t sampleRate) {
  if (!g_spkReady) return false;
  // We deliberately do NOT resample on-device: rejecting a wrong rate beats
  // playing the clip back at the wrong pitch.
  if (sampleRate != SPK_STREAM_RATE) return false;
  xStreamBufferReset(g_playPcm);
  g_playEnded = false; g_playPrimed = false;
  g_playUnderruns = 0; g_lastPlaySample = 0.0f;
  g_playActive = true;
  return true;
}

size_t playbackWrite(const uint8_t* data, size_t len) {
  if (!g_spkReady || !g_playActive) return 0;
  return xStreamBufferSend(g_playPcm, data, len, 0);   // non-blocking
}

void playbackEnd()   { g_playEnded = true; }
void playbackAbort() {
  if (!g_spkReady) return;
  g_playActive = false; g_playEnded = false; g_playPrimed = false;
  xStreamBufferReset(g_playPcm);
}
bool spkIsHealthy()  { return g_spkReady; }

/* =====================================================================
 * ======  SECTION 8 : RECORDER (streaming, never buffering)  ==========
 * ===================================================================== */

static bool     g_recording   = false;
static uint32_t g_recStart    = 0;
static uint32_t g_recDuration = RECORDING_DURATION_MS;
static uint32_t g_recBytes    = 0;
static uint8_t  g_recBuf[MIC_WS_CHUNK_BYTES];

bool startRecording(uint32_t durationMs) {
  if (!micIsHealthy() || g_recording) return false;
  if (durationMs == 0) durationMs = RECORDING_DURATION_MS;
  if (durationMs > RECORDING_MAX_MS) durationMs = RECORDING_MAX_MS;

  g_recDuration = durationMs;
  g_recStart    = millis();
  g_recBytes    = 0;
  g_recording   = true;
  micStartStream();
  return true;
}

void stopRecording() {
  if (!g_recording) return;
  g_recording = false;
  micStopStream();
  // Flush the FIFO so the tail of the clip is not lost.
  size_t n;
  while ((n = micReadStream(g_recBuf, sizeof(g_recBuf))) > 0) {
    if (!netSendAudioChunk(g_recBuf, n)) break;
    g_recBytes += n;
  }
}

void recorderUpdate() {
  if (!g_recording) return;

  // Bounded work per iteration: audio must never starve light control.
  for (int i = 0; i < 4; i++) {
    size_t n = micReadStream(g_recBuf, sizeof(g_recBuf));
    if (n == 0) break;
    if (!netSendAudioChunk(g_recBuf, n)) break;
    g_recBytes += n;
  }
}

bool isRecording() { return g_recording; }

/* =====================================================================
 * ======  SECTION 9 : WEBSOCKET CLIENT  ===============================
 * =====================================================================
 *  This device is a CLIENT. It dials out to the laptop, which owns the
 *  model, Whisper and TTS. It never serves a page and never creates an
 *  access point, so it will not appear in any WiFi list.
 *
 *  Text frames  : JSON control messages (schema in laptop-server/protocol.py)
 *  Binary frames: raw PCM, both directions, bracketed by audio_begin/end. */

static WebSocketsClient g_ws;
static bool     g_socketUp      = false;
static bool     g_wasOnline     = false;
static uint32_t g_lastWifiTry   = 0;
static uint32_t g_wifiAttempts  = 0;
static uint32_t g_lastTelemetry = 0;
static bool     g_receivingAudio = false;
static volatile bool g_sweepRequested = false;
static float    g_currentBrightness = 0.0f;   // held through a WiFi outage

static void sendJson(JsonDocument& doc) {
  String out;
  serializeJson(doc, out);
  g_ws.sendTXT(out);
}

/* Tell the laptop no trustworthy reading exists, and why.
 * There is no substitute for a reading that did not happen: a clamped zero
 * would appear on the dashboard as a measurement nothing ever took. */
static void sendSensorUnavailable(const char* reason) {
  JsonDocument d;
  d["type"] = "sensor_unavailable";
  d["reason"] = reason;
  sendJson(d);
}

static void sendTelemetry() {
  float indoor  = sensorLuxA();
  float outdoor = sensorLuxB();

  if (indoor < 0 && outdoor < 0) { sendSensorUnavailable("both BH1750 sensors unreachable"); return; }
  if (indoor  < 0) { sendSensorUnavailable("indoor BH1750 (0x23) not responding");  return; }
  if (outdoor < 0) { sendSensorUnavailable("outdoor BH1750 (0x5C) not responding"); return; }

  JsonDocument d;
  d["type"]        = "sensor";
  d["indoor_lux"]  = indoor;
  d["outdoor_lux"] = outdoor;
  sendJson(d);
}

bool netSendAudioChunk(const uint8_t* pcm, size_t len) {
  if (!g_socketUp) return false;
  return g_ws.sendBIN((uint8_t*)pcm, len);
}

static void startCapture(uint32_t durationMs) {
  if (!g_socketUp || isRecording()) return;

  JsonDocument begin;
  begin["type"]        = "audio_begin";
  begin["sample_rate"] = MIC_SAMPLE_RATE;
  begin["format"]      = "pcm_s16le";
  sendJson(begin);

  if (!startRecording(durationMs)) {
    JsonDocument end; end["type"] = "audio_end"; sendJson(end);
    Serial.println("[audio] could not start recording");
    return;
  }
  Serial.printf("[audio] recording %u ms\n", (unsigned)durationMs);
}

static void finishCapture() {
  stopRecording();
  JsonDocument end;
  end["type"] = "audio_end";
  sendJson(end);
  Serial.printf("[audio] streamed %u bytes\n", (unsigned)g_recBytes);
}

static void handleTextMessage(uint8_t* payload, size_t len) {
  if (len == 0 || len > JSON_RX_MAX_BYTES) return;
  JsonDocument doc;
  if (deserializeJson(doc, payload, len)) return;

  const char* type = doc["type"] | "";

  if (!strcmp(type, "brightness")) {
    float value = doc["value"] | 0.0f;
    g_currentBrightness = constrain(value, 0.0f, 1.0f);
    int duty = (int)(g_currentBrightness * 255.0f);
    setLightBrightness(duty);
    // Log only on change: at one command per second an unconditional print
    // would bury everything else in the serial log.
    static int lastLoggedDuty = -1;
    if (duty != lastLoggedDuty) {
      lastLoggedDuty = duty;
      Serial.printf("[light] commanded %.0f%% -> duty %d/255 on GPIO %d\n",
                    g_currentBrightness * 100.0f, duty, LIGHT_PANEL_PIN);
    }

    // Acknowledge only AFTER the panel has been commanded. The laptop gates
    // the spoken confirmation on this, so the user is never told the light
    // changed before it did.
    JsonDocument ack;
    ack["type"]       = "ack";
    ack["command_id"] = doc["command_id"];
    ack["brightness"] = g_currentBrightness;
    sendJson(ack);

  } else if (!strcmp(type, "sweep")) {
    // Deferred to loop(): the sweep takes ~2.4 s and must never run inside the
    // socket callback, or the connection stalls for its whole duration.
    g_sweepRequested = true;

  } else if (!strcmp(type, "record")) {
    uint32_t requested = doc["duration_ms"] | (uint32_t)RECORDING_DURATION_MS;
    startCapture(constrain(requested, 1000u, (uint32_t)RECORDING_MAX_MS));

  } else if (!strcmp(type, "audio_begin")) {
    uint32_t rate = doc["sample_rate"] | (uint32_t)SPK_STREAM_RATE;
    g_receivingAudio = playbackBegin(rate);
    if (!g_receivingAudio)
      Serial.printf("[audio] refused playback at %u Hz (need %d)\n",
                    (unsigned)rate, SPK_STREAM_RATE);

  } else if (!strcmp(type, "audio_end")) {
    if (g_receivingAudio) playbackEnd();
    g_receivingAudio = false;

  } else if (!strcmp(type, "status")) {
    Serial.printf("[server] %s\n", doc["message"] | "");
  }
}

static void onWsEvent(WStype_t type, uint8_t* payload, size_t len) {
  switch (type) {
    case WStype_CONNECTED: {
      g_socketUp = true;
      Serial.println("[ws] connected to laptop");
      JsonDocument hello;
      hello["type"]      = "hello";
      hello["device_id"] = DEVICE_ID;
      hello["fw"]        = FIRMWARE_VERSION;
      sendJson(hello);
      break;
    }
    case WStype_DISCONNECTED:
      g_socketUp = false;
      g_receivingAudio = false;
      // The laptop vanishing mid-operation must never wedge the device.
      if (isRecording()) { stopRecording(); }
      playbackAbort();
      Serial.println("[ws] disconnected — holding last brightness");
      break;
    case WStype_TEXT:
      handleTextMessage(payload, len);
      break;
    case WStype_BIN:
      if (g_receivingAudio) playbackWrite(payload, len);
      break;
    case WStype_ERROR:
      Serial.println("[ws] socket error");
      break;
    default: break;
  }
}

static const char* wifiStatusName(wl_status_t st) {
  switch (st) {
    case WL_IDLE_STATUS:     return "idle";
    case WL_NO_SSID_AVAIL:   return "SSID not found in range";
    case WL_SCAN_COMPLETED:  return "scan completed";
    case WL_CONNECTED:       return "connected";
    case WL_CONNECT_FAILED:  return "connect failed (usually a wrong password)";
    case WL_CONNECTION_LOST: return "connection lost";
    case WL_DISCONNECTED:    return "disconnected";
    default:                 return "unknown";
  }
}

/* List what is actually in range.
 *
 * "It will not connect" has several very different causes that look identical
 * from the outside: the SSID is a 5 GHz-only network the ESP32 physically
 * cannot see, the name is misspelled, the router is out of range, or the
 * password is wrong. A scan separates the first three from the last. */
static void wifiScanAndReport() {
  Serial.println("[wifi] scanning for 2.4 GHz networks...");
  // The radio cannot scan and associate at the same time: scanning during an
  // in-flight connection attempt reports zero networks and looks exactly like
  // a dead antenna. Drop the association first, then scan from idle.
  WiFi.disconnect(true);
  WiFi.mode(WIFI_STA);
  delay(200);
  int n = WiFi.scanNetworks(false /*async*/, true /*show hidden*/);
  if (n <= 0) {
    Serial.println("[wifi] no networks visible from here at all");
    return;
  }
  Serial.printf("[wifi] %d network(s) in range:\n", n);
  bool sawTarget = false;
  for (int i = 0; i < n; i++) {
    bool isTarget = (WiFi.SSID(i) == WIFI_SSID);
    if (isTarget) sawTarget = true;
    Serial.printf("   %c %-32s %4d dBm  ch%-3d %s\n",
                  isTarget ? '>' : ' ',
                  WiFi.SSID(i).c_str(), WiFi.RSSI(i), WiFi.channel(i),
                  WiFi.encryptionType(i) == WIFI_AUTH_OPEN ? "open" : "secured");
  }
  Serial.printf("[wifi] target \"%s\": %s\n", WIFI_SSID,
                sawTarget ? "IN RANGE (so a failure here means the password)"
                          : "NOT FOUND — wrong name, or it is a 5 GHz-only network");
  WiFi.scanDelete();
}

static void wifiBegin() {
  WiFi.mode(WIFI_STA);
  WiFi.setHostname(WIFI_HOSTNAME);
  WiFi.setSleep(false);        // keeps WebSocket latency low; matters for audio
  Serial.printf("[wifi] joining \"%s\"...\n", WIFI_SSID);
  WiFi.begin(WIFI_SSID, WIFI_PASSWORD);
}

static void netUpdate() {
  g_ws.loop();

  wl_status_t status = WiFi.status();
  if (status != WL_CONNECTED) {
    if (g_wasOnline) {
      g_wasOnline = false;
      Serial.println("[wifi] lost — light control continues normally");
    }
    if (millis() - g_lastWifiTry > WIFI_RETRY_INTERVAL_MS) {
      g_lastWifiTry = millis();
      Serial.printf("[wifi] retry %u — status: %s\n",
                    (unsigned)++g_wifiAttempts, wifiStatusName(status));
      WiFi.disconnect();
      wifiBegin();
    }
    return;
  }

  if (!g_wasOnline) {
    g_wasOnline = true;
    g_wifiAttempts = 0;
    Serial.printf("[wifi] connected — IP %s, RSSI %d dBm\n",
                  WiFi.localIP().toString().c_str(), (int)WiFi.RSSI());
    Serial.printf("[ws] target ws://%s:%d%s\n", SERVER_HOST, SERVER_PORT, SERVER_PATH);
  }

  if (g_socketUp && millis() - g_lastTelemetry >= TELEMETRY_INTERVAL_MS) {
    g_lastTelemetry = millis();
    sendTelemetry();
  }
}

/* =====================================================================
 * ======  SECTION 10 : SETUP / LOOP  ==================================
 * ===================================================================== */

static void handleButtonEvent(ButtonEvent e) {
  switch (e) {
    case BTN_SINGLE_CLICK:
      Serial.println("[btn] single click -> push to talk");
      startCapture(RECORDING_DURATION_MS);
      break;
    case BTN_DOUBLE_CLICK: {
      Serial.println("[btn] double click -> request AUTO mode");
      JsonDocument d; d["type"] = "request_auto"; sendJson(d);
      break;
    }
    case BTN_LONG_PRESS:
      Serial.printf("[btn] long press -> heap %u, mic %s, spk %s, sensors %s, "
                    "underruns %u\n",
                    (unsigned)ESP.getFreeHeap(),
                    micIsHealthy() ? "ok" : "DEAD",
                    spkIsHealthy() ? "ok" : "DEAD",
                    sensorIsHealthy() ? "ok" : "DEGRADED",
                    (unsigned)g_playUnderruns);
      break;
    default: break;
  }
}

/* Slow, unmistakable 0 -> 255 -> 0 ramp on the panel pin.
 *
 * If the light does NOT visibly change during this, PWM is reaching the pin but
 * the wiring cannot act on it: no series resistor, the wrong leg to 3V3, the LED
 * fed from somewhere other than LIGHT_PANEL_PIN, or a driver that needs more
 * current than a bare GPIO can source (~12 mA safe, 40 mA absolute max).
 *
 * This is the only way to separate a firmware fault from a wiring fault without
 * a meter, so it runs at every boot. */
static void runBrightnessSweep() {
  Serial.printf("[light] sweep on GPIO %d: 0 -> 255 -> 0  (watch the LED)\n",
                LIGHT_PANEL_PIN);
  for (int b = 0; b <= 255; b += 3) { lightWriteDuty(b); delay(14); }
  delay(250);
  for (int b = 255; b >= 0; b -= 3) { lightWriteDuty(b); delay(14); }
  // Leave it at a clearly visible level so "off" vs "on" is unambiguous after
  // the sweep, rather than resetting to dark.
  g_lightTarget = g_lightActual = 160;
  lightWriteDuty(160);
  Serial.println("[light] sweep done — panel parked at duty 160/255 (63%)");
}

void setup() {
  Serial.begin(SERIAL_BAUD);
  delay(400);
  Serial.println();
  Serial.println("ADAPTIVE LIGHTING — ESP32 firmware " FIRMWARE_VERSION);
  Serial.printf("Core %d.%d.%d | free heap %u bytes\n",
                ESP_ARDUINO_VERSION_MAJOR, ESP_ARDUINO_VERSION_MINOR,
                ESP_ARDUINO_VERSION_PATCH, (unsigned)ESP.getFreeHeap());

  /* --- peripherals. Every init failure is survivable. --- */
  lightInit();
  sensorInit();
  buttonInit();
  if (!micInit()) Serial.println("[mic] I2S init FAILED — voice disabled, rest of system OK");
  if (!spkInit()) Serial.println("[spk] I2S init FAILED — playback disabled, rest of system OK");

  /* --- audio tasks on core 0, leaving core 1 entirely for loop() --- */
  micLaunchTask();
  spkLaunchTask();

  /* --- network ---
   * Scan before the first association attempt. From an idle radio this is the
   * only reading that can be trusted, and it distinguishes "wrong password"
   * from "that network is not here" before we start retrying blindly. */
  WiFi.mode(WIFI_STA);
  wifiScanAndReport();
  wifiBegin();
  g_ws.begin(SERVER_HOST, SERVER_PORT, SERVER_PATH);
  g_ws.onEvent(onWsEvent);
  g_ws.setReconnectInterval(3000);
  g_ws.enableHeartbeat(15000, 3000, 2);   // detects half-open TCP connections

  /* Visible proof that PWM reaches the panel pin, before the server can
   * override it. */
  runBrightnessSweep();

  Serial.printf("[setup] ready | free heap %u bytes\n", (unsigned)ESP.getFreeHeap());
}

void loop() {
  /* ---- Priority 1: the light. First, every iteration, always. ----
   * None of these can block. Audio lives in its own tasks and the network
   * layer never waits, so a dead WiFi link, a disconnected laptop or a failed
   * audio operation cannot stop the light from working.
   *
   * WiFi-drop fallback: g_lightTarget is never cleared on disconnect and the
   * LEDC peripheral holds its duty in hardware, so the room stays exactly as
   * the user left it rather than snapping to full or dark. */
  lightUpdate();
  sensorUpdate();

  ButtonEvent e = buttonUpdate();
  if (e != BTN_NONE) handleButtonEvent(e);

  /* ---- Priority 2: network + audio streaming ---- */
  netUpdate();
  recorderUpdate();

  // Panel test on request. This deliberately blocks for ~2.4 s — it is a
  // human-triggered diagnostic, and a sweep that shares the loop with anything
  // else is not visually unambiguous, which is the whole point of it.
  if (g_sweepRequested) {
    g_sweepRequested = false;
    runBrightnessSweep();
  }

  // Recording is time-bounded here rather than in a blocking wait, so
  // telemetry and light control keep running throughout the capture.
  if (isRecording() && millis() - g_recStart >= g_recDuration) {
    finishCapture();
  }
}
