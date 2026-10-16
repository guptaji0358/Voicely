#include "MainWindow.h"

#include "Config.h"
#include "ProtocolClient.h"
#include "ui/Buttons.h"
#include "ui/Icons.h"
#include "ui/Indicators.h"
#include "ui/Motion.h"
#include "ui/ThemeManager.h"

#include <QAudioBufferOutput>
#include <QAudioOutput>
#include <QCheckBox>
#include <QCloseEvent>
#include <QComboBox>
#include <QDialog>
#include <QDialogButtonBox>
#include <QDoubleSpinBox>
#include <QFileDialog>
#include <QFormLayout>
#include <QFrame>
#include <QHBoxLayout>
#include <QJsonArray>
#include <QLabel>
#include <QLineEdit>
#include <QMediaPlayer>
#include <QMenuBar>
#include <QMessageBox>
#include <QPlainTextEdit>
#include <QRegularExpression>
#include <QSlider>
#include <QStackedWidget>
#include <QStandardPaths>
#include <QStringDecoder>
#include <QToolButton>
#include <QUrl>
#include <QVBoxLayout>
#include <cmath>

namespace {

QString fmtTime(qint64 ms) {
    const qint64 s = ms / 1000;
    return QString("%1:%2").arg(s / 60).arg(s % 60, 2, 10, QChar('0'));
}

QLabel *fieldLabel(const QString &text) {
    auto *l = new QLabel(text);
    l->setObjectName("fieldLabel");
    return l;
}

QFrame *card() {
    auto *f = new QFrame;
    f->setObjectName("card");
    return f;
}

// Label + field packed together so the pair can be shown/hidden as one unit.
QWidget *group(const QString &label, QWidget *field, QLabel **labelOut = nullptr) {
    auto *w = new QWidget;
    auto *h = new QHBoxLayout(w);
    h->setContentsMargins(0, 0, 0, 0);
    h->setSpacing(8);
    auto *l = fieldLabel(label);
    if (labelOut) *labelOut = l;
    h->addWidget(l);
    h->addWidget(field, 1);
    return w;
}

AnimatedButton *button(const QString &text, const QString &icon, const char *name, const QString &token = "icon") {
    auto *b = new AnimatedButton(text);
    b->setObjectName(name);
    if (!icon.isEmpty()) b->setIcon(Icons::icon(icon, token));
    return b;
}

}  // namespace

MainWindow::MainWindow(QWidget *parent) : QMainWindow(parent), m_client(new ProtocolClient(this)) {
    m_tmp = new QTemporaryDir;     // unique per window; removed on exit
    m_tempDir = m_tmp->path();
    // The main player must be the first QMediaPlayer child (tests look it up with findChild).
    m_player = new QMediaPlayer(this);
    m_audioOut = new QAudioOutput(this);
    m_player->setAudioOutput(m_audioOut);
    m_levelOut = new QAudioBufferOutput(this);
    m_player->setAudioBufferOutput(m_levelOut);
    m_previewPlayer = new QMediaPlayer(this);
    m_previewOut = new QAudioOutput(this);
    m_previewPlayer->setAudioOutput(m_previewOut);
    buildUi();

    connect(m_client, &ProtocolClient::ready, this, &MainWindow::onReady);
    connect(m_client, &ProtocolClient::progress, this, &MainWindow::onProgress);
    connect(m_client, &ProtocolClient::finished, this, &MainWindow::onFinished);
    connect(m_client, &ProtocolClient::workerStopped, this, &MainWindow::onWorkerStopped);
    connect(m_client, &ProtocolClient::logLine, this, &MainWindow::log);

    connect(m_player, &QMediaPlayer::positionChanged, this, [this](qint64 p) {
        if (!m_seeking) m_seek->setValue(int(p));
        m_time->setText(fmtTime(p) + " / " + fmtTime(m_player->duration()));
    });
    connect(m_player, &QMediaPlayer::durationChanged, this, [this](qint64 d) {
        m_seek->setRange(0, int(d));
        m_time->setText(fmtTime(m_player->position()) + " / " + fmtTime(d));
    });
    connect(m_player, &QMediaPlayer::playbackStateChanged, this, [this](QMediaPlayer::PlaybackState st) {
        const bool playing = st == QMediaPlayer::PlayingState;
        m_play->setText(playing ? "Pause" : "Play");
        m_play->setIcon(Icons::icon(playing ? "pause" : "play"));
        m_stop->setEnabled(st != QMediaPlayer::StoppedState);
        m_wave->setActive(playing);
        if (playing) {
            stopPreview();             // never two voices at once
            m_waveTimer.start();
        } else {
            m_waveTimer.stop();
            if (st == QMediaPlayer::StoppedState) {
                m_levels.clear();
                m_wave->clearLevels();
            }
        }
    });
    connect(m_player, &QMediaPlayer::errorOccurred, this, [this](QMediaPlayer::Error, const QString &msg) {
        setStatus("Playback error: " + msg, true);
        log("[player] " + msg);
    });
    connect(m_levelOut, &QAudioBufferOutput::audioBufferReceived, this, &MainWindow::onAudioBuffer);
    m_waveTimer.setInterval(33);
    connect(&m_waveTimer, &QTimer::timeout, this, &MainWindow::tickWave);

    connect(m_previewPlayer, &QMediaPlayer::playbackStateChanged, this, [this](QMediaPlayer::PlaybackState st) {
        if (st == QMediaPlayer::PlayingState) setStatus("Playing voice preview.");   // only once playback really started
        updatePreviewUi();
    });
    connect(m_previewPlayer, &QMediaPlayer::errorOccurred, this, [this](QMediaPlayer::Error, const QString &msg) {
        setStatus("Preview playback error: " + msg, true);
        updatePreviewUi();
    });

    if (ThemeManager::instance().usingFallback()) setStatus(ThemeManager::instance().lastError(), true);
    QTimer::singleShot(0, this, &MainWindow::startWorker);
}

