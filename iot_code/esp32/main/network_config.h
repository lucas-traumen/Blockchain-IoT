#pragma once
#include "sdkconfig.h"

/* Native tests may select a fixture without loading the user's credentials. */
#ifdef HRC_NETWORK_CONFIG_HEADER
#include HRC_NETWORK_CONFIG_HEADER
#elif __has_include("network_config.local.h")
#include "network_config.local.h"
#endif

/* Repositories without a local header retain the Kconfig/NVS setup path. */
#ifndef HRC_WIFI_LIST_MODE
#define HRC_WIFI_LIST_MODE 0
#endif
#ifndef HRC_WIFI_NETWORKS
#define HRC_WIFI_NETWORKS {{CONFIG_HRC_WIFI_SSID, CONFIG_HRC_WIFI_PASS}}
#endif
#ifndef HRC_MQTT_URI
#define HRC_MQTT_URI CONFIG_HRC_MQTT_URI
#endif
