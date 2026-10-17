// UI-layer tests (offscreen): stylesheet loading, SVG resources, the compact voice row, button states,
// animations and the preview workflow, against the real worker (mock backend).
#include "MainWindow.h"
#include "ui/Buttons.h"
#include "ui/Card.h"
#include "ui/Frameless.h"
#include "ui/Icons.h"
#include "ui/Indicators.h"
#include "ui/ThemeManager.h"

#include <QApplication>
#include <QComboBox>
#include <QDir>
#include <QElapsedTimer>
#include <QFile>
#include <QLabel>
#include <QMenu>
#include <QMenuBar>
#include <QMediaPlayer>
#include <QPlainTextEdit>
#include <QProgressBar>
#include <QPushButton>
#include <QTemporaryDir>
#include <QToolButton>
#include <QScreen>
#include <QtTest>

class TstUi : public QObject {
    Q_OBJECT
    MainWindow *w = nullptr;
    template <class T> T *get(const char *name) { auto *x = w->findChild<T *>(name); Q_ASSERT(x); return x; }
    void waitState(MainWindow::State s, int ms = 30000) {
        QElapsedTimer t; t.start();
        while (w->state() != s && t.elapsed() < ms) QTest::qWait(10);
        QCOMPARE(w->state(), s);
    }

private slots:
    void initTestCase() {
        qputenv("TTS_REDUCE_MOTION", "0");
        w = new MainWindow;
        w->setPreCheckEnabled(false);
        w->resize(1120, 780);
        w->show();
        waitState(MainWindow::State::Idle);
        auto *be = get<QComboBox>("backendCombo");
        be->setCurrentIndex(be->findData("mock"));
    }
    void cleanupTestCase() { w->close(); delete w; }