MainWindow::~MainWindow() {
    m_waveTimer.stop();
    m_client->disconnect(this);
    delete m_client;      // joins the worker process before the widgets go away
    m_client = nullptr;
    m_player->setSource({});
    m_previewPlayer->setSource({});
    delete m_tmp;
}

// The media backend may still hold a file for a moment after setSource({}); retry removal later.
void MainWindow::purgeStale() {
    for (int i = m_stale.size() - 1; i >= 0; --i)
        if (!QFile::exists(m_stale[i]) || QFile::remove(m_stale[i])) m_stale.removeAt(i);
    if (!m_stale.isEmpty()) QTimer::singleShot(300, this, &MainWindow::purgeStale);
}

void MainWindow::buildUi() {
    ThemeManager::instance().ensureApplied();
    setWindowTitle("TTS Studio");
    setWindowIcon(Icons::icon("app-logo"));
    resize(980, 720);
    setMinimumSize(700, 560);
    auto *central = new QWidget;
    central->setObjectName("central");
    setCentralWidget(central);
    auto *root = new QVBoxLayout(central);
    root->setContentsMargins(14, 10, 14, 10);
    root->setSpacing(10);

    // ---- header
    auto *head = new QHBoxLayout;
    head->setSpacing(8);
    m_title = new QLabel("TTS Studio");
    m_title->setObjectName("title");
    m_dot = new StateIcon(18);
    m_dot->setObjectName("statusDot");
    m_status = new QLabel("Starting speech engine…");
    m_status->setObjectName("statusLabel");
    m_status->setWordWrap(true);
    m_settingsBtn = button("Settings", "settings", "settingsButton");
    head->addWidget(new IconWidget("app-logo", "", 28));
    head->addWidget(m_title);
    head->addSpacing(12);
    head->addWidget(m_dot);
    head->addWidget(m_status, 1);
    head->addWidget(m_settingsBtn);
    root->addLayout(head);

    // ---- text editor (gets the most room)
    auto *tb = card();
    auto *tl = new QVBoxLayout(tb);
    tl->setContentsMargins(10, 10, 10, 8);
    tl->setSpacing(8);
    m_text = new QPlainTextEdit;
    m_text->setObjectName("textEditor");
    m_text->setMinimumHeight(140);
    m_inputEmpty = new EmptyState("empty-input", "Nothing to read yet",
                                  "Type, paste or import text to begin.", false, m_text->viewport());
    m_inputEmpty->setGeometry(m_text->viewport()->rect());
    m_text->viewport()->installEventFilter(this);
    auto *tr = new QHBoxLayout;
    tr->setSpacing(6);
    m_import = button("Import Text…", "import", "importButton");
    m_paste = button("Paste", "paste", "pasteButton");
    m_clear = button("Clear Text", "clear", "clearButton");
    m_counts = new QLabel("0 characters · 0 words");
    m_counts->setObjectName("muted");
    tr->addWidget(m_import);
    tr->addWidget(m_paste);
    tr->addWidget(m_clear);
    tr->addStretch(1);
    tr->addWidget(m_counts);
    tl->addWidget(m_text, 1);
    tl->addLayout(tr);
    root->addWidget(tb, 1);

    // ---- voice settings: one compact row, preview speaker inline with the voice selector
    auto *vb = card();
    auto *vl = new QVBoxLayout(vb);
    vl->setContentsMargins(12, 10, 12, 10);
    vl->setSpacing(8);
    m_voice = new QComboBox;
    m_voice->setObjectName("voiceCombo");
    m_voice->setMinimumWidth(180);
    m_backend = new QComboBox;
    m_backend->setObjectName("backendCombo");
    m_lang = new QComboBox;
    m_lang->setObjectName("languageCombo");
    m_speed = new QDoubleSpinBox;
    m_speed->setObjectName("speedSpin");
    m_speed->setRange(0.5, 2.0);
    m_speed->setSingleStep(0.05);
    m_speed->setValue(1.0);
    m_speed->setToolTip("1.0 = the profile's own pace");
    m_style = new QLineEdit;
    m_style->setObjectName("styleEdit");
    m_style->setPlaceholderText("Optional style instruction (Google backend)");
    m_preview = new PreviewButton;
    const int rowH = m_voice->sizeHint().height();
    m_preview->setFixedSize(rowH, rowH);
    m_voiceNote = new QLabel;
    m_voiceNote->setObjectName("muted");
    m_voiceNote->setProperty("small", true);
    m_voiceNote->setWordWrap(true);

    auto *r0 = new QHBoxLayout;
    r0->setSpacing(6);
    r0->addWidget(new IconWidget("voice", "icon-accent", 18));
    r0->addWidget(fieldLabel("Voice:"));
    r0->addWidget(m_voice, 3);
    r0->addWidget(m_preview);                 // speaker icon sits right beside the voice name
    r0->addSpacing(18);
    r0->addWidget(fieldLabel("Backend:"));
    r0->addWidget(m_backend, 2);
    vl->addLayout(r0);

    m_optRow = new QWidget;
    auto *r1 = new QHBoxLayout(m_optRow);
    r1->setContentsMargins(0, 0, 0, 0);
    r1->setSpacing(18);
    m_langGroup = group("Language:", m_lang);
    m_speedGroup = group("Speed:", m_speed);
    m_styleGroup = group("Style:", m_style);
    r1->addWidget(m_langGroup);
    r1->addWidget(m_speedGroup);
    r1->addWidget(m_styleGroup, 1);
    vl->addWidget(m_optRow);
    vl->addWidget(m_voiceNote);
    root->addWidget(vb);

    // ---- audio
    auto *ab = card();
    auto *al = new QVBoxLayout(ab);
    al->setContentsMargins(12, 10, 12, 10);
    al->setSpacing(8);
    auto *a1 = new QHBoxLayout;
    a1->setSpacing(8);
    m_generate = button("Generate Speech", "generate", "primary", "on-accent");
    m_generate->setProperty("testId", "generate");
    m_busy = new StateIcon(18);
    m_busy->setKind(StateIcon::Kind::Busy);
    m_busy->hide();
    m_cancel = button("Cancel", "cancel", "cancelButton");
    m_cancel->hide();
    m_progress = new SmoothProgressBar;
    m_progress->setObjectName("progressBar");
    a1->addWidget(m_generate);
    a1->addWidget(m_busy);
    a1->addWidget(m_progress, 1);
    a1->addWidget(m_cancel);
    al->addLayout(a1);

    m_audioStack = new QStackedWidget;
    m_audioStack->setObjectName("audioStack");
    m_audioStack->setFixedHeight(72);
    m_audioStack->addWidget(new EmptyState("empty-audio", "No audio yet", "Generate speech to preview it here.", true));
    m_wave = new WaveformWidget;
    m_audioStack->addWidget(m_wave);
    al->addWidget(m_audioStack);

    auto *a2 = new QHBoxLayout;
    a2->setSpacing(6);
    m_play = button("Play", "play", "playButton");
    m_stop = button("Stop", "stop", "stopButton");
    m_stop->setEnabled(false);
    m_seek = new QSlider(Qt::Horizontal);
    m_seek->setObjectName("seekSlider");
    m_time = new QLabel("0:00 / 0:00");
    m_time->setObjectName("timeLabel");
    a2->addWidget(m_play);
    a2->addWidget(m_stop);
    a2->addWidget(m_seek, 1);
    a2->addWidget(m_time);
    al->addLayout(a2);

    auto *a3 = new QHBoxLayout;
    a3->setSpacing(6);
    m_bitrate = new QComboBox;
    m_bitrate->setObjectName("bitrateCombo");
    for (int b : {128, 160, 192, 256, 320}) m_bitrate->addItem(QString("%1 kbps").arg(b), b);
    m_bitrate->setCurrentIndex(2);
    m_saveMp3 = button("Save as MP3…", "save-mp3", "saveMp3Button");
    m_saveWav = button("Save WAV…", "export-wav", "saveWavButton");
    m_success = new SuccessBadge;
    m_outLabel = new QLabel("No audio generated yet.");
    m_outLabel->setObjectName("outputLabel");
    m_outLabel->setTextInteractionFlags(Qt::TextSelectableByMouse);
    m_outLabel->setWordWrap(true);
    a3->addWidget(fieldLabel("MP3 bitrate"));
    a3->addWidget(m_bitrate);
    a3->addWidget(m_saveMp3);
    a3->addWidget(m_saveWav);
    a3->addWidget(m_success);
    a3->addStretch(1);
    al->addLayout(a3);
    al->addWidget(m_outLabel);
    root->addWidget(ab);

    // ---- diagnostics
    m_logToggle = new QToolButton;
    m_logToggle->setObjectName("logToggle");
    m_logToggle->setText(" Diagnostics");
    m_logToggle->setIcon(Icons::icon("chevron-right"));
    m_logToggle->setIconSize(QSize(14, 14));
    m_logToggle->setToolButtonStyle(Qt::ToolButtonTextBesideIcon);
    m_logToggle->setCheckable(true);
    m_log = new QPlainTextEdit;
    m_log->setObjectName("logView");
    m_log->setReadOnly(true);
    m_log->setMaximumBlockCount(2000);
    m_log->setMaximumHeight(140);
    m_logPanel = m_log;
    m_logPanel->hide();
    auto *logRow = new QHBoxLayout;
    logRow->setContentsMargins(0, 0, 0, 0);
    logRow->addWidget(m_logToggle);
    logRow->addStretch(1);
    root->addLayout(logRow);
    root->addWidget(m_logPanel);

    connect(m_logToggle, &QToolButton::toggled, this, [this](bool on) {
        m_logPanel->setVisible(on);
        if (on) Fx::fadeIn(m_logPanel, 160);
        m_logToggle->setIcon(Icons::icon(on ? "chevron-down" : "chevron-right"));
    });
    connect(m_settingsBtn, &QPushButton::clicked, this, &MainWindow::openSettings);
    connect(m_import, &QPushButton::clicked, this, &MainWindow::importText);
    connect(m_paste, &QPushButton::clicked, m_text, &QPlainTextEdit::paste);
    connect(m_clear, &QPushButton::clicked, m_text, &QPlainTextEdit::clear);
    connect(m_text, &QPlainTextEdit::textChanged, this, &MainWindow::updateCounts);
    connect(m_generate, &QPushButton::clicked, this, [this] { generate(false); });
    connect(m_preview, &QToolButton::clicked, this, &MainWindow::onPreviewClicked);
    connect(m_cancel, &QPushButton::clicked, this, &MainWindow::cancelJob);
    connect(m_play, &QPushButton::clicked, this, &MainWindow::togglePlay);
    connect(m_stop, &QPushButton::clicked, this, &MainWindow::stopPlay);
    connect(m_saveMp3, &QPushButton::clicked, this, &MainWindow::saveMp3);
    connect(m_saveWav, &QPushButton::clicked, this, &MainWindow::saveWav);
    connect(m_backend, &QComboBox::currentIndexChanged, this, &MainWindow::backendChanged);
    connect(m_seek, &QSlider::sliderPressed, this, [this] { m_seeking = true; });
    connect(m_seek, &QSlider::sliderReleased, this, [this] {
        m_seeking = false;
        m_levels.clear();
        m_player->setPosition(m_seek->value());
    });

    auto *fileMenu = menuBar()->addMenu("&File");
    fileMenu->addAction("&Import Text…", this, &MainWindow::importText, QKeySequence::Open);
    fileMenu->addAction("Save as &MP3…", this, &MainWindow::saveMp3, QKeySequence::Save);
    fileMenu->addSeparator();
    fileMenu->addAction("E&xit", this, &QWidget::close, QKeySequence::Quit);
    auto *toolsMenu = menuBar()->addMenu("&Tools");
    toolsMenu->addAction("&Settings…", this, &MainWindow::openSettings);
    toolsMenu->addAction("&Re-check backend", this, &MainWindow::startWorker);

    setState(State::Starting);
    updateCounts();
}

