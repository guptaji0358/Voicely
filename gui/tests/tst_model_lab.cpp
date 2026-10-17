// Model Lab GUI tests (offscreen): drive the real dialog against the real model_lab worker (real PyTorch, tiny models,
// temporary lab and data folders). Nothing here is mocked except the confirmation dialogs ("a click on Yes") and the stored
// speech records, which are seeded through the real ExperienceStore the way generations + Recheck Speech would create them.
#include "ui/Indicators.h"
#include "ui/ModelLabDialog.h"

#include <QApplication>
#include <QCheckBox>
#include <QComboBox>
#include <QDir>
#include <QElapsedTimer>
#include <QFile>
#include <QLabel>
#include <QPlainTextEdit>
#include <QProcess>
#include <QPushButton>
#include <QSettings>
#include <QSpinBox>
#include <QTemporaryDir>
#include <QTimer>
#include <QtTest>

class TstModelLab : public QObject {
    Q_OBJECT
    QTemporaryDir lab, dataDir;
    ModelLabDialog *d = nullptr;
    int confirmations = 0;
    bool answer = true;

    template <class T> T *get(const char *name) { auto *x = d->findChild<T *>(name); Q_ASSERT(x); return x; }
    void waitPhase(ModelLabDialog::Phase p, int ms = 120000) { QTRY_VERIFY_WITH_TIMEOUT(d->phase() == p, ms); }
    void waitModels(int n) { QTRY_COMPARE_WITH_TIMEOUT(d->modelCount(), n, 30000); }
    void settle() { waitPhase(ModelLabDialog::Phase::Ready, 180000); QTest::qWait(300); }

    // Seeds stored speech exactly the way the app would: consent on, generations added, Recheck outcomes attached.
    void seedExperience(int yes, int no) {
        QProcess py;
        QProcessEnvironment env = QProcessEnvironment::systemEnvironment();
        env.insert("SPEECH_ENGINE_DATA_DIR", dataDir.path());
        env.insert("PYTHONPATH", qEnvironmentVariable("TTS_ENGINE_ROOT"));
        py.setProcessEnvironment(env);
        py.setWorkingDirectory(qEnvironmentVariable("TTS_ENGINE_ROOT"));
        const QString code = QString(
            "import random\n"
            "from speech_engine.experience import ExperienceStore\n"
            "st = ExperienceStore(); st.set_consent(True); rng = random.Random(1)\n"
            "risky = ['Dr. Patel paid $45 on March 3rd for 5 km', 'NASA read the record', 'Call 555-0100 about the 12 kg order']\n"
            "plain = ['The quiet garden grew slowly', 'We walked beside the calm river', 'A gentle breeze moved the paper']\n"
            "items = [(rng.choice(risky) + f' item {i}', 'needs_review') for i in range(%1)] + [(rng.choice(plain) + f' again {i}', 'verified') for i in range(%2)]\n"
            "rng.shuffle(items)\n"
            "for i, (t, s) in enumerate(items):\n"
            "    st.add_generation(f'sess{i}', [{'index': 0, 'text': t}], 'en-us', 'original', 'local')\n"
            "    st.label_from_recheck(f'sess{i}', 0, s, False, [])\n").arg(yes).arg(no);
        py.start(qEnvironmentVariable("TTS_PYTHON"), {"-c", code});
        QVERIFY(py.waitForFinished(60000));
        QVERIFY2(py.exitCode() == 0, qPrintable(QString::fromUtf8(py.readAllStandardError())));
    }

private slots:
    void initTestCase() {
        QProcessEnvironment env;
        env.insert("MODEL_LAB_DIR", lab.path());
        env.insert("SPEECH_ENGINE_DATA_DIR", dataDir.path());
        env.insert("MODEL_LAB_TEST_AVAILABLE_GB", "6");        // deterministic: independent of what else is running
        d = new ModelLabDialog(env);
        d->setConfirmHandler([this](const QString &, const QString &) { ++confirmations; return answer; });
        d->show();
        waitPhase(ModelLabDialog::Phase::Ready, 90000);
    }
    void cleanupTestCase() { delete d; }