    void stylesheetLoadedFromResources() {
        auto &t = ThemeManager::instance();
        QVERIFY2(!t.usingFallback(), qPrintable(t.lastError()));
        QCOMPARE(t.current(), QString("dark"));
        QVERIFY(!t.styleSheet().contains('@'));            // every token substituted
        QVERIFY(t.styleSheet().contains("QFrame#card"));
        QVERIFY(t.apply("light"));
        QVERIFY(t.token("bg") != "");
        QVERIFY(t.apply("dark"));
    }
    void fallbackStyleOnBrokenStylesheet() {
        QTemporaryDir d;
        qputenv("TTS_STYLE_DIR", d.path().toUtf8());       // folder without any .qss
        auto &t = ThemeManager::instance();
        QVERIFY(!t.apply("dark"));
        QVERIFY(t.usingFallback());
        QVERIFY(t.lastError().contains("failed to load"));
        QVERIFY(t.color("icon").isValid());                 // C++ colours still available
        qunsetenv("TTS_STYLE_DIR");
        QVERIFY(t.apply("dark"));
        QVERIFY(!t.usingFallback());
    }
    void allSvgResourcesResolve() {
        QCOMPARE(Icons::brokenResources(), QStringList());
        for (const QString &n : Icons::requiredIcons()) {
            QVERIFY2(QFile::exists(Icons::path(n)), qPrintable(n));
            const QIcon ic = Icons::icon(n);
            QVERIFY2(!ic.pixmap(QSize(24, 24)).isNull(), qPrintable(n));
            const QImage img = ic.pixmap(QSize(24, 24)).toImage();
            bool any = false;
            for (int y = 0; y < img.height() && !any; ++y)
                for (int x = 0; x < img.width(); ++x)
                    if (qAlpha(img.pixel(x, y)) > 0) { any = true; break; }
            QVERIFY2(any, qPrintable(n + " rendered empty"));
        }
        QVERIFY(QFile::exists(":/illustrations/empty-input.svg"));
        QVERIFY(QFile::exists(":/illustrations/empty-audio.svg"));
    }
    void compactVoiceRow() {
        QVERIFY(!w->findChild<QPushButton *>("testButton"));        // standalone button is gone
        auto *combo = get<QComboBox>("voiceCombo");
        auto *prev = get<QToolButton>("previewButton");
        QVERIFY(prev->parentWidget() == combo->parentWidget());     // same card, same row
        QCOMPARE(prev->toolTip(), QString("Preview selected voice"));
        QVERIFY(!prev->icon().isNull());
        const QRect a = combo->geometry(), b = prev->geometry();
        QVERIFY2(qAbs((a.center().y()) - (b.center().y())) <= 2, "preview button is not aligned with the combo");
        QVERIFY2(b.left() - a.right() < 16 && b.left() >= a.right(), "preview button not next to the voice selector");
        QCOMPARE(combo->currentText(), QString("Original Voice"));
    }
    void changingVoiceDoesNotSpeak() {
        auto *combo = get<QComboBox>("voiceCombo");
        combo->addItem("other", "other");
        combo->setCurrentIndex(combo->count() - 1);
        QTest::qWait(150);
        QCOMPARE(w->state(), MainWindow::State::Idle);
        QCOMPARE(w->findChildren<QMediaPlayer *>().at(1)->playbackState(), QMediaPlayer::StoppedState);
        combo->setCurrentIndex(combo->findData("original"));
        combo->removeItem(combo->findData("other"));
    }
    void hoverPressAndDisabledStates() {
        auto *b = get<QPushButton>("importButton");
        auto *fx = dynamic_cast<AnimatedButton *>(b);
        QVERIFY(fx);
        QCOMPARE(fx->hoverLevel(), 0.0);
        QTest::mouseMove(b, b->rect().center());
        QApplication::sendEvent(b, new QEnterEvent(b->rect().center(), b->rect().center(), b->rect().center()));
        QTRY_VERIFY_WITH_TIMEOUT(fx->hoverLevel() > 0.9, 1000);     // animated up
        QTest::mousePress(b, Qt::LeftButton);
        QTRY_VERIFY_WITH_TIMEOUT(fx->pressLevel() > 0.9, 1000);
        QTest::mouseRelease(b, Qt::LeftButton, {}, QPoint(-50, -50));   // release outside: no click dialog
        QTRY_VERIFY_WITH_TIMEOUT(fx->pressLevel() < 0.05, 1000);
        QApplication::sendEvent(b, new QEvent(QEvent::Leave));
        QTRY_VERIFY_WITH_TIMEOUT(fx->hoverLevel() < 0.05, 1000);
        b->setEnabled(false);
        QVERIFY(!b->isEnabled());
        QCOMPARE(fx->hoverLevel(), 0.0);
        b->setEnabled(true);
        // Disabled icon uses a different colour than the normal one.
        const QImage n = b->icon().pixmap(QSize(18, 18), QIcon::Normal).toImage();
        const QImage dis = b->icon().pixmap(QSize(18, 18), QIcon::Disabled).toImage();
        QVERIFY(n != dis);
    }
    void previewWorkflowAndOverlapProtection() {
        auto *prev = get<PreviewButton>("previewButton");
        auto players = w->findChildren<QMediaPlayer *>();
        QMediaPlayer *main = players.at(0), *pv = players.at(1);
        QVERIFY(prev->isEnabled());
        QCOMPARE(prev->mode(), PreviewButton::Mode::Idle);

        // Give the main player some audio first: preview must not destroy it.
        get<QPlainTextEdit>("textEditor")->setPlainText("Main text.");
        QTest::mouseClick(get<QPushButton>("primary"), Qt::LeftButton);
        waitState(MainWindow::State::Idle);
        const QString mainPath = w->lastAudioPath();
        QVERIFY(QFileInfo::exists(mainPath));

        QTest::mouseClick(prev, Qt::LeftButton);
        QCOMPARE(w->state(), MainWindow::State::Generating);
        QCOMPARE(prev->mode(), PreviewButton::Mode::Loading);       // loading indicator
        QVERIFY(prev->isEnabled());                                  // enabled so it can cancel
        QVERIFY(!get<QPushButton>("primary")->isEnabled());          // no overlapping generation
        QCOMPARE(prev->toolTip(), QString("Generating preview… click to cancel"));
        waitState(MainWindow::State::Idle);
        QCOMPARE(w->lastAudioPath(), mainPath);                      // main audio untouched
        QTRY_VERIFY_WITH_TIMEOUT(pv->source().isValid(), 5000);
        QCOMPARE(main->playbackState() == QMediaPlayer::PlayingState, false);
        // Speaking indicator appears only if playback really started.
        QTRY_VERIFY_WITH_TIMEOUT(pv->playbackState() == QMediaPlayer::PlayingState ||
                                 pv->mediaStatus() == QMediaPlayer::EndOfMedia, 8000);
        if (pv->playbackState() == QMediaPlayer::PlayingState) {
            QCOMPARE(prev->mode(), PreviewButton::Mode::Speaking);
            QTest::mouseClick(prev, Qt::LeftButton);                 // stops safely
            QCOMPARE(pv->playbackState(), QMediaPlayer::StoppedState);
        }
        QTRY_COMPARE_WITH_TIMEOUT(prev->mode(), PreviewButton::Mode::Idle, 3000);
        QCOMPARE(prev->toolTip(), QString("Preview selected voice"));
    }
    void cancelPreviewWhileGenerating() {
        qputenv("SPEECH_ENGINE_MOCK_DELAY", "0");
        auto *prev = get<PreviewButton>("previewButton");
        QTest::mouseClick(prev, Qt::LeftButton);
        QCOMPARE(prev->mode(), PreviewButton::Mode::Loading);
        QTest::mouseClick(prev, Qt::LeftButton);                      // second click = cancel, not a second job
        waitState(MainWindow::State::Idle);
        QTRY_COMPARE_WITH_TIMEOUT(prev->mode(), PreviewButton::Mode::Idle, 5000);
    }
    void emptyStatesAndSuccessAnimation() {
        auto *stack = get<QWidget>("audioStack");
        QVERIFY(stack);
        QVERIFY(get<QPushButton>("primary")->icon().isNull() == false);
        QTemporaryDir d;
        w->exportAudio(d.filePath("ui test.mp3"), "mp3");
        waitState(MainWindow::State::Idle);
        auto *badge = get<SuccessBadge>("successBadge");
        QVERIFY(badge->isVisible());
        QTRY_VERIFY_WITH_TIMEOUT(badge->isVisible(), 500);
        QTest::qWait(700);                                           // animation finished, still showing the check
        QVERIFY(badge->isVisible());
    }
    void animationsDoNotBlockTheEventLoop() {
        auto *prev = get<PreviewButton>("previewButton");
        qint64 worst = 0, last = 0;
        QElapsedTimer since; since.start();
        QTimer ticker; ticker.setInterval(10);
        connect(&ticker, &QTimer::timeout, [&] { const qint64 n = since.elapsed(); worst = qMax(worst, n - last); last = n; });
        ticker.start();
        QTest::mouseClick(prev, Qt::LeftButton);
        waitState(MainWindow::State::Idle);
        QTest::qWait(300);
        ticker.stop();
        QVERIFY2(worst < 400, qPrintable(QString("event loop stalled %1 ms").arg(worst)));
    }
    void reducedMotionDisablesAnimation() {
        qputenv("TTS_REDUCE_MOTION", "1");
        QTest::qWait(2100);                                          // the setting is cached for 2 s
        SmoothProgressBar bar;
        bar.show();
        bar.setTarget(80);
        QCOMPARE(bar.value(), 80);                                   // jumped, not animated
        qputenv("TTS_REDUCE_MOTION", "0");
        QTest::qWait(2100);
    }
    void smoothProgressBarGlides() {
        SmoothProgressBar bar;
        bar.show();
        bar.setTarget(80);
        QVERIFY(bar.value() < 80);                                   // animating
        QTRY_COMPARE_WITH_TIMEOUT(bar.value(), 80, 1000);
        bar.hide();
        bar.resetNow();
        QCOMPARE(bar.value(), 0);
    }
    void waveformOnlyRunsWithData() {
        WaveformWidget wf;
        wf.show();
        QVERIFY(!wf.tickerRunning());                                // idle: no timer, no constant motion
        wf.pushLevel(0.8);
        QVERIFY(wf.tickerRunning());
        wf.setActive(false);
        QTRY_VERIFY_WITH_TIMEOUT(!wf.tickerRunning(), 3000);         // settles and stops by itself
        wf.pushLevel(0.5);
        wf.hide();
        QVERIFY(!wf.tickerRunning());                                // hidden: stopped
    }
    // ---- custom frame ---------------------------------------------------------------------------------
    void edgeHitTesting() {
        const QSize sz(400, 300);
        QCOMPARE(Frameless::edgesAt(QPoint(2, 100), sz), Qt::Edges(Qt::LeftEdge));
        QCOMPARE(Frameless::edgesAt(QPoint(398, 100), sz), Qt::Edges(Qt::RightEdge));
        QCOMPARE(Frameless::edgesAt(QPoint(200, 1), sz), Qt::Edges(Qt::TopEdge));
        QCOMPARE(Frameless::edgesAt(QPoint(200, 299), sz), Qt::Edges(Qt::BottomEdge));
        QCOMPARE(Frameless::edgesAt(QPoint(1, 1), sz), Qt::Edges(Qt::LeftEdge | Qt::TopEdge));
        QCOMPARE(Frameless::edgesAt(QPoint(399, 299), sz), Qt::Edges(Qt::RightEdge | Qt::BottomEdge));
        QCOMPARE(Frameless::edgesAt(QPoint(200, 150), sz), Qt::Edges());
        QCOMPARE(Frameless::cursorFor(Qt::LeftEdge | Qt::TopEdge), Qt::SizeFDiagCursor);
        QCOMPARE(Frameless::cursorFor(Qt::RightEdge | Qt::TopEdge), Qt::SizeBDiagCursor);
        QCOMPARE(Frameless::cursorFor(Qt::LeftEdge), Qt::SizeHorCursor);
        QCOMPARE(Frameless::cursorFor(Qt::BottomEdge), Qt::SizeVerCursor);
    }
    void titleBarControlsMaximizeRestoreMinimize() {
        QVERIFY(w->customFrame());
        QVERIFY(w->windowFlags().testFlag(Qt::FramelessWindowHint));
        QVERIFY(w->windowFlags().testFlag(Qt::WindowMinimizeButtonHint));         // keeps taskbar minimise/restore working
        auto *bar = w->findChild<TitleBar *>();
        QVERIFY(bar && bar->isVisible());
        QCOMPARE(bar->maximizeButton()->toolTip(), QString("Maximize"));
        auto *shell = w->findChild<ShellWidget *>("central");
        QVERIFY(shell->inset() > 0);
        QTest::mouseClick(bar->maximizeButton(), Qt::LeftButton);
        QTRY_VERIFY_WITH_TIMEOUT(w->isMaximized(), 3000);
        QTRY_COMPARE_WITH_TIMEOUT(bar->maximizeButton()->toolTip(), QString("Restore"), 2000);
        QCOMPARE(shell->inset(), 0);                                               // no rounded margin while maximised
        QTest::mouseClick(bar->maximizeButton(), Qt::LeftButton);
        QTRY_VERIFY_WITH_TIMEOUT(!w->isMaximized(), 3000);
        QVERIFY(shell->inset() > 0);
        // double-click on the empty title area toggles maximise
        QTest::mouseDClick(bar, Qt::LeftButton, {}, QPoint(bar->width() / 2, bar->height() / 2));
        QTRY_VERIFY_WITH_TIMEOUT(w->isMaximized(), 3000);
        QTest::mouseDClick(bar, Qt::LeftButton, {}, QPoint(bar->width() / 2, bar->height() / 2));
        QTRY_VERIFY_WITH_TIMEOUT(!w->isMaximized(), 3000);
        QTest::mouseClick(bar->minimizeButton(), Qt::LeftButton);
        QTRY_VERIFY_WITH_TIMEOUT(w->isMinimized(), 3000);
        w->showNormal();
        QTRY_VERIFY_WITH_TIMEOUT(!w->isMinimized(), 3000);
    }
    void titleBarCloseButtonClosesTheWindow() {
        MainWindow other;
        other.setPreCheckEnabled(false);
        other.show();
        auto *bar = other.findChild<TitleBar *>();
        QVERIFY(bar);
        QTest::mouseClick(bar->closeButton(), Qt::LeftButton);
        QTRY_VERIFY_WITH_TIMEOUT(!other.isVisible(), 5000);
    }
    void captionButtonsAreKeyboardAccessible() {
        auto *bar = w->findChild<TitleBar *>();
        QList<QPushButton *> bs{bar->minimizeButton(), bar->maximizeButton(), bar->closeButton()};
        for (QPushButton *b : bs) {
            QVERIFY(!b->accessibleName().isEmpty());
            QVERIFY(b->focusPolicy() & Qt::TabFocus);
        }
        QVERIFY(bar->commandMenu()->actions().size() >= 5);
        QVERIFY(!bar->commandMenu()->actions().first()->shortcut().isEmpty());
    }
    void windowPlacementStaysOnAScreen() {
        const QRect avail = QGuiApplication::primaryScreen()->availableGeometry();
        const QRect off = Frameless::fitToScreens(QRect(-9000, -9000, 800, 600), QSize(800, 600));
        QVERIFY2(avail.contains(off.center()), "a window saved on an unplugged monitor must be brought back");
        const QRect huge = Frameless::fitToScreens(QRect(0, 0, 99999, 99999), QSize(800, 600));
        QVERIFY(huge.width() <= avail.width() && huge.height() <= avail.height());
        QCOMPARE(Frameless::fitToScreens(QRect(), QSize(640, 480)).size(), QSize(640, 480));
    }
    void nativeFrameFallbackWorks() {
        qputenv("TTS_NATIVE_FRAME", "1");
        QVERIFY(!Frameless::customFrameWanted());
        MainWindow native;
        native.setPreCheckEnabled(false);
        QVERIFY(!native.customFrame());
        QVERIFY(!native.windowFlags().testFlag(Qt::FramelessWindowHint));
        QVERIFY(native.menuBar() && !native.menuBar()->actions().isEmpty());       // commands are still reachable
        native.show();
        QVERIFY(!native.findChild<TitleBar *>()->isVisible());
        native.close();
        qunsetenv("TTS_NATIVE_FRAME");
        QVERIFY(Frameless::customFrameWanted());
    }
    void highDpiRendering() {
        const qreal dpr = w->devicePixelRatioF();
        const QPixmap shot = w->grab();
        QVERIFY(!shot.isNull());
        QVERIFY(qAbs(shot.devicePixelRatio() - dpr) < 0.01);
        QVERIFY(qAbs(shot.width() / dpr - w->width()) <= 1);
        const QPixmap icon = Icons::pixmap(Icons::path("speaker"), QSize(16, 16), QColor("#fff"), 2.0);
        QCOMPARE(icon.size(), QSize(32, 32));                                      // vector icons render at physical resolution
        QCOMPARE(icon.devicePixelRatio(), 2.0);
        const QImage img = icon.toImage();
        int opaque = 0;
        for (int y = 0; y < img.height(); ++y) for (int x = 0; x < img.width(); ++x) opaque += qAlpha(img.pixel(x, y)) > 128;
        QVERIFY(opaque > 20);
    }
    void animationsStopWhenHidden() {
        auto *b = new AnimatedButton("Hover me");
        b->show();
        QEnterEvent enter(QPointF(5, 5), QPointF(5, 5), QPointF(5, 5));
        QApplication::sendEvent(b, &enter);
        QTRY_VERIFY_WITH_TIMEOUT(b->hoverLevel() > 0.5, 1000);
        b->hide();
        QCOMPARE(b->hoverLevel(), 0.0);                                            // reset, nothing keeps animating
        delete b;
        Card c;
        c.show();
        QEnterEvent enter2(QPointF(5, 5), QPointF(5, 5), QPointF(5, 5));
        QApplication::sendEvent(&c, &enter2);
        QTRY_VERIFY_WITH_TIMEOUT(c.depth() > 0.5, 1000);
        c.hide();
        const qreal d = c.depth();
        QTest::qWait(250);
        QCOMPARE(c.depth(), d);                                                    // a hidden card does not keep animating
    }
    void themeSwitchKeepsRendering() {
        QVERIFY(ThemeManager::instance().apply("light"));
        const QImage light = w->grab().toImage();
        QVERIFY(!light.isNull());
        QVERIFY(ThemeManager::instance().apply("dark"));
        QVERIFY(w->grab().toImage() != light);
    }
    void stateIconSpinsOnlyWhenBusyAndVisible() {
        StateIcon s;
        s.setKind(StateIcon::Kind::Busy);
        QVERIFY(!s.spinning());                                      // not shown yet
        s.show();
        QVERIFY(s.spinning());
        s.hide();
        QVERIFY(!s.spinning());
        s.show();
        s.setKind(StateIcon::Kind::Success);
        QVERIFY(!s.spinning());
    }
};

int main(int argc, char **argv) {
    qputenv("QT_QPA_PLATFORM", "offscreen");
    QApplication app(argc, argv);
    QSettings::setDefaultFormat(QSettings::IniFormat);
    QTemporaryDir cfg;
    QSettings::setPath(QSettings::IniFormat, QSettings::UserScope, cfg.path());
    TstUi t;
    return QTest::qExec(&t, argc, argv);
}
#include "tst_ui.moc"
