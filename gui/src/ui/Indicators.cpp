#include "Indicators.h"

#include "Icons.h"
#include "Motion.h"
#include "ThemeManager.h"

#include <QGraphicsOpacityEffect>
#include <QPainter>
#include <QPainterPath>
#include <QPropertyAnimation>
#include <QStyle>
#include <QTimer>
#include <QVariantAnimation>
#include <QtMath>

namespace Fx {

void fadeIn(QWidget *w, int ms) {
    const int d = Motion::ms(ms);
    if (d == 0 || !w->isVisible()) return;
    auto *effect = new QGraphicsOpacityEffect(w);
    effect->setOpacity(0.0);
    w->setGraphicsEffect(effect);   // replaces (and deletes) any effect from an earlier fade
    auto *a = new QPropertyAnimation(effect, "opacity", effect);
    a->setDuration(d);
    a->setStartValue(0.0);
    a->setEndValue(1.0);
    a->setEasingCurve(QEasingCurve::OutCubic);
    QObject::connect(a, &QPropertyAnimation::finished, w, [w, effect] {
        // Remove the effect after the animation's own signal has returned.
        QTimer::singleShot(0, w, [w, effect] {
            if (w->graphicsEffect() == effect) w->setGraphicsEffect(nullptr);
        });
    });
    a->start();
}

void setProp(QWidget *w, const char *name, const QVariant &value) {
    if (w->property(name) == value) return;
    w->setProperty(name, value);
    w->style()->unpolish(w);
    w->style()->polish(w);
    w->update();
}

}  // namespace Fx

// ---------------------------------------------------------------- IconWidget
IconWidget::IconWidget(const QString &iconName, const QString &colorToken, int size, QWidget *parent)
    : QWidget(parent), m_name(iconName), m_token(colorToken), m_size(size) {
    setFixedSize(size, size);
    setAttribute(Qt::WA_TransparentForMouseEvents);
}

void IconWidget::paintEvent(QPaintEvent *) {
    QPainter p(this);
    const QColor c = m_token.isEmpty() ? QColor() : ThemeManager::instance().color(m_token);
    Icons::paint(&p, Icons::path(m_name), QRectF(rect()), c);
}

// ---------------------------------------------------------------- StateIcon
StateIcon::StateIcon(int size, QWidget *parent) : QWidget(parent), m_size(size) {
    setFixedSize(size, size);
    setAttribute(Qt::WA_TransparentForMouseEvents);
    m_spin = new QVariantAnimation(this);
    m_spin->setStartValue(0.0);
    m_spin->setEndValue(360.0);
    m_spin->setDuration(1000);
    m_spin->setLoopCount(-1);
    connect(m_spin, &QVariantAnimation::valueChanged, this, [this](const QVariant &v) {
        m_angle = v.toReal();
        update();
    });
}

bool StateIcon::spinning() const { return m_spin->state() == QAbstractAnimation::Running; }

void StateIcon::setKind(Kind k) {
    if (k == m_kind) return;
    m_kind = k;
    sync();
    update();
}

void StateIcon::sync() {
    const bool run = m_kind == Kind::Busy && isVisible() && !Motion::reduced();
    if (run && !spinning()) m_spin->start();
    if (!run) m_spin->stop();
}

void StateIcon::showEvent(QShowEvent *) { sync(); }
void StateIcon::hideEvent(QHideEvent *) { m_spin->stop(); }   // never animate while hidden

void StateIcon::paintEvent(QPaintEvent *) {
    if (m_kind == Kind::None) return;
    static const char *names[] = {"", "progress", "success", "warning", "error"};
    static const char *tokens[] = {"", "accent", "success", "warning", "danger"};
    const int i = int(m_kind);
    QPainter p(this);
    if (m_kind == Kind::Busy && !Motion::reduced()) {
        p.translate(rect().center() + QPointF(0.5, 0.5));
        p.rotate(m_angle);
        p.translate(-rect().center() - QPointF(0.5, 0.5));
    }
    Icons::paint(&p, Icons::path(names[i]), QRectF(rect()), ThemeManager::instance().color(tokens[i]));
}

// ---------------------------------------------------------------- WaveformWidget
WaveformWidget::WaveformWidget(QWidget *parent) : QWidget(parent), m_target(kBars, 0.0), m_shown(kBars, 0.0) {
    setObjectName("waveform");
    setMinimumHeight(48);
    m_tick = new QTimer(this);
    m_tick->setInterval(33);
    connect(m_tick, &QTimer::timeout, this, &WaveformWidget::onTick);
}

