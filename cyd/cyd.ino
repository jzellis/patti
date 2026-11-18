// Her chest screen: an ESP32-2432S028 "CYD" (the two-port USB-C + micro-USB
// version), 2.8" 320x240 with resistive touch, talking to the Pi over USB
// serial at 115200. brain/cyd.py is the Pi's end.
//
// It looks like a monochrome amber terminal (black on amber, or amber on
// black), with scanlines. An icon bar down the left picks the screen:
//
//   home      clock, weather, and her status lines
//   music     what's playing through her: a cassette whose reels turn
//   system    \
//   heard      |  lists the Pi fills in (line/sel); tapping a line, or the
//   mood       |  joystick through the Pi, picks it
//   forecast   |
//   settings  /
//
// the terminal, a full-screen grid of coloured 6x12 characters the Pi fills
// in (it runs the terminal itself: the Linux console, or a program), and
// the voice screen, which the Pi switches to by itself when she hears
// speech: two waveforms (what she hears, what she says) drawn here from levels
// the Pi sends, and the words as they're recognised. Until the Pi first
// talks to it, it shows a boot screen.
//
// Line commands from the Pi:
//
//   ping                     -> pong
//   id                       -> the panel's ID registers (tells ST7789 from ILI9341)
//   screen <name>            show a screen (home music system heard mood forecast
//                            settings voice note)
//   clear                    blank home's status lines (and any big text)
//   text <words>             show <words> big, over everything, until the next
//                            clear, row or screen
//   row <n> <colour> <text>  home's status line n (0-7). Colours are amber
//                            shades: grey dim, red an inverse alarm, the rest normal
//   line <screen> <n> <colour> <text>
//                            line n (0-11) of a list screen ("" text to blank it)
//   sel <screen> <n>         highlight line n of a list screen (-1 for none)
//   time <unix> <offset>     set the clock: seconds since 1970 (UTC) and the
//                            local offset from UTC in seconds; it keeps time itself
//   weather <t> <hi> <lo> <icon> <words>
//                            now / today's high / low in whole degrees ("-" if
//                            unknown); icon one of sun moon partly partlynight
//                            cloud fog drizzle rain snow thunder none
//   lvl <in> <out>           the voice screen's loudness, 0-100, heard and said
//   said <words>             the voice screen's words so far ("said" alone clears)
//   cmd <words>              the command she took from them ("cmd" alone clears)
//   np song|artist|album|from <words>
//                            the music screen's song ("from" is airplay or dlna)
//   np at <playing> <pos> <len>
//                            1 playing / 0 paused, and where it is in the song,
//                            in seconds (len 0 if unknown); it counts on itself
//   np none                  nothing's playing
//   tsize <rows> <cols>      the terminal's size (at most 20 x 53), and clear it
//   T <row> <col> <FB> <text>|
//                            a run of terminal cells: F and B are the fore- and
//                            background colours, ANSI 0-f in hex; the text runs
//                            to the final "|" and may use 0x80-0x93 for line
//                            drawing and blocks (term_glyph). No reply.
//   TC <row> <col>           the terminal's cursor (-1 -1 hides it). No reply.
//   mic on|off               is she listening? (the icon bottom right; off shows
//                            it crossed out and highlighted)
//   palette <8 x RRGGBB>     the colours, by role: background, faint, dim,
//                            ink, bright, accent, good, alert (kept in flash)
//   font <name>              the body text: terminus, pixelop, pixelopbold,
//                            toshiba or silkscreen (kept in flash)
//   scan on|off              scanlines
//   bright <0-255>           backlight level to fade to (0 off)
//   auto on|off [lit dark]   scale it by the room's light; lit and dark are raw
//                            sensor readings (it sends "light <raw>" every 5 s)
//   calibrate                touch the four corners as they come up; the
//                            result is kept in flash (and printed)
//
// And from it, unasked:
//
//   hello                    just booted
//   screen <name>            someone tapped the icon bar
//   tap <screen> <n>         someone tapped line n of a list screen (or the
//                            music screen: "tap music 0", play/pause)
//   tap term                 someone tapped the terminal (the Pi leaves it)
//   tap mic                  someone tapped the mic icon (the Pi turns
//                            listening on or off, and says which with "mic")
//   light <raw>              the light sensor, every 5 s
//   touch <x> <y>            finger down (screen pixels, 320x240 landscape)
//   move <x> <y>             it moved (at most 20 a second)
//   release <x> <y>          finger up, where it last was
//
// Every line it sends starts with "cyd: " so the Pi can pick them out of the
// ESP32's boot chatter.

#include <LovyanGFX.hpp>
#include <Preferences.h>
#include <time.h>

#include "terminus.h"   // the body text (Terminus, SIL OFL; made with fontconvert.py)
#include "terminus12.h" // the terminal's (6x12)
#include "pixeloperator.h" // other body fonts, for Settings: Pixel Operator Mono (CC0),
#include "toshibasat.h"    // the Toshiba T3100's (CC BY-SA, VileR) and
#include "silkscreen.h"    // Silkscreen (SIL OFL)

// Hers reads as an ILI9341 (an ST7789 answers RDDID with 85 85 52; this reads
// zeros), and needs no inversion (checked by eye).
#define PANEL_ST7789 0
#define PANEL_INVERT 0

class LGFX : public lgfx::LGFX_Device {
#if PANEL_ST7789
  lgfx::Panel_ST7789 panel;
#else
  lgfx::Panel_ILI9341 panel;
#endif
  lgfx::Bus_SPI bus;
  lgfx::Light_PWM light;
  lgfx::Touch_XPT2046 touch;

 public:
  LGFX() {
    {
      auto cfg = bus.config();
      cfg.spi_host = HSPI_HOST;
      cfg.spi_mode = 0;
      cfg.freq_write = 40000000;
      cfg.freq_read = 16000000;
      cfg.spi_3wire = false;
      cfg.use_lock = true;
      cfg.dma_channel = SPI_DMA_CH_AUTO;
      cfg.pin_sclk = 14;
      cfg.pin_mosi = 13;
      cfg.pin_miso = 12;
      cfg.pin_dc = 2;
      bus.config(cfg);
      panel.setBus(&bus);
    }
    {
      auto cfg = panel.config();
      cfg.pin_cs = 15;
      cfg.pin_rst = -1;
      cfg.pin_busy = -1;
      cfg.panel_width = 240;
      cfg.panel_height = 320;
      cfg.readable = true;
      cfg.invert = PANEL_INVERT;
      cfg.rgb_order = false;
      cfg.bus_shared = false;
      panel.config(cfg);
    }
    {
      auto cfg = light.config();
      cfg.pin_bl = 21;
      cfg.invert = false;
      cfg.freq = 44100;
      cfg.pwm_channel = 7;
      light.config(cfg);
      panel.setLight(&light);
    }
    {
      // The touch chip has its own pins, so its own SPI bus (VSPI).
      // Rough raw ranges until "calibrate" has been run.
      auto cfg = touch.config();
      cfg.spi_host = VSPI_HOST;
      cfg.freq = 1000000;
      cfg.pin_sclk = 25;
      cfg.pin_mosi = 32;
      cfg.pin_miso = 39;
      cfg.pin_cs = 33;
      cfg.pin_int = 36;
      cfg.bus_shared = false;
      cfg.x_min = 300;
      cfg.x_max = 3900;
      cfg.y_min = 200;
      cfg.y_max = 3700;
      cfg.offset_rotation = 0;
      touch.config(cfg);
      panel.setTouch(&touch);
    }
    setPanel(&panel);
  }
};

