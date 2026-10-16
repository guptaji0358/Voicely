#pragma once
#include <QAudioBuffer>
#include <QJsonObject>
#include <QList>
#include <QMainWindow>
#include <QPair>
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
class QAudioBufferOutput;
class QAudioOutput;
class QCheckBox;
class QComboBox;
class QDoubleSpinBox;
class QLabel;
class QLineEdit;
class QMediaPlayer;
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

    enum class State { Starting, Idle, Generating, Exporting, Unavailable };
    State state() const { return m_state; }
    QString lastAudioPath() const { return m_audioPath; }

public slots:
    void startWorker();
    void generate(bool testVoice = false);   // testVoice: short preview of the selected voice (does not touch the main audio)
    void cancelJob();
    void exportAudio(const QString &path, const QString &format);   // used by the Save buttons and tests

protected:
    void closeEvent(QCloseEvent *e) override;
    bool eventFilter(QObject *obj, QEvent *e) override;

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

private:
    void buildUi();
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
    void onAudioBuffer(const QAudioBuffer &buf);
    void tickWave();
    void refreshHealth();
    QProcessEnvironment workerEnv() const;
    void purgeStale();
    QStringList m_stale;

    ProtocolClient *m_client;
    State m_state = State::Starting;
    QString m_jobId, m_healthId, m_capsId, m_audioPath, m_previewPath, m_tempDir, m_exportTarget;
    QTemporaryDir *m_tmp = nullptr;
    bool m_jobIsTest = false;
    bool m_statusError = false;
    int m_genCounter = 0;
    QJsonObject m_caps, m_health;
    QString m_sessionApiKey;

    StateIcon *m_dot, *m_busy;
    QLabel *m_title, *m_status, *m_counts, *m_outLabel, *m_time, *m_voiceNote;
    QPushButton *m_settingsBtn, *m_import, *m_clear, *m_paste, *m_generate, *m_play, *m_stop,
        *m_saveMp3, *m_saveWav, *m_cancel;
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
    QList<QPair<qint64, float>> m_levels;   // (media time in us, level 0..1) decoded ahead of playback
    qint64 m_levelCursor = 0;
    bool m_seeking = false;
};
