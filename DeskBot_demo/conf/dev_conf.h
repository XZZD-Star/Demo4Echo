#ifndef _DEV_CONF_H
#define _DEV_CONF_H

#ifdef __cplusplus
extern "C" {
#endif

#if defined(__arm__) || defined(__aarch64__)
    #define LV_USE_SIMULATOR 0
#else
    #define LV_USE_SIMULATOR 1
#endif

/* 当前阶段只使用核心板扩展传感器，关闭电机控制以释放 H1 引脚。 */
#define ENABLE_MOTOR 0
#define ENABLE_SENSORS 1

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

/* 板端传感器默认参数，可在启动前用同名环境变量覆盖。 */
#define ECHOMATE_I2C_DEVICE "/dev/i2c-0"
#define ECHOMATE_PIR_GPIO 33
#define ECHOMATE_PIR_WARMUP_SECONDS 60
#define ECHOMATE_PIR_REARM_SECONDS 60
#define ECHOMATE_PIR_COOLDOWN_SECONDS 180

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