static LGFX lcd;
static LGFX_Sprite scr(&lcd);   // the whole screen, drawn here then pushed in one go:
                                // 4-bit, with our own palette of exact ambers
static Preferences prefs;

// ---------- the look ----------
// Palette entries (the sprite's colours are these numbers), by role: the
// background, inks from least to most contrast, then an accent (titles and
// the icon bar) and colours for good and bad news. 8-15 are the same colours
// darker, for the scanlines. The themes are the Pi's (brain/themes.py): it
// sends them with "palette". A one-hue theme makes the last three its ink.
enum { BG = 0, FAINT = 1, DIM = 2, INK = 3, BRIGHT = 4, ACCENT = 5, GOOD = 6, ALERT = 7 };
static const int NCOL = 8;
static uint32_t pal[NCOL] = {     // RGB888; till the Pi says otherwise, black on Grundig orange
    0xE85E10u, 0xBA4B0Du, 0x6C2C08u, 0x000000u, 0x000000u, 0x000000u, 0x000000u, 0x000000u};
static const float SCAN = 0.7;   // scanline brightness
static bool scanlines = true;

// ---------- the backlight ----------
// The Pi sets the level it wants ("bright"; 0 is off); it fades there. With
// "auto on" the light sensor (an LDR on GPIO 34) scales it down in a dark room,
// to 30% at the dark reading. Readings go out every 5 s ("light") for tuning.
static const int LDR = 34;
static int bl_target = 140;
static bool bl_auto = false;
static int light_lit = 100, light_dark = 1500;   // raw readings, a lit room and a dark one
static float light = -1;                         // smoothed raw reading
static float bl_now = 0;

// ---------- screens ----------
enum Screen { HOME, MUSIC, SYSTEM, HEARD, MOOD, FORECAST, SETTINGS, VOICE, TERM, NOTE, NSCREENS };
static const char *NAMES[NSCREENS] = {"home", "music", "system", "heard", "mood", "forecast", "settings", "voice", "term", "note"};
static const char *TITLES[NSCREENS] = {"", "NOW PLAYING", "SYSTEM", "HEARD", "MOOD", "FORECAST", "SETTINGS", "VOICE", "", "NOTE"};
static const int NBAR = 7;                   // the first seven have icons on the bar
static const int BAR_W = 38, X0 = 41;        // the bar, and where the screens start
static const int CELL_H = 34;

static Screen screen = HOME;
static bool booted = false;                  // has the Pi said anything yet?
static String big;                           // "text" over everything, if set
static bool dirty = true;

// home
static const int ROWS = 8;
static const int ROW_Y = 114, ROW_H = 15;
static const int TOP_H = 106;
static String rows[ROWS];
static uint8_t level[ROWS];   // 0 dim, 1 normal, 2 accent, 3 alarm, 4 good, 5 warning

// list screens
static const int LINES = 12;
static const int LIST_Y = 26, LINE_H = 17;
static String lines[NSCREENS][LINES];
static uint8_t line_level[NSCREENS][LINES];
static int sel[NSCREENS];
static bool in_page = false;    // the joystick's in the screen, not on the bar: its icon's outlined

// clock and weather
static bool have_time = false;
static int64_t t_base = 0;    // local time (unix + offset) at...
static uint32_t t_ms = 0;     // ...this millis()
static bool have_weather = false;
static String w_now, w_hi, w_lo, w_icon, w_words;

// voice
static float lvl_in = 0, lvl_out = 0;        // targets, 0-1
static String said, cmd;

// listening (the mic icon, bottom right of every screen but voice)
static bool mic_on = true;
static const int MIC_X = 296, MIC_Y = 214, MIC_W = 24, MIC_H = 26;

// the terminal: drawn straight to the panel a run of cells at a time (not
// through the screen sprite), so typing and htop's updates are cheap
static const int TW = 6, TH = 12, TMAXR = 20, TMAXC = 53;
static int t_rows = TMAXR, t_cols = TMAXC, t_x0 = 1, t_y0 = 0;
static uint8_t t_ch[TMAXR][TMAXC];
static uint8_t t_at[TMAXR][TMAXC];            // foreground | background << 4
static int cur_r = -1, cur_c = -1;
static bool cur_on = false;
static bool t_redraw = true;
static LGFX_Sprite t_run(&lcd);               // one row's worth, 16-bit
static const uint32_t ANSI[16] = {             // RGB888: the usual 16, a little softened
    0x000000u, 0xC8372Bu, 0x2EAE4Eu, 0xC8A02Au, 0x3A64D8u, 0xB04CB8u, 0x2AA8B8u, 0xC0C0C0u,
    0x646464u, 0xFF5A4Au, 0x5AF078u, 0xFFE45Au, 0x78A0FFu, 0xF078FFu, 0x5AF0FFu, 0xFFFFFFu};

// music
static bool np_have = false, np_playing = false;
static String np_song, np_artist, np_album, np_from;
static float np_pos = 0, np_len = 0;         // seconds, at...
static uint32_t np_ms = 0;                   // ...this millis()
static float reel_l = 0, reel_r = 0;         // the reels' turn, radians

static uint8_t level_of(const String &name) {
  if (name == "grey") return 0;
  if (name == "blue") return 2;
  if (name == "amber") return 5;
  if (name == "red") return 3;
  if (name == "green") return 4;
  return 1;
}
static int ink_of(uint8_t lv) {
  // a line's colour for its level (3, alarm, is drawn highlighted in ALERT;
  // 5, a warning, is just written in it)
  return lv == 0 ? DIM : lv == 2 ? ACCENT : lv == 4 ? GOOD : lv == 5 ? ALERT : INK;
}

static int screen_of(const String &name) {
  for (int i = 0; i < NSCREENS; i++)
    if (name == NAMES[i]) return i;
  return -1;
}

static bool blink() { return (millis() / 500) % 2 == 0; }

// ---------- text ----------
// Body text is one of these pixel fonts, at a whole-number size that makes
// it about 16 px (Terminus's own: 8 px a character, 10 px capitals). put()
// takes the top of the capitals, not the baseline.
struct BodyFont { const char *name; const lgfx::GFXfont *font; int size, cap; };
static const BodyFont FONTS[] = {
    {"terminus", &Terminus, 1, 10},
    {"pixelop", &PixelOperatorMono, 1, 9},
    {"pixelopbold", &PixelOperatorMonoBold, 1, 9},
    {"toshiba", &ToshibaSat, 1, 11},
    {"silkscreen", &Silkscreen, 2, 5},
};
static const int NFONTS = sizeof(FONTS) / sizeof(FONTS[0]);
static int font_i = 0;
static const int TS = 1;
static int CAP_1 = 10, CAP = 10;  // the current font's capitals, at its own size and as drawn
static void body(int size = TS) {
  scr.setFont(FONTS[font_i].font);
  scr.setTextSize(size * FONTS[font_i].size);
}
static void use_font(int i) {
  font_i = i;
  CAP_1 = FONTS[i].cap;
  CAP = FONTS[i].cap * FONTS[i].size;
}
static String fit(String s, int w) {
  // s, cut short to fit w pixels in the current font
  while (s.length() && scr.textWidth(s) > w) s.remove(s.length() - 1);
  return s;
}
static void put(const String &s, int x, int top, int colour, int bg = BG, textdatum_t datum = baseline_left) {
  scr.setTextColor(colour, bg);
  scr.setTextDatum(datum);
  int cap = scr.getFont() == FONTS[font_i].font ? CAP_1 : 10;   // (anything else is Terminus12's)
  scr.drawString(s, x, top + cap * (int)scr.getTextSizeY());
  scr.setTextDatum(top_left);
}
// "LABEL   VALUE" (two or more spaces) is two columns: the label, then the
// value at col pixels in. Returns where the text ended.
static int put_cols(const String &s, int x, int top, int col, int w, int colour, int bg = BG) {
  int gap = s.indexOf("  ");
  if (gap < 0) {
    String t = fit(s, w);
    put(t, x, top, colour, bg);
    return x + scr.textWidth(t);
  }
  String label = s.substring(0, gap), value = s.substring(gap);
  value.trim();
  put(label, x, top, colour, bg);
  String v = fit(value, w - col);
  put(v, x + col, top, colour, bg);
  return x + col + scr.textWidth(v);
}
static int label_width(const String *ls, int n) {
  // the widest label among lines with two columns, plus a gap
  int w = 0;
  for (int i = 0; i < n; i++) {
    int gap = ls[i].indexOf("  ");
    if (gap > 0) w = max(w, (int)scr.textWidth(ls[i].substring(0, gap)));
  }
  return w + 12;
}