bool WaveformWidget::tickerRunning() const { return m_tick->isActive(); }

void WaveformWidget::pushLevel(qreal level) {
    level = qBound(0.0, level, 1.0);
    m_target.removeFirst();
    m_target.append(level);
    m_shown.removeFirst();
    m_shown.append(m_shown.last() * 0.5);   // new bar rises from half the previous height rather than popping in
    if (isVisible() && !m_tick->isActive()) m_tick->start();
}

void WaveformWidget::setActive(bool playing) {
    m_active = playing;
    if (!playing) {   // let the bars decay to rest; the ticker stops itself
        std::fill(m_target.begin(), m_target.end(), 0.0);
        if (isVisible() && !m_tick->isActive()) m_tick->start();
    }
}

void WaveformWidget::clearLevels() {
    std::fill(m_target.begin(), m_target.end(), 0.0);
    std::fill(m_shown.begin(), m_shown.end(), 0.0);
    m_tick->stop();
    update();
}

void WaveformWidget::onTick() {
    const int d = Motion::reduced() ? 1 : 0;
    bool moving = false;
    for (int i = 0; i < kBars; ++i) {
        const qreal diff = m_target[i] - m_shown[i];
        m_shown[i] = d ? m_target[i] : m_shown[i] + diff * 0.4;
        if (qAbs(diff) > 0.004) moving = true; else m_shown[i] = m_target[i];
    }
    update();
    if (!moving && !m_active) m_tick->stop();
}

void WaveformWidget::hideEvent(QHideEvent *) { m_tick->stop(); }

void WaveformWidget::paintEvent(QPaintEvent *) {
    if (m_gl) return;
    QPainter p(this);
    p.setRenderHint(QPainter::Antialiasing);
    p.setPen(Qt::NoPen);
    const auto &t = ThemeManager::instance();
    const QColor live = t.color("wave"), idle = t.color("wave-idle");
    const qreal w = width(), h = height();
    const qreal slot = w / kBars, bw = qMax(2.0, slot * 0.55);
    for (int i = 0; i < kBars; ++i) {
        const qreal lvl = m_shown[i];
        const qreal bh = qMax(bw, lvl * (h - 6));
        QColor c = lvl > 0.02 ? live : idle;
        if (lvl > 0.02) c.setAlphaF(0.35 + 0.65 * i / (kBars - 1));   // older bars fade out to the left
        p.setBrush(c);
        p.drawRoundedRect(QRectF(i * slot + (slot - bw) / 2, (h - bh) / 2, bw, bh), bw / 2, bw / 2);
    }
}

// ---------------------------------------------------------------- SuccessBadge
SuccessBadge::SuccessBadge(QWidget *parent) : QWidget(parent) {
    setObjectName("successBadge");
    setFixedSize(24, 24);
    setAttribute(Qt::WA_TransparentForMouseEvents);
    hide();
    m_draw = new QVariantAnimation(this);
    m_draw->setStartValue(0.0);
    m_draw->setEndValue(1.0);
    m_draw->setEasingCurve(QEasingCurve::OutCubic);
    connect(m_draw, &QVariantAnimation::valueChanged, this, [this](const QVariant &v) {
        m_t = v.toReal();
        update();
    });
    m_hold = new QTimer(this);
    m_hold->setSingleShot(true);
    m_fade = new QVariantAnimation(this);
    m_fade->setStartValue(1.0);
    m_fade->setEndValue(0.0);
    connect(m_fade, &QVariantAnimation::valueChanged, this, [this](const QVariant &v) {
        m_opacity = v.toReal();
        update();
    });
    connect(m_fade, &QVariantAnimation::finished, this, [this] { hide(); });
    connect(m_hold, &QTimer::timeout, this, [this] {
        const int d = Motion::ms(300);
        if (d == 0) { hide(); return; }
        m_fade->setDuration(d);
        m_fade->start();
    });
    connect(m_draw, &QVariantAnimation::finished, this, [this] { m_hold->start(2200); });
}

void SuccessBadge::play() {
    m_draw->stop(); m_fade->stop(); m_hold->stop();
    m_opacity = 1.0;
    show();
    const int d = Motion::ms(520);
    if (d == 0) {
        m_t = 1.0;
        update();
        m_hold->start(2200);
        return;
    }
    m_t = 0;
    m_draw->setDuration(d);
    m_draw->start();
}

void SuccessBadge::hideEvent(QHideEvent *) {
    m_draw->stop(); m_fade->stop(); m_hold->stop();
}