bool MainWindow::eventFilter(QObject *obj, QEvent *e) {
    if (obj == m_text->viewport() && e->type() == QEvent::Resize) m_inputEmpty->setGeometry(m_text->viewport()->rect());
    return QMainWindow::eventFilter(obj, e);
}

// ---------------------------------------------------------------- state
void MainWindow::setStatus(const QString &text, bool error) {
    m_status->setText(text);
    m_statusError = error;
    Fx::setProp(m_status, "state", error ? "error" : "");
    Fx::fadeIn(m_status, 140);
    updateHeaderIcon();
    log(text);
}

void MainWindow::updateHeaderIcon() {
    StateIcon::Kind k = StateIcon::Kind::Busy;
    QString tip = "Working";
    switch (m_state) {
    case State::Idle:
        k = m_statusError ? StateIcon::Kind::Warning : StateIcon::Kind::Success;
        tip = m_statusError ? "Ready, but the last action reported a problem" : "Backend ready";
        break;
    case State::Unavailable:
        k = StateIcon::Kind::Error;
        tip = "Backend unavailable";
        break;
    default: break;
    }
    m_dot->setKind(k);
    m_dot->setToolTip(tip);
}

void MainWindow::log(const QString &line) {
    m_log->appendPlainText(line);
}

void MainWindow::setState(State s) {
    m_state = s;
    const bool idle = s == State::Idle;
    const bool busy = s == State::Generating || s == State::Exporting;
    const bool haveAudio = !m_audioPath.isEmpty() && QFileInfo::exists(m_audioPath);
    m_generate->setEnabled(idle);
    m_backend->setEnabled(idle);
    m_voice->setEnabled(idle);
    m_lang->setEnabled(idle);
    m_speed->setEnabled(idle);
    m_style->setEnabled(idle);
    m_saveMp3->setEnabled(idle && haveAudio);
    m_saveWav->setEnabled(idle && haveAudio);
    m_bitrate->setEnabled(idle && haveAudio);
    m_play->setEnabled(haveAudio && s != State::Starting);
    m_seek->setEnabled(haveAudio);
    if (busy && !m_cancel->isVisible()) {
        m_cancel->show();
        Fx::fadeIn(m_cancel, 160);
    } else if (!busy) {
        m_cancel->hide();
    }
    // The spinner shows only while the backend is actually working on something.
    m_busy->setVisible(busy);
    if (!busy) m_progress->resetNow();
    updateHeaderIcon();
    updateAudioPanel();
    updatePreviewUi();
}

