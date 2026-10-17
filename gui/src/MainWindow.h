#pragma once
#include <QAudioBuffer>
#include <QJsonArray>
#include <QJsonObject>
#include <QList>
#include <QDragEnterEvent>
#include <QDragLeaveEvent>
#include <QDragMoveEvent>
#include <QDropEvent>
#include <QMainWindow>
#include <QMap>
#include <QPair>
#include <QPointer>
#include <QProcessEnvironment>
#include <QTemporaryDir>
#include <QTimer>

class ProtocolClient;
class PreviewButton;
class StateIcon;
class WaveformWidget;
class SuccessBadge;
class SmoothProgressBar;
class EmptyState;
class ShellWidget;
class TitleBar;
class QaPanel;
class ModelLabDialog;
class Card;
class AnimatedButton;
class QAudioBufferOutput;
class QAudioOutput;
class QCheckBox;
class QComboBox;
class QDoubleSpinBox;
class QLabel;
class QLineEdit;
class QMediaPlayer;
class QScrollArea;
class VoiceCarousel;
class DropOverlay;
class AudioDragHandle;
class QMimeData;
class QPlainTextEdit;
class QPushButton;
class QSlider;
class QStackedWidget;
class QToolButton;
class QWidget;

class MainWindow : public QMainWindow {
    Q_OBJECT
public:
    explicit MainWindow(QWidget *parent = nullptr);
    ~MainWindow() override;

    enum class State { Starting, Idle, Generating, Exporting, Unavailable, Rechecking, Repairing };
    void previewDropOverlay();              // developer aid (TTS_SNAPSHOT_DROP)
    void feedDemoLevels(double t);          // developer aid (TTS_SNAPSHOT_DEMO): synthetic levels to show the 3D waveform
    void applyEffects();                    // (re)creates the OpenGL 3D stage according to the setting
    void setPanelOpen(bool open, bool animate = true);
    State state() const { return m_state; }
    QString lastAudioPath() const { return m_audioPath; }
    QString sessionDir() const { return m_sessionDir; }
    bool customFrame() const;
    bool preCheckEnabled() const;
    void setPreCheckEnabled(bool on);       // "check pronunciation before generating"

public slots:
    void startWorker();
    void generate(bool testVoice = false);   // testVoice: short preview of the selected voice (does not touch the main audio)
    void cancelJob();
    void exportAudio(const QString &path, const QString &format, bool temp = false);   // Save buttons, tests; temp = silent drag file
    void analyzeText(bool thenGenerate);
    void recheckSpeech();
    void runRepair(const QString &mode, const QJsonArray &chunks, const QJsonArray &candidates);
    void acceptRepair();
    void discardRepair();
    void openDictionary();
    void openModelLab();

protected:
    void closeEvent(QCloseEvent *e) override;
    void changeEvent(QEvent *e) override;
    void showEvent(QShowEvent *e) override;
    bool eventFilter(QObject *obj, QEvent *e) override;
    void dragEnterEvent(QDragEnterEvent *e) override;
    void dragMoveEvent(QDragMoveEvent *e) override;
    void dragLeaveEvent(QDragLeaveEvent *e) override;
    void dropEvent(QDropEvent *e) override;

private slots:
    void onReady();
    void onProgress(const QString &id, int pct, const QString &msg);
    void onFinished(const QString &id, const QString &status, const QJsonObject &r);
    void onWorkerStopped(const QString &reason);
    void importText();
    void openSettings();
    void saveMp3();
    void saveWav();
    void togglePlay();
    void stopPlay();
    void updateCounts();
    void backendChanged();
    void onPreviewClicked();
    void onGenerateClicked();

private:
    enum class Job { None, Synth, Preview, Export, Recheck, Repair };
    void buildUi();
    void updateGlActivity();
    bool importFile(const QString &path);
    QStringList droppedTextFiles(const QMimeData *m) const;
    bool dragAcceptable(const QMimeData *m) const;
    void dragHint(const QMimeData *m);
    void handleDrop(const QMimeData *m, const QPointF &at);
    void buildActions();
    // Temporary audio for dragging into other apps (no save dialog). Files live in <temp>/Voicely-audio and are removed
    // automatically after N days (setting tempKeepDays) or via the menu; unused leftovers are deleted when the audio changes.
    QString tempAudioDir() const;
    QString tempAudioFile();                // the file to hand over for the chosen format, or empty if not ready
    void prepareTempAudio();
    void purgeTempAudio(int olderThanDays, int *removed = nullptr, qint64 *bytes = nullptr);
    void setState(State s);
    void setStatus(const QString &text, bool error = false);
    void log(const QString &line);
    void loadAudio(const QString &path);
    void loadPreview(const QString &path);
    void stopPreview();
    void releasePreviewFile();
    void updatePreviewUi();
    void updateAudioPanel();
    void updateHeaderIcon();
    void updateVersionBar();
    void onAudioBuffer(const QAudioBuffer &buf);
    void tickWave();
    void refreshHealth();
    void applyHealth(const QJsonObject &r);
    void onCorrect(const QJsonObject &issue);
    void onReplay(int chunk);
    void onSaveFeedback(const QJsonObject &attempt);
    void showReview(const QJsonObject &analysis, bool thenGenerate);
    void removeSession();
    QString currentLanguage() const;
    QJsonObject qaThresholds() const;      // optional advanced override (setting "qaThresholds" = JSON object)
    bool backendHasIpa() const;
    QProcessEnvironment workerEnv() const;
    void purgeStale();
    QStringList m_stale, m_staleDirs;

