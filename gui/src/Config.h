#pragma once
// Runtime configuration: where Python and the speech_engine package live. No hard-coded paths.
#include <QCoreApplication>
#include <QDir>
#include <QFileInfo>
#include <QSettings>
#include <QStandardPaths>

namespace Config {

inline QSettings &settings() {
    // Explicit format so QSettings::setDefaultFormat()/setPath() (used by the tests to keep the real profile clean) are honoured.
    static QSettings s(QSettings::defaultFormat(), QSettings::UserScope, "TTSStudio", "TTSStudio");
    return s;
}

// A folder is an engine root when it contains speech_engine/worker.py.
inline bool isEngineRoot(const QString &dir) {
    return !dir.isEmpty() && QFileInfo::exists(QDir(dir).filePath("speech_engine/worker.py"));
}

inline QString findEngineRoot() {
    const QString cfg = settings().value("engineRoot").toString();
    if (isEngineRoot(cfg)) return cfg;
    const QString env = qEnvironmentVariable("TTS_ENGINE_ROOT");
    if (isEngineRoot(env)) return QDir::cleanPath(env);
    // Installed layout: <app>/engine ; development layout: walk up from the executable.
    QDir d(QCoreApplication::applicationDirPath());
    for (int i = 0; i < 7; ++i) {
        if (isEngineRoot(d.absolutePath())) return d.absolutePath();
        if (isEngineRoot(d.filePath("engine"))) return d.filePath("engine");
        if (!d.cdUp()) break;
    }
    return {};
}

// Order: Settings > TTS_PYTHON > bundled <app>/python/python.exe > python on PATH.
inline QString findPython() {
    const QString cfg = settings().value("pythonExe").toString();
    if (!cfg.isEmpty()) return cfg;
    const QString env = qEnvironmentVariable("TTS_PYTHON");
    if (!env.isEmpty()) return env;
    const QString bundled = QCoreApplication::applicationDirPath() + "/python/python.exe";
    if (QFileInfo::exists(bundled)) return bundled;
    return QStandardPaths::findExecutable("python");
}

// Interpreter for the optional Model Lab worker (needs PyTorch): Settings > TTS_LAB_PYTHON > the speech interpreter.
inline QString findLabPython() {
    const QString cfg = settings().value("labPythonExe").toString();
    if (!cfg.isEmpty()) return cfg;
    const QString env = qEnvironmentVariable("TTS_LAB_PYTHON");
    if (!env.isEmpty()) return env;
    return findPython();
}

}  // namespace Config
