#include "sensor_manager.h"

#include "../gpio_manager/gpio_manager.h"
#include "../sys_manager/sys_manager.h"
#include "../../conf/dev_conf.h"

#include <errno.h>
#include <fcntl.h>
#include <linux/i2c-dev.h>
#include <math.h>
#include <pthread.h>
#include <signal.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/ioctl.h>
#include <time.h>
#include <unistd.h>

#define BH1750_ADDR_PRIMARY 0x23
#define BH1750_ADDR_ALT 0x5C
#define BMP280_ADDR_PRIMARY 0x76
#define BMP280_ADDR_ALT 0x77
#define BMP280_CHIP_ID 0x58

#define SENSOR_POLL_MS 100
#define LUX_READ_INTERVAL_MS 1000
#define LUX_FILTER_SAMPLES 5
#define LUX_STABLE_SAMPLES 5
#define LUX_MIN_ADJUST_INTERVAL_MS 8000
#define ENV_READ_INTERVAL_MS 2000
#define SENSOR_LOG_INTERVAL_MS 5000
#define SENSOR_RETRY_INTERVAL_MS 5000
#define PIR_DEBOUNCE_MS 300

typedef struct {
    uint16_t dig_T1;
    int16_t dig_T2;
    int16_t dig_T3;
    uint16_t dig_P1;
    int16_t dig_P2;
    int16_t dig_P3;
    int16_t dig_P4;
    int16_t dig_P5;
    int16_t dig_P6;
    int16_t dig_P7;
    int16_t dig_P8;
    int16_t dig_P9;
    int32_t t_fine;
} bmp280_calib_t;

static pthread_t sensor_thread;
static pthread_mutex_t sensor_mutex = PTHREAD_MUTEX_INITIALIZER;
static volatile sig_atomic_t sensor_running = 0;
static bool sensor_started = false;
static sensor_data_t latest_data;
static sensor_pir_callback_t pir_callback;
static void *pir_callback_user_data;
static bool auto_brightness = true;

static int bh1750_fd = -1;
static int bh1750_addr = BH1750_ADDR_PRIMARY;
static int bmp280_fd = -1;
static int bmp280_addr = BMP280_ADDR_PRIMARY;
static bmp280_calib_t bmp280_calib;
static bool pir_exported = false;

static uint64_t now_ms(void)
{
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return (uint64_t)ts.tv_sec * 1000ULL + (uint64_t)ts.tv_nsec / 1000000ULL;
}

static float env_float(const char *name, float fallback)
{
    const char *value = getenv(name);
    if (value == NULL || value[0] == '\0') {
        return fallback;
    }

    char *end = NULL;
    float parsed = strtof(value, &end);
    if (end == value || !isfinite(parsed)) {
        return fallback;
    }
    return parsed;
}

static int env_int(const char *name, int fallback)
{
    const char *value = getenv(name);
    if (value == NULL || value[0] == '\0') {
        return fallback;
    }

    char *end = NULL;
    long parsed = strtol(value, &end, 10);
    if (end == value || parsed < 0 || parsed > INT32_MAX) {
        return fallback;
    }
    return (int)parsed;
}

static bool use_mock_backend(void)
{
    const char *value = getenv("ECHOMATE_SENSOR_MOCK");
    if (value != NULL && value[0] != '\0') {
        return atoi(value) != 0;
    }
    return LV_USE_SIMULATOR != 0;
}

static uint16_t read_u16_le(const uint8_t *data)
{
    return (uint16_t)data[0] | ((uint16_t)data[1] << 8);
}

static int16_t read_s16_le(const uint8_t *data)
{
    return (int16_t)read_u16_le(data);
}

static int i2c_write_bytes(int fd, int addr, const uint8_t *data, size_t len)
{
    if (fd < 0 || ioctl(fd, I2C_SLAVE, addr) < 0) {
        return -1;
    }
    return write(fd, data, len) == (ssize_t)len ? 0 : -1;
}

static int i2c_read_bytes(int fd, int addr, uint8_t *data, size_t len)
{
    if (fd < 0 || ioctl(fd, I2C_SLAVE, addr) < 0) {
        return -1;
    }
    return read(fd, data, len) == (ssize_t)len ? 0 : -1;
}

static int i2c_read_register(int fd, int addr, uint8_t reg,
                             uint8_t *data, size_t len)
{
    if (i2c_write_bytes(fd, addr, &reg, 1) != 0) {
        return -1;
    }
    return i2c_read_bytes(fd, addr, data, len);
}

