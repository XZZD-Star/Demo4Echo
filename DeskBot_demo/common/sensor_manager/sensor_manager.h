#ifndef SENSOR_MANAGER_H
#define SENSOR_MANAGER_H

#ifdef __cplusplus
extern "C" {
#endif

#include <stdbool.h>

typedef struct {
    float lux;
    float instant_lux;
    float temperature_c;
    float humidity_percent;
    float pressure_hpa;
    bool pir_active;
} sensor_data_t;

typedef void (*sensor_pir_callback_t)(const sensor_data_t *data,
                                      void *user_data);

int sensor_manager_init(sensor_pir_callback_t callback, void *user_data);
void sensor_manager_deinit(void);
bool sensor_manager_get_latest(sensor_data_t *data);
void sensor_manager_set_auto_brightness(bool enabled);
bool sensor_manager_get_auto_brightness(void);

#ifdef __cplusplus
}
#endif

#endif
