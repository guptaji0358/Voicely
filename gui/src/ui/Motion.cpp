#include "Motion.h"

#include "Config.h"

#include <QElapsedTimer>
#include <QtGlobal>

#ifdef Q_OS_WIN
#include <windows.h>
#endif

namespace Motion {

bool reduced() {
    static QElapsedTimer age;
    static bool cached = false;
    if (age.isValid() && age.elapsed() < 2000) return cached;   // the OS call is cheap, but hover calls this a lot
    age.start();
    bool r = false;
    const QByteArray env = qgetenv("TTS_REDUCE_MOTION");
    if (!env.isEmpty() && env != "0") r = true;
    if (!r && Config::settings().value("reduceMotion", false).toBool()) r = true;
#ifdef Q_OS_WIN
    if (!r) {
        BOOL on = TRUE;
        if (SystemParametersInfoW(SPI_GETCLIENTAREAANIMATION, 0, &on, 0) && !on) r = true;
    }
#endif
    cached = r;
    return r;
}

}  // namespace Motion
