#pragma once
// A floating glass surface: soft layered shadow, translucent vertical gradient (the OpenGL stage / backdrop shows
// through), rim light, specular glow that follows the pointer and a top highlight, painted directly (no
// QGraphicsEffect, so it composes with the fade-in helper and stays cheap). Hovering lifts it a little.
#include <QFrame>
#include <QPointF>

class QVariantAnimation;

class Card : public QFrame {
    Q_OBJECT
    Q_PROPERTY(qreal depth READ depth WRITE setDepth)
public:
    static constexpr int kInset = 10;   // room around the surface for the shadow (the card's contents margin)

    explicit Card(QWidget *parent = nullptr);
    ~Card() override;
    qreal depth() const { return m_depth; }
    void setDepth(qreal d);
    void setLiftOnHover(bool on) { m_lift = on; }
    // Staggered entrance: fades the card in after `delayMs`. No-op with reduced motion.
    void playEntrance(int delayMs);
    // Pointer "light" in the card's own coordinates (set by a shared tracker): drives the specular glow, the rim light
    // and the small parallax of the shadow, which together fake a 3D tilt towards the pointer.
    void setLight(const QPointF &p);
    // "bar" gives the floating player bar its rounder, pill-like corners.
    void setRole(const QString &role) { m_role = role; update(); }

protected:
    void paintEvent(QPaintEvent *) override;
    void enterEvent(QEnterEvent *) override;
    void leaveEvent(QEvent *) override;
    void hideEvent(QHideEvent *) override;

private:
    void animateTo(qreal v);
    qreal m_depth = 0;
    bool m_lift = true;
    QPointF m_light{-1000, -1000};
    qreal m_tiltX = 0, m_tiltY = 0, m_lightK = 0;
    QString m_role;
    QVariantAnimation *m_anim;
};
