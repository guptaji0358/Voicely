// GUI tests (offscreen): drive the real MainWindow against the real worker (mock backend).
#include "MainWindow.h"

#include <QApplication>
#include <QComboBox>
#include <QElapsedTimer>
#include <QFile>
#include <QLabel>
#include <QMediaPlayer>
#include <QPlainTextEdit>
#include <QProgressBar>
#include <QPushButton>
#include <QTemporaryDir>
#include <QTimer>
#include <QtTest>

class TstGui : public QObject {
    Q_OBJECT
    MainWindow *w = nullptr;
    template <class T> T *get(const char *name) { auto *x = w->findChild<T *>(name); Q_ASSERT(x); return x; }
    static QString longText(int n) {
        QString s;
        for (int i = 0; i < n; ++i) s += QString("Sentence number %1 is here.\n\n").arg(i);
        return s;
    }
    void waitState(MainWindow::State s, int ms = 30000) {
        QElapsedTimer t; t.start();
        while (w->state() != s && t.elapsed() < ms) QTest::qWait(10);
        QCOMPARE(w->state(), s);
    }

private slots:
    void initTestCase() {
        w = new MainWindow;
        w->show();
        waitState(MainWindow::State::Idle);
        auto *be = get<QComboBox>("backendCombo");
        const int i = be->findData("mock");
        QVERIFY2(i >= 0, "mock backend not offered");
        be->setCurrentIndex(i);
        QVERIFY(get<QLabel>("statusLabel")->text().startsWith("Ready"));
    }
    void cleanupTestCase() { w->close(); delete w; }

