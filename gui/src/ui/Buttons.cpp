#include "Buttons.h"

#include "Icons.h"
#include "Motion.h"
#include "ThemeManager.h"

#include <QEasingCurve>
#include <QPainterPath>
#include <QLinearGradient>
#include <QStyleOptionToolButton>
#include <QVariantAnimation>
#include <QtMath>

ButtonFx::ButtonFx(QWidget *owner) : QObject(owner), m_w(owner) {
    m_hoverAnim = new QVariantAnimation(this);
    m_pressAnim = new QVariantAnimation(this);
    connect(m_hoverAnim, &QVariantAnimation::valueChanged, this, [this](const QVariant &v) {
        m_hover = v.toReal();
        m_w->update();
    });
    connect(m_pressAnim, &QVariantAnimation::valueChanged, this, [this](const QVariant &v) {
        m_press = v.toReal();
        m_w->update();
    });
}

void ButtonFx::drive(QVariantAnimation *a, qreal &level, qreal to, int ms) {
    a->stop();
    const int d = Motion::ms(ms);
    if (d == 0 || !m_w->isVisible()) {
        level = to;
        m_w->update();
        return;
    }
    a->setDuration(d);
    a->setEasingCurve(QEasingCurve::OutCubic);
    a->setStartValue(level);
    a->setEndValue(to);
    a->start();
}

void ButtonFx::reset() {
    m_hoverAnim->stop();
    m_pressAnim->stop();
    m_hover = m_press = 0;
    m_w->update();
}

void ButtonFx::paint(QPainter &p) const {
    if (!m_w->isEnabled()) return;
    const QString variant = m_w->property("variant").toString();
    if (variant != "caption" && variant != "ghost" && variant != "tool") {
        // Raised look: a soft gloss on the upper half and a dark lower edge; both flatten while pressed.
        const qreal radius = m_w->property("fxRadius").isValid() ? m_w->property("fxRadius").toReal()
                                                                  : ThemeManager::instance().pixels("radius", 6);
        const QRectF r = QRectF(m_w->rect()).adjusted(0.5, 0.5, -0.5, -0.5);
        p.setRenderHint(QPainter::Antialiasing);
        const qreal lift = 1.0 - 0.85 * m_press;
        QLinearGradient gloss(r.topLeft(), r.bottomLeft());
        gloss.setColorAt(0, QColor(255, 255, 255, int(46 * lift)));
        gloss.setColorAt(0.5, QColor(255, 255, 255, int(8 * lift)));
        gloss.setColorAt(1, QColor(0, 0, 0, int(34 * lift)));
        p.setPen(Qt::NoPen);
        p.setBrush(gloss);
        p.drawRoundedRect(r, radius, radius);
        p.setPen(QPen(QColor(255, 255, 255, int((40 + 40 * m_hover) * lift)), 1));
        p.setBrush(Qt::NoBrush);
        p.drawLine(QPointF(r.left() + radius, r.top() + 0.5), QPointF(r.right() - radius, r.top() + 0.5));
    }
    if (m_hover <= 0.001 && m_press <= 0.001) return;
    const auto &theme = ThemeManager::instance();
    const qreal radius = m_w->property("fxRadius").isValid() ? m_w->property("fxRadius").toReal() : theme.pixels("radius", 6);
    const QRectF r = QRectF(m_w->rect()).adjusted(0.5, 0.5, -0.5, -0.5);
    p.setRenderHint(QPainter::Antialiasing);
    p.setPen(Qt::NoPen);
    auto layer = [&](const QColor &base, qreal level) {
        if (level <= 0.001) return;
        QColor c = base;
        c.setAlphaF(c.alphaF() * level);
        p.setBrush(c);
        p.drawRoundedRect(r, radius, radius);
    };
    const QVariant custom = m_w->property("fxHoverColor");      // e.g. the red hover of the window close button
    layer(custom.canConvert<QColor>() && custom.value<QColor>().isValid() ? custom.value<QColor>()
                                                                          : theme.color("fx-hover", QColor(255, 255, 255, 20)),
          m_hover);
    layer(theme.color("fx-press", QColor(0, 0, 0, 48)), m_press);
}

