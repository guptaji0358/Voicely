// GUI tests (offscreen): drive the real MainWindow against the real worker (mock backend).
#include "MainWindow.h"
#include "ui/AudioDragHandle.h"
#include "ui/LiveAudio.h"

#include <QApplication>
#include <QComboBox>
#include <QElapsedTimer>
#include <QFile>
#include <QLabel>
#include <QMimeData>
#include <QUrl>
#include <QMediaPlayer>
#include <QPlainTextEdit>
#include <QProcess>
#include <QProgressBar>
#include <QSettings>
#include <QSignalSpy>
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
        w->setPreCheckEnabled(false);     // these tests exercise generation directly; the pre-check has its own tests (tst_ui)
        w->show();
        waitState(MainWindow::State::Idle);
        auto *be = get<QComboBox>("backendCombo");
        const int i = be->findData("mock");
        QVERIFY2(i >= 0, "mock backend not offered");
        be->setCurrentIndex(i);
        QVERIFY(get<QLabel>("statusLabel")->text().startsWith("Ready"));
    }
    void cleanupTestCase() { w->setConfirmClose(false); w->close(); delete w; }

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
    void dropTextFileImportsAndRejectsOthers() {
        auto *ed = get<QPlainTextEdit>("textEditor");
        ed->clear();
        QTemporaryDir dir;
        QFile f(dir.filePath("story.txt"));
        QVERIFY(f.open(QIODevice::WriteOnly));
        f.write("Dropped paragraph one.\n\nDropped paragraph two.");
        f.close();
        {   // a text file dropped on the window is imported
            QMimeData mime;
            mime.setUrls({QUrl::fromLocalFile(f.fileName())});
            QDragEnterEvent enter(QPoint(200, 200), Qt::CopyAction, &mime, Qt::LeftButton, Qt::NoModifier);
            QApplication::sendEvent(w, &enter);
            QVERIFY(enter.isAccepted());
            QDropEvent drop(QPointF(200, 200), Qt::CopyAction, &mime, Qt::LeftButton, Qt::NoModifier);
            QApplication::sendEvent(w, &drop);
            QCOMPARE(ed->toPlainText(), QString("Dropped paragraph one.\n\nDropped paragraph two."));
            QVERIFY(get<QLabel>("statusLabel")->text().startsWith("Imported"));
        }
        {   // an unsupported file type is refused and the text is untouched
            QMimeData mime;
            mime.setUrls({QUrl::fromLocalFile(dir.filePath("picture.png"))});
            QDragEnterEvent enter(QPoint(200, 200), Qt::CopyAction, &mime, Qt::LeftButton, Qt::NoModifier);
            QApplication::sendEvent(w, &enter);
            QVERIFY(!enter.isAccepted());
            QDragLeaveEvent leave;
            QApplication::sendEvent(w, &leave);
        }
        {   // plain text is appended
            QMimeData mime;
            mime.setText("Extra words.");
            QDragEnterEvent enter(QPoint(200, 200), Qt::CopyAction, &mime, Qt::LeftButton, Qt::NoModifier);
            QApplication::sendEvent(w, &enter);
            QVERIFY(enter.isAccepted());
            QDropEvent drop(QPointF(200, 200), Qt::CopyAction, &mime, Qt::LeftButton, Qt::NoModifier);
            QApplication::sendEvent(w, &drop);
            QVERIFY2(ed->toPlainText().endsWith("Extra words."), qPrintable(ed->toPlainText() + " | " + get<QLabel>("statusLabel")->text()));
        }
        ed->clear();
    }
    void tempAudioDragFile() {
        get<QPlainTextEdit>("textEditor")->setPlainText("Hello temp audio.");
        QTest::mouseClick(get<QPushButton>("primary"), Qt::LeftButton);
        QCOMPARE(w->state(), MainWindow::State::Generating);
        waitState(MainWindow::State::Idle);
        auto *h = w->findChild<AudioDragHandle *>();
        QVERIFY(h);
        QTRY_COMPARE(h->state(), AudioDragHandle::State::Ready);
        const QString wav = h->fileForDrag();                       // WAV: a temporary copy made on demand
        QVERIFY(QFileInfo::exists(wav));
        QVERIFY(wav.contains("Voicely-audio"));
        QVERIFY(wav.endsWith(".wav", Qt::CaseInsensitive));
        QVERIFY(QFileInfo(wav).fileName().startsWith("hello-temp-audio"));
        emit h->formatChanged("mp3");                               // MP3: exported silently by the worker
        QTRY_VERIFY_WITH_TIMEOUT(h->state() == AudioDragHandle::State::Ready && h->fileForDrag().endsWith(".mp3"), 20000);
        QVERIFY(QFileInfo::exists(h->fileForDrag()));
        QVERIFY(QFileInfo(h->fileForDrag()).size() > 0);
        emit h->formatChanged("wav");
        QCOMPARE(w->state(), MainWindow::State::Idle);
        get<QPlainTextEdit>("textEditor")->clear();                 // later tests expect an empty editor
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
        slow.setPreCheckEnabled(false);
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
        r.setPreCheckEnabled(false);
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

    // ---- document import, limits, counters, remembered settings, unsaved-audio prompt, ETA, live audio ----
    static QString makeDocx(const QString &path, const QStringList &paras) {
        const QString code = QStringLiteral(
            "import sys, zipfile\n"
            "W='http://schemas.openxmlformats.org/wordprocessingml/2006/main'\n"
            "b=''.join('<w:p><w:r><w:t>%s</w:t></w:r></w:p>'%p for p in sys.argv[2:])\n"
            "x='<w:document xmlns:w=\"%s\"><w:body>%s</w:body></w:document>'%(W,b)\n"
            "z=zipfile.ZipFile(sys.argv[1],'w'); z.writestr('word/document.xml',x); z.close()\n");
        QStringList args{"-c", code, path};
        args << paras;
        QProcess::execute(qEnvironmentVariable("TTS_PYTHON", "python"), args);
        return path;
    }
    void importsWordDocumentThroughWorker() {
        auto *ed = get<QPlainTextEdit>("textEditor");
        ed->setPlainText("old text");
        QTemporaryDir d;
        const QString docx = makeDocx(d.filePath("story.docx"), {"First paragraph.", "Second paragraph."});
        QVERIFY(QFileInfo::exists(docx));
        QVERIFY(w->importFile(docx));                                   // started; the reading happens in the worker
        QTRY_COMPARE_WITH_TIMEOUT(ed->toPlainText(), QString("First paragraph.\n\nSecond paragraph."), 20000);
        QVERIFY(get<QLabel>("statusLabel")->text().contains("DOCX"));
        QVERIFY(get<QPushButton>("importButton")->isEnabled());         // import finished: controls are back
        QCOMPARE(w->state(), MainWindow::State::Idle);
        // a damaged document reports why and leaves the text alone
        QFile bad(d.filePath("broken.docx"));
        QVERIFY(bad.open(QIODevice::WriteOnly));
        bad.write("not a zip at all");
        bad.close();
        QVERIFY(w->importFile(bad.fileName()));
        QTRY_VERIFY_WITH_TIMEOUT(get<QLabel>("statusLabel")->text().contains("not a valid .docx"), 20000);
        QCOMPARE(ed->toPlainText(), QString("First paragraph.\n\nSecond paragraph."));
        ed->clear();
    }
    void droppedPdfDocxEpubAreAccepted() {
        QTemporaryDir d;
        for (const char *ext : {"pdf", "docx", "epub"}) {
            QFile f(d.filePath(QString("a.") + ext));
            QVERIFY(f.open(QIODevice::WriteOnly));
            f.close();
            QMimeData mime;
            mime.setUrls({QUrl::fromLocalFile(f.fileName())});
            QDragEnterEvent enter(QPoint(200, 200), Qt::CopyAction, &mime, Qt::LeftButton, Qt::NoModifier);
            QApplication::sendEvent(w, &enter);
            QVERIFY2(enter.isAccepted(), ext);
        }
        QMimeData exe;
        exe.setUrls({QUrl::fromLocalFile(d.filePath("a.exe"))});
        QDragEnterEvent enter(QPoint(200, 200), Qt::CopyAction, &exe, Qt::LeftButton, Qt::NoModifier);
        QApplication::sendEvent(w, &enter);
        QVERIFY(!enter.isAccepted());
    }
    void nothingToReadAndTooLongAreRejectedWithAReason() {
        auto *ed = get<QPlainTextEdit>("textEditor");
        auto *status = get<QLabel>("statusLabel");
        ed->setPlainText("!!! ... ???");
        QTest::mouseClick(get<QPushButton>("primary"), Qt::LeftButton);
        QCOMPARE(w->state(), MainWindow::State::Idle);
        QVERIFY2(status->text().contains("nothing to read aloud"), qPrintable(status->text()));
        ed->setPlainText(QString(20000 + 25, 'a'));
        QVERIFY(get<QLabel>("qaNote")->text().contains("Too long by 25"));
        QTest::mouseClick(get<QPushButton>("primary"), Qt::LeftButton);
        QCOMPARE(w->state(), MainWindow::State::Idle);
        QVERIFY2(status->text().contains("too long") && status->text().contains("Remove about 25"), qPrintable(status->text()));
        ed->clear();
        QVERIFY(get<QLabel>("qaNote")->text().isEmpty());
    }
    void countsShowReadingTime() {
        QCOMPARE(MainWindow::formatDuration(7), QString("7 s"));
        QCOMPARE(MainWindow::formatDuration(187), QString("3:07"));
        QCOMPARE(MainWindow::formatDuration(3729), QString("1:02:09"));
        QCOMPARE(MainWindow::estimateSpeechSeconds(150, 1.0), 60);
        QCOMPARE(MainWindow::estimateSpeechSeconds(150, 2.0), 30);
        auto *ed = get<QPlainTextEdit>("textEditor");
        ed->setPlainText(QString("word ").repeated(300));
        QString line;
        for (auto *l : w->findChildren<QLabel *>()) if (l->text().contains(" words")) line = l->text();
        QVERIFY2(line.contains("300 words") && line.contains("about 2:00 of audio"), qPrintable(line));
        ed->clear();
    }
    void choicesAreRememberedBetweenRuns() {
        auto *speed = get<QComboBox>("speedSpin");
        auto *voice = get<QComboBox>("voiceCombo");
        const int vi = voice->findData("deep");
        QVERIFY(vi >= 0);
        voice->setCurrentIndex(vi);
        speed->setEditText("1.25");
        get<QPlainTextEdit>("textEditor")->setPlainText("Remember me.");
        QTest::mouseClick(get<QPushButton>("primary"), Qt::LeftButton);    // generating stores the choices
        waitState(MainWindow::State::Idle);
        MainWindow again;
        again.show();
        QTRY_COMPARE_WITH_TIMEOUT(again.state(), MainWindow::State::Idle, 30000);
        QTRY_COMPARE_WITH_TIMEOUT(again.findChild<QComboBox *>("voiceCombo")->currentData().toString(), QString("deep"), 5000);
        QCOMPARE(again.findChild<QComboBox *>("speedSpin")->currentText(), QString("1.25"));
        again.close();
        speed->setEditText("1.00");
        voice->setCurrentIndex(voice->findData("original"));
        get<QPlainTextEdit>("textEditor")->clear();
    }
    void unsavedAudioAsksBeforeClosing() {
        MainWindow c;
        c.setPreCheckEnabled(false);
        c.show();
        QTRY_COMPARE_WITH_TIMEOUT(c.state(), MainWindow::State::Idle, 30000);
        auto *be = c.findChild<QComboBox *>("backendCombo");
        be->setCurrentIndex(be->findData("mock"));
        QVERIFY(!c.hasUnsavedAudio());                                    // nothing generated yet
        c.findChild<QPlainTextEdit *>("textEditor")->setPlainText("Please keep this audio.");
        QTest::mouseClick(c.findChild<QPushButton *>("primary"), Qt::LeftButton);
        QTRY_VERIFY_WITH_TIMEOUT(c.state() == MainWindow::State::Idle && c.hasUnsavedAudio(), 30000);
        c.setConfirmClose(true);
        auto press = [](const QString &label) {                           // answers the modal dialog the close raises
            QTimer::singleShot(300, [label] {
                QWidget *m = QApplication::activeModalWidget();
                if (!m) return;
                for (auto *b : m->findChildren<QPushButton *>()) if (b->text() == label) { b->click(); return; }
            });
        };
        press("Cancel");
        QVERIFY(!c.close());                                              // "go back": the window stays open
        QVERIFY(c.isVisible());
        QTemporaryDir d;                                                  // saving (any way) means there is nothing to lose
        c.exportAudio(d.filePath("kept.wav"), "wav");
        QTRY_VERIFY_WITH_TIMEOUT(c.state() == MainWindow::State::Idle, 20000);
        QVERIFY(!c.hasUnsavedAudio());
        QVERIFY(c.close());                                               // no prompt any more
        MainWindow e;                                                     // and "Discard" closes without saving
        e.setPreCheckEnabled(false);
        e.show();
        QTRY_COMPARE_WITH_TIMEOUT(e.state(), MainWindow::State::Idle, 30000);
        e.findChild<QComboBox *>("backendCombo")->setCurrentIndex(e.findChild<QComboBox *>("backendCombo")->findData("mock"));
        e.findChild<QPlainTextEdit *>("textEditor")->setPlainText("Throw this away.");
        QTest::mouseClick(e.findChild<QPushButton *>("primary"), Qt::LeftButton);
        QTRY_VERIFY_WITH_TIMEOUT(e.state() == MainWindow::State::Idle && e.hasUnsavedAudio(), 30000);
        e.setConfirmClose(true);
        press("Discard");
        QVERIFY(e.close());
    }
    void longGenerationShowsTimeLeftAndCanBeCancelled() {
        qputenv("SPEECH_ENGINE_MOCK_DELAY", "0.35");
        MainWindow slow;
        slow.setPreCheckEnabled(false);
        slow.show();
        QTRY_COMPARE_WITH_TIMEOUT(slow.state(), MainWindow::State::Idle, 30000);
        auto *be = slow.findChild<QComboBox *>("backendCombo");
        be->setCurrentIndex(be->findData("mock"));
        slow.findChild<QPlainTextEdit *>("textEditor")->setPlainText(longText(40));
        QTest::mouseClick(slow.findChild<QPushButton *>("primary"), Qt::LeftButton);
        QTRY_VERIFY_WITH_TIMEOUT(slow.findChild<QLabel *>("statusLabel")->text().contains("left"), 20000);
        QVERIFY(slow.findChild<QLabel *>("statusLabel")->text().contains("chunk"));
        QTest::mouseClick(slow.findChild<QPushButton *>("cancelButton"), Qt::LeftButton);
        QTRY_COMPARE_WITH_TIMEOUT(slow.state(), MainWindow::State::Idle, 10000);
        QVERIFY(!slow.hasUnsavedAudio());                                  // a cancelled job leaves nothing to lose
        slow.close();
        qputenv("SPEECH_ENGINE_MOCK_DELAY", "0");
    }
    void liveAudioPlaysWhatIsAppended() {
        if (!LiveAudio::available()) QSKIP("no audio output device");
        LiveAudio live;
        QSignalSpy started(&live, &LiveAudio::playbackStarted);
        if (!live.begin(24000)) QSKIP("device cannot play 24 kHz mono");
        QVERIFY(live.active());
        QByteArray tone(24000 * 2 * 2, '\0');                              // 2 s of a quiet 440 Hz tone
        auto *s = reinterpret_cast<qint16 *>(tone.data());
        for (int i = 0; i < 24000 * 2; ++i) s[i] = qint16(800 * std::sin(2 * M_PI * 440.0 * i / 24000.0));
        live.append(tone.left(24000));                                     // under the 0.35 s pre-roll: waits
        QTest::qWait(150);
        live.append(tone.mid(24000));
        QCOMPARE(live.queuedMs(), qint64(2000));
        QTRY_VERIFY_WITH_TIMEOUT(started.count() == 1, 3000);
        QTRY_VERIFY_WITH_TIMEOUT(live.playedMs() > 300, 3000);
        live.pause();
        const qint64 at = live.playedMs();
        QTest::qWait(300);
        QVERIFY(live.playedMs() - at < 120);                               // paused: the position does not run on
        live.resume();
        QTRY_VERIFY_WITH_TIMEOUT(live.playedMs() > at + 200, 3000);
        live.stop();
        QVERIFY(!live.active());
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
    qputenv("SPEECH_ENGINE_MAX_TEXT_CHARS", "20000");   // the worker reports this limit; keeps the too-long test fast
    qputenv("TTS_NO_CLOSE_PROMPT", "1");     // windows closed by tests must not wait for a person
    QApplication app(argc, argv);
    QSettings::setDefaultFormat(QSettings::IniFormat);
    QTemporaryDir cfg;
    QSettings::setPath(QSettings::IniFormat, QSettings::UserScope, cfg.path());
    QFile::remove(QDir::tempPath() + "/tts_gui_test_export.mp3");
    TstGui t;
    return QTest::qExec(&t, argc, argv);
}
#include "tst_gui.moc"
