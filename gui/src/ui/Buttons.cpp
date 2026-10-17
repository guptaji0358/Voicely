#include "Buttons.h"

#include "Icons.h"
#include "Motion.h"
#include "ThemeManager.h"

#include <QEasingCurve>
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
    if (variant != "caption" && variant != "ghost") {
        // Raised look: a soft gloss on the upper half and a dark lower edge; both flatten while pressed.
        const qreal radius = ThemeManager::instance().pixels("radius", 6);
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
    const qreal radius = theme.pixels("radius", 6);
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