void MainWindow::updateAudioPanel() {
    const bool haveAudio = !m_audioPath.isEmpty() && QFileInfo::exists(m_audioPath);
    const int idx = haveAudio ? 1 : 0;
    if (m_audioStack->currentIndex() != idx) {
        m_audioStack->setCurrentIndex(idx);
        Fx::fadeIn(m_audioStack->currentWidget(), 200);
    }
}

void MainWindow::updatePreviewUi() {
    PreviewButton::Mode mode = PreviewButton::Mode::Idle;
    if (m_state == State::Generating && m_jobIsTest) mode = PreviewButton::Mode::Loading;
    else if (m_previewPlayer->playbackState() == QMediaPlayer::PlayingState) mode = PreviewButton::Mode::Speaking;
    m_preview->setMode(mode);
    // Usable when idle (start) or while a preview is running (cancel/stop).
    m_preview->setEnabled((m_state == State::Idle && m_voice->count() > 0) || mode != PreviewButton::Mode::Idle);
}

QProcessEnvironment MainWindow::workerEnv() const {
    QProcessEnvironment e;
    if (!m_sessionApiKey.isEmpty()) e.insert("GEMINI_API_KEY", m_sessionApiKey);  // memory only, never persisted
    return e;
}

// ---------------------------------------------------------------- worker lifecycle
void MainWindow::startWorker() {
    if (m_client->isRunning()) {
        refreshHealth();
        return;
    }
    setState(State::Starting);
    const QString py = Config::findPython();
    const QString root = Config::findEngineRoot();
    if (py.isEmpty()) {
        setStatus("No Python interpreter found. Open Settings and choose python.exe.", true);
        setState(State::Unavailable);
        return;
    }
    if (root.isEmpty()) {
        setStatus("speech_engine not found. Open Settings and choose the folder containing speech_engine/.", true);
        setState(State::Unavailable);
        return;
    }
    setStatus("Starting speech engine…");
    log("python: " + py + "\nengine: " + root);
    m_client->start(py, root, workerEnv());
}

