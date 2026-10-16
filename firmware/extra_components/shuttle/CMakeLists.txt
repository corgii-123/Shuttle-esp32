# Shuttle (搞快) component: Kconfig-backed configuration + TCP<->WebSocket ADB byte bridge.
# Pure ESP-IDF code; does not depend on xiaozhi's main component.
idf_component_register(
    SRCS "shuttle_config.cc" "adb_bridge.cc" "phone_scanner.cc"
    INCLUDE_DIRS "include"
    REQUIRES esp_event esp_netif
    PRIV_REQUIRES nvs_flash lwip mbedtls esp_psram esp_timer log
)
target_compile_options(${COMPONENT_LIB} PRIVATE $<$<COMPILE_LANGUAGE:CXX>:-std=gnu++23>)
