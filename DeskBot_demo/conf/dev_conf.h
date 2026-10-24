#ifndef _DEV_CONF_H
#define _DEV_CONF_H

#ifdef __cplusplus
extern "C" {
#endif

#define LV_USE_SIMULATOR 1

/*
 * AIChat 服务端地址模式，切换时只改这一行：
 *   0 = 本地模式，连 127.0.0.1（服务端在本虚拟机运行）
 *   1 = 云端模式，连 VPS（服务端在 107.150.26.37 运行）
 * 修改后需要重新编译。
 */
#define ECHOMATE_SERVER_MODE_CLOUD 1

#if ECHOMATE_SERVER_MODE_CLOUD
    #define ECHOMATE_SERVER_URL "107.150.26.37"
#else
    #define ECHOMATE_SERVER_URL "127.0.0.1"
#endif

#define ECHOMATE_SERVER_PORT 8000

#if LV_USE_SIMULATOR
    #define LV_USE_LINUX_FBDEV 0
    #define LV_USE_EVDEV 0
    #define LV_USE_SDL 1
#else
    #define LV_USE_LINUX_FBDEV 1
    #define LV_USE_EVDEV 1
    #define LV_USE_SDL 0
#endif

#ifdef __cplusplus
} /*extern "C"*/
#endif

#endif