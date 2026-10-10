# HRC Safety Log — ESP32 firmware (ESP-IDF v5.5 / v6.0)
#
# Project-level CMakeLists. Các component (mới có "main") được đăng ký
# trong thư mục con và sẽ được ESP-IDF build tự động.
#
# Build verified with ESP-IDF 6.0.1 for ESP32-S3; see README.md for the
# dependency manifest, target selection and remaining hardware checks.

cmake_minimum_required(VERSION 3.16)

include($ENV{IDF_PATH}/tools/cmake/project.cmake)

project(hrc_esp32)