static int open_i2c_device(void)
{
    const char *device = getenv("ECHOMATE_I2C_DEVICE");
    if (device == NULL || device[0] == '\0') {
        device = ECHOMATE_I2C_DEVICE;
    }

    int fd = open(device, O_RDWR);
    if (fd < 0) {
        printf("[SENSOR] open %s failed: %s\n", device, strerror(errno));
    }
    return fd;
}

static int bh1750_init(void)
{
    int fd = open_i2c_device();
    if (fd < 0) {
        return -1;
    }

    const int addresses[] = {BH1750_ADDR_PRIMARY, BH1750_ADDR_ALT};
    for (size_t i = 0; i < sizeof(addresses) / sizeof(addresses[0]); ++i) {
        uint8_t command = 0x01;
        if (i2c_write_bytes(fd, addresses[i], &command, 1) == 0) {
            command = 0x10;
            if (i2c_write_bytes(fd, addresses[i], &command, 1) == 0) {
                bh1750_fd = fd;
                bh1750_addr = addresses[i];
                usleep(180000);
                printf("[SENSOR] BH1750 ready at 0x%02X\n", bh1750_addr);
                return 0;
            }
        }
    }

    close(fd);
    return -1;
}

static int bh1750_read(float *lux)
{
    uint8_t raw[2];
    if (i2c_read_bytes(bh1750_fd, bh1750_addr, raw, sizeof(raw)) != 0) {
        return -1;
    }

    uint16_t value = ((uint16_t)raw[0] << 8) | raw[1];
    *lux = (float)value / 1.2f;
    return 0;
}

static int bmp280_load_calibration(int fd, int addr)
{
    uint8_t data[24];
    if (i2c_read_register(fd, addr, 0x88, data, sizeof(data)) != 0) {
        return -1;
    }

    bmp280_calib.dig_T1 = read_u16_le(&data[0]);
    bmp280_calib.dig_T2 = read_s16_le(&data[2]);
    bmp280_calib.dig_T3 = read_s16_le(&data[4]);
    bmp280_calib.dig_P1 = read_u16_le(&data[6]);
    bmp280_calib.dig_P2 = read_s16_le(&data[8]);
    bmp280_calib.dig_P3 = read_s16_le(&data[10]);
    bmp280_calib.dig_P4 = read_s16_le(&data[12]);
    bmp280_calib.dig_P5 = read_s16_le(&data[14]);
    bmp280_calib.dig_P6 = read_s16_le(&data[16]);
    bmp280_calib.dig_P7 = read_s16_le(&data[18]);
    bmp280_calib.dig_P8 = read_s16_le(&data[20]);
    bmp280_calib.dig_P9 = read_s16_le(&data[22]);
    return 0;
}

static int bmp280_init(void)
{
    int fd = open_i2c_device();
    if (fd < 0) {
        return -1;
    }

    const int addresses[] = {BMP280_ADDR_PRIMARY, BMP280_ADDR_ALT};
    for (size_t i = 0; i < sizeof(addresses) / sizeof(addresses[0]); ++i) {
        uint8_t chip_id = 0;
        if (i2c_read_register(fd, addresses[i], 0xD0, &chip_id, 1) == 0
            && chip_id == BMP280_CHIP_ID
            && bmp280_load_calibration(fd, addresses[i]) == 0) {
            bmp280_fd = fd;
            bmp280_addr = addresses[i];
            printf("[SENSOR] BMP280 ready at 0x%02X\n", bmp280_addr);
            return 0;
        }
    }

    close(fd);
    return -1;
}

static int32_t bmp280_compensate_temperature(int32_t adc_temperature)
{
    int32_t var1 = ((((adc_temperature >> 3)
                      - ((int32_t)bmp280_calib.dig_T1 << 1)))
                    * (int32_t)bmp280_calib.dig_T2) >> 11;
    int32_t var2 = (((((adc_temperature >> 4)
                      - (int32_t)bmp280_calib.dig_T1)
                      * ((adc_temperature >> 4)
                         - (int32_t)bmp280_calib.dig_T1)) >> 12)
                    * (int32_t)bmp280_calib.dig_T3) >> 14;
    bmp280_calib.t_fine = var1 + var2;
    return (bmp280_calib.t_fine * 5 + 128) >> 8;
}

