#include "MainWindow.h"

#include "Config.h"
#include "ProtocolClient.h"
#include "ui/Buttons.h"
#include "ui/Card.h"
#include "ui/DictionaryDialog.h"
#include "ui/Frameless.h"
#include "ui/GlStage.h"
#include "ui/Icons.h"
#include "ui/Indicators.h"
#include "ui/Motion.h"
#include "ui/PronSettings.h"
#include "ui/QaPanel.h"
#include "ui/ReviewDialog.h"
#include "ui/ModelLabDialog.h"
#include "ui/ThemeManager.h"
#include "ui/VoiceCarousel.h"

#include <QAction>
#include <QAudioBufferOutput>
#include <QAudioDevice>
#include <QAudioOutput>
#include <QCheckBox>
#include <QCloseEvent>
#include <QComboBox>
#include <QCryptographicHash>
#include <QDialog>
#include <QDialogButtonBox>
#include <QDir>
#include <QDoubleSpinBox>
#include <QFile>
#include <QFileDialog>
#include <QFormLayout>
#include <QFrame>
#include <QGuiApplication>
#include <QHBoxLayout>
#include <QJsonArray>
#include <QJsonDocument>
#include <QLabel>
#include <QLineEdit>
#include <QMediaDevices>
#include <QPropertyAnimation>
#include <QVariantAnimation>
#include <QMediaPlayer>
#include <QMenu>
#include <QMenuBar>
#include <QMessageBox>
#include <QPlainTextEdit>
#include <QRegularExpression>
#include <QScreen>
#include <QScrollArea>
#include <QSlider>
#include <QStackedWidget>
#include <QStandardItemModel>
#include <QStandardPaths>
#include <QStringDecoder>
#include <QTabWidget>
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

// Label + field packed together so the pair can be shown/hidden as one unit.
QWidget *group(const QString &label, QWidget *field) {
    auto *w = new QWidget;
    auto *h = new QHBoxLayout(w);
    h->setContentsMargins(0, 0, 0, 0);
    h->setSpacing(6);
    h->addWidget(fieldLabel(label));
    h->addWidget(field, 1);
    return w;
}

AnimatedButton *button(const QString &text, const QString &icon, const char *name, const QString &token = "icon") {
    auto *b = new AnimatedButton(text);
    b->setObjectName(name);
    if (!icon.isEmpty()) b->setIcon(Icons::icon(icon, token));
    return b;
}

QString hashOf(const QString &t) {
    return QString::fromLatin1(QCryptographicHash::hash(t.toUtf8(), QCryptographicHash::Sha1).toHex());
}

}  // namespace

// Follow the system default speaker: plugging in earphones (or switching device) must move the sound with it.
static void useDefaultOutput(QAudioOutput *out) {
    const QAudioDevice dev = QMediaDevices::defaultAudioOutput();
    if (out && !dev.isNull() && out->device() != dev) out->setDevice(dev);
}