    void countsAndClear() {
        auto *ed = get<QPlainTextEdit>("textEditor");
        ed->setPlainText("Hello world.\n\nSecond paragraph é.");
        QVERIFY(w->findChildren<QLabel *>().at(0) != nullptr);
        bool found = false;
        for (auto *l : w->findChildren<QLabel *>()) if (l->text().contains("words")) found = l->text().startsWith("33") || l->text().contains("5 words");
        QVERIFY(found);
        QTest::mouseClick(get<QPushButton>("clearButton"), Qt::LeftButton);
        QVERIFY(ed->toPlainText().isEmpty());
    }
    void emptyTextIsRejected() {
        QTest::mouseClick(get<QPushButton>("primary"), Qt::LeftButton);
        QCOMPARE(w->state(), MainWindow::State::Idle);
        QVERIFY(get<QLabel>("statusLabel")->text().contains("Enter or import"));
    }
    void generateIsResponsiveAndCancellable() {
        qputenv("SPEECH_ENGINE_MOCK_DELAY", "0");   // (worker already started; delay applies via text length only)
        get<QPlainTextEdit>("textEditor")->setPlainText(longText(40));
        qint64 worst = 0;
        QElapsedTimer since; since.start();
        qint64 last = 0;
        QTimer ticker; ticker.setInterval(10);
        connect(&ticker, &QTimer::timeout, [&] { qint64 n = since.elapsed(); worst = qMax(worst, n - last); last = n; });
        ticker.start();
        auto *gen = get<QPushButton>("primary");
        QTest::mouseClick(gen, Qt::LeftButton);
        QCOMPARE(w->state(), MainWindow::State::Generating);
        QVERIFY(!gen->isEnabled());                       // duplicate requests impossible
        QTest::mouseClick(gen, Qt::LeftButton);           // ignored
        QVERIFY(get<QPushButton>("cancelButton")->isVisibleTo(w));
        waitState(MainWindow::State::Idle);
        ticker.stop();
        QVERIFY2(worst < 400, qPrintable(QString("event loop stalled %1 ms").arg(worst)));
        QVERIFY(QFileInfo::exists(w->lastAudioPath()));
        QVERIFY(get<QPushButton>("saveMp3Button")->isEnabled());
    }
    void cancelMidGeneration() {
        // New window with a slow worker so there is time to cancel.
        qputenv("SPEECH_ENGINE_MOCK_DELAY", "0.3");
        MainWindow slow;
        slow.show();
        QElapsedTimer t; t.start();
        while (slow.state() != MainWindow::State::Idle && t.elapsed() < 30000) QTest::qWait(10);
        QCOMPARE(slow.state(), MainWindow::State::Idle);
        slow.findChild<QComboBox *>("backendCombo")->setCurrentIndex(slow.findChild<QComboBox *>("backendCombo")->findData("mock"));
        slow.findChild<QPlainTextEdit *>("textEditor")->setPlainText(longText(30));
        QTest::mouseClick(slow.findChild<QPushButton *>("primary"), Qt::LeftButton);
        QTRY_COMPARE(slow.state(), MainWindow::State::Generating);
        QTest::qWait(500);
        QTest::mouseClick(slow.findChild<QPushButton *>("cancelButton"), Qt::LeftButton);
        t.restart();
        while (slow.state() != MainWindow::State::Idle && t.elapsed() < 5000) QTest::qWait(10);
        QCOMPARE(slow.state(), MainWindow::State::Idle);
        QVERIFY(slow.findChild<QLabel *>("statusLabel")->text().contains("Cancelled"));
        // A new request after cancel works
        slow.findChild<QPlainTextEdit *>("textEditor")->setPlainText("Short one.");
        QTest::mouseClick(slow.findChild<QPushButton *>("primary"), Qt::LeftButton);
        t.restart();
        while (slow.state() != MainWindow::State::Idle && t.elapsed() < 20000) QTest::qWait(10);
        QVERIFY(QFileInfo::exists(slow.lastAudioPath()));
        slow.close();
        qputenv("SPEECH_ENGINE_MOCK_DELAY", "0");
    }
    void repeatedGenerationReplacesAudio() {
        get<QPlainTextEdit>("textEditor")->setPlainText("One.");
        QStringList paths;
        for (int i = 0; i < 3; ++i) {
            QTest::mouseClick(get<QPushButton>("primary"), Qt::LeftButton);
            waitState(MainWindow::State::Idle);
            paths << w->lastAudioPath();
        }
        QCOMPARE(QSet<QString>(paths.begin(), paths.end()).size(), 3);
        QTRY_VERIFY_WITH_TIMEOUT(!QFileInfo::exists(paths[0]), 5000);            // previous preview released + removed
        QVERIFY(QFileInfo::exists(paths[2]));
    }
    void playbackOfGeneratedAudio() {
        auto *play = get<QPushButton>("playButton");
        auto *player = w->findChild<QMediaPlayer *>();
        QVERIFY(player);
        QTRY_VERIFY_WITH_TIMEOUT(player->mediaStatus() == QMediaPlayer::LoadedMedia ||
                                 player->mediaStatus() == QMediaPlayer::BufferedMedia, 10000);
        QVERIFY(player->duration() > 100);
        QTest::mouseClick(play, Qt::LeftButton);
        QTRY_VERIFY_WITH_TIMEOUT(player->playbackState() == QMediaPlayer::PlayingState, 5000);
        QTRY_VERIFY_WITH_TIMEOUT(player->position() > 0, 5000);
        QTest::mouseClick(play, Qt::LeftButton);          // pause
        QCOMPARE(player->playbackState(), QMediaPlayer::PausedState);
        QCOMPARE(play->text(), QString("Play"));
        player->setPosition(100);
        QTRY_VERIFY(qAbs(player->position() - 100) < 400);
        QTest::mouseClick(get<QPushButton>("stopButton"), Qt::LeftButton);
        QCOMPARE(player->playbackState(), QMediaPlayer::StoppedState);
    }
    void exportMp3AndWav() {
        QTemporaryDir d;
        const QString dir = d.filePath("é folder 音");
        QDir().mkpath(dir);
        const QString mp3 = dir + "/speech out.mp3", wav = dir + "/speech out.wav";
        w->exportAudio(mp3, "mp3");
        QCOMPARE(w->state(), MainWindow::State::Exporting);
        waitState(MainWindow::State::Idle);
        QFile f(mp3); QVERIFY(f.open(QIODevice::ReadOnly));
        const QByteArray h = f.read(3);
        QVERIFY(h == "ID3" || uchar(h[0]) == 0xFF);
        QVERIFY(get<QLabel>("outputLabel")->text().startsWith("Saved"));
        w->exportAudio(wav, "wav");
        waitState(MainWindow::State::Idle);
        QFile g(wav); QVERIFY(g.open(QIODevice::ReadOnly));
        QCOMPARE(g.read(4), QByteArray("RIFF"));
        qputenv("TTS_TEST_MP3", mp3.toUtf8());           // for the external decoder check
        QFile::copy(mp3, QDir::tempPath() + "/tts_gui_test_export.mp3");
    }
    void realLocalBackendEndToEnd() {   // real Kokoro speech; skipped unless the model is installed
        MainWindow r;
        r.show();
        QElapsedTimer t; t.start();
        while (r.state() != MainWindow::State::Idle && t.elapsed() < 60000) QTest::qWait(10);
        auto *be = r.findChild<QComboBox *>("backendCombo");
        if (r.state() != MainWindow::State::Idle || be->findData("local") < 0) QSKIP("local backend not available");
        be->setCurrentIndex(be->findData("local"));
        QCOMPARE(r.findChild<QComboBox *>("languageCombo")->currentData().toString(), QString("en-us"));
        r.findChild<QPlainTextEdit *>("textEditor")->setPlainText("This is a real speech test.\n\nSecond paragraph.");
        QTest::mouseClick(r.findChild<QPushButton *>("primary"), Qt::LeftButton);
        t.restart();
        while (r.state() != MainWindow::State::Idle && t.elapsed() < 120000) QTest::qWait(10);
        QVERIFY2(QFileInfo(r.lastAudioPath()).size() > 50000, "no real audio produced");
        auto *player = r.findChild<QMediaPlayer *>();
        QTRY_VERIFY_WITH_TIMEOUT(player->duration() > 2000, 10000);
        QTest::mouseClick(r.findChild<QPushButton *>("playButton"), Qt::LeftButton);
        QTRY_VERIFY_WITH_TIMEOUT(player->position() > 300, 5000);
        QTemporaryDir d;
        r.exportAudio(d.filePath("real speech.mp3"), "mp3");
        t.restart();
        while (r.state() != MainWindow::State::Idle && t.elapsed() < 30000) QTest::qWait(10);
        QFile::copy(d.filePath("real speech.mp3"), QDir::tempPath() + "/tts_gui_real_export.mp3");
        QVERIFY(QFileInfo(d.filePath("real speech.mp3")).size() > 20000);
        r.close();
    }
    void errorFromBackendIsShown() {
        get<QPlainTextEdit>("textEditor")->setPlainText("This will fail [[fail]]");
        QTest::mouseClick(get<QPushButton>("primary"), Qt::LeftButton);
        waitState(MainWindow::State::Idle);
        QVERIFY(get<QLabel>("statusLabel")->text().contains("SYNTHESIS_FAILED"));
    }
};

int main(int argc, char **argv) {
    qputenv("QT_QPA_PLATFORM", "offscreen");
    QApplication app(argc, argv);
    QSettings::setDefaultFormat(QSettings::IniFormat);
    QTemporaryDir cfg;
    QSettings::setPath(QSettings::IniFormat, QSettings::UserScope, cfg.path());
    QFile::remove(QDir::tempPath() + "/tts_gui_test_export.mp3");
    TstGui t;
    return QTest::qExec(&t, argc, argv);
}
#include "tst_gui.moc"