void MainWindow::onReady() {
    refreshHealth();
}

void MainWindow::refreshHealth() {
    m_healthId = m_client->request("health", {}, 90000);
    m_capsId = m_client->request("capabilities", {}, 90000);
}

void MainWindow::onWorkerStopped(const QString &reason) {
    m_jobId.clear();
    setStatus(reason, true);
    setState(State::Unavailable);
}

void MainWindow::backendChanged() {
    const QString b = m_backend->currentData().toString();
    const QJsonObject caps = m_caps.value("backends").toObject().value(b).toObject();
    m_lang->clear();
    const QJsonObject langs = caps.value("languages").toObject();
    for (auto it = langs.begin(); it != langs.end(); ++it) m_lang->addItem(it.value().toString(), it.key());
    const int en = m_lang->findData("en-us");   // JSON objects are key-sorted; never default to an arbitrary language
    if (en >= 0) m_lang->setCurrentIndex(en);
    const bool hasLang = m_lang->count() > 0;
    const bool hasSpeed = caps.value("speed").toBool();
    const bool hasStyle = caps.value("style").toBool();
    m_lang->setVisible(hasLang);
    m_speed->setVisible(hasSpeed);
    m_style->setVisible(hasStyle);
    m_langGroup->setVisible(hasLang);
    m_speedGroup->setVisible(hasSpeed);
    m_styleGroup->setVisible(hasStyle);
    m_optRow->setVisible(hasLang || hasSpeed || hasStyle);
    QString note = "“original” is a prompt/config-defined voice profile";
    note += b == "local" ? " (a weighted blend of stock Kokoro voices) — not a newly trained model. Runs offline."
            : b == "google" ? " (a Google prebuilt voice plus a style instruction) — not a trained model. Text is sent to Google."
                            : ". This is a TEST backend that emits a tone, not speech.";
    m_voiceNote->setText(note);
    m_voiceNote->setToolTip(note);
}

