#pragma once
// Custom-drawn controls and cursors, so nothing in the app falls back to the look of stock Qt:
//   FancyCheckBox - drop-in QCheckBox with a gradient box, an animated check stroke, a hover halo and a focus ring.
//   Cursors       - themed arrow and pointing-hand cursors drawn at 2x so they stay sharp on high-DPI screens.
#include "Motion.h"
#include "ThemeManager.h"

#include <QCheckBox>
#include <QCursor>
#include <QEnterEvent>
#include <QFocusEvent>
#include <QFontMetrics>
#include <QHash>
#include <QLinearGradient>
#include <QPainter>
#include <QPainterPath>
#include <QPixmap>
#include <QStyleOptionButton>
#include <QVariantAnimation>

class FancyCheckBox : public QCheckBox {
public:
    static constexpr int kBox = 20;       // indicator edge
    static constexpr int kGap = 10;       // indicator -> text
    static constexpr int kIndent = kBox + kGap;

    explicit FancyCheckBox(const QString &text = {}, QWidget *parent = nullptr) : QCheckBox(text, parent) {
        setAttribute(Qt::WA_Hover);
        setFocusPolicy(Qt::StrongFocus);
        m_on = makeAnim([this](qreal v) { m_check = v; });
        m_hov = makeAnim([this](qreal v) { m_hover = v; });
        m_check = isChecked() ? 1 : 0;
        connect(this, &QCheckBox::toggled, this, [this](bool on) { run(m_on, m_check, on ? 1 : 0, isVisible() ? 190 : 0); });
    }

    QSize sizeHint() const override {
        const QFontMetrics fm(font());
        const int w = text().isEmpty() ? kBox : kIndent + fm.horizontalAdvance(text());
        return {w + 2, qMax(kBox + 4, fm.height() + 6)};
    }
    QSize minimumSizeHint() const override { return {kIndent + 40, qMax(kBox + 4, QFontMetrics(font()).height() + 6)}; }
    bool hasHeightForWidth() const override { return !text().isEmpty(); }
    int heightForWidth(int w) const override {
        const QFontMetrics fm(font());
        const QRect r = fm.boundingRect(QRect(0, 0, qMax(40, w - kIndent), 10000), Qt::TextWordWrap, text());
        return qMax(kBox + 4, r.height() + 6);
    }

protected:
    void enterEvent(QEnterEvent *e) override { run(m_hov, m_hover, 1, 140); QCheckBox::enterEvent(e); }
    void leaveEvent(QEvent *e) override { run(m_hov, m_hover, 0, 200); QCheckBox::leaveEvent(e); }
    void focusInEvent(QFocusEvent *e) override {
        m_ring = e->reason() == Qt::TabFocusReason || e->reason() == Qt::BacktabFocusReason || e->reason() == Qt::ShortcutFocusReason;
        QCheckBox::focusInEvent(e);
    }
    void focusOutEvent(QFocusEvent *e) override { m_ring = false; QCheckBox::focusOutEvent(e); }
    void changeEvent(QEvent *e) override {
        if (e->type() == QEvent::EnabledChange) update();
        QCheckBox::changeEvent(e);
    }