static bool local_time(struct tm *tm) {
  if (!have_time) return false;
  time_t t = (time_t)(t_base + (millis() - t_ms) / 1000);
  gmtime_r(&t, tm);
  return true;
}

// ---------- weather icons, line art in a 40x40 box ----------
static void cloud(int x, int y, int c, int grow) {
  scr.fillCircle(x + 12, y + 24, 7 + grow, c);
  scr.fillCircle(x + 21, y + 17, 10 + grow, c);
  scr.fillCircle(x + 30, y + 24, 7 + grow, c);
  scr.fillRect(x + 12, y + 24 - grow, 18, 7 + 2 * grow, c);
}
static void cloud_outline(int x, int y) {
  cloud(x, y, INK, 0);
  cloud(x, y, BG, -2);
}
static void sun(int cx, int cy, int r) {
  scr.drawCircle(cx, cy, r, INK);
  scr.drawCircle(cx, cy, r - 1, INK);
  for (int i = 0; i < 8; i++) {
    float a = i * PI / 4;
    scr.drawLine(cx + cos(a) * (r + 3), cy + sin(a) * (r + 3), cx + cos(a) * (r + 7), cy + sin(a) * (r + 7), INK);
  }
}
static void moon(int cx, int cy, int r) {
  scr.fillCircle(cx, cy, r, INK);
  scr.fillCircle(cx + r / 2, cy - r / 3, r - 1, BG);
}
static void weather_icon(int x, int y, const String &name) {
  if (name == "sun") {
    sun(x + 20, y + 20, 9);
  } else if (name == "moon") {
    moon(x + 20, y + 20, 13);
  } else if (name == "partly" || name == "partlynight") {
    if (name == "partly") sun(x + 14, y + 13, 6);
    else moon(x + 14, y + 12, 9);
    cloud(x + 2, y + 6, BG, 2);        // a gap round the cloud
    cloud_outline(x + 2, y + 6);
  } else if (name == "cloud") {
    cloud_outline(x, y + 2);
  } else if (name == "fog") {
    for (int i = 0; i < 4; i++) scr.drawFastHLine(x + 4 + (i % 2) * 4, y + 10 + i * 7, 28, INK);
  } else if (name == "drizzle" || name == "rain" || name == "snow" || name == "thunder") {
    cloud_outline(x, y - 4);
    for (int i = 0; i < 3; i++) {
      int dx = x + 12 + i * 8, dy = y + 32;
      if (name == "rain") scr.drawLine(dx + 2, dy, dx - 2, dy + 7, INK);
      else if (name == "drizzle") scr.fillRect(dx, dy + (i % 2) * 3, 2, 2, INK);
      else if (name == "snow") {
        scr.drawFastHLine(dx - 2, dy + 3, 5, INK);
        scr.drawFastVLine(dx, dy + 1, 5, INK);
      }
    }
    if (name == "thunder") {
      scr.drawLine(x + 22, y + 28, x + 17, y + 35, INK);
      scr.drawLine(x + 17, y + 35, x + 23, y + 35, INK);
      scr.drawLine(x + 23, y + 35, x + 18, y + 42, INK);
    }
  } else if (name != "none") {
    body();
    put("?", x + 20, y + 15, DIM, BG, baseline_center);
  }
}

// ---------- the icon bar: 24x24 line icons, centred in 38x40 cells ----------
static void bar_icon(int i, int cx, int cy, int c, int bg) {
  switch (i) {
    case HOME:     // a house
      scr.drawTriangle(cx - 11, cy - 1, cx, cy - 11, cx + 11, cy - 1, c);
      scr.drawRect(cx - 8, cy - 1, 16, 12, c);
      scr.fillRect(cx - 2, cy + 4, 5, 7, c);
      break;
    case MUSIC:    // a cassette
      scr.drawRoundRect(cx - 12, cy - 8, 25, 17, 2, c);
      scr.drawCircle(cx - 5, cy - 1, 3, c);
      scr.drawCircle(cx + 5, cy - 1, 3, c);
      scr.drawFastHLine(cx - 2, cy + 2, 5, c);
      scr.drawLine(cx - 7, cy + 8, cx - 5, cy + 5, c);
      scr.drawFastHLine(cx - 5, cy + 5, 11, c);
      scr.drawLine(cx + 5, cy + 5, cx + 7, cy + 8, c);
      break;
    case SYSTEM:   // a chip
      scr.drawRect(cx - 7, cy - 7, 15, 15, c);
      scr.fillRect(cx - 3, cy - 3, 7, 7, c);
      for (int k = -4; k <= 4; k += 4) {
        scr.drawFastHLine(cx - 11, cy + k, 4, c);
        scr.drawFastHLine(cx + 8, cy + k, 4, c);
        scr.drawFastVLine(cx + k, cy - 11, 4, c);
        scr.drawFastVLine(cx + k, cy + 8, 4, c);
      }
      break;
    case HEARD:    // a speech bubble
      scr.drawRoundRect(cx - 11, cy - 9, 23, 15, 4, c);
      scr.fillTriangle(cx - 6, cy + 5, cx - 1, cy + 5, cx - 8, cy + 11, c);
      for (int k = -5; k <= 5; k += 5) scr.fillRect(cx + k - 1, cy - 3, 2, 2, c);
      break;
    case MOOD:     // a face
      scr.drawCircle(cx, cy, 11, c);
      scr.fillRect(cx - 5, cy - 5, 2, 4, c);
      scr.fillRect(cx + 4, cy - 5, 2, 4, c);
      scr.drawArc(cx, cy + 1, 7, 6, 30, 150, c);
      break;
    case FORECAST: // a cloud with the sun behind
      scr.drawCircle(cx + 5, cy - 4, 5, c);
      scr.fillCircle(cx - 4, cy + 3, 6, c);
      scr.fillCircle(cx + 3, cy, 7, c);
      scr.fillCircle(cx + 9, cy + 4, 5, c);
      scr.fillRect(cx - 4, cy + 3, 13, 7, c);
      scr.fillCircle(cx - 4, cy + 3, 4, bg);
      scr.fillCircle(cx + 3, cy, 5, bg);
      scr.fillCircle(cx + 9, cy + 4, 3, bg);
      scr.fillRect(cx - 4, cy + 3, 13, 5, bg);
      break;
    case SETTINGS: // three sliders
      for (int k = -1; k <= 1; k++) {
        int y = cy + k * 7, kx = cx + (k == 0 ? 4 : k < 0 ? -5 : 1);
        scr.drawFastHLine(cx - 11, y, 23, c);
        scr.fillRect(kx - 2, y - 3, 5, 7, c);
      }
      break;
  }
}

