if(CMAKE_SOURCE_DIR STREQUAL CMAKE_CURRENT_SOURCE_DIR)
  function(servicelib_add_coro_custom_serde_probe)
    add_executable(cppcoroservicelib_custom_serde_probe
        /repo/conformance/serde/custom_cpp_probe.cpp)
    target_compile_definitions(cppcoroservicelib_custom_serde_probe PRIVATE
        SERVICELIB_CUSTOM_SERDE_CORO=1)
    target_include_directories(cppcoroservicelib_custom_serde_probe PRIVATE
        /repo/cppcoroexample
        /repo/cppcoroexample/model_cppcoro/include)
    target_link_libraries(cppcoroservicelib_custom_serde_probe PRIVATE
        servicelib::servicelib)
  endfunction()
  cmake_language(DEFER CALL servicelib_add_coro_custom_serde_probe)
endif()