static uint32_t bmp280_compensate_pressure(int32_t adc_pressure)
{
    int64_t var1 = ((int64_t)bmp280_calib.t_fine) - 128000;
    int64_t var2 = var1 * var1 * (int64_t)bmp280_calib.dig_P6;
    var2 += (var1 * (int64_t)bmp280_calib.dig_P5) << 17;
    var2 += ((int64_t)bmp280_calib.dig_P4) << 35;
    var1 = ((var1 * var1 * (int64_t)bmp280_calib.dig_P3) >> 8)
           + ((var1 * (int64_t)bmp280_calib.dig_P2) << 12);
    var1 = (((((int64_t)1) << 47) + var1)
            * (int64_t)bmp280_calib.dig_P1) >> 33;
    if (var1 == 0) {
        return 0;
    }

    int64_t pressure = 1048576 - adc_pressure;
    pressure = (((pressure << 31) - var2) * 3125) / var1;
    var1 = ((int64_t)bmp280_calib.dig_P9 * (pressure >> 13)
            * (pressure >> 13)) >> 25;
    var2 = ((int64_t)bmp280_calib.dig_P8 * pressure) >> 19;
    pressure = ((pressure + var1 + var2) >> 8)
               + ((int64_t)bmp280_calib.dig_P7 << 4);
    return (uint32_t)pressure;
}

static int bmp280_read(float *temperature_c, float *pressure_hpa)
{
    uint8_t command[2] = {0xF4, 0x25};
    if (i2c_write_bytes(bmp280_fd, bmp280_addr,
                        command, sizeof(command)) != 0) {
        return -1;
    }
    usleep(100000);

    uint8_t data[6];
    if (i2c_read_register(bmp280_fd, bmp280_addr,
                          0xF7, data, sizeof(data)) != 0) {
        return -1;
    }

    int32_t adc_pressure = ((int32_t)data[0] << 12)
                         | ((int32_t)data[1] << 4)
                         | (data[2] >> 4);
    int32_t adc_temperature = ((int32_t)data[3] << 12)
                            | ((int32_t)data[4] << 4)
                            | (data[5] >> 4);
    int32_t temperature = bmp280_compensate_temperature(adc_temperature);
    uint32_t pressure = bmp280_compensate_pressure(adc_pressure);
    if (pressure == 0) {
        return -1;
    }

    *temperature_c = (float)temperature / 100.0f;
    *pressure_hpa = (float)pressure / 25600.0f;
    return 0;
}

static void pir_init(void)
{
    if (!pir_exported) {
        int result = gpio_export(ECHOMATE_PIR_GPIO);
        if (result == 0 || result == EBUSY) {
            pir_exported = true;
        }
    }
    gpio_set_direction(ECHOMATE_PIR_GPIO, IN_DIRECTION);
}

static int normal_light_band(float lux)
{
    if (lux < 80.0f) {
        return 0;
    }
    if (lux < 300.0f) {
        return 1;
    }
    return 2;
}

static int map_lux_to_band(float lux, int current_band)
{
    int band = normal_light_band(lux);
    if (current_band < 0) {
        return band;
    }

    /* Add hysteresis around the two boundaries. */
    switch (current_band) {
    case 0:
        return lux < 100.0f ? 0 : band;
    case 1:
        if (lux <= 60.0f) {
            return 0;
        }
        return lux >= 350.0f ? band : 1;
    default:
        return lux <= 280.0f ? band : 2;
    }
}

static int brightness_for_band(int band)
{
    static const int brightness[] = {85, 50, 20};
    if (band < 0 || band >= (int)(sizeof(brightness) / sizeof(brightness[0]))) {
        return 45;
    }
    return brightness[band];
}

static float median_lux(const float *samples, size_t count)
{
    float sorted[LUX_FILTER_SAMPLES];
    if (count == 0) {
        return 0.0f;
    }
    if (count > LUX_FILTER_SAMPLES) {
        count = LUX_FILTER_SAMPLES;
    }

    memcpy(sorted, samples, count * sizeof(sorted[0]));
    for (size_t i = 1; i < count; ++i) {
        float value = sorted[i];
        size_t j = i;
        while (j > 0 && sorted[j - 1] > value) {
            sorted[j] = sorted[j - 1];
            --j;
        }
        sorted[j] = value;
    }
    return sorted[count / 2];
}