static void draw_bar() {
  for (int i = 0; i < NBAR; i++) {
    int y = i * CELL_H;
    bool on = i == screen, fill = on && !in_page;
    if (fill) scr.fillRect(0, y, BAR_W, CELL_H, ACCENT);
    else if (on) for (int k = 0; k < 2; k++) scr.drawRect(k, y + k, BAR_W - 2 * k, CELL_H - 2 * k, ACCENT);
    bar_icon(i, BAR_W / 2, y + CELL_H / 2, fill ? BG : ACCENT, fill ? ACCENT : BG);
  }
  scr.drawFastVLine(BAR_W + 1, 0, 240, DIM);
}

// ---------- drawing ----------
static void dim_scanlines() {
  // Every other line darker: colour n becomes n + 8. The sprite is 4-bit, two
  // pixels a byte.
  uint8_t *p = (uint8_t *)scr.getBuffer();
  int stride = scr.width() / 2;
  for (int y = 1; y < scr.height(); y += 2) {
    uint8_t *q = p + y * stride;
    for (int x = 0; x < stride; x++) q[x] |= 0x88;
  }
}

static void draw_boot() {
  static const char *ls[] = {
      "PATTI INDUSTRIES",
      "CHEST TERMINAL  BIOS 1.1",
      "",
      "CPU    ESP32 240MHZ ..... OK",
      "VIDEO  ILI9341 320X240 .. OK",
      "TOUCH  XPT2046 ........... OK",
      "",
      "WAITING FOR BRAIN",
  };
  body();
  int y = 22;
  for (auto l : ls) {
    put(l, 14, y, INK);
    y += 21;
  }
  int dots = (millis() / 400) % 4;
  put(String("....").substring(0, dots), 14 + scr.textWidth("WAITING FOR BRAIN") + 4, y - 21, INK);
  if (blink()) scr.fillRect(14, y, 8, CAP, INK);
}

static void draw_big() {
  // Word-wrapped, at double size, over everything.
  body(2);
  String ls[7];
  int n = 0;
  String word, line;
  String s = big + " ";
  s.toUpperCase();
  for (unsigned i = 0; i < s.length() && n < 7; i++) {
    char c = s[i];
    if (c != ' ') { word += c; continue; }
    String longer = line.length() ? line + " " + word : word;
    if (line.length() && scr.textWidth(longer) > 300) { ls[n++] = line; line = fit(word, 300); }
    else line = fit(longer, 300);
    word = "";
  }
  if (line.length() && n < 7) ls[n++] = line;
  int y = (240 - n * 28) / 2;
  for (int i = 0; i < n; i++) {
    put(ls[i], 160, y, INK, BG, baseline_center);
    y += 28;
  }
  body();
}

static void title(const char *t) {
  // a list screen's title, with the time on the right
  body();
  put(t, X0 + 6, 5, ACCENT);
  struct tm tm;
  if (local_time(&tm)) {
    char hm[6];
    snprintf(hm, sizeof hm, "%02d:%02d", tm.tm_hour, tm.tm_min);
    put(hm, 316, 5, DIM, BG, baseline_right);
  }
  for (int x = X0; x < 320; x += 6) scr.drawFastHLine(x, 21, 3, DIM);
}

static void draw_home() {
  static const char *days[] = {"SUN", "MON", "TUE", "WED", "THU", "FRI", "SAT"};
  static const char *months[] = {"JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"};
  const int WX = 214;                          // the weather's column
  scr.setTextDatum(top_left);
  struct tm tm;
  if (local_time(&tm)) {
    char hh[3], mm[3], date[24];
    snprintf(hh, sizeof hh, "%02d", tm.tm_hour);
    snprintf(mm, sizeof mm, "%02d", tm.tm_min);
    snprintf(date, sizeof date, "%s %02d %s %04d", days[tm.tm_wday], tm.tm_mday, months[tm.tm_mon], tm.tm_year + 1900);
    // 7-segment digits, as big as fit beside the weather
    scr.setFont(&fonts::Font7);
    scr.setTextSize(1);
    float k = min(1.5f, (float)(WX - X0 - 14) / (2 * scr.textWidth("88") + scr.textWidth(":")));
    scr.setTextSize(k);
    int wd = scr.textWidth("88"), wc = scr.textWidth(":");
    scr.setTextColor(BRIGHT, BG);
    scr.drawString(hh, X0 + 4, 8);
    if (tm.tm_sec % 2 == 0) scr.drawString(":", X0 + 4 + wd, 8);
    scr.drawString(mm, X0 + 4 + wd + wc, 8);
    body();
    put(date, X0 + 6, 86, INK);
  } else {
    scr.setFont(&fonts::Font7);
    scr.setTextSize(1);
    scr.setTextColor(FAINT, BG);
    scr.drawString("--:--", X0 + 4, 8);
  }
  scr.drawFastVLine(WX - 6, 8, TOP_H - 16, FAINT);
  if (have_weather) {
    weather_icon(WX - 2, 4, w_icon);
    scr.setFont(&fonts::Font7);
    scr.setTextSize(1);
    scr.setTextColor(BRIGHT, BG);
    scr.setTextDatum(top_right);
    scr.drawString(w_now == "-" ? "--" : w_now, 306, 4);
    scr.setTextDatum(top_left);
    body();
    put("C", 309, 6, BRIGHT);
    put(fit(w_words, 318 - WX), WX, 60, INK);
    put("H " + w_hi + "  L " + w_lo, WX, 78, DIM);
  } else {
    body();
    put("NO WEATHER", WX, 44, FAINT);
  }
  for (int x = X0; x < 320; x += 6) scr.drawFastHLine(x, TOP_H, 3, DIM);

  // status lines, with a blinking cursor after the last
  body();
  const int x = X0 + 16, w = 318 - x;
  int col = label_width(rows, ROWS);
  int last = -1, end = x;
  for (int n = 0; n < ROWS; n++) {
    if (!rows[n].length()) continue;
    last = n;
    int y = ROW_Y + 2 + n * ROW_H;
    put(">", X0 + 3, y, DIM);
    if (level[n] == 3) {
      scr.fillRect(x - 3, y - 2, w + 3, CAP + 4, ALERT);
      end = put_cols(rows[n], x, y, col, w, BG, ALERT);
    } else {
      end = put_cols(rows[n], x, y, col, w, ink_of(level[n]));
    }
  }
  if (blink()) {
    int n = last < 0 ? 0 : last;
    scr.fillRect(min(last < 0 ? x : end + 4, 312), ROW_Y + 2 + n * ROW_H, 7, CAP, INK);
  }
}

static void draw_list() {
  title(TITLES[screen]);
  body();
  const int x = X0 + 6, w = 314 - x;
  int col = label_width(lines[screen], LINES);
  for (int n = 0; n < LINES; n++) {
    const String &s = lines[screen][n];
    if (!s.length()) continue;
    int y = LIST_Y + 3 + n * LINE_H;
    if (n == sel[screen] || line_level[screen][n] == 3) {
      int hi = line_level[screen][n] == 3 ? ALERT : INK;
      scr.fillRect(X0 + 2, y - 3, 320 - X0 - 4, LINE_H - 1, hi);
      put_cols(s, x, y, col, w, BG, hi);
    } else {
      put_cols(s, x, y, col, w, ink_of(line_level[screen][n]));
    }
  }
}

