#pragma once
// A floating surface: soft layered shadow, vertical gradient, hairline border and a top highlight, painted
// directly (no QGraphicsEffect, so it composes with the fade-in helper and stays cheap). Hovering lifts it a little.
#include <QFrame>

class QVariantAnimation;

class Card : public QFrame {
    Q_OBJECT
    Q_PROPERTY(qreal depth READ depth WRITE setDepth)
public:
    static constexpr int kInset = 10;   // room around the surface for the shadow (the card's contents margin)

    explicit Card(QWidget *parent = nullptr);
    qreal depth() const { return m_depth; }
    void setDepth(qreal d);
    void setLiftOnHover(bool on) { m_lift = on; }
    // Staggered entrance: fades the card in after `delayMs`. No-op with reduced motion.
    void playEntrance(int delayMs);

protected:
    void paintEvent(QPaintEvent *) override;
    void enterEvent(QEnterEvent *) override;
    void leaveEvent(QEvent *) override;
    void hideEvent(QHideEvent *) override;

private:
    void animateTo(qreal v);
    qreal m_depth = 0;
    bool m_lift = true;
    QVariantAnimation *m_anim;
};
