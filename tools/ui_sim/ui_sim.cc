// Desktop renderer for the Shuttle e-paper UI.
// Runs the real firmware UI code (shuttle_ui.cc + mascot) on LVGL 9.5 with the device fonts,
// converts every frame to black/white exactly like the Waveshare e-paper driver
// (RGB565 value < 0x7fff -> black) and writes PGM files that render.py turns into PNG/GIF.
#include <lvgl.h>
#include <material_symbols.h>

#include <cstdio>
#include <cstdlib>
#include <string>
#include <vector>

#include "shuttle_ui.h"

#include <cstring>

LV_FONT_DECLARE(font_noto_sans_basic_14_1);
LV_FONT_DECLARE(font_noto_sans_basic_20_4);
LV_FONT_DECLARE(font_material_symbols_14_1);

static constexpr int kW = 200, kH = 200;
static uint16_t g_draw_buf[kW * kH];
static uint8_t g_fb[kW * kH];  // 1 = black
static uint32_t g_now = 0;
static std::string g_out;

static uint32_t TickCb() { return g_now; }

static void FlushCb(lv_display_t* disp, const lv_area_t* area, uint8_t* px) {
    auto* p = reinterpret_cast<uint16_t*>(px);
    for (int y = area->y1; y <= area->y2; y++) {
        for (int x = area->x1; x <= area->x2; x++) {
            g_fb[y * kW + x] = (*p < 0x7fff) ? 1 : 0;  // same rule as custom_lcd_display.cc
            p++;
        }
    }
    lv_display_flush_ready(disp);
}

static void Save(const std::string& name) {
    lv_refr_now(nullptr);
    std::string path = g_out + "/" + name + ".pgm";
    FILE* f = fopen(path.c_str(), "wb");
    fprintf(f, "P5\n%d %d\n255\n", kW, kH);
    for (int i = 0; i < kW * kH; i++) {
        fputc(g_fb[i] ? 0 : 255, f);
    }
    fclose(f);
    printf("  %s\n", path.c_str());
}

static void Advance(ShuttleUi& ui, uint32_t ms) {
    for (uint32_t t = 0; t < ms; t += 100) {
        g_now += 100;
        lv_timer_handler();
    }
}

template <typename T>
static T Rd(const uint8_t* p) {
    T v;
    memcpy(&v, p, sizeof(T));
    return v;
}

// xiaozhi "cbin" fonts are lv_font_t images laid out for 32-bit MCUs (pointers stored as
// 4-byte offsets). The firmware loads them with cbin_font_create(); on a 64-bit desktop we
// parse the same layout explicitly. Kerning is skipped (irrelevant for CJK).
static const lv_font_t* LoadCbin(const char* path) {
    FILE* f = fopen(path, "rb");
    if (f == nullptr) {
        return nullptr;
    }
    fseek(f, 0, SEEK_END);
    long n = ftell(f);
    fseek(f, 0, SEEK_SET);
    auto* bin = static_cast<uint8_t*>(malloc(n));
    fread(bin, 1, n, f);
    fclose(f);

    auto* font = static_cast<lv_font_t*>(calloc(1, sizeof(lv_font_t)));
    font->get_glyph_dsc = lv_font_get_glyph_dsc_fmt_txt;
    font->get_glyph_bitmap = lv_font_get_bitmap_fmt_txt;
    font->line_height = Rd<int32_t>(bin + 12);
    font->base_line = Rd<int32_t>(bin + 16);
    uint8_t flags = bin[20];
    font->subpx = flags & 0x3;
    font->underline_position = static_cast<int8_t>(bin[21]);
    font->underline_thickness = static_cast<int8_t>(bin[22]);

    const uint8_t* d = bin + Rd<uint32_t>(bin + 24);
    auto* dsc = static_cast<lv_font_fmt_txt_dsc_t*>(calloc(1, sizeof(lv_font_fmt_txt_dsc_t)));
    dsc->glyph_bitmap = d + Rd<uint32_t>(d + 0);
    dsc->glyph_dsc = reinterpret_cast<const lv_font_fmt_txt_glyph_dsc_t*>(d + Rd<uint32_t>(d + 4));
    uint16_t bits = Rd<uint16_t>(d + 18);
    dsc->kern_scale = Rd<uint16_t>(d + 16);
    dsc->cmap_num = bits & 0x1ff;
    dsc->bpp = (bits >> 9) & 0xf;
    dsc->kern_classes = 0;
    dsc->bitmap_format = (bits >> 14) & 0x3;
    dsc->stride = d[20];
    dsc->kern_dsc = nullptr;

    const uint8_t* c = d + Rd<uint32_t>(d + 8);
    auto* cmaps = static_cast<lv_font_fmt_txt_cmap_t*>(calloc(dsc->cmap_num, sizeof(lv_font_fmt_txt_cmap_t)));
    const uint8_t* cur = c;
    for (int i = 0; i < dsc->cmap_num; i++, cur += 20) {
        cmaps[i].range_start = Rd<uint32_t>(cur);
        cmaps[i].range_length = Rd<uint16_t>(cur + 4);
        cmaps[i].glyph_id_start = Rd<uint16_t>(cur + 6);
        uint32_t ul = Rd<uint32_t>(cur + 8), go = Rd<uint32_t>(cur + 12);
        cmaps[i].unicode_list = ul ? reinterpret_cast<const uint16_t*>(c + ul) : nullptr;
        cmaps[i].glyph_id_ofs_list = go ? c + go : nullptr;
        cmaps[i].list_length = Rd<uint16_t>(cur + 16);
        cmaps[i].type = static_cast<lv_font_fmt_txt_cmap_type_t>(cur[18]);
    }
    dsc->cmaps = cmaps;
    font->dsc = dsc;
    return font;
}