// ---------- the voice screen ----------
// Each trace is a few sine waves with slowly drifting frequencies and phases,
// tapered at the ends, sized by its (smoothed) level: it never repeats, and it
// moves with the real loudness.
struct Wave {
  float amp = 0, ph[3] = {0, 2.1, 4.2};
  void step(float target) {
    amp += (target - amp) * (target > amp ? 0.45f : 0.12f);
    ph[0] += 0.31f, ph[1] -= 0.47f, ph[2] += 0.83f;
  }
  void draw(int x0, int w, int cy, int h) {
    float t = millis() / 1000.0f;
    float f0 = 0.045f + 0.012f * sin(t * 0.37f), f1 = 0.11f + 0.03f * sin(t * 0.23f + 1), f2 = 0.23f + 0.05f * sin(t * 0.61f + 2);
    int py = cy;
    for (int i = 0; i <= w; i += 2) {
      float env = sin(PI * i / w);
      float v = 0.55f * sin(f0 * i + ph[0]) + 0.3f * sin(f1 * i + ph[1]) + 0.15f * sin(f2 * i + ph[2]);
      v = v * amp * env * h + (random(-100, 101) / 100.0f) * (0.6f + amp * 3);
      int y = cy - (int)v;
      if (i) {
        scr.drawLine(x0 + i - 2, py, x0 + i, y, INK);
        scr.drawLine(x0 + i - 2, py + 1, x0 + i, y + 1, INK);
      }
      py = y;
    }
  }
};
static Wave wave_in, wave_out;

static void draw_voice() {
  title("VOICE");
  body();
  put("IN", X0 + 6, 30, DIM);
  put("OUT", X0 + 6, 114, DIM);
  int x0 = X0 + 34, w = 320 - x0 - 6;
  scr.drawFastHLine(x0, 70, w, FAINT);
  scr.drawFastHLine(x0, 146, w, FAINT);
  wave_in.draw(x0, w, 70, 34);
  wave_out.draw(x0, w, 146, 26);
  for (int x = X0; x < 320; x += 6) scr.drawFastHLine(x, 176, 3, DIM);
  // the words, up to two lines, and the command
  const int tw = 320 - X0 - 12;
  String t = said;
  t.toUpperCase();
  String first = fit(t, tw);
  if (first.length() < t.length()) {
    int cut = first.lastIndexOf(' ');
    if (cut > 0) first = first.substring(0, cut);
  }
  put(first, X0 + 6, 184, INK);
  String second = t.substring(first.length());
  second.trim();
  put(fit(second, tw), X0 + 6, 201, INK);
  if (cmd.length()) {
    String c = "> " + cmd;
    c.toUpperCase();
    scr.fillRect(X0 + 2, 219, 320 - X0 - 4, 18, INK);
    put(fit(c, tw), X0 + 6, 223, BG, INK);
  }
}

// ---------- the music screen ----------
static float np_now() {
  // where the song is now: it counts on from the last "np at" while playing
  float p = np_pos + (np_playing ? (millis() - np_ms) / 1000.0f : 0);
  return np_len > 0 ? min(p, np_len) : p;
}

static String mmss(float t) {
  int s = (int)t;
  char b[12];
  snprintf(b, sizeof b, "%d:%02d", s / 60, s % 60);
  return b;
}

static int wrap(String s, int w, String *out, int most) {
  // s in lines of at most w pixels (the current font), cut at spaces
  int n = 0;
  s.trim();
  while (s.length() && n < most) {
    String t = fit(s, w);
    if (t.length() < s.length()) {
      if (n == most - 1) {                     // the last line: cut it short
        t = fit(s, w - scr.textWidth(".."));
        t.trim();
        t += "..";
        out[n++] = t;
        break;
      }
      int cut = t.lastIndexOf(' ');
      if (cut > 0) t = t.substring(0, cut);
    }
    out[n++] = t;
    s = s.substring(t.length());
    s.trim();
  }
  return n;
}

static void reel(int cx, int cy, float r, float turn) {
  // a pack of tape r across, on a hub with three spokes
  scr.fillCircle(cx, cy, (int)r, FAINT);
  scr.drawCircle(cx, cy, (int)r, DIM);
  scr.fillCircle(cx, cy, 9, BG);
  scr.drawCircle(cx, cy, 9, INK);
  for (int k = 0; k < 3; k++) {
    float a = turn + k * 2 * PI / 3;
    scr.drawLine(cx + cos(a) * 3, cy + sin(a) * 3, cx + cos(a) * 8, cy + sin(a) * 8, INK);
  }
}

static void draw_music() {
  title("NOW PLAYING");
  if (!np_have) {
    body();
    put("NOTHING PLAYING", 180, 96, INK, BG, baseline_center);
    put("AIRPLAY OR DLNA TO", 180, 126, DIM, BG, baseline_center);
    put("PATTI", 180, 143, DIM, BG, baseline_center);
    return;
  }
  body();
  String from = np_from;
  from.toUpperCase();
  put(from, 268, 5, DIM, BG, baseline_right);
  // the song, big, on up to two lines; then the artist and album
  const int x = X0 + 6, w = 316 - x;
  body(2);
  String ls[2];
  int n = wrap(np_song, w, ls, 2);
  for (int i = 0; i < n; i++) put(ls[i], x, 30 + i * 24, BRIGHT);
  body();
  int y = n > 1 ? 82 : 58;
  put(fit(np_artist, w), x, y, INK);
  put(fit(np_album, w), x, y + 17, DIM);

  // the cassette: the left reel's tape winds onto the right one as it plays
  const int cx = 180, cy = 151, d = 58;
  float p = np_len > 0 ? np_now() / np_len : 0.5f;
  const float r0 = 10, r1 = 30;
  float rl = sqrt(r0 * r0 + (r1 * r1 - r0 * r0) * (1 - p));
  float rr = sqrt(r0 * r0 + (r1 * r1 - r0 * r0) * p);
  scr.drawRoundRect(cx - 114, 114, 228, 76, 6, DIM);
  scr.drawLine(cx - 40, 189, cx - 32, 180, DIM);
  scr.drawFastHLine(cx - 32, 180, 65, DIM);
  scr.drawLine(cx + 32, 180, cx + 40, 189, DIM);
  // the tape, round the guides and along the bottom
  const int gx = 36, gy = 180;
  scr.drawLine(cx - d - (int)rl, cy, cx - d - gx, gy, INK);
  scr.drawFastHLine(cx - d - gx, gy + 3, 2 * (d + gx), INK);
  scr.drawLine(cx + d + (int)rr, cy, cx + d + gx, gy, INK);
  scr.fillCircle(cx - d - gx, gy, 3, INK);
  scr.fillCircle(cx + d + gx, gy, 3, INK);
  reel(cx - d, cy, rl, reel_l);
  reel(cx + d, cy, rr, reel_r);
  // playing or paused, between the reels
  if (np_playing) scr.fillTriangle(cx - 4, cy - 7, cx - 4, cy + 7, cx + 7, cy, INK);
  else if (blink()) {
    scr.fillRect(cx - 6, cy - 7, 4, 15, INK);
    scr.fillRect(cx + 3, cy - 7, 4, 15, INK);
  }

  // where it's got to
  put(mmss(np_now()), x, 200, INK);
  if (np_len > 0) {
    put(mmss(np_len), 316, 200, DIM, BG, baseline_right);
    int bx = x + 48, bw = 316 - 48 - bx;
    scr.drawRect(bx, 200, bw, 10, DIM);
    scr.fillRect(bx + 2, 202, (int)((bw - 4) * min(1.0f, p)), 6, INK);
  }
}