    void workerReportsRealHardwareAndNoModelYet() {
        QVERIFY2(d->statusLabel()->text().contains("ready"), qPrintable(d->statusLabel()->text()));
        QCOMPARE(d->chart()->trainPoints(), 0);                    // nothing is drawn before a real run
        QCOMPARE(d->modelCount(), 0);
        QVERIFY(!get<QPushButton>("labTrain")->isEnabled() && !get<QPushButton>("labLearn")->isEnabled());
    }
    void learningIsOffByDefaultAndTheSwitchIsPersisted() {
        auto *consent = get<QCheckBox>("labConsent");
        QTRY_VERIFY_WITH_TIMEOUT(get<QLabel>("labExperience")->text().contains("OFF"), 10000);
        QVERIFY(!consent->isChecked());
        consent->setChecked(true);                                  // the user's click
        QTRY_VERIFY_WITH_TIMEOUT(get<QLabel>("labExperience")->text().contains("ON"), 10000);
        QVERIFY(QFile::exists(dataDir.path() + "/experience/consent.json"));
        consent->setChecked(false);
        QTRY_VERIFY_WITH_TIMEOUT(get<QLabel>("labExperience")->text().contains("OFF"), 10000);
        consent->setChecked(true);
    }
    void createOnlyOneModel() {
        get<QComboBox>("labSize")->setCurrentText("10K");
        QTest::mouseClick(get<QPushButton>("labCreate"), Qt::LeftButton);        // no dataset yet: a tiny bootstrap one is made
        waitPhase(ModelLabDialog::Phase::Ready);
        waitModels(1);
        QVERIFY(d->stateLabel()->text().contains("Randomly initialized"));
        QVERIFY(!d->activeId().isEmpty());
        const QString first = d->activeId();
        const int before = confirmations;
        QCOMPARE(get<QPushButton>("labCreate")->text(), QString("Replace with a new random model"));
        QTest::mouseClick(get<QPushButton>("labCreate"), Qt::LeftButton);        // replacing asks first
        QTRY_VERIFY_WITH_TIMEOUT(confirmations == before + 1, 5000);
        waitPhase(ModelLabDialog::Phase::Ready);
        QTRY_VERIFY_WITH_TIMEOUT(d->activeId() != first && !d->activeId().isEmpty(), 30000);
        QCOMPARE(d->modelCount(), 1);                                            // never a second model
    }
    void decliningTheReplacementChangesNothing() {
        const QString id = d->activeId();
        answer = false;
        QTest::mouseClick(get<QPushButton>("labCreate"), Qt::LeftButton);
        QTest::qWait(500);
        answer = true;
        QCOMPARE(d->activeId(), id);
        QCOMPARE(d->modelCount(), 1);
    }
    void trainingMakesACandidateAndIsResponsive() {
        QTRY_VERIFY_WITH_TIMEOUT(get<QComboBox>("datasetCombo")->count() >= 1, 20000);
        QTRY_VERIFY_WITH_TIMEOUT(d->memoryLabel()->text().contains("Estimated peak"), 20000);
        d->setHyperparameters(150, 16, 0.003, -1);
        QTest::mouseClick(get<QPushButton>("labTrain"), Qt::LeftButton);
        QCOMPARE(d->phase(), ModelLabDialog::Phase::Training);
        QVERIFY(!get<QPushButton>("labTrain")->isEnabled() && get<QPushButton>("labCancel")->isEnabled());
        qint64 worst = 0, last = 0;
        QElapsedTimer since; since.start();
        QTimer ticker; ticker.setInterval(10);
        connect(&ticker, &QTimer::timeout, [&] { const qint64 n = since.elapsed(); worst = qMax(worst, n - last); last = n; });
        ticker.start();
        settle();
        ticker.stop();
        QVERIFY2(worst < 500, qPrintable(QString("GUI stalled %1 ms during training").arg(worst)));
        QVERIFY2(d->chart()->trainPoints() >= 3 && d->chart()->valPoints() >= 1, "no real training points reached the chart");
        waitModels(2);                                                           // the current model + ONE pending candidate
        QVERIFY(!d->candidateId().isEmpty());
        QVERIFY(d->memoryLabel()->text().contains("Actual peak"));
        QVERIFY(get<QPlainTextEdit>("labEvalText")->toPlainText().contains("rule baseline"));
        QVERIFY(get<QPushButton>("labReject")->isEnabled());
        const bool passed = get<QPlainTextEdit>("labEvalText")->toPlainText().contains("CANDIDATE PASSED");
        QCOMPARE(get<QPushButton>("labAccept")->isEnabled(), passed);            // only a passed candidate can replace the model
    }
    void discardingTheCandidateKeepsTheModel() {
        const QString active = d->activeId();
        QTest::mouseClick(get<QPushButton>("labReject"), Qt::LeftButton);
        waitModels(1);
        QCOMPARE(d->activeId(), active);
        QVERIFY(d->candidateId().isEmpty());
    }
    void notEnoughSpeechDataIsExplainedNotFaked() {
        QTest::mouseClick(get<QPushButton>("labLearn"), Qt::LeftButton);
        settle();
        QVERIFY2(d->lastError().contains("NOT_ENOUGH_DATA") && d->lastError().contains("Recheck Speech"), qPrintable(d->lastError()));
        QCOMPARE(d->modelCount(), 1);
        QVERIFY(d->chart()->trainPoints() == 0 || d->phase() == ModelLabDialog::Phase::Ready);
    }
    void learningFromRecheckedSpeechTrainsACandidateAndAcceptingReplacesTheModel() {
        seedExperience(60, 60);
        d->refresh();
        QTRY_VERIFY_WITH_TIMEOUT(get<QLabel>("labExperience")->text().contains("120 rechecked") ||
                                 get<QLabel>("labExperience")->text().contains("rechecked (60 with issues, 60 verified)"), 20000);
        d->setHyperparameters(250, 32, 0.003, -1);
        const QString before = d->activeId();
        QTest::mouseClick(get<QPushButton>("labLearn"), Qt::LeftButton);
        settle();
        waitModels(2);
        const QString text = get<QPlainTextEdit>("labEvalText")->toPlainText();
        QVERIFY2(text.contains("your own rechecked speech"), qPrintable(text));
        QVERIFY(text.contains("candidate") || text.contains("CANDIDATE"));
        QCOMPARE(d->activeId(), before);                                         // nothing replaced without approval
        QVERIFY(d->chart()->trainPoints() >= 3);
        if (get<QPushButton>("labAccept")->isEnabled()) {
            QTest::mouseClick(get<QPushButton>("labAccept"), Qt::LeftButton);
            waitModels(1);
            QVERIFY(d->activeId() != before);
        } else {
            QTest::mouseClick(get<QPushButton>("labReject"), Qt::LeftButton);
            waitModels(1);
        }
    }
    void deletingStoredSpeechAsksAndRemovesIt() {
        QTRY_VERIFY_WITH_TIMEOUT(get<QPushButton>("labForget")->isEnabled(), 20000);
        const int before = confirmations;
        QTest::mouseClick(get<QPushButton>("labForget"), Qt::LeftButton);
        QTRY_VERIFY_WITH_TIMEOUT(confirmations == before + 1, 5000);
        QTRY_VERIFY_WITH_TIMEOUT(get<QLabel>("labExperience")->text().contains("nothing stored"), 20000);
    }
    void expansionReplacesTheModelAndNeedsTraining() {
        settle();
        const QString before = d->activeId();
        get<QSpinBox>("labTarget")->setValue(60000);
        QTest::mouseClick(get<QPushButton>("labExpand"), Qt::LeftButton);
        waitPhase(ModelLabDialog::Phase::Ready, 60000);
        QTRY_VERIFY_WITH_TIMEOUT(d->activeId() != before && !d->activeId().isEmpty(), 30000);
        QCOMPARE(d->modelCount(), 1);
        QVERIFY(get<QPlainTextEdit>("labEvalText")->toPlainText().contains("Expansion adds capacity, not knowledge"));
        QTRY_VERIFY_WITH_TIMEOUT(d->stateLabel()->text().contains("Expanded"), 10000);
    }
    void oversizedReplacementIsRefusedAndTheModelSurvives() {
        const QString id = d->activeId();
        get<QComboBox>("labSize")->setCurrentText("8B");
        QTest::mouseClick(get<QPushButton>("labCreate"), Qt::LeftButton);
        waitPhase(ModelLabDialog::Phase::Ready, 60000);
        QTRY_VERIFY_WITH_TIMEOUT(d->lastError().contains("cannot instantiate"), 10000);
        QCOMPARE(d->activeId(), id);                                              // refused before anything was deleted
        QCOMPARE(d->modelCount(), 1);
    }
};

int main(int argc, char **argv) {
    qputenv("QT_QPA_PLATFORM", "offscreen");
    qputenv("TTS_REDUCE_MOTION", "1");
    QApplication app(argc, argv);
    QSettings::setDefaultFormat(QSettings::IniFormat);
    QTemporaryDir cfg;
    QSettings::setPath(QSettings::IniFormat, QSettings::UserScope, cfg.path());
    TstModelLab t;
    return QTest::qExec(&t, argc, argv);
}
#include "tst_model_lab.moc"
