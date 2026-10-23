// End-to-end GUI workflow for pronunciation QA: pre-check -> generate -> Recheck Speech -> correct -> repair ->
// accept -> recheck -> MP3 export, driving the real MainWindow, the real worker process and the real session files.
//
// MOCKED (and labelled as such in the reports): the TTS backend (sine tones), the ASR (scripted transcripts from
// SPEECH_ENGINE_MOCK_ASR_FILE). Nothing here proves a real recogniser finds real mispronunciations.
#include "MainWindow.h"
#include "ui/DictionaryDialog.h"
#include "ui/Indicators.h"
#include "ui/QaPanel.h"
#include "ui/ReviewDialog.h"

#include <QApplication>
#include <QComboBox>
#include <QDialogButtonBox>
#include <QElapsedTimer>
#include <QFile>
#include <QJsonDocument>
#include <QJsonObject>
#include <QLabel>
#include <QLineEdit>
#include <QListWidget>
#include <QPlainTextEdit>
#include <QPushButton>
#include <QTemporaryDir>
#include <QTimer>
#include <QtTest>

namespace {
const char *kText = "Alpha beta gamma.\n\nPlease call Nguyen now.\n\nDelta epsilon zeta.";

template <class F> void onModal(F fn) {
    auto *t = new QTimer(qApp);
    t->setInterval(40);
    QObject::connect(t, &QTimer::timeout, qApp, [t, fn] {
        if (QWidget *m = QApplication::activeModalWidget()) {
            t->stop();
            t->deleteLater();
            fn(m);
        }
    });
    t->start();
}
}  // namespace

class TstQaGui : public QObject {
    Q_OBJECT
    MainWindow *w = nullptr;
    QaPanel *qa = nullptr;
    template <class T> T *get(const char *name) { auto *x = w->findChild<T *>(name); Q_ASSERT_X(x, name, "widget not found"); return x; }
    void waitState(MainWindow::State s, int ms = 40000) {
        QElapsedTimer t; t.start();
        while (w->state() != s && t.elapsed() < ms) QTest::qWait(10);
        QCOMPARE(w->state(), s);
    }
    void snap(const char *name) {
        const QString dir = qEnvironmentVariable("TTS_TEST_SNAPSHOT_DIR");
        if (dir.isEmpty()) return;
        QTest::qWait(450);                         // let the short fade-in transitions finish before grabbing
        w->grab().save(QDir(dir).filePath(QString(name) + ".png"));
    }

private slots:
    void initTestCase() {
        w = new MainWindow;
        w->resize(1180, 820);
        w->show();
        waitState(MainWindow::State::Idle);
        auto *be = get<QComboBox>("backendCombo");
        be->setCurrentIndex(be->findData("mock"));
        qa = w->findChild<QaPanel *>();
        QVERIFY(qa);
        QTRY_VERIFY_WITH_TIMEOUT(get<QPushButton>("recheckButton") != nullptr, 1000);
    }
    void cleanupTestCase() { w->close(); delete w; }