AnimatedButton::AnimatedButton(const QString &text, QWidget *parent) : FxButton<QPushButton>(parent) {
    setText(text);
    setIconSize(QSize(Icons::kSize, Icons::kSize));
}

// ---------------------------------------------------------------- PreviewButton
PreviewButton::PreviewButton(QWidget *parent) : FxButton<QToolButton>(parent) {
    setObjectName("previewButton");
    setIcon(Icons::icon("speaker"));
    setIconSize(QSize(Icons::kSize, Icons::kSize));
    setToolTip(tr("Preview selected voice"));
    setAccessibleName(tr("Preview selected voice"));
    m_phaseAnim = new QVariantAnimation(this);
    m_phaseAnim->setStartValue(0.0);
    m_phaseAnim->setEndValue(1.0);
    m_phaseAnim->setDuration(900);
    m_phaseAnim->setLoopCount(-1);
    connect(m_phaseAnim, &QVariantAnimation::valueChanged, this, [this](const QVariant &v) {
        m_phase = v.toReal();
        update();
    });
}

void PreviewButton::setMode(Mode m) {
    if (m == m_mode) return;
    m_mode = m;
    switch (m) {
    case Mode::Idle:
        setToolTip(tr("Preview selected voice"));
        break;
    case Mode::Loading:
        setToolTip(tr("Generating preview… click to cancel"));
        break;
    case Mode::Speaking:
        setToolTip(tr("Stop preview"));
        break;
    }
    syncAnimation();
    update();
}

void PreviewButton::syncAnimation() {
    const bool run = m_mode != Mode::Idle && isVisible() && !Motion::reduced();
    if (run && m_phaseAnim->state() != QAbstractAnimation::Running) m_phaseAnim->start();
    if (!run) m_phaseAnim->stop();
}

void PreviewButton::showEvent(QShowEvent *e) {
    FxButton<QToolButton>::showEvent(e);
    syncAnimation();
}

void PreviewButton::hideEvent(QHideEvent *e) {
    FxButton<QToolButton>::hideEvent(e);
    m_phaseAnim->stop();
}

void PreviewButton::paintEvent(QPaintEvent *e) {
    if (m_mode == Mode::Idle) {
        FxButton<QToolButton>::paintEvent(e);
        return;
    }
    QPainter p(this);
    QStyleOptionToolButton opt;
    initStyleOption(&opt);
    opt.icon = QIcon();
    style()->drawComplexControl(QStyle::CC_ToolButton, &opt, &p, this);
    m_fx.paint(p);

    const auto &theme = ThemeManager::instance();
    const QColor accent = theme.color("icon-accent");
    const QPointF c = QRectF(rect()).center();
    p.setRenderHint(QPainter::Antialiasing);
    if (m_mode == Mode::Loading) {
        p.translate(c);
        p.rotate(Motion::reduced() ? 0.0 : m_phase * 360.0);
        const qreal s = Icons::kSize;
        Icons::paint(&p, ":/icons/progress.svg", QRectF(-s / 2, -s / 2, s, s), accent);
    } else {   // Speaking: three bars that move only while playback is actually running
        p.setPen(Qt::NoPen);
        p.setBrush(accent);
        const qreal bw = 3, gap = 3, maxH = 14, minH = 4;
        for (int i = 0; i < 3; ++i) {
            const qreal wave = Motion::reduced() ? (i == 1 ? 1.0 : 0.55)
                                                 : 0.5 + 0.5 * qSin(m_phase * 2 * M_PI + i * 1.9);
            const qreal h = minH + (maxH - minH) * wave;
            const qreal x = c.x() + (i - 1) * (bw + gap) - bw / 2;
            p.drawRoundedRect(QRectF(x, c.y() - h / 2, bw, h), bw / 2, bw / 2);
        }
    }
}

