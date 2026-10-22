#include "VoiceLibraryDialog.h"

#include <QAudioOutput>
#include <QButtonGroup>
#include <QColor>
#include <QStyle>
#include <functional>
#include <QFile>
#include <QFrame>
#include <QGridLayout>
#include <QHBoxLayout>
#include <QLabel>
#include <QLineEdit>
#include <QMediaPlayer>
#include <QMouseEvent>
#include <QPushButton>
#include <QScrollArea>
#include <QUrl>
#include <QVBoxLayout>

namespace {
constexpr int kColumns = 2;

bool isClassic(const QString &n) { return n == "original" || n == "reference" || n == "deep" || n == "boy"; }

// Card that selects itself on click and "uses" the voice on double-click.
class CardFrame : public QFrame {
public:
    std::function<void()> onClick, onDouble;
    using QFrame::QFrame;

protected:
    void mousePressEvent(QMouseEvent *e) override {
        QFrame::mousePressEvent(e);
        if (e->button() == Qt::LeftButton && onClick) onClick();
    }
    void mouseDoubleClickEvent(QMouseEvent *e) override {
        QFrame::mouseDoubleClickEvent(e);
        if (onDouble) onDouble();
    }
};

QString avatarStyle(const QString &name, bool female) {
    const int hue = int(qHash(name) % 360);
    const QColor a = QColor::fromHsv(hue, 150, 255), b = QColor::fromHsv((hue + 50) % 360, 220, 170);
    Q_UNUSED(female)
    return QString("background:qlineargradient(x1:0,y1:0,x2:1,y2:1,stop:0 %1,stop:1 %2);color:white;"
                   "border-radius:22px;font-weight:bold;font-size:15px;")
        .arg(a.name(), b.name());
}
}  // namespace

VoiceLibraryDialog::VoiceLibraryDialog(const QList<VoiceEntry> &voices, const QString &current, QWidget *parent)
    : QDialog(parent), m_voices(voices), m_selected(current) {
    setObjectName("voiceLibrary");
    setWindowTitle(tr("Voice library"));
    resize(860, 680);
    setStyleSheet(
        "QFrame#voiceCard{background:rgba(255,255,255,0.05);border:1px solid rgba(255,255,255,0.14);border-radius:12px;}"
        "QFrame#voiceCard:hover{background:rgba(255,255,255,0.09);border-color:rgba(255,255,255,0.3);}"
        "QFrame#voiceCard[selected=\"true\"]{border:2px solid #3d7eff;background:rgba(61,126,255,0.16);}"
        "QPushButton[chip=\"true\"]{border-radius:14px;padding:5px 16px;border:1px solid rgba(255,255,255,0.25);}"
        "QPushButton[chip=\"true\"]:checked{background:#3d7eff;color:white;border-color:#3d7eff;}");

    m_player = new QMediaPlayer(this);
    m_out = new QAudioOutput(this);
    m_player->setAudioOutput(m_out);
    connect(m_player, &QMediaPlayer::playbackStateChanged, this, [this](QMediaPlayer::PlaybackState st) {
        if (st == QMediaPlayer::StoppedState) {
            m_playing.clear();
            for (auto &c : m_cards) c.play->setText(tr("▶  Play"));
        }
    });

    auto *root = new QVBoxLayout(this);
    root->setSpacing(10);

    auto *top = new QHBoxLayout;
    m_search = new QLineEdit;
    m_search->setPlaceholderText(tr("Search voices…"));
    m_search->setClearButtonEnabled(true);
    top->addWidget(m_search, 1);
    auto *grp = new QButtonGroup(this);
    const struct { const char *label; const char *key; } filters[] = {
        {"All", ""}, {"Girls", "female"}, {"Boys", "male"}};
    for (const auto &f : filters) {
        auto *b = new QPushButton(tr(f.label));
        b->setCheckable(true);
        b->setChecked(!*f.key);
        b->setProperty("chip", true);
        b->setCursor(Qt::PointingHandCursor);
        grp->addButton(b);
        top->addWidget(b);
        connect(b, &QPushButton::clicked, this, [this, key = QString(f.key)] {
            m_filter = key;
            relayout();
        });
    }
    root->addLayout(top);

    m_count = new QLabel;
    m_count->setObjectName("muted");
    root->addWidget(m_count);

    auto *scroll = new QScrollArea;
    scroll->setFrameShape(QFrame::NoFrame);
    scroll->setWidgetResizable(true);
    auto *host = new QWidget;
    m_grid = new QGridLayout(host);
    m_grid->setSpacing(10);
    m_grid->setContentsMargins(2, 2, 2, 2);
    for (int c = 0; c < kColumns; ++c) m_grid->setColumnStretch(c, 1);
    for (QLabel *&hd : m_headers) {
        hd = new QLabel(host);
        hd->setStyleSheet("font-size:12px;font-weight:700;letter-spacing:1px;padding:10px 2px 2px 2px;");
        hd->hide();
    }
    scroll->setWidget(host);
    root->addWidget(scroll, 1);

    m_empty = new QLabel(tr("No voices match your search."));
    m_empty->setAlignment(Qt::AlignCenter);
    m_empty->hide();
    root->addWidget(m_empty);

    auto *bottom = new QHBoxLayout;
    bottom->addStretch(1);
    auto *cancel = new QPushButton(tr("Cancel"));
    m_use = new QPushButton(tr("Use this voice"));
    m_use->setObjectName("primary");
    m_use->setDefault(true);
    bottom->addWidget(cancel);
    bottom->addWidget(m_use);
    root->addLayout(bottom);
    connect(cancel, &QPushButton::clicked, this, &QDialog::reject);
    connect(m_use, &QPushButton::clicked, this, &QDialog::accept);
    connect(m_search, &QLineEdit::textChanged, this, [this] { relayout(); });
    connect(this, &QDialog::finished, this, [this] { stopPlayback(); });

    for (const VoiceEntry &v : m_voices) {
        Card c;
        c.v = v;
        auto *f = new CardFrame;
        f->setObjectName("voiceCard");
        f->setCursor(Qt::PointingHandCursor);
        f->onClick = [this, n = v.name] { select(n); };
        f->onDouble = [this, n = v.name] { select(n); accept(); };
        auto *h = new QHBoxLayout(f);
        h->setContentsMargins(14, 12, 14, 12);
        h->setSpacing(12);
        auto *av = new QLabel(v.display.left(1).toUpper());
        av->setFixedSize(44, 44);
        av->setAlignment(Qt::AlignCenter);
        av->setStyleSheet(avatarStyle(v.name, v.gender == "female"));
        h->addWidget(av, 0, Qt::AlignVCenter);
        auto *col = new QVBoxLayout;
        col->setSpacing(3);
        auto *name = new QLabel(v.display);
        name->setStyleSheet("font-size:15px;font-weight:600;");
        auto *tag = new QLabel(v.tagline);
        tag->setWordWrap(true);
        tag->setObjectName("muted");
        col->addWidget(name);
        col->addWidget(tag);
        h->addLayout(col, 1);
        c.play = new QPushButton(tr("▶  Play"));
        c.play->setMinimumWidth(92);
        c.play->setCursor(Qt::PointingHandCursor);
        c.play->setEnabled(!v.sample.isEmpty() && QFile::exists(v.sample));
        c.play->setToolTip(c.play->isEnabled() ? tr("Hear this voice (stored sample - no API credit used)")
                                               : tr("No stored sample for this voice"));
        connect(c.play, &QPushButton::clicked, this, [this, n = v.name] { togglePlay(n); });
        h->addWidget(c.play, 0, Qt::AlignVCenter);
        c.w = f;
        m_cards << c;
    }
    relayout();
    select(m_selected);
}

