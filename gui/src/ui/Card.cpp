#include "Card.h"

#include "Indicators.h"
#include "Motion.h"
#include "ThemeManager.h"

#include <QApplication>
#include <QCursor>
#include <QEnterEvent>
#include <QLineF>
#include <QLinearGradient>
#include <QPainter>
#include <QPainterPath>
#include <QPointer>
#include <QRadialGradient>
#include <QTimer>
#include <QVariantAnimation>
#include <QVector>
#include <cmath>

// One shared application-level tracker keeps every visible card informed about the pointer (child widgets swallow
// mouse moves, so the cards themselves would not see them). Throttled to ~40 Hz; a card only repaints when it matters.
namespace {
class LightTracker : public QObject {
public:
    static LightTracker &get() {
        static LightTracker t;
        return t;
    }
    void add(Card *c) {
        if (m_cards.isEmpty()) qApp->installEventFilter(this);
        m_cards << c;
    }
    void remove(Card *c) {
        m_cards.removeAll(QPointer<Card>(c));
        if (m_cards.isEmpty() && qApp) qApp->removeEventFilter(this);
    }
    bool eventFilter(QObject *, QEvent *e) override {
        if (e->type() == QEvent::MouseMove || e->type() == QEvent::Leave || e->type() == QEvent::WindowDeactivate) arm();
        return false;
    }

private:
    LightTracker() {
        m_t.setSingleShot(true);
        m_t.setInterval(25);
        QObject::connect(&m_t, &QTimer::timeout, [this] {
            const QPoint g = QCursor::pos();
            for (auto &c : m_cards)
                if (c && c->isVisible()) c->setLight(c->mapFromGlobal(g));
        });
    }
    void arm() {
        if (!m_t.isActive()) m_t.start();
    }
    QVector<QPointer<Card>> m_cards;
    QTimer m_t;
};
}  // namespace

Card::Card(QWidget *parent) : QFrame(parent) {
    setObjectName("floatCard");
    setContentsMargins(kInset, kInset, kInset, kInset);
    m_anim = new QVariantAnimation(this);
    connect(m_anim, &QVariantAnimation::valueChanged, this, [this](const QVariant &v) { setDepth(v.toReal()); });
    LightTracker::get().add(this);
}

Card::~Card() { LightTracker::get().remove(this); }

void Card::setLight(const QPointF &p) {
    const QRectF r = QRectF(rect()).adjusted(kInset, kInset, -kInset, -kInset);
    // Distance from the surface: full strength inside, fading out over 90 px, nothing beyond.
    const qreal dx = qMax(r.left() - p.x(), qMax(0.0, p.x() - r.right()));
    const qreal dy = qMax(r.top() - p.y(), qMax(0.0, p.y() - r.bottom()));
    const qreal k = qBound(0.0, 1.0 - std::hypot(dx, dy) / 90.0, 1.0);
    if (k == 0 && m_lightK == 0) return;
    if (qAbs(k - m_lightK) < 0.02 && QLineF(p, m_light).length() < 3) return;
    m_light = p;
    m_lightK = k;
    m_tiltX = qBound(-1.0, (p.x() - r.center().x()) / (r.width() / 2), 1.0) * k;
    m_tiltY = qBound(-1.0, (p.y() - r.center().y()) / (r.height() / 2), 1.0) * k;
    update();
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
    const qreal radius = m_role == "bar" ? t.pixels("radius-lg", 10) * 1.8 : t.pixels("radius-lg", 10);
    const QRectF r = QRectF(rect()).adjusted(kInset, kInset, -kInset, -kInset);

    // Soft shadow: stacked translucent rounded rects, offset downward and away from the pointer (parallax); deeper
    // when lifted.
    QColor sh = t.color("shadow", QColor(0, 0, 0, 110));
    const qreal dy = 2.5 + 3.0 * m_depth - 2.0 * m_tiltY;
    const qreal dx = -4.0 * m_tiltX;
    constexpr int rings = 9;
    p.setPen(Qt::NoPen);
    for (int i = rings; i >= 1; --i) {
        const qreal spread = i * (0.9 + 0.35 * m_depth);
        QColor c = sh;
        const qreal f = 1.0 - qreal(i) / (rings + 1);
        c.setAlphaF(qMin(1.0, sh.alphaF() * (1.0 + 0.5 * m_depth) * f * f / 5.0));
        p.setBrush(c);
        p.drawRoundedRect(r.adjusted(-spread + dx, -spread + dy, spread + dx, spread + dy), radius + spread, radius + spread);
    }
    // Glass surface (translucent vertical gradient: the 3D stage shows through)
    QLinearGradient g(r.topLeft(), r.bottomLeft());
    g.setColorAt(0, t.color("surface-top", QColor("#222733")));
    g.setColorAt(1, t.color("surface-bottom", QColor("#1a1e27")));
    p.setBrush(g);
    p.drawRoundedRect(r, radius, radius);

    const QColor glow = t.color("glow", QColor("#3d7eff"));
    // Specular glow under the pointer
    if (m_lightK > 0.01) {
        QPainterPath clip;
        clip.addRoundedRect(r, radius, radius);
        QColor c0 = glow.lighter(140);
        c0.setAlphaF(0.20 * m_lightK);
        QColor c1 = glow;
        c1.setAlphaF(0);
        QRadialGradient rg(m_light, 190);
        rg.setColorAt(0, c0);
        rg.setColorAt(1, c1);
        p.save();
        p.setClipPath(clip);
        p.fillRect(r, rg);
        p.restore();
    }
    // Rim light: bright toward the top-left, hairline elsewhere (a cheap bevel)
    QColor border = t.color("border");
    border.setAlphaF(0.85);
    const QColor hi = t.color("highlight", QColor(255, 255, 255, 26));
    QColor rimA = hi;
    rimA.setAlphaF(qMin(1.0, 0.55 + 0.3 * m_lightK));
    QLinearGradient rim(r.topLeft(), r.bottomRight());
    rim.setColorAt(0, rimA);
    rim.setColorAt(0.5, border);
    rim.setColorAt(1, border);
    p.setBrush(Qt::NoBrush);
    p.setPen(QPen(QBrush(rim), 1));
    p.drawRoundedRect(r.adjusted(0.5, 0.5, -0.5, -0.5), radius, radius);
    if (m_lightK > 0.01) {                      // brighter edge facing the cursor
        QColor edge = glow.lighter(150);
        edge.setAlphaF(0.55 * m_lightK);
        QRadialGradient eg(m_light, 120);
        eg.setColorAt(0, edge);
        eg.setColorAt(1, QColor(edge.red(), edge.green(), edge.blue(), 0));
        p.setPen(QPen(QBrush(eg), 1.2));
        p.drawRoundedRect(r.adjusted(0.6, 0.6, -0.6, -0.6), radius, radius);
    }
    // Top highlight hairline for a sense of edge light
    p.setPen(QPen(hi, 1));
    p.drawLine(QPointF(r.left() + radius, r.top() + 1.5), QPointF(r.right() - radius, r.top() + 1.5));
}
