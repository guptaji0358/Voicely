#include "VoiceCarousel.h"
#include "Controls.h"

#include "Icons.h"
#include "ThemeManager.h"

#include <QAbstractItemModel>
#include <QComboBox>
#include <QEnterEvent>
#include <QFontMetrics>
#include <QHBoxLayout>
#include <QLinearGradient>
#include <QPainter>
#include <QRadialGradient>
#include <QScrollArea>
#include <QScrollBar>
#include <QToolButton>
#include <QTransform>
#include <QWheelEvent>

// ------------------------------------------------------------------------------------------------ VoiceChip
VoiceChip::VoiceChip(const QString &name, QWidget *parent) : QAbstractButton(parent) {
    setText(name);
    setCheckable(true);
    setAutoExclusive(false);
    setCursor(Cursors::pointer());
    setFocusPolicy(Qt::TabFocus);
    setToolTip(name);
    setAccessibleName(name);
}

void VoiceChip::enterEvent(QEnterEvent *e) {
    QAbstractButton::enterEvent(e);
    m_hover = true;
    update();
}

void VoiceChip::leaveEvent(QEvent *e) {
    QAbstractButton::leaveEvent(e);
    m_hover = false;
    update();
}

void VoiceChip::paintEvent(QPaintEvent *) {
    QPainter p(this);
    p.setRenderHint(QPainter::Antialiasing);
    const auto &t = ThemeManager::instance();
    const QColor accent = t.color("accent", QColor("#3d7eff"));
    const QRectF r = QRectF(rect()).adjusted(1.5, 1.5, -1.5, -1.5);
    const bool on = isChecked();
    const qreal radius = 12;

    QLinearGradient g(r.topLeft(), r.bottomLeft());
    QColor top = on ? accent.darker(160) : QColor(255, 255, 255, m_hover ? 30 : 16);
    QColor bot = on ? accent.darker(260) : QColor(255, 255, 255, m_hover ? 12 : 4);
    if (on) { top.setAlpha(190); bot.setAlpha(150); }
    g.setColorAt(0, top);
    g.setColorAt(1, bot);
    p.setPen(Qt::NoPen);
    p.setBrush(g);
    p.drawRoundedRect(r, radius, radius);

    if (on) {                                                  // neon rim + soft glow under the avatar
        QRadialGradient rg(r.left() + 26, r.top() + 24, 70);
        QColor c0 = accent.lighter(130);
        c0.setAlpha(70);
        rg.setColorAt(0, c0);
        rg.setColorAt(1, QColor(accent.red(), accent.green(), accent.blue(), 0));
        p.setBrush(rg);
        p.drawRoundedRect(r, radius, radius);
        p.setBrush(Qt::NoBrush);
        p.setPen(QPen(accent.lighter(125), 1.4));
    } else {
        p.setBrush(Qt::NoBrush);
        p.setPen(QPen(QColor(255, 255, 255, m_hover ? 60 : 34), 1));
    }
    p.drawRoundedRect(r, radius, radius);

    // avatar: gradient disc with the initial, hue derived from the name
    const int hue = int(qHash(text()) % 360);
    const QRectF av(r.left() + 10, r.top() + 9, 28, 28);
    QLinearGradient ag(av.topLeft(), av.bottomRight());
    ag.setColorAt(0, QColor::fromHsv(hue, 150, 255));
    ag.setColorAt(1, QColor::fromHsv((hue + 50) % 360, 220, 170));
    p.setPen(Qt::NoPen);
    p.setBrush(ag);
    p.drawEllipse(av);
    p.setPen(QColor(255, 255, 255, 235));
    QFont f = font();
    f.setBold(true);
    f.setPointSizeF(10.5);
    p.setFont(f);
    p.drawText(av, Qt::AlignCenter, text().left(1).toUpper());

    // name
    QFont nf = font();
    nf.setPointSizeF(9);
    nf.setBold(on);
    p.setFont(nf);
    p.setPen(on ? QColor(255, 255, 255) : t.color("text", QColor("#dde1e9")));
    const QFontMetrics fm(nf);
    p.drawText(QRectF(r.left() + 8, r.top() + 38, r.width() - 16, 16), Qt::AlignLeft | Qt::AlignVCenter,
               fm.elidedText(text(), Qt::ElideRight, int(r.width() - 16)));
}

