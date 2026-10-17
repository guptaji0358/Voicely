#include "Card.h"

#include "Indicators.h"
#include "Motion.h"
#include "ThemeManager.h"

#include <QEnterEvent>
#include <QLinearGradient>
#include <QPainter>
#include <QTimer>
#include <QVariantAnimation>

Card::Card(QWidget *parent) : QFrame(parent) {
    setObjectName("floatCard");
    setContentsMargins(kInset, kInset, kInset, kInset);
    m_anim = new QVariantAnimation(this);
    connect(m_anim, &QVariantAnimation::valueChanged, this, [this](const QVariant &v) { setDepth(v.toReal()); });
}

void Card::setDepth(qreal d) {
    m_depth = d;
    update();
}

void Card::animateTo(qreal v) {
    m_anim->stop();
    const int ms = Motion::ms(160);
    if (ms == 0 || !isVisible()) {
        setDepth(v);
        return;
    }
    m_anim->setDuration(ms);
    m_anim->setEasingCurve(QEasingCurve::OutCubic);
    m_anim->setStartValue(m_depth);
    m_anim->setEndValue(v);
    m_anim->start();
}

void Card::enterEvent(QEnterEvent *e) {
    QFrame::enterEvent(e);
    if (m_lift) animateTo(1.0);
}

void Card::leaveEvent(QEvent *e) {
    QFrame::leaveEvent(e);
    animateTo(0.0);
}

void Card::hideEvent(QHideEvent *e) {
    QFrame::hideEvent(e);
    m_anim->stop();
}

void Card::playEntrance(int delayMs) {
    if (Motion::reduced()) return;
    QTimer::singleShot(delayMs, this, [this] { Fx::fadeIn(this, 260); });
}

void Card::paintEvent(QPaintEvent *) {
    QPainter p(this);
    p.setRenderHint(QPainter::Antialiasing);
    const auto &t = ThemeManager::instance();
    const qreal radius = t.pixels("radius-lg", 10);
    const QRectF r = QRectF(rect()).adjusted(kInset, kInset, -kInset, -kInset);

    // Soft shadow: stacked translucent rounded rects, offset downward; deeper when lifted.
    QColor sh = t.color("shadow", QColor(0, 0, 0, 110));
    const qreal dy = 2.5 + 3.0 * m_depth;
    constexpr int rings = 9;
    p.setPen(Qt::NoPen);
    for (int i = rings; i >= 1; --i) {
        const qreal spread = i * (0.9 + 0.35 * m_depth);
        QColor c = sh;
        const qreal f = 1.0 - qreal(i) / (rings + 1);
        c.setAlphaF(qMin(1.0, sh.alphaF() * (1.0 + 0.5 * m_depth) * f * f / 5.0));
        p.setBrush(c);
        p.drawRoundedRect(r.adjusted(-spread, -spread + dy, spread, spread + dy), radius + spread, radius + spread);
    }
    // Surface
    QLinearGradient g(r.topLeft(), r.bottomLeft());
    g.setColorAt(0, t.color("surface-top", QColor("#222733")));
    g.setColorAt(1, t.color("surface-bottom", QColor("#1a1e27")));
    p.setBrush(g);
    QColor border = t.color("border");
    border.setAlphaF(0.9);
    p.setPen(QPen(border, 1));
    p.drawRoundedRect(r.adjusted(0.5, 0.5, -0.5, -0.5), radius, radius);
    // Top highlight hairline for a sense of edge light
    QColor hi = t.color("highlight", QColor(255, 255, 255, 26));
    p.setPen(QPen(hi, 1));
    p.drawLine(QPointF(r.left() + radius, r.top() + 1.5), QPointF(r.right() - radius, r.top() + 1.5));
}