    void precheckShowsReviewDialogThenGenerates() {
        QVERIFY(w->preCheckEnabled());
        get<QPlainTextEdit>("textEditor")->setPlainText(kText);
        int rows = -1;
        onModal([&rows](QWidget *m) {
            auto *d = qobject_cast<ReviewDialog *>(m);
            QVERIFY(d);
            rows = d->findingRows();
            d->findChild<QPushButton *>("reviewContinue")->click();
        });
        QTest::mouseClick(get<QPushButton>("primary"), Qt::LeftButton);
        QTRY_VERIFY_WITH_TIMEOUT(rows >= 0, 20000);
        QVERIFY2(rows >= 1, "the analysis should flag at least the name");
        waitState(MainWindow::State::Generating, 20000);
        waitState(MainWindow::State::Idle);
        QVERIFY(!w->sessionDir().isEmpty() && QDir(w->sessionDir()).exists());
        QVERIFY(QFileInfo::exists(w->sessionDir() + "/assembled/original.wav"));
        QCOMPARE(qa->status(), QaPanel::Status::NotChecked);              // generated, but NOT verified
        QVERIFY(get<QPushButton>("recheckButton")->isEnabled());
        snap("01_generated_not_checked");
    }
    void recheckFindsTheSuspectedWordAndShowsItSideBySide() {
        QTest::mouseClick(get<QPushButton>("recheckButton"), Qt::LeftButton);
        QCOMPARE(w->state(), MainWindow::State::Rechecking);
        QVERIFY(!get<QPushButton>("primary")->isEnabled());              // no overlapping work
        waitState(MainWindow::State::Idle);
        QCOMPARE(qa->status(), QaPanel::Status::NeedsReview);
        QCOMPARE(qa->issueCount(), 1);
        const QJsonObject issue = qa->currentIssue();
        QCOMPARE(issue.value("intended").toString(), QString("Nguyen"));
        QVERIFY(get<QPlainTextEdit>("intendedText")->toPlainText().contains("Please call Nguyen now."));
        QCOMPARE(get<QPlainTextEdit>("recognizedText")->toPlainText(), QString("please call win now"));
        QVERIFY(qa->reportText().contains("MOCKED"));                     // the report says the recogniser is mocked
        QVERIFY(qa->reportText().contains("not phonetic proof"));
        snap("02_needs_review");
    }
    void correctionRepairsOnlyThatChunkAndKeepsTheOriginal() {
        const QString sess = w->sessionDir();
        QFile f(sess + "/assembled/original.wav");
        QVERIFY(f.open(QIODevice::ReadOnly));
        const QByteArray originalBytes = f.readAll();
        f.close();
        onModal([](QWidget *m) {
            auto *ed = qobject_cast<EntryEditor *>(m);
            QVERIFY(ed);
            ed->findChild<QLineEdit *>("entryHint")->setText("nwin");
            ed->findChild<QPushButton *>("primary")->click();
        });
        QTest::mouseClick(get<QPushButton>("correctButton"), Qt::LeftButton);
        QTRY_COMPARE_WITH_TIMEOUT(w->state(), MainWindow::State::Repairing, 20000);
        waitState(MainWindow::State::Idle);
        QVERIFY(get<QPushButton>("acceptRepairButton")->isVisibleTo(w));
        QVERIFY(get<QPushButton>("versionRepaired")->isVisibleTo(w));     // original / repaired toggle appears
        QVERIFY(QFileInfo::exists(sess + "/chunks/0001.g1.wav"));
        QVERIFY(!QFileInfo::exists(sess + "/chunks/0000.g1.wav"));        // only the affected chunk was regenerated
        QVERIFY(!QFileInfo::exists(sess + "/chunks/0002.g1.wav"));
        QVERIFY(f.open(QIODevice::ReadOnly));
        QCOMPARE(f.readAll(), originalBytes);                              // original audio untouched
        QVERIFY(w->lastAudioPath().endsWith("candidate.wav"));
        snap("03_repaired_candidate");
    }
    void acceptRecheckAndVerifiedExport() {
        QTest::mouseClick(get<QPushButton>("acceptRepairButton"), Qt::LeftButton);
        QTRY_VERIFY_WITH_TIMEOUT(w->lastAudioPath().endsWith("accepted.wav"), 10000);
        QTRY_VERIFY_WITH_TIMEOUT(!get<QPushButton>("acceptRepairButton")->isVisibleTo(w), 5000);
        QTest::mouseClick(get<QPushButton>("recheckButton"), Qt::LeftButton);
        waitState(MainWindow::State::Rechecking, 5000);
        waitState(MainWindow::State::Idle);
        QCOMPARE(qa->status(), QaPanel::Status::Verified);
        QVERIFY(qa->reportText().contains("limitations", Qt::CaseInsensitive));
        QTemporaryDir d;
        w->exportAudio(d.filePath("qa test.mp3"), "mp3");
        waitState(MainWindow::State::Idle);
        QVERIFY(get<QLabel>("outputLabel")->text().contains("speech check: verified"));
        QVERIFY(get<SuccessBadge>("successBadge")->isVisible());
        snap("04_verified_export");
    }
    void skippingRecheckIsAllowedAndStateIsClear() {
        get<QPlainTextEdit>("textEditor")->setPlainText("Alpha beta gamma.");
        w->setPreCheckEnabled(false);
        QTest::mouseClick(get<QPushButton>("primary"), Qt::LeftButton);
        waitState(MainWindow::State::Generating, 5000);
        waitState(MainWindow::State::Idle);
        QCOMPARE(qa->status(), QaPanel::Status::NotChecked);              // new audio resets the verification state
        QTemporaryDir d;
        w->exportAudio(d.filePath("skip.mp3"), "mp3");                     // exporting without a recheck works
        waitState(MainWindow::State::Idle);
        QVERIFY(get<QLabel>("outputLabel")->text().contains("speech check: not checked"));
    }
};

int main(int argc, char **argv) {
    qputenv("QT_QPA_PLATFORM", "offscreen");
    QApplication app(argc, argv);
    QSettings::setDefaultFormat(QSettings::IniFormat);
    QTemporaryDir cfg, data;
    QSettings::setPath(QSettings::IniFormat, QSettings::UserScope, cfg.path());
    // scripted ASR for the MOCK recogniser: chunk files are named NNNN.gV.wav
    QFile f(data.filePath("asr.json"));
    f.open(QIODevice::WriteOnly);
    f.write(R"({"0000.g0.wav":"alpha beta gamma","0001.g0.wav":"please call win now","0002.g0.wav":"delta epsilon zeta",
                "0001.g1.wav":"please call nwin now"})");
    f.close();
    qputenv("SPEECH_ENGINE_ASR", "mock");
    qputenv("SPEECH_ENGINE_MOCK_ASR_FILE", f.fileName().toUtf8());
    qputenv("SPEECH_ENGINE_DATA_DIR", data.filePath("data").toUtf8());
    QSettings s(QSettings::defaultFormat(), QSettings::UserScope, "TTSStudio", "TTSStudio");
    s.setValue("qaThresholds", R"({"min_rate":0,"max_rate":1e9})");     // the mock backend emits tones, not speech
    s.sync();
    TstQaGui t;
    return QTest::qExec(&t, argc, argv);
}
#include "tst_qa_gui.moc"
