#pragma once
#ifndef INC_FREERTOS_H
#error "include FreeRTOS.h before task.h"
#endif
#include "idf_stub.h"
#define pdPASS 1
typedef void *TaskHandle_t;
void vTaskDelay(unsigned);
void vTaskDelete(TaskHandle_t);
int xTaskCreate(void (*)(void *),const char *,unsigned,void *,unsigned,TaskHandle_t *);

TickType_t xTaskGetTickCount(void);
int xTaskDelayUntil(TickType_t *,TickType_t);
