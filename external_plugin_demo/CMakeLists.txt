cmake_minimum_required(VERSION 3.15)
project(external_plugin_demo LANGUAGES CXX)

find_package(openusd REQUIRED CONFIG)

# Renders myplugin/plugInfo.json.in the same way upstream's own
# _plugInfo_subst() does for the recipe's built-in plugins (see
# cmake/macros/Private.cmake in the OpenUSD source): a literal @VAR@
# substitution, here just for the shared library suffix so the manifest
# names the right file on each platform.
set(SHLIB_SUFFIX "${CMAKE_SHARED_LIBRARY_SUFFIX}")
configure_file(
    "${CMAKE_CURRENT_SOURCE_DIR}/myplugin/plugInfo.json.in"
    "${CMAKE_CURRENT_BINARY_DIR}/myplugin/plugInfo.json"
    @ONLY
)

# The plugin itself: never depended on by test_external_plugin below, it's
# meant to be found by USD's Plug registry at runtime, not linked directly.
add_library(MyExternalPlugin SHARED myplugin.cpp)
target_link_libraries(MyExternalPlugin PRIVATE openusd::tf)
set_target_properties(MyExternalPlugin PROPERTIES
    OUTPUT_NAME "MyExternalPlugin"
    LIBRARY_OUTPUT_DIRECTORY "${CMAKE_CURRENT_BINARY_DIR}/myplugin"
    RUNTIME_OUTPUT_DIRECTORY "${CMAKE_CURRENT_BINARY_DIR}/myplugin"
)

# The harness: only ever talks to PlugRegistry, never links MyExternalPlugin.
add_executable(test_external_plugin main.cpp)
target_link_libraries(test_external_plugin PRIVATE openusd::tf openusd::plug)