void VoiceLibraryDialog::relayout() {
    while (m_grid->count()) m_grid->takeAt(0);
    const QString q = m_search->text().trimmed();
    struct Sec { const char *title; int kind; };            // kind: 0 classic, 1 girls, 2 boys
    int shown = 0, row = 0;
    for (const Sec &sec : {Sec{"CLASSIC VOICES", 0}, Sec{"GIRLS", 1}, Sec{"BOYS", 2}}) {
        QList<Card *> in;
        for (auto &c : m_cards) {
            const int kind = isClassic(c.v.name) ? 0 : c.v.gender == "female" ? 1 : 2;
            const bool ok = kind == sec.kind && (m_filter.isEmpty() || (kind != 0 && c.v.gender == m_filter)) &&
                            (q.isEmpty() || c.v.display.contains(q, Qt::CaseInsensitive) ||
                             c.v.tagline.contains(q, Qt::CaseInsensitive) || c.v.name.contains(q, Qt::CaseInsensitive));
            c.w->setVisible(ok);
            if (ok) in << &c;
        }
        QLabel *hd = m_headers[sec.kind];
        hd->setVisible(!in.isEmpty());
        if (in.isEmpty()) continue;
        hd->setText(QString("%1  ·  %2").arg(tr(sec.title)).arg(in.size()));
        m_grid->addWidget(hd, row++, 0, 1, kColumns);
        for (int i = 0; i < in.size(); ++i) m_grid->addWidget(in[i]->w, row + i / kColumns, i % kColumns);
        row += (in.size() + kColumns - 1) / kColumns;
        shown += in.size();
    }
    m_grid->setRowStretch(row, 1);
    m_count->setText(tr("%1 of %2 voices").arg(shown).arg(m_cards.size()));
    m_empty->setVisible(shown == 0);
}

void VoiceLibraryDialog::select(const QString &name) {
    m_selected = name;
    for (auto &c : m_cards) {
        c.w->setProperty("selected", c.v.name == name);
        c.w->style()->unpolish(c.w);
        c.w->style()->polish(c.w);
    }
    m_use->setEnabled(!name.isEmpty());
}

void VoiceLibraryDialog::togglePlay(const QString &name) {
    const bool same = m_playing == name;
    stopPlayback();
    if (same) return;
    for (auto &c : m_cards) {
        if (c.v.name != name) continue;
        m_player->setSource(QUrl::fromLocalFile(c.v.sample));
        m_player->play();
        m_playing = name;
        c.play->setText(tr("■  Stop"));
        select(name);
    }
}

void VoiceLibraryDialog::stopPlayback() {
    if (m_player->playbackState() != QMediaPlayer::StoppedState) m_player->stop();
    m_playing.clear();
    for (auto &c : m_cards) c.play->setText(tr("▶  Play"));
}