// ---------------------------------------------------------------- PlayButton
PlayButton::PlayButton(QWidget *parent) : QAbstractButton(parent) {
    setFocusPolicy(Qt::TabFocus);
    setAttribute(Qt::WA_Hover);
    auto make = [this](qreal *target) {
        auto *a = new QVariantAnimation(this);
        a->setEasingCurve(QEasingCurve::OutCubic);
        connect(a, &QVariantAnimation::valueChanged, this, [this, target](const QVariant &v) {
            *target = v.toReal();
            update();
        });
        return a;
    };
    m_hoverAnim = make(&m_hover);
    m_pressAnim = make(&m_press);
    m_morphAnim = make(&m_morph);
    m_pulseAnim = new QVariantAnimation(this);
    m_pulseAnim->setStartValue(0.0);
    m_pulseAnim->setEndValue(1.0);
    m_pulseAnim->setDuration(1300);
    m_pulseAnim->setLoopCount(-1);
    connect(m_pulseAnim, &QVariantAnimation::valueChanged, this, [this](const QVariant &v) {
        m_pulse = v.toReal();
        update();
    });
}

void PlayButton::animate(QVariantAnimation *a, qreal from, qreal to, int ms) {
    a->stop();
    const int d = Motion::ms(ms);
    if (d == 0 || !isVisible()) {
        a->setStartValue(to);
        a->setEndValue(to);
        a->setDuration(1);
    } else {
        a->setStartValue(from);
        a->setEndValue(to);
        a->setDuration(d);
    }
    a->start();
}

void PlayButton::setPlaying(bool on) {
    if (on == m_playing) return;
    m_playing = on;
    animate(m_morphAnim, m_morph, on ? 1 : 0, 200);
    if (!on) m_progress = -1;
    syncPulse();
    update();
}

void PlayButton::setProgress(qreal p) {
    m_progress = p < 0 ? -1 : qBound<qreal>(0, p, 1);
    update();
}

void PlayButton::syncPulse() {
    const bool run = m_playing && isVisible() && !Motion::reduced();
    if (run && m_pulseAnim->state() != QAbstractAnimation::Running) m_pulseAnim->start();
    if (!run) {
        m_pulseAnim->stop();
        m_pulse = 0;
    }
}

void PlayButton::enterEvent(QEnterEvent *e) { QAbstractButton::enterEvent(e); animate(m_hoverAnim, m_hover, 1, 140); }
void PlayButton::leaveEvent(QEvent *e) {
    QAbstractButton::leaveEvent(e);
    animate(m_hoverAnim, m_hover, 0, 200);
    animate(m_pressAnim, m_press, 0, 120);
}
void PlayButton::mousePressEvent(QMouseEvent *e) {
    QAbstractButton::mousePressEvent(e);
    animate(m_pressAnim, m_press, 1, 60);
}
void PlayButton::mouseReleaseEvent(QMouseEvent *e) {
    QAbstractButton::mouseReleaseEvent(e);
    animate(m_pressAnim, m_press, 0, 140);
}
void PlayButton::showEvent(QShowEvent *e) { QAbstractButton::showEvent(e); syncPulse(); }
void PlayButton::hideEvent(QHideEvent *e) { QAbstractButton::hideEvent(e); m_pulseAnim->stop(); }
void PlayButton::changeEvent(QEvent *e) {
    QAbstractButton::changeEvent(e);
    if (e->type() == QEvent::EnabledChange) update();
}

