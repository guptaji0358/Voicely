#include "SettingsDialog.h"
#include "Controls.h"

#include "../Config.h"
#include "Buttons.h"
#include "Icons.h"
#include "PronSettings.h"
#include "ThemeManager.h"
#include "UiSounds.h"

#include <QCheckBox>
#include <QComboBox>
#include <QFileDialog>
#include <QFrame>
#include <QGridLayout>
#include <QHBoxLayout>
#include <QLabel>
#include <QLineEdit>
#include <QPalette>
#include <QListWidget>
#include <QScrollArea>
#include <QSpinBox>
#include <QStackedWidget>
#include <QVBoxLayout>

namespace {
QLabel *label(const QString &text, const char *objectName) {
    auto *l = new QLabel(text);
    l->setObjectName(objectName);
    return l;
}

// One titled card: a heading, an optional one-line hint, then rows added by the caller.
struct Section {
    QFrame *frame;
    QVBoxLayout *body;
};

Section section(const QString &title, const QString &hint = {}) {
    auto *f = new QFrame;
    f->setObjectName("settingsCard");
    auto *v = new QVBoxLayout(f);
    v->setContentsMargins(20, 16, 20, 18);
    v->setSpacing(12);
    auto *t = label(title.toUpper(), "settingsCardTitle");
    QFont tf = t->font();
    tf.setLetterSpacing(QFont::AbsoluteSpacing, 1.0);
    t->setFont(tf);
    v->addWidget(t);
    if (!hint.isEmpty()) {
        auto *h = label(hint, "muted");
        h->setWordWrap(true);
        v->addWidget(h);
    }
    auto *rule = new QFrame;                                // hairline between the heading and the controls
    rule->setObjectName("settingsDivider");
    rule->setFixedHeight(1);
    v->addWidget(rule);
    return {f, v};
}

// "Label" above its control, so long fields get the full card width.
QWidget *field(const QString &caption, QWidget *control) {
    auto *w = new QWidget;
    auto *v = new QVBoxLayout(w);
    v->setContentsMargins(0, 0, 0, 0);
    v->setSpacing(4);
    v->addWidget(label(caption, "fieldLabel"));
    v->addWidget(control);
    return w;
}

QWidget *fieldRow(const QString &caption, QLineEdit *edit, QPushButton *browse) {
    auto *row = new QWidget;
    auto *h = new QHBoxLayout(row);
    h->setContentsMargins(0, 0, 0, 0);
    h->setSpacing(8);
    h->addWidget(edit, 1);
    h->addWidget(browse);
    return field(caption, row);
}
}  // namespace

SettingsDialog::SettingsDialog(ProtocolClient *client, const QString &backend, bool ipaSupported, const QString &apiKey,
                               QWidget *parent)
    : StyledDialog(parent, "settings") {
    setObjectName("settingsDialog");
    setWindowTitle(tr("Settings"));
    setMinimumSize(800, 580);
    resize(930, 720);

    auto *outer = new QVBoxLayout(this);
    outer->setContentsMargins(14, 4, 14, 10);
    outer->setSpacing(14);

    auto *head = new QVBoxLayout;
    head->setSpacing(2);
    auto *sub = label(tr("Engine location, appearance and pronunciation checks."), "muted");
    head->addWidget(sub);
    outer->addLayout(head);

    auto *mid = new QHBoxLayout;
    mid->setSpacing(20);
    m_nav = new QListWidget;
    m_nav->setObjectName("settingsNav");
    m_nav->setFixedWidth(184);
    m_nav->setIconSize(QSize(Icons::kSize, Icons::kSize));
    m_nav->setFocusPolicy(Qt::TabFocus);
    m_nav->setHorizontalScrollBarPolicy(Qt::ScrollBarAlwaysOff);
    m_nav->setVerticalScrollBarPolicy(Qt::ScrollBarAlwaysOff);
    m_nav->setSpacing(3);
    m_nav->viewport()->setCursor(Cursors::pointer());
    new QListWidgetItem(Icons::icon("settings"), tr("General"), m_nav);
    new QListWidgetItem(Icons::icon("pron-check"), tr("Pronunciation"), m_nav);
    mid->addWidget(m_nav);

    m_pages = new QStackedWidget;
    auto page = [](QWidget *inner) {
        auto *sc = new QScrollArea;
        sc->setObjectName("settingsPage");
        sc->setWidgetResizable(true);
        sc->setFrameShape(QFrame::NoFrame);
        sc->setHorizontalScrollBarPolicy(Qt::ScrollBarAlwaysOff);
        sc->setWidget(inner);
        return sc;
    };
    m_pages->addWidget(page(buildGeneral(apiKey)));
    m_pron = new PronSettings(client, backend, ipaSupported);
    m_pages->addWidget(page(m_pron));
    mid->addWidget(m_pages, 1);
    outer->addLayout(mid, 1);

    connect(m_nav, &QListWidget::currentRowChanged, m_pages, &QStackedWidget::setCurrentIndex);
    m_nav->setCurrentRow(0);
    connect(m_pron, &PronSettings::changed, this, &SettingsDialog::pronunciationChanged);

    auto *line = new QFrame;
    line->setObjectName("settingsDivider");
    line->setFixedHeight(1);
    outer->addWidget(line);
    auto *bottom = new QHBoxLayout;
    bottom->addStretch(1);
    auto *cancel = new AnimatedButton(tr("Cancel"));
    auto *save = new AnimatedButton(tr("Save"));
    save->setObjectName("primary");
    save->setDefault(true);
    bottom->addWidget(cancel);
    bottom->addWidget(save);
    outer->addLayout(bottom);
    connect(cancel, &QPushButton::clicked, this, &QDialog::reject);
    connect(save, &QPushButton::clicked, this, &QDialog::accept);
}