MainWindow::MainWindow(QWidget *parent) : QMainWindow(parent), m_client(new ProtocolClient(this)) {
    const bool custom = Frameless::customFrameWanted();
    if (custom) {
        // Frameless + translucent: the shell paints the rounded, shadowed window. The extra hints keep the taskbar
        // minimise/restore behaviour, Win+Arrow snapping and the system menu working.
        setWindowFlags(Qt::Window | Qt::FramelessWindowHint | Qt::WindowMinimizeButtonHint | Qt::WindowMaximizeButtonHint |
                       Qt::WindowCloseButtonHint | Qt::WindowSystemMenuHint);
        setAttribute(Qt::WA_TranslucentBackground);
    }
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

    auto *mediaDevices = new QMediaDevices(this);
    connect(mediaDevices, &QMediaDevices::audioOutputsChanged, this, [this] {
        useDefaultOutput(m_audioOut);
        useDefaultOutput(m_previewOut);
    });

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
        updateGlActivity();
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

bool MainWindow::customFrame() const { return m_shell && m_shell->customFrame(); }
bool MainWindow::preCheckEnabled() const { return m_precheck->isChecked(); }
void MainWindow::setPreCheckEnabled(bool on) { m_precheck->setChecked(on); }

// The media backend may still hold a file for a moment after setSource({}); retry removal later.
void MainWindow::purgeStale() {
    for (int i = m_stale.size() - 1; i >= 0; --i)
        if (!QFile::exists(m_stale[i]) || QFile::remove(m_stale[i])) m_stale.removeAt(i);
    for (int i = m_staleDirs.size() - 1; i >= 0; --i)
        if (!QDir(m_staleDirs[i]).exists() || QDir(m_staleDirs[i]).removeRecursively()) m_staleDirs.removeAt(i);
    if (!m_stale.isEmpty() || !m_staleDirs.isEmpty()) QTimer::singleShot(300, this, &MainWindow::purgeStale);
}

void MainWindow::removeSession() {
    if (!m_sessionDir.isEmpty()) m_staleDirs << m_sessionDir;
    m_sessionDir.clear();
    m_candidatePath.clear();
    m_acceptedPath.clear();
    m_repairResult = {};
}

void MainWindow::buildActions() {
    auto mk = [this](const QString &text, const char *icon, const QKeySequence &key, auto slot) {
        auto *a = new QAction(Icons::icon(icon), text, this);
        if (!key.isEmpty()) a->setShortcut(key);
        a->setShortcutContext(Qt::WindowShortcut);
        connect(a, &QAction::triggered, this, slot);
        addAction(a);
        m_actions << a;
        return a;
    };
    mk("Import text…", "import", QKeySequence::Open, [this] { importText(); });
    mk("Check pronunciation", "pron-check", QKeySequence("Ctrl+Shift+P"), [this] { if (m_state == State::Idle) analyzeText(false); });
    mk("Generate speech", "generate", QKeySequence("Ctrl+Return"), [this] { onGenerateClicked(); });
    mk("Recheck speech", "recheck", QKeySequence("Ctrl+R"), [this] { recheckSpeech(); });
    mk("Pronunciation dictionary…", "dictionary", QKeySequence("Ctrl+D"), [this] { openDictionary(); });
    mk("Model Lab…", "ai", QKeySequence("Ctrl+M"), [this] { openModelLab(); });
    mk("Save as MP3…", "save-mp3", QKeySequence::Save, [this] { saveMp3(); });
    mk("Show / hide side panel", "settings", QKeySequence("Ctrl+B"), [this] { setPanelOpen(!m_panelOpen); });
    mk("Settings…", "settings", QKeySequence("Ctrl+,"), [this] { openSettings(); });
    mk("Re-check backend", "recheck", QKeySequence(), [this] { startWorker(); });
    auto *sep = new QAction(this);
    sep->setSeparator(true);
    m_actions << sep;
    mk("Exit", "window-close", QKeySequence::Quit, [this] { close(); });
}

void MainWindow::buildUi() {
    ThemeManager::instance().ensureApplied();
    setWindowTitle("Voicely");
    setWindowIcon(Icons::icon("app-logo"));
    const bool custom = windowFlags().testFlag(Qt::FramelessWindowHint);
    setMinimumSize(custom ? 980 : 940, custom ? 700 : 660);
    m_shell = new ShellWidget(custom);
    setCentralWidget(m_shell);
    auto *outer = new QVBoxLayout(m_shell);
    outer->setContentsMargins(0, 0, 0, 0);
    outer->setSpacing(0);

    m_titleBar = new TitleBar;
    m_titleBar->setVisible(custom);
    outer->addWidget(m_titleBar);

    auto *body = new QVBoxLayout;
    body->setContentsMargins(8, 0, 8, 8);
    body->setSpacing(2);
    outer->addLayout(body, 1);
    auto *cols = new QHBoxLayout;
    cols->setSpacing(0);
    body->addLayout(cols, 1);

    auto newCard = [this](int top = 12, int bottom = 12) {
        auto *c = new Card;
        m_cards << c;
        auto *l = new QVBoxLayout(c);
        l->setContentsMargins(12, top - 4, 12, bottom - 4);
        l->setSpacing(8);
        return c;
    };

    // ============================================================ left column: text workspace + audio
    auto *left = new QVBoxLayout;
    left->setSpacing(0);
    cols->addLayout(left, 1);

    auto *tb = newCard();
    auto *tl = static_cast<QVBoxLayout *>(tb->layout());
    m_text = new QPlainTextEdit;
    m_text->setObjectName("textEditor");
    m_text->setMinimumHeight(240);
    m_inputEmpty = new EmptyState("empty-input", "Nothing to read yet", "Type, paste or import text to begin.", false, m_text->viewport());
    m_inputEmpty->setGeometry(m_text->viewport()->rect());
    m_text->viewport()->installEventFilter(this);
    auto *tr = new QHBoxLayout;
    tr->setSpacing(6);
    m_import = button("Import…", "import", "importButton");
    m_paste = button("Paste", "paste", "pasteButton");
    m_clear = button("Clear", "clear", "clearButton");
    m_checkText = button("Check", "pron-check", "checkTextButton");
    m_checkText->setToolTip("Find names, numbers, acronyms and ambiguous words that may be mispronounced (the text is not changed)");
    m_textBadge = new QLabel;
    m_textBadge->setObjectName("qaNote");
    m_counts = new QLabel("0 characters · 0 words");
    m_counts->setToolTip("Characters and words in the text");
    m_counts->setObjectName("muted");
    tr->addWidget(m_import);
    tr->addWidget(m_paste);
    tr->addWidget(m_clear);
    tr->addWidget(m_checkText);
    tr->addWidget(m_textBadge);
    tr->addStretch(1);
    tr->addWidget(m_counts);
    tl->addWidget(m_text, 1);
    tl->addLayout(tr);
    left->addWidget(tb, 1);

    auto *ab = newCard();
    ab->setRole("bar");
    auto *al = static_cast<QVBoxLayout *>(ab->layout());
    m_audioStack = new QStackedWidget;
    m_audioStack->setObjectName("audioStack");
    m_audioStack->setFixedHeight(96);
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

    m_versionBar = new QWidget;
    auto *vb = new QHBoxLayout(m_versionBar);
    vb->setContentsMargins(0, 0, 0, 0);
    vb->setSpacing(6);
    m_verOriginal = button("Original", "", "versionOriginal");
    m_verRepaired = button("Repaired (not accepted yet)", "repair", "versionRepaired");
    for (auto *b : {m_verOriginal, m_verRepaired}) b->setCheckable(true);
    m_verOriginal->setChecked(true);
    vb->addWidget(fieldLabel("Listen to:"));
    vb->addWidget(m_verOriginal);
    vb->addWidget(m_verRepaired);
    vb->addStretch(1);
    m_versionBar->hide();
    al->addWidget(m_versionBar);

    auto *a3 = new QHBoxLayout;
    a3->setSpacing(6);
    m_bitrate = new QComboBox;
    m_bitrate->setObjectName("bitrateCombo");
    for (int b : {128, 160, 192, 256, 320}) m_bitrate->addItem(QString("%1 kbps").arg(b), b);
    m_bitrate->setCurrentIndex(2);
    m_saveMp3 = button("Save as MP3…", "save-mp3", "saveMp3Button");
    m_saveWav = button("Save WAV…", "export-wav", "saveWavButton");
    m_success = new SuccessBadge;
    a3->addWidget(fieldLabel("MP3"));
    a3->addWidget(m_bitrate);
    a3->addWidget(m_saveMp3);
    a3->addWidget(m_saveWav);
    a3->addWidget(m_success);
    a3->addStretch(1);
    al->addLayout(a3);
    m_outLabel = new QLabel("No audio generated yet.");
    m_outLabel->setObjectName("outputLabel");
    m_outLabel->setTextInteractionFlags(Qt::TextSelectableByMouse);
    m_outLabel->setWordWrap(true);
    al->addWidget(m_outLabel);
    auto *barRow = new QHBoxLayout;                  // floats inside the column: extra side margin
    barRow->setContentsMargins(14, 0, 14, 2);
    barRow->addWidget(ab);
    left->addLayout(barRow);

    // ============================================================ right column: voice, generate, QA
    auto *scroll = new QScrollArea;
    scroll->setObjectName("rightScroll");
    scroll->setWidgetResizable(true);
    scroll->setFrameShape(QFrame::NoFrame);
    scroll->setHorizontalScrollBarPolicy(Qt::ScrollBarAlwaysOff);
    scroll->setFixedWidth(390);
    m_sideScroll = scroll;
    auto *rw = new QWidget;
    rw->setMinimumWidth(372);
    auto *right = new QVBoxLayout(rw);
    right->setContentsMargins(0, 0, 0, 0);
    right->setSpacing(0);
    scroll->setWidget(rw);
    cols->addWidget(scroll);

    auto *vc = newCard();
    auto *vl = static_cast<QVBoxLayout *>(vc->layout());
    m_voice = new QComboBox;
    m_voice->setObjectName("voiceCombo");
    m_voice->setMinimumWidth(120);
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
    r0->addWidget(m_voice, 1);
    r0->addWidget(m_preview);                 // speaker icon sits right beside the voice name
    vl->addLayout(r0);
    m_voiceInfoLabel = new QLabel;
    m_voiceInfoLabel->setObjectName("voiceInfoLabel");
    m_voiceInfoLabel->setProperty("small", true);
    m_voiceInfoLabel->setWordWrap(true);
    vl->addWidget(m_voiceInfoLabel);
    m_carousel = new VoiceCarousel(m_voice);
    vl->addWidget(m_carousel);
    connect(m_voice, &QComboBox::currentIndexChanged, this, [this] {
        m_voiceInfoLabel->setText(m_voiceInfo.value(m_voice->currentData().toString()));
    });
    vl->addWidget(group("Backend:", m_backend));
    m_optRow = new QWidget;
    auto *r1 = new QVBoxLayout(m_optRow);
    r1->setContentsMargins(0, 0, 0, 0);
    r1->setSpacing(8);
    auto *r1a = new QHBoxLayout;
    r1a->setSpacing(12);
    m_langGroup = group("Language:", m_lang);
    m_speedGroup = group("Speed:", m_speed);
    m_styleGroup = group("Style:", m_style);
    r1a->addWidget(m_langGroup, 1);
    r1a->addWidget(m_speedGroup);
    r1->addLayout(r1a);
    r1->addWidget(m_styleGroup);
    vl->addWidget(m_optRow);
    vl->addWidget(m_voiceNote);
    right->addWidget(vc);

    auto *gc = newCard();
    auto *gl = static_cast<QVBoxLayout *>(gc->layout());
    auto *g1 = new QHBoxLayout;
    g1->setSpacing(8);
    m_generate = button("Generate Speech", "generate", "primary", "on-accent");
    m_generate->setProperty("testId", "generate");
    m_generate->setMinimumHeight(38);
    m_busy = new StateIcon(18);
    m_busy->setKind(StateIcon::Kind::Busy);
    m_busy->hide();
    m_cancel = button("Cancel", "cancel", "cancelButton");
    m_cancel->hide();
    g1->addWidget(m_generate, 1);
    g1->addWidget(m_busy);
    g1->addWidget(m_cancel);
    gl->addLayout(g1);
    m_progress = new SmoothProgressBar;
    m_progress->setObjectName("progressBar");
    gl->addWidget(m_progress);
    m_precheck = new QCheckBox("Check pronunciation before generating");
    m_precheck->setObjectName("precheckBox");
    m_precheck->setChecked(Config::settings().value("precheck", true).toBool());
    m_precheck->setToolTip("Analyses the text for names, numbers, acronyms and ambiguous words first; the text is never rewritten");
    gl->addWidget(m_precheck);
    right->addWidget(gc);

    auto *qc = newCard();
    auto *ql = static_cast<QVBoxLayout *>(qc->layout());
    auto *qt = new QLabel("Pronunciation check");
    qt->setObjectName("sectionTitle");
    ql->addWidget(qt);
    m_qa = new QaPanel;
    ql->addWidget(m_qa, 1);
    right->addWidget(qc, 1);

    // ============================================================ status strip + diagnostics
    auto *sr = new QHBoxLayout;
    sr->setContentsMargins(12, 0, 12, 0);
    sr->setSpacing(8);
    m_dot = new StateIcon(16);
    m_dot->setObjectName("statusDot");
    m_status = new QLabel("Starting speech engine…");
    m_status->setObjectName("statusLabel");
    m_status->setWordWrap(true);
    m_title = new QLabel;     // kept for compatibility with older code paths; the title lives in the title bar
    m_title->hide();
    m_logToggle = new QToolButton;
    m_logToggle->setObjectName("logToggle");
    m_logToggle->setText(" Diagnostics");
    m_logToggle->setIcon(Icons::icon("chevron-right"));
    m_logToggle->setIconSize(QSize(14, 14));
    m_logToggle->setToolButtonStyle(Qt::ToolButtonTextBesideIcon);
    m_logToggle->setCheckable(true);
    m_settingsBtn = button("Settings", "settings", "settingsButton");
    sr->addWidget(m_dot);
    sr->addWidget(m_status, 1);
    sr->addWidget(m_logToggle);
    sr->addWidget(m_settingsBtn);
    body->addLayout(sr);
    m_log = new QPlainTextEdit;
    m_log->setObjectName("logView");
    m_log->setReadOnly(true);
    m_log->setMaximumBlockCount(2000);
    m_log->setMaximumHeight(130);
    m_logPanel = m_log;
    m_logPanel->hide();
    body->addWidget(m_logPanel);

    connect(m_logToggle, &QToolButton::toggled, this, [this](bool on) {
        m_logPanel->setVisible(on);
        if (on) Fx::fadeIn(m_logPanel, 160);
        m_logToggle->setIcon(Icons::icon(on ? "chevron-down" : "chevron-right"));
    });
    connect(m_settingsBtn, &QPushButton::clicked, this, &MainWindow::openSettings);
    connect(m_import, &QPushButton::clicked, this, &MainWindow::importText);
    connect(m_paste, &QPushButton::clicked, m_text, &QPlainTextEdit::paste);
    connect(m_clear, &QPushButton::clicked, m_text, &QPlainTextEdit::clear);
    connect(m_checkText, &QPushButton::clicked, this, [this] { analyzeText(false); });
    connect(m_text, &QPlainTextEdit::textChanged, this, &MainWindow::updateCounts);
    connect(m_generate, &QPushButton::clicked, this, &MainWindow::onGenerateClicked);
    connect(m_precheck, &QCheckBox::toggled, this, [](bool on) { Config::settings().setValue("precheck", on); });
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
    connect(m_verOriginal, &QPushButton::clicked, this, [this] {
        m_verOriginal->setChecked(true);
        m_verRepaired->setChecked(false);
        if (!m_acceptedPath.isEmpty() && m_audioPath != m_acceptedPath) loadAudio(m_acceptedPath);
    });
    connect(m_verRepaired, &QPushButton::clicked, this, [this] {
        m_verOriginal->setChecked(false);
        m_verRepaired->setChecked(true);
        if (!m_candidatePath.isEmpty() && m_audioPath != m_candidatePath) loadAudio(m_candidatePath);
    });
    connect(m_qa, &QaPanel::recheckRequested, this, &MainWindow::recheckSpeech);
    connect(m_qa, &QaPanel::replayRequested, this, &MainWindow::onReplay);
    connect(m_qa, &QaPanel::correctRequested, this, &MainWindow::onCorrect);
    connect(m_qa, &QaPanel::repairRequested, this, [this](const QString &mode, const QJsonArray &chunks) { runRepair(mode, chunks, {}); });
    connect(m_qa, &QaPanel::acceptRepairRequested, this, &MainWindow::acceptRepair);
    connect(m_qa, &QaPanel::discardRepairRequested, this, &MainWindow::discardRepair);
    connect(m_qa, &QaPanel::saveFeedbackRequested, this, &MainWindow::onSaveFeedback);
    connect(m_qa, &QaPanel::dictionaryRequested, this, &MainWindow::openDictionary);

    buildActions();
    if (custom) {
        for (auto *a : m_actions) m_titleBar->commandMenu()->addAction(a);
    } else {                                   // native frame fallback: the same commands in a normal menu bar
        auto *menu = menuBar()->addMenu("&Menu");
        for (auto *a : m_actions) menu->addAction(a);
    }

    // window placement: restore, but only onto a screen that still exists
    const QRect saved = Config::settings().value("windowRect").toRect();
    setGeometry(Frameless::fitToScreens(saved, QSize(1120, 780)));
    if (Config::settings().value("windowMaximized", false).toBool()) setWindowState(windowState() | Qt::WindowMaximized);

    m_qa->setSessionAvailable(false);
    setState(State::Starting);
    updateCounts();

    connect(m_titleBar->panelButton(), &QToolButton::toggled, this, [this](bool on) { setPanelOpen(on); });
    connect(m_shell, &ShellWidget::effectsFailed, this, [this](const QString &why) {
        m_titleBar->setOrbMode(false);
        m_wave->setGlMode(false);
        log("3D effects off: " + why);
    });
    setPanelOpen(Config::settings().value("sidePanel", true).toBool(), false);
    applyEffects();
}

void MainWindow::setPanelOpen(bool open, bool animate) {
    m_panelOpen = open;
    Config::settings().setValue("sidePanel", open);
    { QSignalBlocker b(m_titleBar->panelButton()); m_titleBar->panelButton()->setChecked(open); }
    const int to = open ? 390 : 0;
    auto setW = [this](int w) {                       // fixed width (min = max) so the column never gets squeezed
        m_sideScroll->setMinimumWidth(w);
        m_sideScroll->setMaximumWidth(w);
    };
    if (!animate || Motion::reduced() || !isVisible()) {
        setW(to);
        m_sideScroll->setVisible(open);
        return;
    }
    if (open) m_sideScroll->setVisible(true);
    auto *a = new QVariantAnimation(this);
    a->setDuration(Motion::ms(260));
    a->setEasingCurve(QEasingCurve::OutCubic);
    a->setStartValue(m_sideScroll->maximumWidth());
    a->setEndValue(to);
    connect(a, &QVariantAnimation::valueChanged, this, [setW](const QVariant &v) { setW(v.toInt()); });
    connect(a, &QVariantAnimation::finished, this, [this, open] { if (!open && !m_panelOpen) m_sideScroll->hide(); });
    a->start(QAbstractAnimation::DeleteWhenStopped);
}

void MainWindow::applyEffects() {
    const bool on = m_shell->setEffectsEnabled(Config::settings().value("effects", true).toBool());
    m_titleBar->setOrbMode(on);
    m_wave->setGlMode(on);
    if (auto *gl = m_shell->glStage()) {
        gl->setOrbAnchor(m_titleBar->orbSlot());
        gl->setWaveAnchor(m_audioStack);
        const bool haveAudio = !m_audioPath.isEmpty() && QFileInfo::exists(m_audioPath);
        gl->setWaveStrength(haveAudio ? 1.0 : 0.35);
    }
    updateGlActivity();
}

void MainWindow::feedDemoLevels(double t) {
    if (auto *gl = m_shell->glStage()) {
        gl->setWaveStrength(1.0);
        gl->setActivity(GlStage::Activity::Playing);
        gl->pushLevel(0.5 + 0.45 * std::sin(t * 5.0) * std::sin(t * 1.3 + 1.0));
    }
}

void MainWindow::updateGlActivity() {
    auto *gl = m_shell ? m_shell->glStage() : nullptr;
    if (!gl) return;
    const bool busy = m_state == State::Generating || m_state == State::Exporting || m_state == State::Rechecking ||
                      m_state == State::Repairing;
    const bool playing = m_player->playbackState() == QMediaPlayer::PlayingState;
    gl->setActivity(playing ? GlStage::Activity::Playing : busy ? GlStage::Activity::Busy : GlStage::Activity::Idle);
}

void MainWindow::showEvent(QShowEvent *e) {
    QMainWindow::showEvent(e);
    if (!m_entrancePlayed) {
        m_text->setFocus();                     // text-first workflow: start typing immediately
        // staggered, subtle panel entrance (skipped with reduced motion)
        m_entrancePlayed = true;
        int i = 0;
        for (Card *c : m_cards) c->playEntrance(60 * i++);
    }
}

void MainWindow::changeEvent(QEvent *e) {
    QMainWindow::changeEvent(e);
    if (e->type() == QEvent::WindowStateChange && m_shell) {
        m_shell->setMaximizedLook(isMaximized() || isFullScreen());
        m_titleBar->syncWindowState();
    }
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
    QString tip = "Working", chip = "Working…";
    const QString be = m_backend && m_backend->count() ? m_backend->currentData().toString() : QString();
    switch (m_state) {
    case State::Idle:
        k = m_statusError ? StateIcon::Kind::Warning : StateIcon::Kind::Success;
        tip = m_statusError ? "Ready, but the last action reported a problem" : "Backend ready";
        chip = "Ready" + (be.isEmpty() ? QString() : " · " + be);
        break;
    case State::Unavailable:
        k = StateIcon::Kind::Error;
        tip = "Backend unavailable";
        chip = "Backend unavailable";
        break;
    case State::Starting: chip = "Starting…"; break;
    case State::Rechecking: chip = "Checking speech…"; break;
    case State::Repairing: chip = "Repairing…"; break;
    default: break;
    }
    m_dot->setKind(k);
    m_dot->setToolTip(tip);
    m_titleBar->setBackendStatus(int(k), chip);
}

void MainWindow::log(const QString &line) {
    m_log->appendPlainText(line);
}

void MainWindow::setState(State s) {
    m_state = s;
    const bool idle = s == State::Idle;
    const bool busy = s == State::Generating || s == State::Exporting || s == State::Rechecking || s == State::Repairing;
    const bool haveAudio = !m_audioPath.isEmpty() && QFileInfo::exists(m_audioPath);
    m_generate->setEnabled(idle && !m_analyzing);
    m_checkText->setEnabled(idle && !m_analyzing);
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
    m_qa->setBusy(s == State::Rechecking || s == State::Repairing,
                  s == State::Rechecking ? "Transcribing and comparing the speech…" : "Regenerating and rechecking the affected chunks…");
    m_qa->setSessionAvailable(!m_sessionDir.isEmpty() && (idle || busy));
    m_qa->setCandidateAvailable(!m_candidatePath.isEmpty());
    updateHeaderIcon();
    updateAudioPanel();
    updatePreviewUi();
    updateGlActivity();
}

void MainWindow::updateAudioPanel() {
    const bool haveAudio = !m_audioPath.isEmpty() && QFileInfo::exists(m_audioPath);
    const int idx = haveAudio ? 1 : 0;
    if (m_audioStack->currentIndex() != idx) {
        m_audioStack->setCurrentIndex(idx);
        Fx::fadeIn(m_audioStack->currentWidget(), 200);
    }
    if (m_shell && m_shell->glStage()) m_shell->glStage()->setWaveStrength(haveAudio ? 1.0 : 0.35);
    updateVersionBar();
}

void MainWindow::updateVersionBar() {
    const bool has = !m_candidatePath.isEmpty();
    if (has != m_versionBar->isVisibleTo(this)) {
        m_versionBar->setVisible(has);
        if (has) Fx::fadeIn(m_versionBar, 200);
    }
    if (has) {
        const bool rep = m_audioPath == m_candidatePath;
        m_verRepaired->setChecked(rep);
        m_verOriginal->setChecked(!rep);
    }
}

void MainWindow::updatePreviewUi() {
    PreviewButton::Mode mode = PreviewButton::Mode::Idle;
    if (m_state == State::Generating && m_jobIsTest) mode = PreviewButton::Mode::Loading;
    else if (m_previewPlayer->playbackState() == QMediaPlayer::PlayingState) mode = PreviewButton::Mode::Speaking;
    m_preview->setMode(mode);
    // Usable when idle (start) or while a preview is running (cancel/stop).
    m_preview->setEnabled((m_state == State::Idle && m_voice->count() > 0 && !m_analyzing) || mode != PreviewButton::Mode::Idle);
}

QProcessEnvironment MainWindow::workerEnv() const {
    QProcessEnvironment e;
    if (!m_sessionApiKey.isEmpty()) e.insert("GEMINI_API_KEY", m_sessionApiKey);  // memory only, never persisted
    return e;
}

QString MainWindow::currentLanguage() const {
    return m_lang->isVisible() && m_lang->count() ? m_lang->currentData().toString() : QStringLiteral("en-us");
}

QJsonObject MainWindow::qaThresholds() const {
    const QByteArray raw = Config::settings().value("qaThresholds").toString().toUtf8();
    const QJsonDocument doc = QJsonDocument::fromJson(raw);
    return doc.isObject() ? doc.object() : QJsonObject();
}

bool MainWindow::backendHasIpa() const {
    return m_caps.value("pronunciation").toObject().value(m_backend->currentData().toString()).toObject().value("ipa").toBool();
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

void MainWindow::applyHealth(const QJsonObject &r) {
    const QJsonObject asr = r.value("asr").toObject(), llm = r.value("llm").toObject();
    QStringList p;
    for (const auto &x : asr.value("problems").toArray()) p << x.toString();
    m_qa->setAsrAvailable(asr.isEmpty() ? false : asr.value("available").toBool(), p.join(' '));
    m_qa->setLlmAvailable(llm.value("enabled").toBool() && llm.value("available").toBool());
    if (!asr.value("available").toBool() && !asr.isEmpty()) log("[health] speech recognition unavailable: " + p.join(' '));
}

void MainWindow::onWorkerStopped(const QString &reason) {
    m_jobId.clear();
    m_job = Job::None;
    m_analyzing = false;
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
    note += backendHasIpa() ? " Pronunciation control: respelling and IPA." : " Pronunciation control: respelling only (no IPA).";
    m_voiceNote->setText(note);
    m_voiceNote->setToolTip(note);
    updateHeaderIcon();
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
        const QString previous = m_backend->count() ? m_backend->currentData().toString() : QString();
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
        const int keep = m_backend->findData(previous);
        if (keep >= 0) m_backend->setCurrentIndex(keep);
        m_backend->blockSignals(false);
        for (const QString &p : problems) log("[health] " + p);
        if (!r.value("mp3").toObject().value("available").toBool())
            log("[health] MP3 export unavailable: " + r.value("mp3").toObject().value("problem").toString());
        applyHealth(r);
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
            m_voiceSamples.clear();
            m_voiceInfo.clear();
            // Voice library: grouped like other voice platforms (Classic / Girls / Boys), friendly names, stored samples.
            struct Group { const char *title; QString gender; bool classic; };
            const QList<Group> groups{{"Classic voices", {}, true}, {"Girls", "female", false}, {"Boys", "male", false}};
            const auto isClassic = [](const QString &n) {
                return n == "original" || n == "reference" || n == "deep" || n == "boy";
            };
            auto *model = qobject_cast<QStandardItemModel *>(m_voice->model());
            for (const Group &g : groups) {
                bool headerAdded = false;
                for (const auto &v : r.value("voices").toArray()) {
                    const QJsonObject o = v.toObject();
                    const QString name = o.value("name").toString();
                    const bool inGroup = g.classic ? isClassic(name)
                                                   : (!isClassic(name) && o.value("gender").toString() == g.gender);
                    if (!inGroup) continue;
                    if (!headerAdded && model) {
                        m_voice->addItem(QString(g.title).toUpper());
                        if (QStandardItem *h = model->item(m_voice->count() - 1)) {
                            h->setFlags(Qt::NoItemFlags);                 // a label, not a choice
                            QFont f = h->font();
                            f.setBold(true);
                            f.setPointSizeF(f.pointSizeF() * 0.85);
                            h->setFont(f);
                        }
                        headerAdded = true;
                    }
                    const QString display = o.value("display_name").toString(name);
                    const QString tag = o.value("tagline").toString();
                    m_voice->addItem(display, name);
                    m_voice->setItemData(m_voice->count() - 1,
                                         display + " — " + tag + "\n" + o.value("description").toString() + "\n" +
                                             o.value("kind").toString(),
                                         Qt::ToolTipRole);
                    m_voiceSamples.insert(name, o.value("sample_path").toString());
                    const QString who = o.value("gender").toString() == "female" ? "Female"
                                        : o.value("gender").toString() == "male" ? "Male" : QString();
                    m_voiceInfo.insert(name, who.isEmpty() ? tag : who + "  ·  " + tag);
                }
            }
            const int i = m_voice->findData("original");
            if (i >= 0) m_voice->setCurrentIndex(i);
            m_voiceInfoLabel->setText(m_voiceInfo.value(m_voice->currentData().toString()));
            if (m_backend->count() > 0) backendChanged();
            updatePreviewUi();
        }
        return;
    }
    if (id != m_jobId) return;   // reply to a cancel request, a stale job, or a callback-based request

    m_jobId.clear();
    const Job job = m_job;
    m_job = Job::None;
    if (status == "success") {
        switch (job) {
        case Job::Export: {
            const QString out = r.value("output_path").toString();
            QFileInfo fi(out);
            if (!fi.exists() || fi.size() == 0) {
                setStatus("Export reported success but the file is missing or empty: " + out, true);
            } else {
                setStatus(QString("Saved %1 (%2 KB, %3 s)").arg(QDir::toNativeSeparators(out))
                              .arg(fi.size() / 1024).arg(r.value("duration_s").toDouble(), 0, 'f', 1));
                static const char *labels[] = {"not checked", "checking", "verified", "needs review", "failed"};
                m_outLabel->setText("Saved: " + QDir::toNativeSeparators(out) + "  ·  speech check: " +
                                    labels[int(m_qa->status())]);
                m_success->play();
            }
            break;
        }
        case Job::Preview:
            loadPreview(r.value("output_path").toString());
            setStatus(QString("Voice preview generated (%1 s).").arg(r.value("duration_s").toDouble(), 0, 'f', 1));
            { useDefaultOutput(m_previewOut); m_previewPlayer->play(); }
            break;
        case Job::Recheck: {
            m_qa->setBusy(false);
            m_qa->setSummary(r.value("summary").toObject());
            const QString st = r.value("summary").toObject().value("status").toString();
            setStatus(st == "verified" ? "Speech check finished: all automated checks passed."
                      : st == "failed" ? "Speech check finished: failed — see the report."
                                       : QString("Speech check finished: %1 suspected issue(s) need review.")
                                             .arg(r.value("summary").toObject().value("issue_count").toInt()), st == "failed");
            break;
        }
        case Job::Repair: {
            m_qa->setBusy(false);
            m_repairResult = r;
            m_qa->setRepairResult(r);
            m_candidatePath = r.value("candidate_path").toString();
            const int n = r.value("repaired_chunks").toArray().size(), u = r.value("unresolved_chunks").toArray().size();
            if (!m_candidatePath.isEmpty()) {
                loadAudio(m_candidatePath);           // preview the repaired version; the original stays untouched in the session
                setStatus(QString("Repaired %1 chunk(s) and rechecked them%2. Listen, then accept or keep the original.")
                              .arg(n).arg(u ? QString("; %1 could not be fixed").arg(u) : QString()));
            } else {
                setStatus("No correction could be confirmed by the recheck — the original audio is unchanged. See the report for why.", true);
            }
            break;
        }
        case Job::Synth:
        default:
            if (r.contains("session")) {
                removeSession();
                m_sessionDir = r.value("session").toObject().value("dir").toString();
                m_acceptedPath = r.value("output_path").toString();
                m_qa->reset();
            }
            loadAudio(r.value("output_path").toString());
            setStatus(QString("Generated %1 s of audio with %2 / %3.")
                          .arg(r.value("duration_s").toDouble(), 0, 'f', 1, ' ')
                          .arg(r.value("backend").toString(), r.value("voice").toString()));
            break;
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
    const QString verb = m_state == State::Exporting ? "Exporting… " : m_state == State::Rechecking ? "Checking… "
                         : m_state == State::Repairing ? "Repairing… " : "Generating… ";
    m_status->setText(verb + msg);
}

// ---------------------------------------------------------------- actions
void MainWindow::onGenerateClicked() {
    if (m_state != State::Idle || !m_jobId.isEmpty() || m_analyzing) return;
    if (m_text->toPlainText().trimmed().isEmpty()) {
        setStatus("Enter or import some text first.", true);
        return;
    }
    if (m_precheck->isChecked() && m_backend->count() > 0) analyzeText(true);
    else generate(false);
}

void MainWindow::analyzeText(bool thenGenerate) {
    if (m_state != State::Idle || m_analyzing) return;
    const QString text = m_text->toPlainText();
    if (text.trimmed().isEmpty()) {
        setStatus("Enter or import some text first.", true);
        return;
    }
    m_analyzing = true;
    const int seq = ++m_analysisSeq;
    setState(m_state);
    setStatus("Checking the text for pronunciation problems…");
    m_client->call("analyze_text", {{"text", text}, {"backend", m_backend->currentData().toString()}, {"language", currentLanguage()}}, this,
                   [this, seq, text, thenGenerate](const QString &st, const QJsonObject &r) {
        if (seq != m_analysisSeq) return;                          // a newer request superseded this answer
        m_analyzing = false;
        setState(m_state);
        if (st != "success") {
            setStatus("Pronunciation check unavailable (" + r.value("message").toString() + ")", true);
            if (thenGenerate) generate(false);
            return;
        }
        const int review = r.value("counts").toObject().value("review").toInt();
        m_textBadge->setText(review ? QString("%1 to review").arg(review) : "no concerns found");
        const bool changed = text != m_text->toPlainText();
        if (changed) { m_textBadge->clear(); return; }
        if (review > 0 && (!thenGenerate || hashOf(text) != m_ackHash)) {
            showReview(r, thenGenerate);
            return;
        }
        setStatus(review ? QString("%1 item(s) may need review.").arg(review) : "No pronunciation concerns found in the text.");
        if (thenGenerate) generate(false);
    }, 60000);
}

void MainWindow::showReview(const QJsonObject &analysis, bool thenGenerate) {
    const QString text = m_text->toPlainText();
    ReviewDialog dlg(m_client, analysis, text, currentLanguage(), m_backend->currentData().toString(),
                     m_health.value("llm").toObject(), this);
    if (dlg.exec() == QDialog::Accepted) {
        m_ackHash = hashOf(text);                 // do not ask again for this exact text
        generate(false);
    } else if (thenGenerate) {
        setStatus("Generation cancelled — review the pronunciations or try again.");
    }
}

void MainWindow::generate(bool testVoice) {
    if (m_state != State::Idle || !m_jobId.isEmpty()) return;   // no duplicate requests
    QString text = testVoice ? QStringLiteral("Hello. This is a short test of the selected voice.") : m_text->toPlainText();
    if (text.trimmed().isEmpty()) {
        setStatus("Enter or import some text first.", true);
        return;
    }
    releasePreviewFile();    // a new job always supersedes any preview
    QString out;
    QJsonObject extra;
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
        removeSession();
        m_qa->reset();
        m_qa->setSessionAvailable(false);
        m_textBadge->clear();
        purgeStale();
        out = QString("%1/preview-%2.wav").arg(m_tempDir).arg(++m_genCounter);
        extra["session_dir"] = QString("%1/session-%2").arg(m_tempDir).arg(m_genCounter);
    }

    QJsonObject p{{"text", text}, {"backend", m_backend->currentData().toString()},
                  {"voice", m_voice->currentData().toString()}, {"output_path", out}, {"output_format", "wav"},
                  {"overwrite", true}};
    for (auto it = extra.begin(); it != extra.end(); ++it) p[it.key()] = it.value();
    if (m_lang->isVisible() && m_lang->count()) p["language"] = m_lang->currentData().toString();
    if (m_speed->isVisible() && !qFuzzyCompare(m_speed->value(), 1.0)) p["speed"] = m_speed->value();
    if (m_style->isVisible() && !m_style->text().trimmed().isEmpty()) p["style"] = m_style->text().trimmed();
    m_jobIsTest = testVoice;
    m_job = testVoice ? Job::Preview : Job::Synth;
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
    case PreviewButton::Mode::Idle: {
        // Stored sample (rendered once, shipped with the app) -> instant, no run-time synthesis.
        // Only valid for the local backend those samples were made with; other backends synthesize live.
        const QString sample = m_voiceSamples.value(m_voice->currentData().toString());
        if (m_state == State::Idle && m_backend->currentData().toString() == "local" && !sample.isEmpty() &&
            QFile::exists(sample)) {
            releasePreviewFile();
            if (m_player->playbackState() == QMediaPlayer::PlayingState) m_player->pause();
            m_previewPlayer->setSource(QUrl::fromLocalFile(sample));   // m_previewPath stays empty: never deleted
            { useDefaultOutput(m_previewOut); m_previewPlayer->play(); }
        } else {
            generate(true);
        }
        break;
    }
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
    m_player->stop();
    m_audioPath = path;
    m_player->setSource(QUrl::fromLocalFile(path));
    const bool rep = !m_candidatePath.isEmpty() && path == m_candidatePath;
    m_outLabel->setText((rep ? "Repaired version (not accepted yet): " : "Preview: ") + QDir::toNativeSeparators(path) +
                        "  (temporary — use Save to keep it)");
}

void MainWindow::exportAudio(const QString &path, const QString &format) {
    if (m_state != State::Idle || m_audioPath.isEmpty()) return;
    QJsonObject p{{"input_path", m_audioPath}, {"output_path", path}, {"output_format", format},
                  {"overwrite", true}};   // overwrite was confirmed by the native save dialog
    if (format == "mp3") p["bitrate_kbps"] = m_bitrate->currentData().toInt();
    m_jobIsTest = false;
    m_job = Job::Export;
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
    else { useDefaultOutput(m_audioOut); m_player->play(); }
}

void MainWindow::stopPlay() {
    m_player->stop();
}

// ---------------------------------------------------------------- pronunciation QA
void MainWindow::recheckSpeech() {
    if (m_state != State::Idle || m_sessionDir.isEmpty()) return;
    const bool candidate = !m_candidatePath.isEmpty() && m_audioPath == m_candidatePath;
    m_job = Job::Recheck;
    m_jobIsTest = false;
    QJsonObject rq{{"session_dir", m_sessionDir}, {"which", candidate ? "candidate" : "accepted"}};
    if (!qaThresholds().isEmpty()) rq["thresholds"] = qaThresholds();
    m_jobId = m_client->request("recheck", rq, 30 * 60 * 1000);
    m_progress->resetNow();
    setState(State::Rechecking);
    setStatus("Rechecking the speech…");
}

void MainWindow::runRepair(const QString &mode, const QJsonArray &chunks, const QJsonArray &candidates) {
    if (m_state != State::Idle || m_sessionDir.isEmpty()) return;
    bool useLlm = false, allowNet = false;
    const QJsonObject llm = m_health.value("llm").toObject();
    if (mode == "auto" && llm.value("enabled").toBool() && llm.value("available").toBool()) {
        const bool net = llm.value("network").toBool();
        const auto a = QMessageBox::question(this, "Use AI suggestions?",
            net ? QString("Auto-repair can also ask the “%1” AI provider for pronunciation suggestions. That sends the sentences "
                          "that need repair to the provider over the network. Use AI suggestions?").arg(llm.value("provider").toString())
                : "Auto-repair can also ask the configured AI model for pronunciation suggestions. Use AI suggestions?",
            QMessageBox::Yes | QMessageBox::No | QMessageBox::Cancel);
        if (a == QMessageBox::Cancel) return;
        useLlm = a == QMessageBox::Yes;
        allowNet = useLlm && net;
    }
    QJsonObject p{{"session_dir", m_sessionDir}, {"mode", mode}, {"candidates", candidates}, {"max_attempts", 3},
                  {"use_llm", useLlm}, {"allow_network", allowNet}};
    if (!chunks.isEmpty()) p["chunks"] = chunks;
    if (!qaThresholds().isEmpty()) p["thresholds"] = qaThresholds();
    m_job = Job::Repair;
    m_jobIsTest = false;
    m_jobId = m_client->request("regenerate", p, 60 * 60 * 1000);
    m_progress->resetNow();
    setState(State::Repairing);
    setStatus("Repairing the flagged chunks…");
}

void MainWindow::onCorrect(const QJsonObject &issue) {
    QJsonObject init{{"written", issue.value("intended").toString()}, {"category", "name"},
                     {"language", "*"}, {"note", "added from a speech check"}};
    EntryEditor ed(init, backendHasIpa(), true, this);
    if (ed.exec() != QDialog::Accepted) return;
    const QJsonObject e = ed.entry();
    const QJsonArray cand{QJsonObject{{"chunk", issue.value("chunk")}, {"written", e.value("written")},
                                      {"hint", e.value("hint")}, {"ipa", e.value("ipa")}}};
    const int chunk = issue.value("chunk").toInt();
    if (ed.saveToDictionary()) {
        DictionaryDialog::addEntry(m_client, this, e, m_backend->currentData().toString(), [=](bool stored, const QJsonObject &) {
            if (stored) log("[dictionary] added " + e.value("written").toString());
            runRepair("review", QJsonArray{chunk}, cand);   // regenerate even if the user declined to store it
        });
    } else {
        runRepair("review", QJsonArray{chunk}, cand);
    }
}

void MainWindow::onReplay(int chunk) {
    if (m_sessionDir.isEmpty()) return;
    const bool candidate = !m_candidatePath.isEmpty() && m_audioPath == m_candidatePath;
    m_client->call("segment", {{"session_dir", m_sessionDir}, {"chunk", chunk}, {"which", candidate ? "candidate" : "accepted"}}, this,
                   [this](const QString &st, const QJsonObject &r) {
        if (st != "success") { setStatus("Cannot replay: " + r.value("message").toString(), true); return; }
        if (m_player->playbackState() == QMediaPlayer::PlayingState) m_player->pause();
        m_previewPlayer->stop();
        m_previewPlayer->setSource(QUrl::fromLocalFile(r.value("path").toString()));
        { useDefaultOutput(m_previewOut); m_previewPlayer->play(); }
        setStatus("Replaying chunk " + QString::number(r.value("chunk").toInt() + 1) + ".");
    });
}

void MainWindow::acceptRepair() {
    if (m_candidatePath.isEmpty()) return;
    m_client->call("accept_repair", {{"session_dir", m_sessionDir}}, this, [this](const QString &st, const QJsonObject &r) {
        if (st != "success") { setStatus("Could not accept the repair: " + r.value("message").toString(), true); return; }
        m_candidatePath.clear();
        m_acceptedPath = r.value("accepted_path").toString();
        loadAudio(m_acceptedPath);
        if (!r.value("summary").isNull()) m_qa->setSummary(r.value("summary").toObject());
        m_qa->setCandidateAvailable(false);
        setState(m_state);
        setStatus("Repair accepted. The original audio is still stored in this session; run Recheck Speech to verify the whole result.");
    });
}

void MainWindow::discardRepair() {
    if (m_candidatePath.isEmpty()) return;
    m_client->call("discard_repair", {{"session_dir", m_sessionDir}}, this, [this](const QString &st, const QJsonObject &r) {
        if (st != "success") { setStatus("Could not discard the repair: " + r.value("message").toString(), true); return; }
        m_candidatePath.clear();
        loadAudio(m_acceptedPath);
        if (!r.value("summary").isNull()) m_qa->setSummary(r.value("summary").toObject());
        m_qa->setCandidateAvailable(false);
        setState(m_state);
        setStatus("Kept the original audio.");
    });
}

void MainWindow::onSaveFeedback(const QJsonObject &attempt) {
    const QString term = attempt.value("term").toString();
    const QJsonObject cand = attempt.value("candidate").toObject();
    const auto add = QMessageBox::question(this, "Keep this correction?",
        QString("The recheck confirmed that “%1” is now recognised correctly with “%2”.\n\nAdd it to your pronunciation dictionary so "
                "it is used automatically from now on?").arg(term, cand.value("hint").toString().isEmpty() ? "IPA " + cand.value("ipa").toString()
                                                                                                         : cand.value("hint").toString()),
        QMessageBox::Yes | QMessageBox::No | QMessageBox::Cancel);
    if (add == QMessageBox::Cancel) return;
    const QString backend = m_backend->currentData().toString();
    auto record = [this, attempt, term, cand, backend] {
        QString chunkText;
        for (const auto &c : m_qa->summary().value("chunks").toArray())
            if (c.toObject().value("chunk").toInt() == attempt.value("chunk").toInt()) chunkText = c.toObject().value("intended").toString();
        const QJsonObject rec{{"original_text", chunkText.isEmpty() ? m_text->toPlainText().left(1000) : chunkText}, {"term", term},
                              {"language", currentLanguage()}, {"voice", m_voice->currentData().toString()}, {"backend", backend},
                              {"pronunciation_settings", QJsonObject{{"speed", m_speed->value()}, {"style", m_style->text()}}},
                              {"verification", QJsonObject{{"status", attempt.value("recheck_status")}, {"recognized_after", attempt.value("recognized")}}},
                              {"correction", QJsonObject{{"written", term}, {"hint", cand.value("hint")}, {"ipa", cand.value("ipa")},
                                                         {"category", "name"}}},
                              {"succeeded", attempt.value("term_resolved").toBool()},
                              {"versions", m_qa->summary().value("versions").toObject()}};
        m_client->call("feedback", {{"op", "status"}}, this, [this, rec](const QString &st, const QJsonObject &s) {
            if (st != "success") return;
            if (!s.value("consent").toObject().value("store_feedback").toBool()) {
                if (QMessageBox::question(this, "Save corrections locally?",
                        "Save approved corrections on this computer so they can be inspected, exported, deleted and used to test future "
                        "dictionary changes? Nothing is uploaded and no AI model is trained.") != QMessageBox::Yes) return;
                m_client->call("feedback", {{"op", "set_consent"}, {"store_feedback", true}}, this, [](const QString &, const QJsonObject &) {});
            }
            m_client->call("feedback", {{"op", "record"}, {"record", rec}}, this, [this](const QString &st2, const QJsonObject &r) {
                setStatus(st2 == "success" ? "Correction saved for learning (local)." : "Could not save: " + r.value("message").toString(), st2 != "success");
            });
        });
    };
    if (add == QMessageBox::Yes) {
        const QJsonObject entry{{"written", term}, {"hint", cand.value("hint")}, {"ipa", cand.value("ipa")}, {"category", "name"},
                                {"source", cand.value("source").toString() == "llm" ? "llm-approved" : "feedback"}};
        DictionaryDialog::addEntry(m_client, this, entry, backend, [record](bool, const QJsonObject &) { record(); });
    } else {
        record();
    }
}

void MainWindow::openDictionary() {
    DictionaryDialog d(m_client, m_backend->count() ? m_backend->currentData().toString() : QString(), backendHasIpa(), this);
    d.exec();
}

// Optional: the Model Lab runs in its own worker process and window; closing it never affects speech generation.
void MainWindow::openModelLab() {
    if (!m_lab) {
        m_lab = new ModelLabDialog({}, this);
        m_lab->setAttribute(Qt::WA_DeleteOnClose);
    }
    m_lab->show();
    m_lab->raise();
    m_lab->activateWindow();
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
    if (peak >= 0) {
        m_wave->pushLevel(peak);
        if (auto *gl = m_shell->glStage()) gl->pushLevel(peak);
    }   // no data -> the meter stays still rather than faking motion
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
    m_textBadge->clear();
    const bool empty = t.isEmpty();
    if (m_inputEmpty->isHidden() == empty) {
        m_inputEmpty->setVisible(empty);
        if (empty) Fx::fadeIn(m_inputEmpty, 200);
    }
}

void MainWindow::openSettings() {
    QDialog d(this);
    d.setWindowTitle("Settings");
    d.setMinimumWidth(620);
    auto *outer = new QVBoxLayout(&d);
    auto *tabs = new QTabWidget;
    outer->addWidget(tabs);
    auto *general = new QWidget;
    auto *form = new QFormLayout(general);
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
    auto *fx = new QCheckBox("3D visual effects (OpenGL background, orb and waveform)");
    fx->setChecked(Config::settings().value("effects", true).toBool());
    auto *native = new QCheckBox("Use the standard Windows title bar (takes effect after restart)");
    native->setChecked(Config::settings().value("nativeFrame", false).toBool());
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
    form->addRow("", fx);
    form->addRow("", native);
    auto *note = new QLabel("The API key is held in memory and passed to the worker; it is never written to disk. "
                            "Set GEMINI_API_KEY in your user environment for a persistent key. Saving restarts the worker.");
    note->setWordWrap(true);
    note->setObjectName("muted");
    form->addRow(note);
    tabs->addTab(general, "General");
    auto *scroll = new QScrollArea;
    scroll->setWidgetResizable(true);
    auto *pron = new PronSettings(m_client, m_backend->count() ? m_backend->currentData().toString() : QString(), backendHasIpa());
    scroll->setWidget(pron);
    tabs->addTab(scroll, "Pronunciation");
    connect(pron, &PronSettings::changed, this, [this] {
        m_client->call("health", {}, this, [this](const QString &st, const QJsonObject &r) {
            if (st == "success") { m_health = r; applyHealth(r); }
        }, 90000);
    });
    auto *bb = new QDialogButtonBox(QDialogButtonBox::Save | QDialogButtonBox::Cancel);
    outer->addWidget(bb);
    connect(bb, &QDialogButtonBox::accepted, &d, &QDialog::accept);
    connect(bb, &QDialogButtonBox::rejected, &d, &QDialog::reject);
    d.resize(680, 620);
    if (d.exec() != QDialog::Accepted) return;
    Config::settings().setValue("pythonExe", py->text().trimmed());
    Config::settings().setValue("engineRoot", root->text().trimmed());
    Config::settings().setValue("reduceMotion", reduce->isChecked());
    Config::settings().setValue("nativeFrame", native->isChecked());
    Config::settings().setValue("effects", fx->isChecked());
    applyEffects();
    Config::settings().setValue("theme", theme->currentText());
    if (theme->currentText() != ThemeManager::instance().current()) {
        ThemeManager::instance().apply(theme->currentText());
        if (ThemeManager::instance().usingFallback()) setStatus(ThemeManager::instance().lastError(), true);
    }
    m_sessionApiKey = key->text().trimmed();
    if (m_state == State::Generating || m_state == State::Exporting || m_state == State::Rechecking || m_state == State::Repairing)
        cancelJob();
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
    ++m_analysisSeq;
    // remember where the window was (its normal, un-maximised rectangle)
    Config::settings().setValue("windowMaximized", isMaximized());
    if (!isMaximized() && !isFullScreen()) Config::settings().setValue("windowRect", geometry());
    else Config::settings().setValue("windowRect", normalGeometry());
    if (!m_jobId.isEmpty()) m_client->cancel(m_jobId);
    m_player->stop();
    m_player->setSource({});
    m_previewPlayer->stop();
    m_previewPlayer->setSource({});
    m_client->stop();
    e->accept();
}
