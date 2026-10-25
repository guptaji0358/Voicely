#include "MainWindow.h"
#include "ui/Icons.h"

#include <QApplication>
#include <QPainter>
#include <QPixmap>
#include <QScreen>
#include <QSplashScreen>
#include <QTimer>

#ifdef Q_OS_WIN
#include <windows.h>
#include <shobjidl.h>
#endif

// Start-up splash: background is an SVG asset, text is drawn in code so it can appear before MainWindow (which spawns the speech engine) is built.
static QPixmap makeSplashPixmap(qreal dpr) {
    const QSize sz(460, 280);
    QPixmap pm(sz * dpr);
    pm.setDevicePixelRatio(dpr);
    pm.fill(Qt::transparent);
    QPainter p(&pm);
    p.setRenderHints(QPainter::Antialiasing | QPainter::TextAntialiasing | QPainter::SmoothPixmapTransform);
    Icons::paint(&p, ":/illustrations/splash.svg", QRectF(QPointF(0, 0), QSizeF(sz)));
    const QPixmap logo = Icons::icon("app-logo").pixmap(QSize(88, 88));
    p.drawPixmap((sz.width() - 88) / 2, 44, logo);
    QFont f = p.font();
    f.setPointSize(24);
    f.setWeight(QFont::DemiBold);
    p.setFont(f);
    p.setPen(QColor("#f2f3fa"));
    p.drawText(QRect(0, 146, sz.width(), 40), Qt::AlignCenter, "Voicely");
    f.setPointSize(10);
    f.setWeight(QFont::Normal);
    p.setFont(f);
    p.setPen(QColor(255, 255, 255, 150));
    p.drawText(QRect(0, 186, sz.width(), 24), Qt::AlignCenter, "Natural speech, on your machine");
    p.setPen(QColor(255, 255, 255, 110));
    p.drawText(QRect(0, 236, sz.width(), 24), Qt::AlignCenter, "Starting up…");
    return pm;
}

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
    const bool snapshotRun = !qEnvironmentVariableIsEmpty("TTS_SNAPSHOT");
    QSplashScreen splash(makeSplashPixmap(app.primaryScreen() ? app.primaryScreen()->devicePixelRatio() : 1.0),
                         Qt::WindowStaysOnTopHint | Qt::FramelessWindowHint);
    splash.setAttribute(Qt::WA_TranslucentBackground);
    if (!snapshotRun) {
        splash.show();
        app.processEvents();            // paint it now; building the main window below can take a moment
    }
    MainWindow w;
    w.show();
    splash.finish(&w);
    // Developer aid: TTS_SNAPSHOT=<file.png> renders the window after start-up (and after TTS_SNAPSHOT_TEXT is typed
    // into the editor), saves it and exits. It is how UI screenshots are made without screen-capture permissions.
    const QString snap = qEnvironmentVariable("TTS_SNAPSHOT");
    if (!snap.isEmpty()) {
        const QString sample = qEnvironmentVariable("TTS_SNAPSHOT_TEXT");
        if (!qEnvironmentVariableIsEmpty("TTS_SNAPSHOT_DEMO")) {      // synthetic playback levels for the 3D waveform
            auto *demo = new QTimer(&w);
            QObject::connect(demo, &QTimer::timeout, &w, [&w] {
                static int n = 0;
                w.feedDemoLevels(0.03 * n++);
            });
            demo->start(30);
        }
        if (!qEnvironmentVariableIsEmpty("TTS_SNAPSHOT_DROP")) QTimer::singleShot(6500, &w, [&w] { w.previewDropOverlay(); });
        QTimer::singleShot(8000, &w, [&w, &app, snap, sample] {
            if (!sample.isEmpty()) w.setProperty("snapshotText", sample);
            w.grab().save(snap);
            app.quit();
        });
    }
    return app.exec();
}