    ProtocolClient *m_client;
    State m_state = State::Starting;
    Job m_job = Job::None;
    QString m_jobId, m_healthId, m_capsId, m_audioPath, m_previewPath, m_tempDir, m_exportTarget;
    QString m_sessionDir, m_candidatePath, m_acceptedPath, m_ackHash;
    QTemporaryDir *m_tmp = nullptr;
    bool m_jobIsTest = false;
    bool m_statusError = false;
    bool m_analyzing = false;
    int m_genCounter = 0, m_analysisSeq = 0;
    QJsonObject m_caps, m_health, m_repairResult;
    QString m_sessionApiKey;

    ShellWidget *m_shell;
    DropOverlay *m_drop = nullptr;
    AudioDragHandle *m_dragHandle = nullptr;
    QString m_dragFormat = "wav", m_tempStem, m_tempFor, m_tempWav, m_tempMp3;
    bool m_tempExport = false, m_tempUsed = false;
    QScrollArea *m_sideScroll = nullptr;
    VoiceCarousel *m_carousel = nullptr;
    bool m_panelOpen = true;
    TitleBar *m_titleBar;
    QaPanel *m_qa;
    QList<Card *> m_cards;
    bool m_entrancePlayed = false;
    StateIcon *m_dot, *m_busy;
    QLabel *m_title, *m_status, *m_counts, *m_outLabel, *m_time, *m_voiceNote, *m_textBadge, *m_voiceInfoLabel;
    QMap<QString, QString> m_voiceSamples, m_voiceInfo;   // voice -> stored sample path / "Female · tagline"
    QPushButton *m_settingsBtn, *m_import, *m_clear, *m_paste, *m_generate, *m_play, *m_stop,
        *m_saveMp3, *m_saveWav, *m_cancel, *m_checkText, *m_verOriginal, *m_verRepaired;
    QCheckBox *m_precheck;
    QWidget *m_versionBar;
    PreviewButton *m_preview;
    QPlainTextEdit *m_text, *m_log;
    QComboBox *m_voice, *m_backend, *m_lang, *m_bitrate;
    QDoubleSpinBox *m_speed;
    QLineEdit *m_style;
    SmoothProgressBar *m_progress;
    QSlider *m_seek;
    QToolButton *m_logToggle;
    QWidget *m_logPanel, *m_optRow, *m_langGroup, *m_speedGroup, *m_styleGroup;
    QStackedWidget *m_audioStack;
    WaveformWidget *m_wave;
    EmptyState *m_inputEmpty;
    SuccessBadge *m_success;
    QMediaPlayer *m_player, *m_previewPlayer;
    QAudioOutput *m_audioOut, *m_previewOut;
    QAudioBufferOutput *m_levelOut;
    QTimer m_waveTimer;
    QTimer m_trimTimer;                      // idle / minimised: give unused pages back to Windows (MemoryTrim.h)
    QList<QPair<qint64, float>> m_levels;   // (media time in us, level 0..1) decoded ahead of playback
    qint64 m_levelCursor = 0;
    bool m_seeking = false;
    QList<QAction *> m_actions;
    QPointer<ModelLabDialog> m_lab;
};