int main(int argc, char** argv) {
    if (argc < 3) {
        fprintf(stderr, "usage: ui_sim <font_noto_sans_common_20_4.bin> <outdir>\n");
        return 2;
    }
    g_out = argv[2];
    lv_init();
    lv_tick_set_cb(TickCb);
    lv_display_t* disp = lv_display_create(kW, kH);
    lv_display_set_color_format(disp, LV_COLOR_FORMAT_RGB565);
    lv_display_set_buffers(disp, g_draw_buf, nullptr, sizeof(g_draw_buf), LV_DISPLAY_RENDER_MODE_FULL);
    lv_display_set_flush_cb(disp, FlushCb);

    const lv_font_t* text = LoadCbin(argv[1]);
    if (text == nullptr) {
        fprintf(stderr, "cannot load %s, falling back to the builtin basic font\n", argv[1]);
        text = &font_noto_sans_basic_20_4;
    }
    ShuttleUi ui;
    ui.Create(lv_screen_active(), kW, kH, {text, &font_noto_sans_basic_14_1, &font_material_symbols_14_1});

    auto status_bar = [&](bool phone, bool server, bool home = false) {
        ui.SetClock("09:41");
        ui.SetBattery(82, false);
        ui.SetWifiIcon(MATERIAL_SYMBOLS_WIFI);
        ui.SetHomeNetwork(home);
        ui.SetPhoneConnected(phone);
        ui.SetServerConnected(server);
    };

    printf("rendering to %s\n", g_out.c_str());
    status_bar(true, true);
    ui.SetMood(ShuttleUi::Mood::kIdle, "待命", "Xiaomi 14");
    ui.SetMessage("按住 BOOT 说话");
    Save("01_idle");
    Advance(ui, 91000);
    Save("01_idle_blink");

    ui.SetMood(ShuttleUi::Mood::kConnecting, nullptr, "");
    ui.SetMessage("");
    Save("02_connecting");

    ui.SetMood(ShuttleUi::Mood::kListening, nullptr, "松开发送");
    Save("03_listening_0");
    Advance(ui, 700);
    Save("03_listening_1");

    ui.SetMood(ShuttleUi::Mood::kThinking, nullptr, "识别中…");
    Save("04_thinking_0");
    ui.SetSubtitle("");
    ui.SetMessage("「打开微信，给张三发消息说晚上一起吃饭」");
    Advance(ui, 900);
    Save("04_thinking_1");
    Advance(ui, 900);
    Save("04_thinking_2");

    ui.SetMood(ShuttleUi::Mood::kActing, nullptr, "第 3 步");
    ui.SetMessage("点击 搜索框，输入“张三”");
    Save("05_acting_0");
    Advance(ui, 900);
    Save("05_acting_1");

    ui.SetMood(ShuttleUi::Mood::kDone, nullptr, "");
    ui.SetMessage("好了，已经给张三发了消息：晚上一起吃饭");
    Save("06_done");

    ui.SetMood(ShuttleUi::Mood::kSpeaking, "完成", nullptr);
    Save("07_speaking_0");
    Advance(ui, 600);
    Save("07_speaking_1");

    ui.SetMood(ShuttleUi::Mood::kError, nullptr, "");
    ui.SetMessage("没听清，请再说一遍");
    Save("08_error");

    status_bar(false, true);
    ui.SetMood(ShuttleUi::Mood::kOffline, "待命", "未发现手机");
    ui.SetMessage("手机连同一 Wi-Fi，执行 adb tcpip 5555");
    Save("09_offline");

    status_bar(false, false);
    ui.SetMood(ShuttleUi::Mood::kOffline, "待命", "服务器断开");
    ui.SetMessage("检查网络或服务器地址");
    ui.SetBattery(15, false);
    Save("10_no_server");

    status_bar(true, true, true);
    ui.SetMood(ShuttleUi::Mood::kIdle, "待命", "Pixel 8 +1");
    ui.SetMessage("按住 BOOT 说话");
    Save("12_home_wifi");

    status_bar(false, true);
    ui.SetMood(ShuttleUi::Mood::kOffline, "待命", "搜索手机");
    ui.SetMessage("正在找开了无线调试的手机");
    Save("13_scanning");

    status_bar(true, true, true);
    ui.SetMood(ShuttleUi::Mood::kDone, nullptr, "");
    ui.SetMessage("好的，换到Pixel 8");
    Save("14_switched");

    status_bar(true, true);
    ui.SetMood(ShuttleUi::Mood::kIdle, "待命", "Xiaomi 14");
    ui.SetMessage("");
    Advance(ui, 601000);
    Save("11_sleep");
    printf("done\n");
    return 0;
}
