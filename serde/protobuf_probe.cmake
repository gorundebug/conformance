if(CMAKE_SOURCE_DIR STREQUAL CMAKE_CURRENT_SOURCE_DIR)
  function(servicelib_add_protobuf_wire_probe)
    add_executable(servicelib_protobuf_wire_probe
        /repo/conformance/serde/protobuf_cpp_probe.cpp)
    target_compile_features(servicelib_protobuf_wire_probe PRIVATE cxx_std_20)
    target_link_libraries(servicelib_protobuf_wire_probe PRIVATE
        example_inventory_service_api)
  endfunction()
  cmake_language(DEFER CALL servicelib_add_protobuf_wire_probe)
endif()