void MainWindow::onFinished(const QString &id, const QString &status, const QJsonObject &r) {
    const QString code = r.value("code").toString();
    const QString message = r.value("message").toString();

    if (id == m_healthId) {
        m_healthId.clear();
        if (status != "success") {
            setStatus(message.isEmpty() ? "Health check failed." : message, true);
            setState(State::Unavailable);
            return;
        }
        m_health = r;
        m_backend->blockSignals(true);
        m_backend->clear();
        QStringList problems;
        const QJsonObject bs = r.value("backends").toObject();
        for (auto it = bs.begin(); it != bs.end(); ++it) {
            const QJsonObject b = it.value().toObject();
            if (b.value("available").toBool()) {
                m_backend->addItem(it.key() + (b.value("network").toBool() ? "  (online, sends text to the cloud)" : "  (offline)"),
                                   it.key());
            } else {
                for (const auto &p : b.value("problems").toArray()) problems << it.key() + ": " + p.toString();
            }
            log("[health] " + it.key() + (b.value("available").toBool() ? " OK" : " unavailable"));
        }
        m_backend->blockSignals(false);
        for (const QString &p : problems) log("[health] " + p);
        if (!r.value("mp3").toObject().value("available").toBool())
            log("[health] MP3 export unavailable: " + r.value("mp3").toObject().value("problem").toString());
        if (m_backend->count() == 0) {
            setStatus("No speech backend is available. " + problems.join("  "), true);
            setState(State::Unavailable);
        } else {
            backendChanged();
            setStatus("Ready — Python " + r.value("python").toString());
            setState(State::Idle);
        }
        return;
    }
    if (id == m_capsId) {
        m_capsId.clear();
        if (status == "success") {
            m_caps = r;
            m_voice->clear();
            for (const auto &v : r.value("voices").toArray()) {
                const QJsonObject o = v.toObject();
                const QString name = o.value("name").toString();
                m_voice->addItem(name == "original" ? QStringLiteral("Original Voice") : name, name);
                m_voice->setItemData(m_voice->count() - 1, o.value("description").toString() + "\n" + o.value("kind").toString(),
                                     Qt::ToolTipRole);
            }
            const int i = m_voice->findData("original");
            if (i >= 0) m_voice->setCurrentIndex(i);
            if (m_backend->count() > 0) backendChanged();
            updatePreviewUi();
        }
        return;
    }
    if (id != m_jobId) return;   // reply to a cancel request or a stale job

    m_jobId.clear();
    const bool wasExport = m_state == State::Exporting;
    if (status == "success") {
        if (wasExport) {
            const QString out = r.value("output_path").toString();
            QFileInfo fi(out);
            if (!fi.exists() || fi.size() == 0) {
                setStatus("Export reported success but the file is missing or empty: " + out, true);
            } else {
                setStatus(QString("Saved %1 (%2 KB, %3 s)").arg(QDir::toNativeSeparators(out))
                              .arg(fi.size() / 1024).arg(r.value("duration_s").toDouble(), 0, 'f', 1));
                m_outLabel->setText("Saved: " + QDir::toNativeSeparators(out));
                m_success->play();
            }
        } else if (m_jobIsTest) {
            loadPreview(r.value("output_path").toString());
            setStatus(QString("Voice preview generated (%1 s).").arg(r.value("duration_s").toDouble(), 0, 'f', 1));
            m_previewPlayer->play();
        } else {
            loadAudio(r.value("output_path").toString());
            setStatus(QString("Generated %1 s of audio with %2 / %3.")
                          .arg(r.value("duration_s").toDouble(), 0, 'f', 1, ' ')
                          .arg(r.value("backend").toString(), r.value("voice").toString()));
        }
    } else if (status == "cancelled") {
        setStatus("Cancelled.");
    } else {
        setStatus((code.isEmpty() ? "" : code + ": ") + message, true);
    }
    m_jobIsTest = false;
    setState(State::Idle);
    if (!m_client->isRunning()) setState(State::Unavailable);
}

void MainWindow::onProgress(const QString &id, int pct, const QString &msg) {
    if (id != m_jobId) return;
    if (pct > 0) m_progress->setTarget(pct);   // 0 = "starting": the spinner shows activity, no invented percentage
    m_status->setText((m_state == State::Exporting ? "Exporting… " : "Generating… ") + msg);
}

// ---------------------------------------------------------------- actions
void MainWindow::generate(bool testVoice) {
    if (m_state != State::Idle || !m_jobId.isEmpty()) return;   // no duplicate requests
    QString text = testVoice ? QStringLiteral("Hello. This is a short test of the selected voice.") : m_text->toPlainText();
    if (text.trimmed().isEmpty()) {
        setStatus("Enter or import some text first.", true);
        return;
    }
    releasePreviewFile();    // a new job always supersedes any preview
    QString out;
    if (testVoice) {
        if (m_player->playbackState() == QMediaPlayer::PlayingState) m_player->pause();
        out = QString("%1/voice-preview-%2.wav").arg(m_tempDir).arg(++m_genCounter);
        m_previewPath = out;
    } else {
        // Release the previous file before it is replaced (Windows keeps playing files locked).
        m_player->stop();
        m_player->setSource({});
        if (!m_audioPath.isEmpty()) m_stale << m_audioPath;
        m_audioPath.clear();
        purgeStale();
        out = QString("%1/preview-%2.wav").arg(m_tempDir).arg(++m_genCounter);
    }

    QJsonObject p{{"text", text}, {"backend", m_backend->currentData().toString()},
                  {"voice", m_voice->currentData().toString()}, {"output_path", out}, {"output_format", "wav"},
                  {"overwrite", true}};
    if (m_lang->isVisible() && m_lang->count()) p["language"] = m_lang->currentData().toString();
    if (m_speed->isVisible() && !qFuzzyCompare(m_speed->value(), 1.0)) p["speed"] = m_speed->value();
    if (m_style->isVisible() && !m_style->text().trimmed().isEmpty()) p["style"] = m_style->text().trimmed();
    m_jobIsTest = testVoice;
    m_jobId = m_client->request("synthesize", p);
    m_progress->resetNow();
    setState(State::Generating);
    setStatus(testVoice ? "Generating voice preview…" : "Generating…");
}

void MainWindow::onPreviewClicked() {
    switch (m_preview->mode()) {
    case PreviewButton::Mode::Loading:
        cancelJob();
        break;
    case PreviewButton::Mode::Speaking:
        stopPreview();
        setStatus("Preview stopped.");
        break;
    case PreviewButton::Mode::Idle:
        generate(true);
        break;
    }
}

void MainWindow::stopPreview() {
    if (m_previewPlayer->playbackState() != QMediaPlayer::StoppedState) m_previewPlayer->stop();
}

void MainWindow::releasePreviewFile() {
    m_previewPlayer->stop();
    m_previewPlayer->setSource({});
    if (!m_previewPath.isEmpty()) m_stale << m_previewPath;
    m_previewPath.clear();
    purgeStale();
}

void MainWindow::loadPreview(const QString &path) {
    m_previewPath = path;
    m_previewPlayer->setSource(QUrl::fromLocalFile(path));
}

