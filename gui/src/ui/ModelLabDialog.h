#pragma once
// Model Lab: control panel for THE model (there is only ever one, plus a temporary candidate waiting for your decision).
// It talks to its OWN Python worker process (model_lab.worker) so training never shares a process with, or slows down, speech
// generation. Everything shown comes from worker replies: no placeholder metrics, no simulated progress.
//
// Learning from your speech is opt-in: when switched on, the text of speech you generate is remembered on this computer and
// labelled by Recheck Speech; "Train from my speech" turns those rechecked segments into a training run.
#include "DialogFrame.h"
#include <QJsonArray>
#include <QJsonObject>
#include <QProcessEnvironment>
#include <QVector>
#include <functional>

class AnimatedButton;
class ProtocolClient;
class SmoothProgressBar;
class StateIcon;
class QCheckBox;
class QComboBox;
class QDoubleSpinBox;
class QLabel;
class QLineEdit;
class QPlainTextEdit;
class QSpinBox;
class QTreeWidget;

// Minimal painted line chart: training and validation loss against step. Shows "no data yet" until real points arrive.
class LossChart : public QWidget {
    Q_OBJECT
public:
    explicit LossChart(QWidget *parent = nullptr);
    void clear();
    void addTrain(int step, double loss);
    void addVal(int step, double loss);
    int trainPoints() const { return m_train.size(); }
    int valPoints() const { return m_val.size(); }
    QSize sizeHint() const override { return QSize(420, 180); }
    QSize minimumSizeHint() const override { return QSize(240, 120); }

protected:
    void paintEvent(QPaintEvent *) override;

private:
    QVector<QPointF> m_train, m_val;
};

class ModelLabDialog : public StyledDialog {
    Q_OBJECT
public:
    // `env` is passed to the worker (tests use it to point MODEL_LAB_DIR / SPEECH_ENGINE_DATA_DIR at temporary folders).
    explicit ModelLabDialog(const QProcessEnvironment &env = {}, QWidget *parent = nullptr);
    ~ModelLabDialog() override;

    enum class Phase { Starting, Ready, Unavailable, Training, Evaluating, Busy };
    Phase phase() const { return m_phase; }
    static QString stateText(const QString &registryState, bool training);

    void refresh();
    void createDataset();
    void createModel();
    void startTraining(bool resume = false, bool confirmed = false);
    void learnFromMySpeech(bool confirmed = false);
    void pauseTraining();
    void cancelTraining();
    void evaluateModel();
    void expandModel();
    void acceptCandidate();
    void rejectCandidate();
    void setLearning(bool on);
    void deleteMyData();

    LossChart *chart() const { return m_chart; }
    QString activeId() const { return m_activeId; }
    QString candidateId() const { return m_candidateId; }
    int modelCount() const;                       // models that exist: the current one plus at most one pending candidate
    QLabel *statusLabel() const { return m_status; }
    QLabel *stateLabel() const { return m_stateLabel; }
    QLabel *memoryLabel() const { return m_memory; }
    QString lastError() const { return m_lastError; }
    void setHyperparameters(int steps, int batch, double lr, int sizeIndex);
    // Asks the person to confirm (replace the model, delete data, memory warning). Default: a message box. Tests install their own.
    void setConfirmHandler(std::function<bool(const QString &title, const QString &text)> h) { m_confirm = std::move(h); }

signals:
    void phaseChanged(int phase);

private:
    void buildUi();
    void setPhase(Phase p, const QString &text = {});
    void onModels(const QJsonObject &r);
    void updateButtons();
    void showError(const QString &code, const QString &message);
    void log(const QString &line);
    QJsonObject modelById(const QString &id) const;
    QString selectedDataset() const;
    void requestEstimate();
    void showEvaluation(const QJsonObject &r);
    void showCandidateResult(const QJsonObject &res);
    void runTrainingJob(const QString &action, const QJsonObject &params, bool resume);
    void updateExperience(const QJsonObject &e);
    ProtocolClient *m_client;
    Phase m_phase = Phase::Starting;
    QString m_jobId, m_resumeRun, m_lastError, m_lastActual, m_lastEstimate, m_activeId, m_candidateId;
    QJsonArray m_models;
    QJsonArray m_datasets;
    QJsonObject m_experience;
    bool m_torch = false;
    bool m_updatingConsent = false;
    std::function<bool(const QString &, const QString &)> m_confirm;

    StateIcon *m_icon;
    QLabel *m_status, *m_stateLabel, *m_memory, *m_hardware, *m_banner, *m_step, *m_experienceLabel;
    QTreeWidget *m_tree;
    QComboBox *m_dataset, *m_size;
    QLineEdit *m_name;
    QSpinBox *m_steps, *m_batch, *m_target;
    QDoubleSpinBox *m_lr;
    QCheckBox *m_consent;
    AnimatedButton *m_btnData, *m_btnCreate, *m_btnTrain, *m_btnLearn, *m_btnPause, *m_btnResume, *m_btnCancel, *m_btnEval,
        *m_btnExpand, *m_btnAccept, *m_btnReject, *m_btnExport, *m_btnForget;
    SmoothProgressBar *m_progress;
    LossChart *m_chart;
    QPlainTextEdit *m_eval, *m_log;
};