static void turn_reels(float dt) {
  // tape runs at a steady speed, so the reel with less on it turns faster
  if (!np_playing) return;
  float p = np_len > 0 ? np_now() / np_len : 0.5f;
  const float r0 = 10, r1 = 30, v = 30;        // px of tape a second
  float rl = sqrt(r0 * r0 + (r1 * r1 - r0 * r0) * (1 - p));
  float rr = sqrt(r0 * r0 + (r1 * r1 - r0 * r0) * p);
  reel_l += v / rl * dt;
  reel_r += v / rr * dt;
}

static void draw_mic() {
  // a microphone: dim while listening; crossed out, in a lit box, when not
  int cx = MIC_X + MIC_W / 2, cy = MIC_Y + 11;
  int c = mic_on ? DIM : BG;
  if (!mic_on) scr.fillRoundRect(MIC_X, MIC_Y, MIC_W, MIC_H - 1, 3, INK);
  scr.fillRoundRect(cx - 3, cy - 8, 7, 12, 3, c);
  scr.drawArc(cx, cy, 8, 7, 0, 180, c);
  scr.drawFastVLine(cx, cy + 8, 4, c);
  scr.drawFastHLine(cx - 4, cy + 12, 9, c);
  if (!mic_on) {
    scr.drawLine(cx - 9, cy - 9, cx + 9, cy + 11, c);
    scr.drawLine(cx - 8, cy - 10, cx + 10, cy + 10, INK);   // a gap either side of the slash
    scr.drawLine(cx - 10, cy - 8, cx + 8, cy + 12, INK);
  }
}

// ---------- the terminal ----------
static void term_glyph(int x, uint8_t ch, uint32_t fg) {
  // one character into t_run at x: line drawing and blocks drawn, the rest
  // from the font. The lines run through x+2 and y 5 (of 6x12).
  int cx = x + 2, cy = 5;
  switch (ch) {
    case 0x80: t_run.drawFastHLine(x, cy, TW, fg); break;                                          // ─
    case 0x81: t_run.drawFastVLine(cx, 0, TH, fg); break;                                          // │
    case 0x82: t_run.drawFastHLine(cx, cy, TW - 2, fg); t_run.drawFastVLine(cx, cy, TH - cy, fg); break;  // ┌
    case 0x83: t_run.drawFastHLine(x, cy, 3, fg); t_run.drawFastVLine(cx, cy, TH - cy, fg); break;        // ┐
    case 0x84: t_run.drawFastHLine(cx, cy, TW - 2, fg); t_run.drawFastVLine(cx, 0, cy + 1, fg); break;    // └
    case 0x85: t_run.drawFastHLine(x, cy, 3, fg); t_run.drawFastVLine(cx, 0, cy + 1, fg); break;          // ┘
    case 0x86: t_run.drawFastVLine(cx, 0, TH, fg); t_run.drawFastHLine(cx, cy, TW - 2, fg); break;        // ├
    case 0x87: t_run.drawFastVLine(cx, 0, TH, fg); t_run.drawFastHLine(x, cy, 3, fg); break;              // ┤
    case 0x88: t_run.drawFastHLine(x, cy, TW, fg); t_run.drawFastVLine(cx, cy, TH - cy, fg); break;       // ┬
    case 0x89: t_run.drawFastHLine(x, cy, TW, fg); t_run.drawFastVLine(cx, 0, cy + 1, fg); break;         // ┴
    case 0x8A: t_run.drawFastHLine(x, cy, TW, fg); t_run.drawFastVLine(cx, 0, TH, fg); break;             // ┼
    case 0x8B: t_run.fillRect(x, 0, TW, TH, fg); break;                                            // █
    case 0x8C: t_run.fillRect(x, 0, TW, TH / 2, fg); break;                                        // ▀
    case 0x8D: t_run.fillRect(x, TH / 2, TW, TH / 2, fg); break;                                   // ▄
    case 0x8E: case 0x8F: case 0x90:                                                               // ░ ▒ ▓
      for (int y = 0; y < TH; y++)
        for (int i = 0; i < TW; i++) {
          bool on = ch == 0x8E ? (i % 2 == 0 && y % 2 == 0) : ch == 0x8F ? (i + y) % 2 == 0 : !(i % 2 && y % 2);
          if (on) t_run.drawPixel(x + i, y, fg);
        }
      break;
    case 0x91: t_run.fillRect(x, 0, TW / 2, TH, fg); break;                                        // ▌
    case 0x92: t_run.fillRect(x + TW / 2, 0, TW / 2, TH, fg); break;                               // ▐
    case 0x93: t_run.fillRect(cx, cy, 2, 2, fg); break;                                            // ·
    default:
      if (ch > 32 && ch < 127) {
        char b[2] = {(char)ch, 0};
        t_run.setTextColor(fg);
        t_run.drawString(b, x, 10);
      }
  }
}

static void term_draw(int r, int c0, int n) {
  // cells c0.. of row r (n of them), onto the panel
  if (r < 0 || r >= t_rows || !t_run.getBuffer()) return;
  n = min(n, t_cols - c0);
  if (c0 < 0 || n <= 0) return;
  for (int i = 0; i < n; i++) {
    int c = c0 + i;
    uint8_t at = t_at[r][c];
    uint32_t fg = ANSI[at & 15], bg = ANSI[at >> 4];
    if (cur_on && r == cur_r && c == cur_c) std::swap(fg, bg);
    t_run.fillRect(i * TW, 0, TW, TH, t_run.color888(bg >> 16, (bg >> 8) & 255, bg & 255));
    term_glyph(i * TW, t_ch[r][c], t_run.color888(fg >> 16, (fg >> 8) & 255, fg & 255));
  }
  int y0 = t_y0 + r * TH;
  if (scanlines) {
    // every other screen line darker, as on the other screens
    uint16_t *p = (uint16_t *)t_run.getBuffer();
    for (int y = 0; y < TH; y++) {
      if (!((y0 + y) & 1)) continue;
      uint16_t *q = p + y * t_run.width();
      for (int x = 0; x < n * TW; x++) {
        uint16_t v = __builtin_bswap16(q[x]);
        uint16_t R = ((v >> 11) & 31) * 7 / 10, G = ((v >> 5) & 63) * 7 / 10, B = (v & 31) * 7 / 10;
        q[x] = __builtin_bswap16((R << 11) | (G << 5) | B);
      }
    }
  }
  lcd.setClipRect(t_x0 + c0 * TW, y0, n * TW, TH);
  t_run.pushSprite(t_x0 + c0 * TW, y0);
  lcd.clearClipRect();
}

static void term_clear() {
  for (int r = 0; r < TMAXR; r++)
    for (int c = 0; c < TMAXC; c++) t_ch[r][c] = ' ', t_at[r][c] = 0x07;
}

static void term_full() {
  lcd.fillScreen(TFT_BLACK);
  for (int r = 0; r < t_rows; r++) term_draw(r, 0, t_cols);
  t_redraw = false;
}

static void draw() {
  if (booted && screen == TERM && !big.length()) {
    if (t_redraw) term_full();
    dirty = false;
    return;
  }
  scr.fillScreen(BG);
  if (!booted) draw_boot();
  else if (big.length()) draw_big();
  else {
    draw_bar();
    if (screen == HOME) draw_home();
    else if (screen == VOICE) draw_voice();
    else if (screen == MUSIC) draw_music();
    else draw_list();
    if (screen != VOICE) draw_mic();
  }
  if (scanlines) dim_scanlines();
  scr.pushSprite(0, 0);
  dirty = false;
}