void MainWindow::cancelJob() {
    if (m_jobId.isEmpty()) return;
    m_client->cancel(m_jobId);
    setStatus("Cancelling…");
}

void MainWindow::loadAudio(const QString &path) {
    m_audioPath = path;
    m_player->setSource(QUrl::fromLocalFile(path));
    m_outLabel->setText("Preview: " + QDir::toNativeSeparators(path) + "  (temporary — use Save to keep it)");
}

void MainWindow::exportAudio(const QString &path, const QString &format) {
    if (m_state != State::Idle || m_audioPath.isEmpty()) return;
    QJsonObject p{{"input_path", m_audioPath}, {"output_path", path}, {"output_format", format},
                  {"overwrite", true}};   // overwrite was confirmed by the native save dialog
    if (format == "mp3") p["bitrate_kbps"] = m_bitrate->currentData().toInt();
    m_jobIsTest = false;
    m_jobId = m_client->request("export", p);
    m_progress->resetNow();
    setState(State::Exporting);
    setStatus("Exporting…");
}

void MainWindow::saveMp3() {
    if (m_audioPath.isEmpty()) return;
    const QString dir = Config::settings().value("lastDir", QStandardPaths::writableLocation(QStandardPaths::MusicLocation)).toString();
    QString f = QFileDialog::getSaveFileName(this, "Save as MP3", dir + "/speech.mp3", "MP3 audio (*.mp3)");
    if (f.isEmpty()) return;
    if (!f.endsWith(".mp3", Qt::CaseInsensitive)) f += ".mp3";
    Config::settings().setValue("lastDir", QFileInfo(f).absolutePath());
    exportAudio(f, "mp3");
}

void MainWindow::saveWav() {
    if (m_audioPath.isEmpty()) return;
    const QString dir = Config::settings().value("lastDir", QStandardPaths::writableLocation(QStandardPaths::MusicLocation)).toString();
    QString f = QFileDialog::getSaveFileName(this, "Save WAV", dir + "/speech.wav", "WAV audio (*.wav)");
    if (f.isEmpty()) return;
    if (!f.endsWith(".wav", Qt::CaseInsensitive)) f += ".wav";
    Config::settings().setValue("lastDir", QFileInfo(f).absolutePath());
    exportAudio(f, "wav");
}

void MainWindow::togglePlay() {
    if (m_audioPath.isEmpty()) return;
    if (m_player->playbackState() == QMediaPlayer::PlayingState) m_player->pause();
    else m_player->play();
}

void MainWindow::stopPlay() {
    m_player->stop();
}

// ---------------------------------------------------------------- playback levels (real data)
// QAudioBufferOutput hands us the decoded buffers the player is about to render. They are split into
// ~30 ms slices, stamped with their media time, and released to the meter when playback reaches them.
void MainWindow::onAudioBuffer(const QAudioBuffer &buf) {
    if (!buf.isValid()) return;
    const QAudioFormat fmt = buf.format();
    const int ch = fmt.channelCount(), rate = fmt.sampleRate(), frames = buf.frameCount();
    if (ch <= 0 || rate <= 0 || frames <= 0) return;
    const qint64 start = buf.startTime() >= 0 ? buf.startTime() : m_levelCursor;
    m_levelCursor = start + buf.duration();
    const int slice = qMax(1, rate * 30 / 1000);
    auto run = [&](const auto *d, double scale, double bias) {
        for (int f = 0; f < frames; f += slice) {
            const int n = qMin(slice, frames - f) * ch;
            double acc = 0;
            for (int i = 0; i < n; ++i) {
                const double s = (double(d[qsizetype(f) * ch + i]) - bias) * scale;
                acc += s * s;
            }
            const double rms = std::sqrt(acc / n);
            m_levels.append({start + qint64(f) * 1000000 / rate, float(qMin(1.0, std::pow(rms * 3.2, 0.7)))});
        }
    };
    switch (fmt.sampleFormat()) {
    case QAudioFormat::UInt8: run(buf.constData<quint8>(), 1.0 / 128, 128); break;
    case QAudioFormat::Int16: run(buf.constData<qint16>(), 1.0 / 32768, 0); break;
    case QAudioFormat::Int32: run(buf.constData<qint32>(), 1.0 / 2147483648.0, 0); break;
    case QAudioFormat::Float: run(buf.constData<float>(), 1.0, 0); break;
    default: return;
    }
    if (m_levels.size() > 4000) m_levels.remove(0, m_levels.size() - 4000);
}

void MainWindow::tickWave() {
    if (m_player->playbackState() != QMediaPlayer::PlayingState) return;
    const qint64 posUs = m_player->position() * 1000;
    float peak = -1;
    while (!m_levels.isEmpty() && m_levels.first().first <= posUs) {
        peak = qMax(peak, m_levels.first().second);
        m_levels.removeFirst();
    }
    if (peak >= 0) m_wave->pushLevel(peak);   // no data -> the meter stays still rather than faking motion
}

