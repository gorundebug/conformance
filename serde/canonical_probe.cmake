if(CMAKE_SOURCE_DIR STREQUAL CMAKE_CURRENT_SOURCE_DIR)
  function(servicelib_add_custom_serde_probe)
    add_executable(servicelib_custom_serde_probe
        /repo/conformance/serde/custom_cpp_probe.cpp)
    target_compile_definitions(servicelib_custom_serde_probe PRIVATE
        SERVICELIB_CUSTOM_SERDE_CANONICAL=1)
    target_include_directories(servicelib_custom_serde_probe PRIVATE
        /repo/cppexample)
    target_link_libraries(servicelib_custom_serde_probe PRIVATE
        servicelib userver-core)
  endfunction()
  cmake_language(DEFER CALL servicelib_add_custom_serde_probe)
endif()