static void update_auto_brightness(float lux)
{
    static int candidate = -1;
    static int candidate_count = 0;
    static int applied = -1;
    static int applied_band = -1;
    static uint64_t last_adjust = 0;

    pthread_mutex_lock(&sensor_mutex);
    bool enabled = auto_brightness;
    pthread_mutex_unlock(&sensor_mutex);
    if (!enabled) {
        return;
    }

    int band = map_lux_to_band(lux, applied_band);
    int target = brightness_for_band(band);
    if (target != candidate) {
        candidate = target;
        candidate_count = 1;
        return;
    }
    if (candidate_count < LUX_STABLE_SAMPLES) {
        candidate_count++;
        return;
    }

    uint64_t current = now_ms();
    if (last_adjust != 0
        && current - last_adjust < LUX_MIN_ADJUST_INTERVAL_MS) {
        return;
    }
    if (target != applied) {
        if (sys_set_lcd_brightness(target) == 0) {
            applied = target;
            applied_band = band;
            last_adjust = current;
            printf("[SENSOR] auto brightness: lux=%.1f band=%d brightness=%d\n",
                   lux, band, target);
        }
    }
}

static void update_latest(sensor_data_t *data)
{
    pthread_mutex_lock(&sensor_mutex);
    latest_data = *data;
    pthread_mutex_unlock(&sensor_mutex);
}

static void run_mock_sensor(void)
{
    uint64_t started = now_ms();
    uint64_t next_pir = 0;
    uint64_t last_pir_report = 0;
    uint64_t last_log = 0;
    int pir_interval = env_int("ECHOMATE_MOCK_PIR_INTERVAL_SEC", 0);
    int cooldown = env_int("ECHOMATE_PIR_COOLDOWN_SECONDS",
                           ECHOMATE_PIR_COOLDOWN_SECONDS);
    float mock_lux = env_float("ECHOMATE_MOCK_LUX", 120.0f);
    float mock_temperature = env_float("ECHOMATE_MOCK_TEMP_C", 25.6f);
    float mock_pressure = env_float("ECHOMATE_MOCK_PRESSURE_HPA", 1012.4f);

    if (pir_interval > 0) {
        next_pir = started + (uint64_t)pir_interval * 1000ULL;
    }

    while (sensor_running) {
        uint64_t current = now_ms();
        sensor_data_t data = {
            .lux = mock_lux,
            .instant_lux = mock_lux,
            .temperature_c = mock_temperature,
            .pressure_hpa = mock_pressure,
            .pir_active = false,
        };

        if (pir_interval > 0 && current >= next_pir) {
            data.pir_active = true;
            next_pir = current + (uint64_t)pir_interval * 1000ULL;
            if ((last_pir_report == 0
                 || current - last_pir_report
                    >= (uint64_t)cooldown * 1000ULL)
                && pir_callback != NULL) {
                last_pir_report = current;
                pir_callback(&data, pir_callback_user_data);
            }
        }

        update_latest(&data);
        update_auto_brightness(data.lux);

        if (last_log == 0 || current - last_log >= SENSOR_LOG_INTERVAL_MS) {
            printf("[SENSOR] mock temp=%.1fC pressure=%.1fhPa "
                   "lux=%.1f pir=%d\n",
                   data.temperature_c, data.pressure_hpa, data.lux,
                   data.pir_active ? 1 : 0);
            fflush(stdout);
            last_log = current;
        }

        usleep(SENSOR_POLL_MS * 1000);
    }
}

