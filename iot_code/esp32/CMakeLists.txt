# HRC Safety Log — ESP32 firmware (ESP-IDF v5.x)
#
# Project-level CMakeLists. Các component (mới có "main") được đăng ký
# trong thư mục con và sẽ được ESP-IDF build tự động.
#
# Giai đoạn M4a: chỉ dựng khung project — chưa build/flash được trên máy
# phát triển (không có toolchain), xem iot_code/esp32/README.md.

cmake_minimum_required(VERSION 3.16)

include($ENV{IDF_PATH}/tools/cmake/project.cmake)

project(hrc_esp32)