static void show(int s) {
  if (s < 0) return;
  if (s == TERM) t_redraw = true;
  screen = (Screen)s;
  big = "";
  dirty = true;
}

// ---------- commands ----------
static void use_palette() {
  for (int i = 0; i < NCOL; i++) {
    uint32_t c = pal[i];
    uint8_t r = (c >> 16) & 0xff, g = (c >> 8) & 0xff, b = c & 0xff;
    scr.setPaletteColor(i, r, g, b);
    scr.setPaletteColor(i + 8, r * SCAN, g * SCAN, b * SCAN);
  }
}

static void calibrate() {
  uint16_t cal[8];
  lcd.fillScreen(pal[BG]);
  lcd.setTextColor(pal[INK], pal[BG]);
  lcd.setTextDatum(middle_center);
  lcd.setFont(FONTS[font_i].font);
  lcd.setTextSize(FONTS[font_i].size);
  lcd.drawString("TOUCH EACH CORNER", lcd.width() / 2, lcd.height() / 2);
  lcd.calibrateTouch(cal, pal[INK], pal[BG], 15);
  prefs.putBytes("cal", cal, sizeof(cal));
  Serial.print("cyd: calibrated");
  for (int i = 0; i < 8; i++) Serial.printf(" %u", cal[i]);
  Serial.println();
  dirty = true;
}

static String word_at(const String &s, int n, int *rest = nullptr) {
  // the nth space-separated word of s (and where the rest after it starts)
  int i = 0;
  for (int w = 0; w <= n; w++) {
    while (i < (int)s.length() && s[i] == ' ') i++;
    int j = s.indexOf(' ', i);
    if (j < 0) j = s.length();
    if (w == n) {
      if (rest) *rest = j < (int)s.length() ? j + 1 : s.length();
      return s.substring(i, j);
    }
    i = j;
  }
  if (rest) *rest = s.length();
  return "";
}

static void ok() { Serial.println("cyd: ok"); }

static void command(String line) {
  line.trim();
  String verb = word_at(line, 0);
  int rest;
  word_at(line, 0, &rest);
  String args = line.substring(rest);
  if (verb.length() && verb != "ping" && verb != "id") booted = true;

  if (verb == "ping") {
    Serial.println("cyd: pong");
  } else if (verb == "id") {
    auto p = lcd.getPanel();
    Serial.printf("cyd: id 04=%08lx d3=%08lx 09=%08lx\n",
                  (unsigned long)p->readCommand(0x04, 0, 4),
                  (unsigned long)p->readCommand(0xd3, 0, 4),
                  (unsigned long)p->readCommand(0x09, 0, 4));
  } else if (verb == "screen") {
    int s = screen_of(args);
    if (s < 0) { Serial.println("cyd: what? " + line); return; }
    show(s);
    ok();
  } else if (verb == "clear") {
    for (auto &r : rows) r = "";
    big = "";
    dirty = true;
    ok();
  } else if (verb == "text") {
    big = args;
    dirty = true;
    ok();
  } else if (verb == "row") {
    int n = word_at(line, 1).toInt();
    String col = word_at(line, 2, &rest);
    if (n < 0 || n >= ROWS || !col.length()) { Serial.println("cyd: what? " + line); return; }
    rows[n] = line.substring(rest);
    rows[n].toUpperCase();
    level[n] = level_of(col);
    big = "";
    if (screen == HOME) dirty = true;
    ok();
  } else if (verb == "line") {
    int s = screen_of(word_at(line, 1)), n = word_at(line, 2).toInt();
    String col = word_at(line, 3, &rest);
    if (s < 0 || n < 0 || n >= LINES || !col.length()) { Serial.println("cyd: what? " + line); return; }
    lines[s][n] = line.substring(rest);
    lines[s][n].toUpperCase();
    line_level[s][n] = level_of(col);
    if (screen == s) dirty = true;
    ok();
  } else if (verb == "sel") {
    int s = screen_of(word_at(line, 1));
    if (s < 0) { Serial.println("cyd: what? " + line); return; }
    sel[s] = word_at(line, 2).toInt();
    if (screen == s) dirty = true;
    ok();
  } else if (verb == "focus") {
    in_page = word_at(line, 1) == "page";
    dirty = true;
    ok();
  } else if (verb == "time") {
    t_base = atoll(word_at(line, 1).c_str()) + atol(word_at(line, 2).c_str());
    t_ms = millis();
    have_time = true;
    dirty = true;
    ok();
  } else if (verb == "weather") {
    w_now = word_at(line, 1);
    w_hi = word_at(line, 2);
    w_lo = word_at(line, 3);
    w_icon = word_at(line, 4, &rest);
    w_words = line.substring(rest);
    w_words.toUpperCase();
    have_weather = w_icon != "none" || w_now != "-";
    dirty = true;
    ok();
  } else if (verb == "lvl") {
    // (no reply: these come many times a second)
    lvl_in = constrain(word_at(line, 1).toInt(), 0, 100) / 100.0f;
    lvl_out = constrain(word_at(line, 2).toInt(), 0, 100) / 100.0f;
  } else if (verb == "said") {
    said = args;
    if (screen == VOICE) dirty = true;
    ok();
  } else if (verb == "cmd") {
    cmd = args;
    if (screen == VOICE) dirty = true;
    ok();
  } else if (verb == "np") {
    String what = word_at(line, 1, &rest);
    String v = line.substring(rest);
    if (what == "none") np_have = false;
    else if (what == "song") np_song = v, np_have = true;
    else if (what == "artist") np_artist = v;
    else if (what == "album") np_album = v;
    else if (what == "from") np_from = v;
    else if (what == "at") {
      np_playing = word_at(line, 2) == "1";
      np_pos = word_at(line, 3).toFloat();
      np_len = word_at(line, 4).toFloat();
      np_ms = millis();
    } else { Serial.println("cyd: what? " + line); return; }
    if (screen == MUSIC) dirty = true;
    ok();
  } else if (verb == "T") {
    // (no reply: these come thick and fast)
    int r = word_at(line, 1).toInt(), c = word_at(line, 2).toInt();
    String fb = word_at(line, 3, &rest);
    int end = line.lastIndexOf('|');
    if (r < 0 || r >= TMAXR || c < 0 || c >= TMAXC || fb.length() != 2 || end < rest) return;
    uint8_t at = (uint8_t)strtol(fb.substring(0, 1).c_str(), nullptr, 16) |
                 (uint8_t)strtol(fb.substring(1, 2).c_str(), nullptr, 16) << 4;
    int n = 0;
    for (int i = rest; i < end && c + n < TMAXC; i++, n++) t_ch[r][c + n] = (uint8_t)line[i], t_at[r][c + n] = at;
    if (screen == TERM && !t_redraw) term_draw(r, c, n);
  } else if (verb == "TC") {
    int r0 = cur_r, c0 = cur_c;
    cur_r = word_at(line, 1).toInt(), cur_c = word_at(line, 2).toInt();
    if (screen == TERM && !t_redraw && (r0 != cur_r || c0 != cur_c)) {
      bool was = cur_on;
      cur_on = false;
      term_draw(r0, c0, 1);
      cur_on = was;
      term_draw(cur_r, cur_c, 1);
    }
  } else if (verb == "tsize") {
    t_rows = constrain(word_at(line, 1).toInt(), 1, TMAXR);
    t_cols = constrain(word_at(line, 2).toInt(), 1, TMAXC);
    t_x0 = (320 - t_cols * TW) / 2, t_y0 = (240 - t_rows * TH) / 2;
    term_clear();
    t_redraw = true;
    if (screen == TERM) dirty = true;
    ok();
  } else if (line == "mic on" || line == "mic off") {
    mic_on = line == "mic on";
    dirty = true;
    ok();
  } else if (verb == "palette") {
    uint32_t p[NCOL];
    for (int i = 0; i < NCOL; i++) {
      String w = word_at(line, i + 1);
      if (w.length() != 6) { Serial.println("cyd: what? " + line); return; }
      p[i] = strtoul(w.c_str(), nullptr, 16);
    }
    if (memcmp(p, pal, sizeof(pal))) {
      memcpy(pal, p, sizeof(pal));
      prefs.putBytes("pal", pal, sizeof(pal));
      use_palette();
      dirty = true;
    }
    ok();
  } else if (verb == "font") {
    String w = word_at(line, 1);
    int i = 0;
    while (i < NFONTS && w != FONTS[i].name) i++;
    if (i == NFONTS) { Serial.println("cyd: what? " + line); return; }
    if (i != font_i) {
      use_font(i);
      prefs.putUChar("font", i);
      dirty = true;
    }
    ok();
  } else if (line == "scan on" || line == "scan off") {
    scanlines = line == "scan on";
    dirty = true;
    ok();
  } else if (verb == "bright") {
    bl_target = constrain(word_at(line, 1).toInt(), 0, 255);
    ok();
  } else if (verb == "auto") {
    bl_auto = word_at(line, 1) == "on";
    if (word_at(line, 2).length()) light_lit = word_at(line, 2).toInt();
    if (word_at(line, 3).length()) light_dark = word_at(line, 3).toInt();
    ok();
  } else if (verb == "calibrate") {
    calibrate();
  } else if (line.length()) {
    Serial.println("cyd: what? " + line);
  }
}

