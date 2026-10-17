// Model Lab GUI tests (offscreen): drive the real dialog against the real model_lab worker (real PyTorch, tiny model,
// temporary lab folder). Nothing here is mocked except the confirmation dialogs.
#include "ui/Indicators.h"
#include "ui/ModelLabDialog.h"

#include <QApplication>
#include <QComboBox>
#include <QDir>
#include <QElapsedTimer>
#include <QLabel>
#include <QPlainTextEdit>
#include <QPushButton>
#include <QSettings>
#include <QSpinBox>
#include <QTemporaryDir>
#include <QTimer>
#include <QtTest>

class TstModelLab : public QObject {
    Q_OBJECT
    QTemporaryDir lab;
    ModelLabDialog *d = nullptr;

    template <class T> T *get(const char *name) { auto *x = d->findChild<T *>(name); Q_ASSERT(x); return x; }
    void waitPhase(ModelLabDialog::Phase p, int ms = 120000) {
        QTRY_VERIFY_WITH_TIMEOUT(d->phase() == p, ms);
    }

private slots:
    void initTestCase() {
        QProcessEnvironment env;
        env.insert("MODEL_LAB_DIR", lab.path());
        env.insert("SPEECH_ENGINE_DATA_DIR", lab.path() + "/data");
        d = new ModelLabDialog(env);
        d->setConfirmHandler([](const QString &, const QString &) { return true; });   // the only stand-in: a click on "Yes"
        d->show();
        waitPhase(ModelLabDialog::Phase::Ready, 90000);
    }
    void cleanupTestCase() { delete d; }

    void workerReportsRealHardware() {
        QVERIFY2(d->statusLabel()->text().contains("ready"), qPrintable(d->statusLabel()->text()));
        QVERIFY(d->findChild<QLabel *>()->isVisible());
        QCOMPARE(d->chart()->trainPoints(), 0);                    // nothing is drawn before a real run
    }
    void datasetModelAndFirstState() {
        QTest::mouseClick(get<QPushButton>("labMakeData"), Qt::LeftButton);
        waitPhase(ModelLabDialog::Phase::Ready);
        QTRY_VERIFY_WITH_TIMEOUT(get<QComboBox>("datasetCombo")->count() == 1, 20000);
        get<QComboBox>("labSize")->setCurrentText("10K");
        QTest::mouseClick(get<QPushButton>("labCreate"), Qt::LeftButton);
        waitPhase(ModelLabDialog::Phase::Ready);
        QTRY_COMPARE_WITH_TIMEOUT(d->modelCount(), 1, 20000);
        QVERIFY(d->stateLabel()->text().contains("Randomly initialized"));
        QVERIFY(!get<QPushButton>("labPromote")->isEnabled());      // random model can never be promoted
    }
    void estimateIsShownBeforeTraining() {
        QTRY_VERIFY_WITH_TIMEOUT(d->memoryLabel()->text().contains("Estimated peak"), 10000);
    }
    void trainingIsRealResponsiveAndRecorded() {
        d->setHyperparameters(120, 16, 0.003, -1);
        auto *train = get<QPushButton>("labTrain");
        QTest::mouseClick(train, Qt::LeftButton);
        QCOMPARE(d->phase(), ModelLabDialog::Phase::Training);
        QVERIFY(!train->isEnabled());
        QVERIFY(get<QPushButton>("labCancel")->isEnabled());
        qint64 worst = 0, last = 0;
        QElapsedTimer since; since.start();
        QTimer ticker; ticker.setInterval(10);
        connect(&ticker, &QTimer::timeout, [&] { const qint64 n = since.elapsed(); worst = qMax(worst, n - last); last = n; });
        ticker.start();
        waitPhase(ModelLabDialog::Phase::Ready, 180000);
        ticker.stop();
        QVERIFY2(worst < 500, qPrintable(QString("GUI stalled %1 ms during training").arg(worst)));
        QVERIFY2(d->chart()->trainPoints() >= 3, "no real training points reached the chart");
        QVERIFY(d->chart()->valPoints() >= 1);
        QTRY_COMPARE_WITH_TIMEOUT(d->modelCount(), 2, 20000);       // a NEW checkpoint entry, the original is kept
        QVERIFY(d->memoryLabel()->text().contains("Actual peak"));
    }
    void evaluateSetsStateAndPromotionNeedsApproval() {
        QVERIFY(d->selectModelByState("trained"));
        QTest::mouseClick(get<QPushButton>("labEval"), Qt::LeftButton);
        waitPhase(ModelLabDialog::Phase::Ready, 180000);
        const QString text = get<QPlainTextEdit>("labEvalText")->toPlainText();
        QVERIFY2(text.contains("Acceptance checks"), qPrintable(text));
        QVERIFY(text.contains("rule baseline"));
        const bool passed = text.contains("EVALUATION PASSED");
        QTRY_VERIFY_WITH_TIMEOUT(d->stateLabel()->text().contains(passed ? "Evaluation passed" : "Evaluation failed"), 10000);
        QCOMPARE(get<QPushButton>("labPromote")->isEnabled(), passed);   // only a passed model can be promoted
    }
    void cancelKeepsResumableCheckpoint() {
        QVERIFY(d->selectModelByState("trained") || d->selectModelByState("eval_failed") || d->selectModelByState("eval_passed"));
        d->setHyperparameters(5000, 16, 0.003, -1);
        QTest::mouseClick(get<QPushButton>("labTrain"), Qt::LeftButton);
        QTRY_VERIFY_WITH_TIMEOUT(d->chart()->trainPoints() >= 1, 60000);
        QTest::mouseClick(get<QPushButton>("labPause"), Qt::LeftButton);
        waitPhase(ModelLabDialog::Phase::Ready, 60000);
        QVERIFY(d->statusLabel()->text().contains("resumable checkpoint"));
        QVERIFY(get<QPushButton>("labResume")->isEnabled());
    }
    void expansionCreatesSeparateModelThatNeedsTraining() {
        const int before = d->modelCount();
        get<QSpinBox>("labTarget")->setValue(60000);
        QTest::mouseClick(get<QPushButton>("labExpand"), Qt::LeftButton);
        waitPhase(ModelLabDialog::Phase::Ready, 60000);
        QTRY_COMPARE_WITH_TIMEOUT(d->modelCount(), before + 1, 20000);
        QVERIFY(d->stateLabel()->text().contains("Expanded") || d->selectModelByState("expanded"));
        QVERIFY(get<QPlainTextEdit>("labEvalText")->toPlainText().contains("Expansion adds capacity, not knowledge"));
        QVERIFY(d->selectModelByState("expanded"));
        QVERIFY(!get<QPushButton>("labPromote")->isEnabled());
    }
    void oversizedModelIsRefusedNotFaked() {
        const int before = d->modelCount();
        get<QComboBox>("labSize")->setCurrentText("8B");
        QTest::mouseClick(get<QPushButton>("labCreate"), Qt::LeftButton);
        waitPhase(ModelLabDialog::Phase::Ready, 60000);
        QTRY_VERIFY_WITH_TIMEOUT(d->lastError().contains("cannot instantiate") || d->lastError().contains("REFUSED"), 10000);
        QCOMPARE(d->modelCount(), before);
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