static void run_board_sensor(void)
{
    uint64_t started = now_ms();
    uint64_t last_lux_read = 0;
    uint64_t last_env_read = 0;
    uint64_t last_log = 0;
    uint64_t last_retry = 0;
    uint64_t last_pir_report = 0;
    uint64_t pir_high_since = 0;
    uint64_t pir_low_since = 0;
    bool pir_armed = false;
    float lux_samples[LUX_FILTER_SAMPLES] = {0};
    size_t lux_sample_count = 0;
    size_t lux_sample_index = 0;
    int warmup = env_int("ECHOMATE_PIR_WARMUP_SECONDS",
                         ECHOMATE_PIR_WARMUP_SECONDS);
    int rearm = env_int("ECHOMATE_PIR_REARM_SECONDS",
                        ECHOMATE_PIR_REARM_SECONDS);
    int cooldown = env_int("ECHOMATE_PIR_COOLDOWN_SECONDS",
                           ECHOMATE_PIR_COOLDOWN_SECONDS);
    sensor_data_t data = {0};

    pir_init();
    bh1750_init();
    bmp280_init();

    while (sensor_running) {
        uint64_t current = now_ms();

        if (current - last_retry >= SENSOR_RETRY_INTERVAL_MS) {
            if (bh1750_fd < 0) {
                bh1750_init();
            }
            if (bmp280_fd < 0) {
                bmp280_init();
            }
            last_retry = current;
        }

        if (bh1750_fd >= 0
            && current - last_lux_read >= LUX_READ_INTERVAL_MS) {
            float lux = 0.0f;
            if (bh1750_read(&lux) == 0) {
                data.instant_lux = lux;
                lux_samples[lux_sample_index] = lux;
                lux_sample_index = (lux_sample_index + 1)
                                   % LUX_FILTER_SAMPLES;
                if (lux_sample_count < LUX_FILTER_SAMPLES) {
                    lux_sample_count++;
                }
                data.lux = median_lux(lux_samples, lux_sample_count);
                update_auto_brightness(data.lux);
            } else {
                close(bh1750_fd);
                bh1750_fd = -1;
            }
            last_lux_read = current;
        }

        if (bmp280_fd >= 0
            && current - last_env_read >= ENV_READ_INTERVAL_MS) {
            float temperature = 0.0f;
            float pressure = 0.0f;
            if (bmp280_read(&temperature, &pressure) == 0) {
                data.temperature_c = temperature;
                data.pressure_hpa = pressure;
            } else {
                close(bmp280_fd);
                bmp280_fd = -1;
            }
            last_env_read = current;
        }

        int pir_level = gpio_get_value(ECHOMATE_PIR_GPIO);
        data.pir_active = pir_level > 0;
        if (data.pir_active) {
            pir_low_since = 0;
            if (pir_high_since == 0) {
                pir_high_since = current;
            }
        } else {
            pir_high_since = 0;
            if (pir_low_since == 0) {
                pir_low_since = current;
            }
            if (!pir_armed
                && current - pir_low_since
                   >= (uint64_t)rearm * 1000ULL) {
                pir_armed = true;
            }
        }

        if (data.pir_active
            && pir_high_since != 0
            && current - pir_high_since >= PIR_DEBOUNCE_MS
            && pir_armed
            && current - started >= (uint64_t)warmup * 1000ULL) {
            pir_armed = false;
            if ((last_pir_report == 0
                 || current - last_pir_report
                    >= (uint64_t)cooldown * 1000ULL)
                && pir_callback != NULL) {
                last_pir_report = current;
                pir_callback(&data, pir_callback_user_data);
            }
        }
        update_latest(&data);

        if (last_log == 0 || current - last_log >= SENSOR_LOG_INTERVAL_MS) {
            printf("[SENSOR] temp=%.1fC pressure=%.1fhPa lux=%.1f pir=%d\n",
                   data.temperature_c, data.pressure_hpa, data.lux,
                   data.pir_active ? 1 : 0);
            fflush(stdout);
            last_log = current;
        }

        usleep(SENSOR_POLL_MS * 1000);
    }

    if (bh1750_fd >= 0) {
        close(bh1750_fd);
        bh1750_fd = -1;
    }
    if (bmp280_fd >= 0) {
        close(bmp280_fd);
        bmp280_fd = -1;
    }
}

static void *sensor_thread_entry(void *arg)
{
    (void)arg;
    if (use_mock_backend()) {
        printf("[SENSOR] mock backend enabled\n");
        run_mock_sensor();
    } else {
        printf("[SENSOR] board backend enabled\n");
        run_board_sensor();
    }
    return NULL;
}

int sensor_manager_init(sensor_pir_callback_t callback, void *user_data)
{
#if !ENABLE_SENSORS
    (void)callback;
    (void)user_data;
    return 0;
#else
    if (sensor_started) {
        return 0;
    }

    latest_data = (sensor_data_t){0};
    pir_callback = callback;
    pir_callback_user_data = user_data;
    auto_brightness = true;
    sensor_running = 1;

    if (pthread_create(&sensor_thread, NULL, sensor_thread_entry, NULL) != 0) {
        sensor_running = 0;
        return -1;
    }
    sensor_started = true;
    return 0;
#endif
}

void sensor_manager_deinit(void)
{
    if (!sensor_started) {
        return;
    }
    sensor_running = 0;
    pthread_join(sensor_thread, NULL);
    sensor_started = false;
}

bool sensor_manager_get_latest(sensor_data_t *data)
{
    if (data == NULL) {
        return false;
    }

    pthread_mutex_lock(&sensor_mutex);
    *data = latest_data;
    pthread_mutex_unlock(&sensor_mutex);
    return true;
}

void sensor_manager_set_auto_brightness(bool enabled)
{
    pthread_mutex_lock(&sensor_mutex);
    auto_brightness = enabled;
    pthread_mutex_unlock(&sensor_mutex);
}

bool sensor_manager_get_auto_brightness(void)
{
    pthread_mutex_lock(&sensor_mutex);
    bool enabled = auto_brightness;
    pthread_mutex_unlock(&sensor_mutex);
    return enabled;
}
