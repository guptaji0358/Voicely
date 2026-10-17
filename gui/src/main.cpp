#include "MainWindow.h"
#include "ui/Icons.h"

#include <QApplication>
#include <QPixmap>
#include <QTimer>

#ifdef Q_OS_WIN
#include <windows.h>
#include <shobjidl.h>
#endif

int main(int argc, char *argv[]) {
#ifdef Q_OS_WIN
    // Own taskbar identity (grouping, pinning, jump list) instead of inheriting python.exe / the host's.
    SetCurrentProcessExplicitAppUserModelID(L"Voicely.App");
#endif
    QApplication app(argc, argv);                       // Qt 6: per-monitor high-DPI scaling is on by default
    QApplication::setApplicationName("Voicely");
    QApplication::setApplicationDisplayName("Voicely");
    QApplication::setOrganizationName("TTSStudio");
    QApplication::setWindowIcon(Icons::icon("app-logo"));
    MainWindow w;
    w.show();
    // Developer aid: TTS_SNAPSHOT=<file.png> renders the window after start-up (and after TTS_SNAPSHOT_TEXT is typed
    // into the editor), saves it and exits. It is how UI screenshots are made without screen-capture permissions.
    const QString snap = qEnvironmentVariable("TTS_SNAPSHOT");
    if (!snap.isEmpty()) {
        const QString sample = qEnvironmentVariable("TTS_SNAPSHOT_TEXT");
        QTimer::singleShot(8000, &w, [&w, &app, snap, sample] {
            if (!sample.isEmpty()) w.setProperty("snapshotText", sample);
            w.grab().save(snap);
            app.quit();
        });
    }
    return app.exec();
}