static void backlight() {
  static uint32_t last = 0, last_read = 0, last_report = 0;
  static int set = -1;
  uint32_t now = millis();
  if (now - last < 40) return;
  last = now;
  if (now - last_read >= 200) {
    int raw = analogRead(LDR);
    light = light < 0 ? raw : light * 0.9f + raw * 0.1f;
    last_read = now;
  }
  if (now - last_report >= 5000) {
    Serial.printf("cyd: light %d\n", (int)light);
    last_report = now;
  }
  float want = bl_target;
  if (bl_auto && bl_target > 0 && light_dark != light_lit) {
    float k = constrain((light - light_lit) / (float)(light_dark - light_lit), 0.0f, 1.0f);
    want = max(8.0f, bl_target * (1 - 0.7f * k));
  }
  bl_now += (want - bl_now) * 0.15f;
  if (fabs(want - bl_now) < 0.5f) bl_now = want;
  int v = (int)roundf(bl_now);
  if (v != set) {
    lcd.setBrightness(v);
    set = v;
  }
}

static void tapped(int x, int y) {
  // the icon bar switches screens here and now; a list line is the Pi's to act on
  if (!booted || big.length()) return;
  if (screen == TERM) {
    Serial.println("cyd: tap term");
  } else if (screen != VOICE && x >= MIC_X - 6 && y >= MIC_Y - 6) {
    Serial.println("cyd: tap mic");
  } else if (x < BAR_W + 4) {
    int i = y / CELL_H;
    if (i < NBAR) {
      show(i);
      Serial.printf("cyd: screen %s\n", NAMES[i]);
    }
  } else if (screen == MUSIC) {
    if (np_have) Serial.println("cyd: tap music 0");
  } else if (screen != HOME && screen != VOICE && y >= LIST_Y - 1) {
    int n = (y - LIST_Y + 1) / LINE_H;
    if (n < LINES && lines[screen][n].length()) Serial.printf("cyd: tap %s %d\n", NAMES[screen], n);
  }
}

static void touches() {
  static bool down = false;
  static int32_t lx, ly;
  static uint32_t last_ms = 0, last_move = 0;
  uint32_t now = millis();
  if (now - last_ms < 20) return;
  last_ms = now;
  int32_t x, y;
  bool on = lcd.getTouch(&x, &y);
  if (on && !down) {
    Serial.printf("cyd: touch %ld %ld\n", (long)x, (long)y);
    lx = x, ly = y, last_move = now;
    tapped(x, y);
  } else if (on && (abs(x - lx) > 4 || abs(y - ly) > 4) && now - last_move >= 50) {
    Serial.printf("cyd: move %ld %ld\n", (long)x, (long)y);
    lx = x, ly = y, last_move = now;
  } else if (!on && down) {
    Serial.printf("cyd: release %ld %ld\n", (long)lx, (long)ly);
  }
  down = on;
}

void setup() {
  Serial.begin(115200);
  prefs.begin("cyd");
  lcd.init();
  lcd.setRotation(3);  // landscape, 320 wide; 3 not 1, as she mounts it (micro USB in)
  uint16_t cal[8];
  if (prefs.getBytes("cal", cal, sizeof(cal)) == sizeof(cal)) lcd.setTouchCalibrate(cal);
  lcd.setBrightness(0);    // fades up in loop()
  scr.setColorDepth(lgfx::palette_4bit);
  if (!scr.createSprite(320, 240)) Serial.println("cyd: no memory for the screen");
  if (prefs.getBytes("pal", pal, sizeof(pal)) != sizeof(pal)) prefs.putBytes("pal", pal, sizeof(pal));
  use_palette();
  use_font(min((int)prefs.getUChar("font", 0), NFONTS - 1));
  for (auto &s : sel) s = -1;
  t_run.setColorDepth(16);
  if (!t_run.createSprite(TMAXC * TW, TH)) Serial.println("cyd: no memory for the terminal");
  t_run.setFont(&Terminus12);
  t_run.setTextDatum(baseline_left);
  term_clear();
  draw();
  Serial.println("cyd: hello");
}

void loop() {
  static String line;
  static uint32_t last_half = 0, last_frame = 0;
  while (Serial.available()) {
    char c = Serial.read();
    if (c == '\n') {
      command(line);
      line = "";
    } else if (c != '\r' && line.length() < 200) {
      line += c;
    }
  }
  touches();
  backlight();
  uint32_t now = millis();
  if (booted && screen == VOICE && !big.length()) {
    // the waveforms: about 25 frames a second
    if (now - last_frame >= 40) {
      wave_in.step(lvl_in);
      wave_out.step(lvl_out);
      draw();
      last_frame = now;
    }
  } else if (booted && screen == TERM && !big.length()) {
    // the cursor blinks; the rest is drawn as it comes
    if (dirty) draw();
    if (now / 500 != last_half) {
      last_half = now / 500;
      cur_on = !cur_on;
      term_draw(cur_r, cur_c, 1);
    }
  } else if (booted && screen == MUSIC && np_have && np_playing && !big.length()) {
    // the reels turning: about 15 frames a second
    if (now - last_frame >= 66) {
      turn_reels(min(0.1f, (now - last_frame) / 1000.0f));
      draw();
      last_frame = now;
    }
  } else {
    // otherwise on any change, and twice a second for the clock and cursor
    uint32_t half = now / 500;
    if (dirty || half != last_half) {
      draw();
      last_half = half;
    }
  }
}