void MainWindow::importText() {
    const QString dir = Config::settings().value("lastDir", QStandardPaths::writableLocation(QStandardPaths::DocumentsLocation)).toString();
    const QString f = QFileDialog::getOpenFileName(this, "Import text", dir, "Text files (*.txt);;All files (*)");
    if (f.isEmpty()) return;
    Config::settings().setValue("lastDir", QFileInfo(f).absolutePath());
    QFile file(f);
    if (!file.open(QIODevice::ReadOnly)) {
        setStatus("Cannot read " + f + ": " + file.errorString(), true);
        return;
    }
    QByteArray bytes = file.readAll();
    if (bytes.startsWith("\xEF\xBB\xBF")) bytes.remove(0, 3);
    QStringDecoder dec(QStringDecoder::Utf8, QStringDecoder::Flag::Stateless);
    const QString text = dec(bytes);
    if (dec.hasError()) {
        setStatus("This file is not valid UTF-8. Re-save it as UTF-8 text and import again.", true);
        return;
    }
    m_text->setPlainText(text);   // QPlainTextEdit keeps line breaks (paragraphs) as-is
    setStatus(QString("Imported %1").arg(QDir::toNativeSeparators(f)));
}

void MainWindow::updateCounts() {
    const QString t = m_text->toPlainText();
    const int words = t.isEmpty() ? 0 : t.split(QRegularExpression("\\s+"), Qt::SkipEmptyParts).size();
    m_counts->setText(QString("%1 characters · %2 words").arg(t.size()).arg(words));
    const bool empty = t.isEmpty();
    if (m_inputEmpty->isHidden() == empty) {
        m_inputEmpty->setVisible(empty);
        if (empty) Fx::fadeIn(m_inputEmpty, 200);
    }
}

void MainWindow::openSettings() {
    QDialog d(this);
    d.setWindowTitle("Settings");
    d.setMinimumWidth(560);
    auto *form = new QFormLayout(&d);
    auto *py = new QLineEdit(Config::settings().value("pythonExe").toString());
    py->setPlaceholderText("auto: " + Config::findPython());
    auto *root = new QLineEdit(Config::settings().value("engineRoot").toString());
    root->setPlaceholderText("auto: " + Config::findEngineRoot());
    auto *key = new QLineEdit(m_sessionApiKey);
    key->setEchoMode(QLineEdit::Password);
    key->setPlaceholderText("Optional. Otherwise GEMINI_API_KEY from the environment is used.");
    auto *theme = new QComboBox;
    theme->addItems(ThemeManager::availableThemes());
    theme->setCurrentText(ThemeManager::instance().current());
    auto *reduce = new QCheckBox("Reduce motion (also follows the Windows animation setting)");
    reduce->setChecked(Config::settings().value("reduceMotion", false).toBool());
    auto browse = [&](QLineEdit *e, bool dir) {
        auto *b = new AnimatedButton("Browse…");
        connect(b, &QPushButton::clicked, &d, [=, &d] {
            const QString p = dir ? QFileDialog::getExistingDirectory(&d, "Engine folder (contains speech_engine/)")
                                  : QFileDialog::getOpenFileName(&d, "Python interpreter", {}, "python.exe (python*.exe);;All files (*)");
            if (!p.isEmpty()) e->setText(p);
        });
        auto *row = new QHBoxLayout;
        row->addWidget(e, 1);
        row->addWidget(b);
        return row;
    };
    form->addRow("Python interpreter", browse(py, false));
    form->addRow("Engine folder", browse(root, true));
    form->addRow("Google API key (this session only)", key);
    form->addRow("Theme", theme);
    form->addRow("", reduce);
    auto *note = new QLabel("The API key is held in memory and passed to the worker; it is never written to disk. "
                            "Set GEMINI_API_KEY in your user environment for a persistent key. Saving restarts the worker.");
    note->setWordWrap(true);
    note->setObjectName("muted");
    form->addRow(note);
    auto *bb = new QDialogButtonBox(QDialogButtonBox::Save | QDialogButtonBox::Cancel);
    form->addRow(bb);
    connect(bb, &QDialogButtonBox::accepted, &d, &QDialog::accept);
    connect(bb, &QDialogButtonBox::rejected, &d, &QDialog::reject);
    if (d.exec() != QDialog::Accepted) return;
    Config::settings().setValue("pythonExe", py->text().trimmed());
    Config::settings().setValue("engineRoot", root->text().trimmed());
    Config::settings().setValue("reduceMotion", reduce->isChecked());
    Config::settings().setValue("theme", theme->currentText());
    if (theme->currentText() != ThemeManager::instance().current()) {
        ThemeManager::instance().apply(theme->currentText());
        if (ThemeManager::instance().usingFallback()) setStatus(ThemeManager::instance().lastError(), true);
    }
    m_sessionApiKey = key->text().trimmed();
    if (m_state == State::Generating || m_state == State::Exporting) cancelJob();
    // Restart: stopping triggers workerStopped; start again afterwards.
    if (m_client->isRunning()) {
        auto *conn = new QMetaObject::Connection;
        *conn = connect(m_client, &ProtocolClient::workerStopped, this, [this, conn] {
            disconnect(*conn);
            delete conn;
            QTimer::singleShot(0, this, &MainWindow::startWorker);
        });
        m_client->stop();
    } else {
        startWorker();
    }
}

void MainWindow::closeEvent(QCloseEvent *e) {
    m_waveTimer.stop();
    if (!m_jobId.isEmpty()) m_client->cancel(m_jobId);
    m_player->stop();
    m_player->setSource({});
    m_previewPlayer->stop();
    m_previewPlayer->setSource({});
    m_client->stop();
    e->accept();
}