    void paintEvent(QPaintEvent *) override {
        const ThemeManager &th = ThemeManager::instance();
        const bool en = isEnabled();
        const QColor accentTop = th.color("accent-top", QColor("#6aa1ff"));
        const QColor accentBot = th.color("accent-bottom", QColor("#3568de"));
        const QColor border = th.color("border-strong", QColor("#3a4050"));
        const QColor raised = th.color("surface-raised", QColor("#20242c"));
        const QColor txt = th.color(en ? "text" : "text-disabled", palette().color(QPalette::WindowText));

        QPainter p(this);
        p.setRenderHint(QPainter::Antialiasing);
        const qreal top = height() <= kBox + 10 ? (height() - kBox) / 2.0 : 3;      // centred, or aligned to the first line when wrapped
        const QRectF b(1.5, top + 1.5, kBox - 3, kBox - 3);

        if (!en) p.setOpacity(0.5);

        // hover halo
        if (m_hover > 0.01 && en) {
            QColor halo = accentTop;
            halo.setAlphaF(0.20 * m_hover);
            p.setPen(Qt::NoPen);
            p.setBrush(halo);
            p.drawRoundedRect(b.adjusted(-3, -3, 3, 3), 8, 8);
        }
        // keyboard focus ring
        if (m_ring && hasFocus()) {
            p.setBrush(Qt::NoBrush);
            p.setPen(QPen(accentTop, 1.6));
            p.drawRoundedRect(b.adjusted(-3, -3, 3, 3), 8, 8);
        }

        // box: empty -> gradient accent
        QLinearGradient g(b.topLeft(), b.bottomLeft());
        g.setColorAt(0, mix(raised, accentTop, m_check));
        g.setColorAt(1, mix(raised, accentBot, m_check));
        const QColor edge = mix(mix(border, accentTop, m_hover * 0.8), accentTop, m_check);
        p.setBrush(g);
        p.setPen(QPen(edge, 1.4));
        p.drawRoundedRect(b, 6, 6);

        // check stroke, drawn progressively
        if (m_check > 0.01) {
            const QPointF a(b.left() + 4.8, b.top() + 8.8), m(b.left() + 7.6, b.top() + 11.8), c(b.left() + 13.2, b.top() + 5.2);
            const qreal t = qBound<qreal>(0, m_check, 1);
            QPen pen(Qt::white, 2.2, Qt::SolidLine, Qt::RoundCap, Qt::RoundJoin);
            p.setPen(pen);
            const qreal first = qMin<qreal>(1, t / 0.4);
            p.drawLine(a, a + (m - a) * first);
            if (t > 0.4) {
                const qreal second = (t - 0.4) / 0.6;
                p.drawLine(m, m + (c - m) * second);
            }
        }

        // text
        if (!text().isEmpty()) {
            p.setOpacity(1);
            p.setPen(txt);
            p.setFont(font());
            p.drawText(QRect(kIndent, 0, width() - kIndent, height()), Qt::AlignVCenter | Qt::TextWordWrap, text());
        }
    }

private:
    static QColor mix(const QColor &a, const QColor &b, qreal t) {
        t = qBound<qreal>(0, t, 1);
        return QColor::fromRgbF(a.redF() + (b.redF() - a.redF()) * t, a.greenF() + (b.greenF() - a.greenF()) * t,
                                a.blueF() + (b.blueF() - a.blueF()) * t, a.alphaF() + (b.alphaF() - a.alphaF()) * t);
    }
    template <typename F> QVariantAnimation *makeAnim(F set) {
        auto *a = new QVariantAnimation(this);
        a->setEasingCurve(QEasingCurve::OutCubic);
        connect(a, &QVariantAnimation::valueChanged, this, [this, set](const QVariant &v) { set(v.toReal()); update(); });
        return a;
    }
    void run(QVariantAnimation *a, qreal from, qreal to, int ms) {
        a->stop();
        const int d = Motion::ms(ms);
        if (d == 0) {
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

    QVariantAnimation *m_on, *m_hov;
    qreal m_check = 0, m_hover = 0;
    bool m_ring = false;
};

namespace Cursors {
namespace detail {
inline QPainterPath handPath() {
    QPainterPath p;
    p.addRoundedRect(QRectF(9, 3, 5.6, 16), 2.8, 2.8);          // index finger
    p.addRoundedRect(QRectF(14.2, 11, 4.6, 10), 2.3, 2.3);      // middle knuckle
    p.addRoundedRect(QRectF(18.4, 12.2, 4.6, 9.5), 2.3, 2.3);   // ring knuckle
    p.addRoundedRect(QRectF(22.6, 14, 4.2, 8), 2.1, 2.1);       // little knuckle
    p.addRoundedRect(QRectF(7, 15, 20, 12), 5.5, 5.5);          // palm
    return p.simplified();
}
inline QPainterPath arrowPath() {
    QPainterPath p;
    p.moveTo(4, 2.5);
    p.lineTo(4, 22.5);
    p.lineTo(9.2, 17.6);
    p.lineTo(12.8, 25.6);
    p.lineTo(16.6, 23.9);
    p.lineTo(13, 16.2);
    p.lineTo(20.4, 16.2);
    p.closeSubpath();
    return p;
}
inline QCursor make(bool hand) {
    const ThemeManager &th = ThemeManager::instance();
    const QColor top = th.color("accent-top", QColor("#6aa1ff")), bot = th.color("accent-bottom", QColor("#3568de"));
    constexpr int kLogical = 32, kScale = 2;
    QPixmap pm(kLogical * kScale, kLogical * kScale);
    pm.setDevicePixelRatio(kScale);
    pm.fill(Qt::transparent);
    QPainter p(&pm);
    p.setRenderHint(QPainter::Antialiasing);
    const QPainterPath shape = hand ? handPath() : arrowPath();
    p.save();                                                   // soft drop shadow
    p.translate(1.2, 1.6);
    p.setPen(Qt::NoPen);
    p.setBrush(QColor(0, 0, 0, 70));
    p.drawPath(shape);
    p.restore();
    if (hand) {                                                 // white hand, accent outline
        p.setBrush(QColor("#ffffff"));
        p.setPen(QPen(bot, 1.6, Qt::SolidLine, Qt::RoundCap, Qt::RoundJoin));
    } else {                                                    // accent arrow, white outline
        QLinearGradient g(0, 2, 0, 26);
        g.setColorAt(0, top);
        g.setColorAt(1, bot);
        p.setBrush(g);
        p.setPen(QPen(QColor("#ffffff"), 1.6, Qt::SolidLine, Qt::RoundCap, Qt::RoundJoin));
    }
    p.drawPath(shape);
    p.end();
    return hand ? QCursor(pm, 11, 3) : QCursor(pm, 4, 3);
}
inline QCursor cached(bool hand) {
    static QHash<QString, QCursor> cache;
    const QString key = ThemeManager::instance().current() + (hand ? "/hand" : "/arrow");
    auto it = cache.find(key);
    if (it == cache.end()) it = cache.insert(key, make(hand));
    return *it;
}
}  // namespace detail

inline QCursor arrow() { return detail::cached(false); }
inline QCursor pointer() { return detail::cached(true); }
}  // namespace Cursors