// ------------------------------------------------------------------------------------------ VoiceCarousel
VoiceCarousel::VoiceCarousel(QComboBox *source, QWidget *parent) : QWidget(parent), m_src(source) {
    setObjectName("voiceCarousel");
    auto *h = new QHBoxLayout(this);
    h->setContentsMargins(0, 0, 0, 0);
    h->setSpacing(2);
    auto arrow = [this](const char *icon, const QString &tip) {
        auto *b = new QToolButton;
        b->setIcon(Icons::icon(icon));
        b->setIconSize(QSize(14, 14));
        b->setAutoRepeat(true);
        b->setAutoRepeatInterval(60);
        b->setToolTip(tip);
        b->setFixedSize(22, 60);
        b->setFocusPolicy(Qt::NoFocus);
        return b;
    };
    m_left = arrow("chevron-right", tr("Scroll voices left"));
    m_right = arrow("chevron-right", tr("Scroll voices right"));
    // the chevron asset points right; mirror it for the left button
    m_left->setIcon(QIcon(Icons::icon("chevron-right").pixmap(14, 14).transformed(QTransform().scale(-1, 1))));
    m_scroll = new QScrollArea;
    m_scroll->setObjectName("voiceStrip");
    m_scroll->setFrameShape(QFrame::NoFrame);
    m_scroll->setWidgetResizable(true);
    m_scroll->setHorizontalScrollBarPolicy(Qt::ScrollBarAlwaysOff);
    m_scroll->setVerticalScrollBarPolicy(Qt::ScrollBarAlwaysOff);
    m_scroll->setFixedHeight(64);
    m_inner = new QWidget;
    m_inner->setObjectName("voiceStripInner");
    m_row = new QHBoxLayout(m_inner);
    m_row->setContentsMargins(2, 2, 2, 2);
    m_row->setSpacing(8);
    m_scroll->setWidget(m_inner);
    m_scroll->viewport()->installEventFilter(this);
    h->addWidget(m_left);
    h->addWidget(m_scroll, 1);
    h->addWidget(m_right);
    connect(m_left, &QToolButton::clicked, this, [this] { scrollBy(-116); });
    connect(m_right, &QToolButton::clicked, this, [this] { scrollBy(116); });

    auto *model = m_src->model();
    connect(model, &QAbstractItemModel::rowsInserted, this, &VoiceCarousel::rebuild);
    connect(model, &QAbstractItemModel::rowsRemoved, this, &VoiceCarousel::rebuild);
    connect(model, &QAbstractItemModel::modelReset, this, &VoiceCarousel::rebuild);
    connect(model, &QAbstractItemModel::dataChanged, this, &VoiceCarousel::rebuild);
    connect(m_src, &QComboBox::currentIndexChanged, this, &VoiceCarousel::sync);
    rebuild();
}

void VoiceCarousel::rebuild() {
    if (m_rebuilding) return;
    m_rebuilding = true;
    qDeleteAll(m_chips);
    m_chips.clear();
    while (m_row->count()) delete m_row->takeAt(0);
    for (int i = 0; i < m_src->count(); ++i) {
        const auto flags = m_src->model()->flags(m_src->model()->index(i, 0));
        if (!(flags & Qt::ItemIsSelectable) || !(flags & Qt::ItemIsEnabled)) continue;   // group headers / separators
        auto *c = new VoiceChip(m_src->itemText(i));
        connect(c, &QAbstractButton::clicked, this, [this, i] { m_src->setCurrentIndex(i); });
        c->setProperty("row", i);
        m_row->addWidget(c);
        m_chips << c;
    }
    m_row->addStretch(1);
    m_rebuilding = false;
    sync();
}

void VoiceCarousel::sync() {
    const int cur = m_src->currentIndex();
    for (auto *c : m_chips) {
        const bool on = c->property("row").toInt() == cur;
        c->setChecked(on);
        if (on) m_scroll->ensureWidgetVisible(c, 40, 0);
    }
}

void VoiceCarousel::scrollBy(int dx) {
    auto *sb = m_scroll->horizontalScrollBar();
    sb->setValue(sb->value() + dx);
}

bool VoiceCarousel::eventFilter(QObject *o, QEvent *e) {
    if (o == m_scroll->viewport() && e->type() == QEvent::Wheel) {      // vertical wheel scrolls the strip sideways
        auto *w = static_cast<QWheelEvent *>(e);
        const int d = w->angleDelta().y() != 0 ? w->angleDelta().y() : w->angleDelta().x();
        scrollBy(-d / 2);
        return true;
    }
    return QWidget::eventFilter(o, e);
}