void SuccessBadge::paintEvent(QPaintEvent *) {
    QPainter p(this);
    p.setRenderHint(QPainter::Antialiasing);
    p.setOpacity(m_opacity);
    QPen pen(ThemeManager::instance().color("success"), 1.9, Qt::SolidLine, Qt::RoundCap, Qt::RoundJoin);
    p.setPen(pen);
    const QRectF ring = QRectF(rect()).adjusted(2.5, 2.5, -2.5, -2.5);
    const qreal ringT = qMin(1.0, m_t / 0.65);
    p.drawArc(ring, 90 * 16, int(-360 * 16 * ringT));
    const qreal checkT = qBound(0.0, (m_t - 0.45) / 0.55, 1.0);
    if (checkT > 0) {
        const qreal s = width() / 24.0;
        const QPointF a(7.6 * s, 12.4 * s), b(10.7 * s, 15.5 * s), c(16.4 * s, 9.2 * s);
        QPainterPath path(a);
        if (checkT < 0.4) {
            path.lineTo(a + (b - a) * (checkT / 0.4));
        } else {
            path.lineTo(b);
            path.lineTo(b + (c - b) * ((checkT - 0.4) / 0.6));
        }
        p.setBrush(Qt::NoBrush);
        p.drawPath(path);
    }
}

// ---------------------------------------------------------------- SmoothProgressBar
SmoothProgressBar::SmoothProgressBar(QWidget *parent) : QProgressBar(parent) {
    setRange(0, 100);
    setValue(0);
    setTextVisible(false);
    m_anim = new QPropertyAnimation(this, "value", this);
    m_anim->setEasingCurve(QEasingCurve::OutCubic);
}

void SmoothProgressBar::setTarget(int v) {
    v = qBound(minimum(), v, maximum());
    const int d = Motion::ms(180);
    m_anim->stop();
    if (d == 0 || !isVisible() || v <= value()) {
        setValue(v);
        return;
    }
    m_anim->setDuration(d);
    m_anim->setStartValue(value());
    m_anim->setEndValue(v);
    m_anim->start();
}

void SmoothProgressBar::resetNow() {
    m_anim->stop();
    setValue(0);
}

void SmoothProgressBar::hideEvent(QHideEvent *) { m_anim->stop(); }

// ---------------------------------------------------------------- EmptyState
EmptyState::EmptyState(const QString &illustrationName, const QString &title, const QString &hint, bool compact, QWidget *parent)
    : QWidget(parent), m_illustration(illustrationName), m_title(title), m_hint(hint), m_compact(compact) {
    setAttribute(Qt::WA_TransparentForMouseEvents);
    setAccessibleName(title);
    setAccessibleDescription(hint);
}

void EmptyState::paintEvent(QPaintEvent *) {
    QPainter p(this);
    p.setRenderHint(QPainter::Antialiasing);
    const auto &t = ThemeManager::instance();
    QColor muted = t.color("text-muted");
    QColor soft = muted;
    soft.setAlphaF(0.75);
    QFont tf = font();
    tf.setBold(true);
    QFont hf = font();
    hf.setPointSizeF(hf.pointSizeF() * 0.9);
    const QFontMetrics tm(tf), hm(hf);
    const int icon = m_compact ? 36 : 56;
    const QString path = ":/illustrations/" + m_illustration + ".svg";
    if (m_compact) {
        const int tw = qMax(tm.horizontalAdvance(m_title), hm.horizontalAdvance(m_hint));
        const int gap = 12, total = icon + gap + tw;
        const int x0 = (width() - total) / 2, cy = height() / 2;
        Icons::paint(&p, path, QRectF(x0, cy - icon / 2.0, icon, icon), soft);
        const int tx = x0 + icon + gap;
        p.setFont(tf); p.setPen(muted);
        p.drawText(tx, cy - 2, m_title);
        p.setFont(hf); p.setPen(soft);
        p.drawText(tx, cy + hm.ascent() + 2, m_hint);
    } else {
        const int total = icon + 10 + tm.height() + 2 + hm.height();
        int y = (height() - total) / 2;
        Icons::paint(&p, path, QRectF((width() - icon) / 2.0, y, icon, icon), soft);
        y += icon + 10;
        p.setFont(tf); p.setPen(muted);
        p.drawText(QRect(0, y, width(), tm.height()), Qt::AlignHCenter | Qt::AlignVCenter, m_title);
        y += tm.height() + 2;
        p.setFont(hf); p.setPen(soft);
        p.drawText(QRect(0, y, width(), hm.height()), Qt::AlignHCenter | Qt::AlignVCenter, m_hint);
    }
}