QWidget *SettingsDialog::buildGeneral(const QString &apiKey) {
    auto *host = new QWidget;
    host->setObjectName("settingsPageBody");
    auto *col = new QVBoxLayout(host);
    col->setContentsMargins(0, 0, 8, 8);
    col->setSpacing(16);

    auto browseBtn = [this](QLineEdit *e, bool dir) {
        auto *b = new AnimatedButton(tr("Browse…"));
        connect(b, &QPushButton::clicked, this, [this, e, dir] {
            const QString p = dir ? QFileDialog::getExistingDirectory(this, tr("Engine folder (contains speech_engine/)"))
                                  : QFileDialog::getOpenFileName(this, tr("Python interpreter"), {},
                                                                 tr("python.exe (python*.exe);;All files (*)"));
            if (!p.isEmpty()) e->setText(p);
        });
        return b;
    };

    // --- Engine
    auto engine = section(tr("Engine"), tr("Where Python and the speech engine are found. Leave empty to detect automatically."));
    m_py = new QLineEdit(Config::settings().value("pythonExe").toString());
    m_py->setPlaceholderText("auto: " + Config::findPython());
    m_root = new QLineEdit(Config::settings().value("engineRoot").toString());
    m_root->setPlaceholderText("auto: " + Config::findEngineRoot());
    engine.body->addWidget(fieldRow(tr("Python interpreter"), m_py, browseBtn(m_py, false)));
    engine.body->addWidget(fieldRow(tr("Engine folder"), m_root, browseBtn(m_root, true)));
    col->addWidget(engine.frame);

    // --- Google key
    auto google = section(tr("Google (Gemini) API key"),
                          tr("Only needed for the Google voices. Stored encrypted with Windows, readable only by your account."));
    m_key = new QLineEdit(apiKey);
    m_key->setEchoMode(QLineEdit::Password);
    m_key->setPlaceholderText(tr("Paste your key, or leave empty to use GEMINI_API_KEY"));
    google.body->addWidget(field(tr("API key (remembered)"), m_key));
    auto *how = new QLabel(
        tr("<a href='https://aistudio.google.com/apikey'>Get a key at aistudio.google.com/apikey</a> &middot; "
           "<a href='https://ai.google.dev/gemini-api/docs/pricing'>pricing and limits</a>. "
           "Voice previews use stored samples and never spend API credit. Saving restarts the worker."));
    how->setObjectName("muted");
    how->setWordWrap(true);
    how->setTextFormat(Qt::RichText);
    how->setOpenExternalLinks(true);
    how->setTextInteractionFlags(Qt::TextBrowserInteraction);
    QPalette pal = how->palette();                          // links use the theme accent, not Qt's default dark blue
    pal.setColor(QPalette::Link, ThemeManager::instance().color("accent-top", QColor("#4d8dff")));
    pal.setColor(QPalette::LinkVisited, pal.color(QPalette::Link));
    how->setPalette(pal);
    google.body->addWidget(how);
    col->addWidget(google.frame);

    // --- Appearance
    auto look = section(tr("Appearance"));
    auto check = [&](const QString &text, const QString &hint, bool on) {
        auto *row = new QFrame;                             // one toggle: box + title, hint aligned under the title
        row->setObjectName("settingsRow");
        auto *rv = new QVBoxLayout(row);
        rv->setContentsMargins(12, 7, 12, 8);
        rv->setSpacing(0);
        auto *c = new FancyCheckBox(text);
        c->setChecked(on);
        rv->addWidget(c);
        auto *h = label(hint, "muted");
        h->setProperty("small", true);
        h->setContentsMargins(FancyCheckBox::kIndent, 0, 0, 0);   // lines up with the text, past the indicator
        rv->addWidget(h);
        look.body->addWidget(row);
        return c;
    };
    m_reduce = check(tr("Reduce motion"), tr("Also follows the Windows animation setting."),
                     Config::settings().value("reduceMotion", false).toBool());
    m_fx = check(tr("3D visual effects"), tr("OpenGL background, orb and waveform."),
                 Config::settings().value("effects", true).toBool());
    m_snd = check(tr("UI sound effects"), tr("Clicks, drop and success sounds."), UiSounds::instance().enabled());
    m_native = check(tr("Standard Windows title bar"), tr("Takes effect after restart."),
                     Config::settings().value("nativeFrame", false).toBool());
    col->addWidget(look.frame);

    // --- Storage
    auto store = section(tr("Storage"));
    m_keep = new QSpinBox;
    m_keep->setRange(1, 60);
    m_keep->setSuffix(tr(" days"));
    m_keep->setValue(Config::settings().value("tempKeepDays", 3).toInt());
    m_keep->setMaximumWidth(160);
    m_keep->setToolTip(tr("Audio dragged out of the title bar is a temporary file; files older than this are deleted automatically."));
    store.body->addWidget(field(tr("Delete temporary audio after"), m_keep));
    col->addWidget(store.frame);

    col->addStretch(1);
    return host;
}

QString SettingsDialog::pythonExe() const { return m_py->text().trimmed(); }
QString SettingsDialog::engineRoot() const { return m_root->text().trimmed(); }
QString SettingsDialog::apiKey() const { return m_key->text().trimmed(); }
bool SettingsDialog::reduceMotion() const { return m_reduce->isChecked(); }
bool SettingsDialog::effects() const { return m_fx->isChecked(); }
bool SettingsDialog::sounds() const { return m_snd->isChecked(); }
int SettingsDialog::tempKeepDays() const { return m_keep->value(); }
bool SettingsDialog::nativeFrame() const { return m_native->isChecked(); }
