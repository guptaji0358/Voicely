cmake_minimum_required(VERSION 3.21)
project(TTSStudio LANGUAGES CXX)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_AUTOMOC ON)
set(CMAKE_AUTORCC ON)

find_package(Qt6 REQUIRED COMPONENTS Widgets Multimedia Svg Test)
find_package(Python3 COMPONENTS Interpreter)   # only used to run the tests

add_library(ttscore STATIC
  src/ProtocolClient.cpp src/MainWindow.cpp
  src/ui/Motion.cpp src/ui/ThemeManager.cpp src/ui/Icons.cpp src/ui/Buttons.cpp src/ui/Indicators.cpp
  src/ui/Card.cpp src/ui/Frameless.cpp src/ui/QaPanel.cpp src/ui/DictionaryDialog.cpp src/ui/ReviewDialog.cpp src/ui/PronSettings.cpp
  resources/resources.qrc)   # QSS themes + SVG icons, addressed as :/styles/*, :/icons/*, :/illustrations/*
target_include_directories(ttscore PUBLIC src)
target_link_libraries(ttscore PUBLIC Qt6::Widgets Qt6::Multimedia Qt6::Svg)
if(WIN32)
  target_link_libraries(ttscore PUBLIC shell32)
endif()

add_executable(TTSStudio WIN32 src/main.cpp)
target_link_libraries(TTSStudio PRIVATE ttscore)

# ---- tests ---------------------------------------------------------------
enable_testing()
get_target_property(_qmake Qt6::qmake IMPORTED_LOCATION)
get_filename_component(_qtbin "${_qmake}" DIRECTORY)
get_filename_component(_qtplugins "${_qtbin}/../plugins" ABSOLUTE)     # lets the offscreen platform plugin be found by the tests
set(TTS_ENGINE_ROOT "${CMAKE_CURRENT_SOURCE_DIR}/.." CACHE PATH "Folder containing speech_engine/")
foreach(t tst_protocol tst_gui tst_ui tst_qa_gui)
  add_executable(${t} tests/${t}.cpp)
  target_link_libraries(${t} PRIVATE ttscore Qt6::Test)
  add_test(NAME ${t} COMMAND ${t})
  set_tests_properties(${t} PROPERTIES
    TIMEOUT 180
    ENVIRONMENT "QT_QPA_PLATFORM=offscreen;TTS_ENGINE_ROOT=${TTS_ENGINE_ROOT};TTS_PYTHON=${Python3_EXECUTABLE};SPEECH_ENGINE_ALLOW_MOCK=1;QT_PLUGIN_PATH=${_qtplugins}")
endforeach()

# high-DPI: UI tests again with a 2x scale factor (the offscreen platform honours QT_SCALE_FACTOR)
add_test(NAME tst_ui_hidpi COMMAND tst_ui highDpiRendering edgeHitTesting titleBarControlsMaximizeRestoreMinimize)
set_tests_properties(tst_ui_hidpi PROPERTIES TIMEOUT 120
  ENVIRONMENT "QT_QPA_PLATFORM=offscreen;QT_SCALE_FACTOR=2;TTS_ENGINE_ROOT=${TTS_ENGINE_ROOT};TTS_PYTHON=${Python3_EXECUTABLE};SPEECH_ENGINE_ALLOW_MOCK=1;QT_PLUGIN_PATH=${_qtplugins}")

# Python backend tests (speech_engine/*) as part of the same ctest run
if(Python3_EXECUTABLE)
  add_test(NAME pytest_backend COMMAND ${Python3_EXECUTABLE} -m pytest -q WORKING_DIRECTORY "${TTS_ENGINE_ROOT}")
  set_tests_properties(pytest_backend PROPERTIES TIMEOUT 900)
endif()

# ---- deployment helper: cmake --build . --target deploy ----------------------
find_program(WINDEPLOYQT windeployqt HINTS "${_qtbin}")
if(WINDEPLOYQT)
  add_custom_target(deploy COMMAND ${WINDEPLOYQT} --no-translations $<TARGET_FILE:TTSStudio> DEPENDS TTSStudio)
endif()