void PlayButton::paintEvent(QPaintEvent *) {
    const auto &th = ThemeManager::instance();
    const QColor top = th.color("accent-top", QColor("#6aa1ff")), bot = th.color("accent-bottom", QColor("#3568de"));
    const bool en = isEnabled();
    QPainter p(this);
    p.setRenderHint(QPainter::Antialiasing);
    const QPointF c = QRectF(rect()).center();
    const qreal base = qMin(width(), height()) / 2.0;
    const qreal R = (base - 8) * (1.0 + 0.07 * m_hover - 0.06 * m_press);     // disk radius

    if (en) {
        QColor halo = top;                                                    // hover halo + the pulse while playing
        halo.setAlphaF(0.20 * m_hover);
        p.setPen(Qt::NoPen);
        p.setBrush(halo);
        p.drawEllipse(c, R + 6, R + 6);
        if (m_playing) {
            QColor pc = top;
            pc.setAlphaF(0.42 * (1 - m_pulse));
            p.setBrush(pc);
            p.drawEllipse(c, R + 1 + 6 * m_pulse, R + 1 + 6 * m_pulse);
        }
    }
    if (hasFocus()) {
        p.setBrush(Qt::NoBrush);
        p.setPen(QPen(top, 1.6));
        p.drawEllipse(c, R + 5, R + 5);
    }
    if (m_progress >= 0 && en) {                                              // position ring
        const QRectF ring(c.x() - R - 4, c.y() - R - 4, 2 * (R + 4), 2 * (R + 4));
        p.setBrush(Qt::NoBrush);
        p.setPen(QPen(QColor(255, 255, 255, 36), 2.4, Qt::SolidLine, Qt::RoundCap));
        p.drawEllipse(ring);
        p.setPen(QPen(top, 2.6, Qt::SolidLine, Qt::RoundCap));
        p.drawArc(ring, 90 * 16, -int(m_progress * 360 * 16));
    }

    // disk
    QLinearGradient g(c.x(), c.y() - R, c.x(), c.y() + R);
    if (en) {
        g.setColorAt(0, m_playing ? bot.lighter(112) : top);
        g.setColorAt(1, m_playing ? bot.darker(115) : bot);
    } else {
        g.setColorAt(0, QColor(255, 255, 255, 26));
        g.setColorAt(1, QColor(255, 255, 255, 12));
    }
    p.setBrush(g);
    p.setPen(QPen(en ? top.lighter(125) : QColor(255, 255, 255, 36), 1));
    p.drawEllipse(c, R, R);
    if (en) {                                                                 // gloss on the upper half
        QLinearGradient gl(c.x(), c.y() - R, c.x(), c.y());
        gl.setColorAt(0, QColor(255, 255, 255, 90));
        gl.setColorAt(1, QColor(255, 255, 255, 0));
        QPainterPath cap;
        cap.addEllipse(QRectF(c.x() - R + 2, c.y() - R + 1.5, 2 * R - 4, R));
        p.setPen(Qt::NoPen);
        p.setBrush(gl);
        p.drawPath(cap);
    }

    // glyph: triangle <-> rounded square, cross-faded
    const QColor ink = en ? QColor("#ffffff") : th.color("text-disabled", QColor("#5d6475"));
    const qreal s = R * 0.46;
    if (m_morph < 0.999) {
        p.save();
        p.setOpacity(1 - m_morph);
        p.translate(c.x() + s * 0.12, c.y());
        p.scale(1 - 0.25 * m_morph, 1 - 0.25 * m_morph);
        QPainterPath tri;
        tri.moveTo(-s * 0.78, -s * 1.0);
        tri.lineTo(s * 1.0, 0);
        tri.lineTo(-s * 0.78, s * 1.0);
        tri.closeSubpath();
        p.setBrush(ink);
        p.setPen(QPen(ink, 2.2, Qt::SolidLine, Qt::RoundCap, Qt::RoundJoin));
        p.drawPath(tri);
        p.restore();
    }
    if (m_morph > 0.001) {
        p.save();
        p.setOpacity(m_morph);
        p.translate(c);
        p.scale(0.75 + 0.25 * m_morph, 0.75 + 0.25 * m_morph);
        p.setBrush(ink);
        p.setPen(Qt::NoPen);
        p.drawRoundedRect(QRectF(-s * 0.82, -s * 0.82, s * 1.64, s * 1.64), 2.6, 2.6);
        p.restore();
    }
}
