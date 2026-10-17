#include "DropOverlay.h"

#include "Icons.h"
#include "Motion.h"
#include "ThemeManager.h"

#include <QPainter>
#include <QPainterPath>
#include <QRadialGradient>
#include <QVariantAnimation>
#include <QtMath>

DropOverlay::DropOverlay(QWidget *parent) : QWidget(parent) {
    setAttribute(Qt::WA_TransparentForMouseEvents);
    setAttribute(Qt::WA_NoSystemBackground);
    hide();
    m_fade = new QVariantAnimation(this);
    connect(m_fade, &QVariantAnimation::valueChanged, this, [this](const QVariant &v) {
        m_t = v.toReal();
        update();
    });
    connect(m_fade, &QVariantAnimation::finished, this, [this] {
        if (m_target <= 0.0 && m_rip >= 1.0) hide();
    });
    m_march = new QVariantAnimation(this);          // marching ants + icon bob (one loop drives both)
    m_march->setStartValue(0.0);
    m_march->setEndValue(1.0);
    m_march->setDuration(1400);
    m_march->setLoopCount(-1);
    connect(m_march, &QVariantAnimation::valueChanged, this, [this](const QVariant &v) {
        m_dash = v.toReal();
        update();
    });
    m_ripple = new QVariantAnimation(this);
    m_ripple->setStartValue(0.0);
    m_ripple->setEndValue(1.0);
    m_ripple->setDuration(520);
    m_ripple->setEasingCurve(QEasingCurve::OutCubic);
    connect(m_ripple, &QVariantAnimation::valueChanged, this, [this](const QVariant &v) {
        m_rip = v.toReal();
        update();
    });
    connect(m_ripple, &QVariantAnimation::finished, this, [this] {
        m_rip = 1.0;
        if (m_target <= 0.0 && m_t <= 0.001) hide();
    });
}

void DropOverlay::drive(qreal to) {
    m_target = to;
    m_fade->stop();
    const int ms = Motion::ms(to > 0 ? 180 : 260);
    if (ms == 0) {
        m_t = to;
        if (to <= 0 && m_rip >= 1.0) hide();
        update();
        return;
    }
    m_fade->setDuration(ms);
    m_fade->setEasingCurve(QEasingCurve::OutCubic);
    m_fade->setStartValue(m_t);
    m_fade->setEndValue(to);
    m_fade->start();
}

void DropOverlay::showHint(const QString &title, const QString &subtitle, bool acceptable) {
    m_title = title;
    m_sub = subtitle;
    m_ok = acceptable;
    setGeometry(parentWidget()->rect().adjusted(parentWidget()->contentsMargins().left(), parentWidget()->contentsMargins().top(),
                                                -parentWidget()->contentsMargins().right(), -parentWidget()->contentsMargins().bottom()));
    raise();
    show();
    if (!Motion::reduced() && m_march->state() != QAbstractAnimation::Running) m_march->start();
    drive(1.0);
}

void DropOverlay::hideHint() {
    m_march->stop();
    drive(0.0);
}

void DropOverlay::ripple(const QPointF &at) {
    m_ripAt = at;
    if (Motion::reduced()) return;
    show();
    raise();
    m_rip = 0.0;
    m_ripple->stop();
    m_ripple->start();
}

void DropOverlay::paintEvent(QPaintEvent *) {
    const auto &th = ThemeManager::instance();
    const QColor accent = m_ok ? th.color("accent", QColor("#4d8dff")) : th.color("danger", QColor("#ff6b6b"));
    QPainter p(this);
    p.setRenderHint(QPainter::Antialiasing);
    const QRectF all = QRectF(rect());
    const qreal radius = 12;

    if (m_t > 0.001) {
        // dimmed glass sheet
        QPainterPath sheet;
        sheet.addRoundedRect(all, radius, radius);
        QColor dim(6, 9, 20, int(190 * m_t));
        p.fillPath(sheet, dim);
        QRadialGradient glow(all.center(), qMax(all.width(), all.height()) * 0.6);
        QColor g0 = accent;
        g0.setAlphaF(0.20 * m_t);
        QColor g1 = accent;
        g1.setAlphaF(0);
        glow.setColorAt(0, g0);
        glow.setColorAt(1, g1);
        p.fillPath(sheet, glow);

        // neon dashed outline, marching; pulses slightly
        const qreal pulse = Motion::reduced() ? 0.0 : 0.5 + 0.5 * qSin(m_dash * 2 * M_PI);
        const qreal inset = 22 + 3 * pulse - 8 * (1.0 - m_t);               // settles inwards as it fades in
        const QRectF frame = all.adjusted(inset, inset, -inset, -inset);
        QPen halo(QColor(accent.red(), accent.green(), accent.blue(), int((50 + 40 * pulse) * m_t)), 9);
        halo.setJoinStyle(Qt::RoundJoin);
        p.setPen(halo);
        p.setBrush(Qt::NoBrush);
        p.drawRoundedRect(frame, 22, 22);
        QPen dash(QColor(accent.lighter(130).red(), accent.lighter(130).green(), accent.lighter(130).blue(), int(255 * m_t)), 2.6);
        dash.setStyle(Qt::CustomDashLine);
        dash.setDashPattern({5.0, 4.0});
        dash.setDashOffset(-m_dash * 36.0);
        dash.setCapStyle(Qt::RoundCap);
        p.setPen(dash);
        p.drawRoundedRect(frame, 22, 22);

        // icon (bobbing) + text
        const qreal bob = Motion::reduced() ? 0.0 : 6.0 * qSin(m_dash * 2 * M_PI);
        const qreal s = 68;
        const QRectF icon(all.center().x() - s / 2, all.center().y() - 78 + bob - 10 * (1.0 - m_t), s, s);
        p.setOpacity(m_t);
        Icons::paint(&p, m_ok ? ":/icons/import.svg" : ":/icons/error.svg", icon, accent.lighter(135));
        QFont f = font();
        f.setPointSizeF(20);
        f.setBold(true);
        p.setFont(f);
        p.setPen(QColor(255, 255, 255));
        p.drawText(QRectF(all.left(), icon.bottom() + 14, all.width(), 36), Qt::AlignHCenter | Qt::AlignVCenter, m_title);
        QFont f2 = font();
        f2.setPointSizeF(11.5);
        p.setFont(f2);
        p.setPen(QColor(200, 210, 230));
        p.drawText(QRectF(all.left() + 40, icon.bottom() + 54, all.width() - 80, 28), Qt::AlignHCenter | Qt::AlignVCenter, m_sub);
        p.setOpacity(1.0);
    }

    if (m_rip < 1.0) {                                                      // drop ripple: two expanding rings
        for (int i = 0; i < 2; ++i) {
            const qreal k = qBound(0.0, m_rip * 1.25 - i * 0.2, 1.0);
            if (k <= 0) continue;
            QColor c = accent.lighter(140);
            c.setAlphaF(0.75 * (1.0 - k));
            p.setPen(QPen(c, 3.0 * (1.0 - k) + 0.8));
            p.setBrush(Qt::NoBrush);
            const qreal rr = 16 + k * qMax(all.width(), all.height()) * 0.42;
            p.drawEllipse(m_ripAt, rr, rr);
        }
    }
}
